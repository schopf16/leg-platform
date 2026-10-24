"""Requests for help with a lost sign-in method (D-106): the answer for the address that asked.

The page answers every address the same way (no account is revealed). In the background an active
account gets one of two mails:

* an ordinary account with no building, metering point or IBAN entered gets the recovery link
  (``second_factor_reset``, purpose SELF_RECOVERY): after its confirmation the old sign-in methods are
  removed and the person chooses a new one;
* every other active account (something entered, staff user, system administrator) gets the
  instructions: contact the LEG administrator with an official ID document (``recovery_guidance``).

An address without an active account gets nothing.
"""

from leg_platform.accounts import emails, second_factor_reset
from leg_platform.accounts.models import User
from leg_platform.accounts.normalization import normalize_email_address


def process_recovery_request(address, greeting_name=""):
    """Handle the request in the background worker; return the sending function or None."""
    address = normalize_email_address(address)
    user = User._base_manager.filter(normalized_email=address, status=User.Status.ACTIVE).first()
    if user is None:
        return None
    send = second_factor_reset.process_reset_link(user.pk, user.email, greeting_name, self_service=True)
    if send is not None:
        return send
    return lambda: emails.send_recovery_guidance(user.email)
