"""The flow "Change sign-in" of the profile (owner specification, profile chapters 2 to 4; D-106, D-062).

Three screens, one task each: the proof, the choice, the confirmation (or the setup of the number code). The flow adds
no security logic of its own. It asks for the one proof of D-062 first (``forms.SignInChangeProofForm``) and keeps a short
grant in the session; every later step needs that grant and reads the state of the account again. The changes stay in the
existing code: ``totp.start_enrolment`` / ``totp.confirm_enrolment`` (number code on), ``totp.disable`` (number code off)
and ``passkey_methods.activate`` (passkey, reached through the setup grant of ``passkey_methods``).

The grant is bound to the account and to the session hash of the account (a changed password or method ends it) and it
is used up by the step that makes the change. It is no new way to change a method: nobody gets it without the proof.
"""

import time

from django.utils.crypto import constant_time_compare

from leg_platform.accounts import login_methods, second_factor
from leg_platform.accounts.models import User

SESSION_KEY = "sign_in_change"
# The same window as every other recent proof of the profile (D-055).
LIFETIME = second_factor.RECENT_AUTHENTICATION_WINDOW

# The steps of the address (``?step=``); the first one is the default.
PROOF, CHOOSE, SAME, DISABLE, SCAN, NUMBER, CANCEL = (
    "proof", "choose", "same", "disable", "scan", "number", "cancel",
)
STEPS = (PROOF, CHOOSE, SAME, DISABLE, SCAN, NUMBER, CANCEL)
# Steps that make no change and need the grant (the screens a person reads). The choice "Passkey" has no screen of its
# own: it hands the grant to the passkey setup at once (D-112).
CONFIRM_STEPS = (CHOOSE, SAME, DISABLE)
# Of three screens in all, as in the building wizard: "Step n of 3".
TOTAL_STEPS = 3


def uses_flow(user):
    """Whether the account changes its sign-in through this flow: a password account (with or without a number code)
    that has something to choose. A passkey account, a recovery account and an account that must still set up its
    mandatory second factor keep the earlier pages."""
    if user.authentication_method != User.AuthenticationMethod.PASSWORD or not login_methods.has_choice(user):
        return False
    return not (second_factor.requires_second_factor(user) and not second_factor.has_second_factor(user))


def grant(request):
    """Remember that the current proof was given. Call only after a valid ``SignInChangeProofForm``."""
    request.session[SESSION_KEY] = {
        "user": request.user.pk,
        "hash": request.user.get_session_auth_hash(),
        "at": time.time(),
    }


def granted(request):
    """Whether this session gave the current proof for this account a short time ago and nothing changed since."""
    value = request.session.get(SESSION_KEY)
    if not isinstance(value, dict):
        return False
    at = value.get("at")
    return bool(
        value.get("user") == request.user.pk
        and isinstance(at, (int, float))
        and 0 <= time.time() - at < LIFETIME.total_seconds()
        and constant_time_compare(str(value.get("hash", "")), request.user.get_session_auth_hash())
    )


def revoke(request):
    request.session.pop(SESSION_KEY, None)
