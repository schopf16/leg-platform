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
from dataclasses import dataclass

from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

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


@dataclass(frozen=True)
class Option:
    """One option of the choice: the title, "What is this?" and "What happens?" (texts by the state of the account)."""

    key: str
    title: object
    what: object
    happens: object


def options(user):
    """The options of this account in the order of the page, from ``login_methods.available``: the server decides.

    "What happens?" depends on what the account has now. The security levels, brand names and the sentence that the
    key never leaves the device are not shown (owner decisions open; D-106: no proof of device independence).
    """
    has_code = login_methods.current(user) == login_methods.PASSWORD_AND_APP
    texts = {
        login_methods.PASSWORD_ONLY: Option(
            key=login_methods.PASSWORD_ONLY,
            title=_("Password"),
            what=_("A password is a secret word that only you know."),
            happens=(
                _("You enter only your password each time you sign in. Anyone who knows your password can sign in. "
                  "The number code is turned off.")
                if has_code
                else _("You enter only your password each time you sign in. Anyone who knows your password can sign in.")
            ),
        ),
        login_methods.PASSWORD_AND_APP: Option(
            key=login_methods.PASSWORD_AND_APP,
            title=_("Password and number code from your phone"),
            what=_(
                "A number code is a six-digit number. It changes every 30 seconds and is shown by a program on your "
                "phone. Such programs are called authenticator apps. You find them in the app store or play store of "
                "your phone."
            ),
            happens=_(
                "You enter your password and then the number that the program shows right now. Without the number "
                "there is no access."
            ),
        ),
        login_methods.PASSKEY: Option(
            key=login_methods.PASSKEY,
            title=pgettext_lazy("sign-in flow", "Passkey"),
            what=_(
                "A passkey, also called a sign-in key, is a key that your device stores. It replaces the password. "
                "It is never sent to the website. It does not work on fake websites."
            ),
            happens=(
                _("You confirm the sign-in on your device, for example with fingerprint, face recognition or device "
                  "code. Your password and your number code are deleted in the process. You need neither afterwards.")
                if has_code
                else _("You confirm the sign-in on your device, for example with fingerprint, face recognition or "
                       "device code. Your password is deleted in the process. You do not need it afterwards.")
            ),
        ),
    }
    return [texts[key] for key in login_methods.available_keys(user)]
