"""The address of a pending email request and the time of the next resend (D-058).

After a person asks for a link (registration or password reset) the next page says to which
address the email goes and offers "send again" after a pause. The address and the time are kept in
the session of this browser only, never in the URL. The pause of 60 seconds is enforced here on the
server; the countdown on the page (``static/js/resend-cooldown.js``) only shows it. The limits per
address, per network and for the instance (D-038, ``mail_queue.submit_mail_request``) apply to every
resend as to every request.
"""

import time
from typing import NamedTuple

RESEND_COOLDOWN_SECONDS = 60
REGISTRATION = "registration"
PASSWORD_RESET = "password_reset"
_PURPOSES = (REGISTRATION, PASSWORD_RESET)
_KEY = "mail_request_%s"


class PendingRequest(NamedTuple):
    address: str
    wait: int  # seconds until a resend is possible, 0 if it is possible now


def remember(request, purpose, address):
    """Remember the address of the request that was just made and start the pause."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    request.session[_KEY % purpose] = {
        "address": address,
        "available_at": time.time() + RESEND_COOLDOWN_SECONDS,
    }


def forget(request, purpose):
    """Remove the pending request of this browser from the session (D-058; review B2): after the
    registration or the reset is finished, and when a link was refused."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    request.session.pop(_KEY % purpose, None)


def pending(request, purpose):
    """Return the pending request of this browser, or None. Values of the session are checked."""
    if purpose not in _PURPOSES:
        raise ValueError(purpose)
    stored = request.session.get(_KEY % purpose)
    if not isinstance(stored, dict):
        return None
    address = stored.get("address")
    available_at = stored.get("available_at")
    if (
        not isinstance(address, str)
        or not address
        or isinstance(available_at, bool)
        or not isinstance(available_at, (int, float))
    ):
        return None
    wait = max(0, int(available_at - time.time() + 0.999))
    # A time far in the future (a clock jump) never blocks for longer than the pause.
    return PendingRequest(address, min(wait, RESEND_COOLDOWN_SECONDS))
