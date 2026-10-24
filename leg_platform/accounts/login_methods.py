"""The sign-in methods a person can choose between, and which of them one account is offered (D-106, "Guidance").

The options are decided here, never in a template: ``available(user)`` is the one list that the choice
page shows and the one list the posted choice is validated against (``forms.LoginMethodForm``). The
methods route into the existing flows (``profile_views.LoginMethodView``); this module holds no security logic.

* ``PASSWORD_ONLY``: password without a second factor. Users only (D-004: the second factor is mandatory for
  staff users and system administrators).
* ``PASSWORD_AND_APP``: password and a code from an authenticator app (TOTP).
* ``PASSKEY``: discoverable, user-verified credential without a password or app.
"""

import dataclasses
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from leg_platform.accounts import second_factor

PASSWORD_ONLY = "password"
PASSWORD_AND_APP = "app"
PASSKEY = "passkey"


@dataclass(frozen=True)
class Method:
    """One card of the choice. The title and one sentence (``summary``) are always shown; the rest opens under "More about
    this": what it is, "You need", "If you lose it" and, for an account that exists, "What happens?" (``happens``, by what
    the account has now; empty at the registration, where there is nothing to change)."""

    key: str
    title: object
    summary: object
    what: object
    need: object
    lose: object
    happens: object = ""


_METHODS = {
    PASSWORD_ONLY: Method(
        key=PASSWORD_ONLY,
        title=_("Password only"),
        summary=_("The simplest way, but less protected."),
        what=_("Whoever knows your password can sign in."),
        need=_("Your password."),
        lose=_("If you forget your password, we send a link to your email address."),
    ),
    PASSWORD_AND_APP: Method(
        key=PASSWORD_AND_APP,
        title=_("Password and code from an app"),
        summary=_("Safer: after your password you enter a code from an app."),
        what=_(
            "An authenticator app is a free app from your phone's app store. It shows a new 6-digit code every 30 "
            "seconds. After your password you type the current code. This proves that you hold your phone."
        ),
        need=_("A smartphone with a free authenticator app."),
        lose=_(
            "You also receive 10 recovery codes: with one of them you sign in without the app. If you lose them, send "
            "an ID document by email to the LEG administrator, who starts the recovery."
        ),
    ),
    PASSKEY: Method(
        key=PASSKEY,
        title=pgettext_lazy("sign-in method", "Passkey"),
        summary=_("Quick and safe: you sign in with fingerprint, face or screen lock, without a password."),
        what=_(
            "A passkey replaces your password. Your phone or computer holds a secret key. You confirm with fingerprint, "
            "face or screen lock, just like unlocking your phone. There is nothing to remember or type, it cannot be "
            "guessed and it does not work on fake websites."
        ),
        need=_("A phone or computer with a screen lock. We recommend a second device."),
        lose=_(
            "Sign in with your second device. Without one, send an ID document by email to the LEG administrator, who "
            "starts the recovery."
        ),
    ),
}


def _happens(user, key):
    """What choosing the method does to this account: depends on what the account has now (a number code is turned off
    or deleted, a password is deleted). Security levels, brand names and the claim that the key never leaves the device are
    not shown (D-106: no proof of device independence)."""
    has_code = current(user) == PASSWORD_AND_APP
    if key == PASSWORD_ONLY:
        if has_code:
            return _(
                "You enter only your password each time you sign in. Anyone who knows your password can sign in. "
                "The number code is turned off."
            )
        return _("You enter only your password each time you sign in. Anyone who knows your password can sign in.")
    if key == PASSWORD_AND_APP:
        return _(
            "You enter your password and then the number that the program shows right now. Without the number there is "
            "no access."
        )
    if has_code:
        return _(
            "You confirm the sign-in on your device, for example with fingerprint, face recognition or device code. "
            "Your password and your number code are deleted in the process. You need neither afterwards."
        )
    return _(
        "You confirm the sign-in on your device, for example with fingerprint, face recognition or device code. "
        "Your password is deleted in the process. You do not need it afterwards."
    )


def is_privileged(user):
    """Staff users and system administrators: no "password only" (D-004)."""
    return second_factor.requires_second_factor(user)


def available_keys(user):
    """The keys of the methods this account may choose, in the order of the page (most familiar first)."""
    keys = [] if is_privileged(user) else [PASSWORD_ONLY]
    keys.append(PASSWORD_AND_APP)
    keys.append(PASSKEY)
    return keys


def available(user):
    """The methods this account may choose, in the order of the page: ``Method`` objects."""
    return [_METHODS[key] for key in available_keys(user)]


def cards(user, *, registering=False):
    """The cards of the choice, the one list of every place that shows it (registration, profile, the flow "Change sign-in",
    recovery). ``registering``: an unverified account has nothing to change, so "What happens?" is left out."""
    methods = available(user)
    if registering:
        return methods
    return [dataclasses.replace(method, happens=_happens(user, method.key)) for method in methods]


def has_choice(user):
    """Whether there is more than one permitted method."""
    return len(available_keys(user)) > 1


def current(user):
    """The key of the method the account uses now."""
    if user.authentication_method == "passkey":
        return PASSKEY
    if user.authentication_method == "recovery":
        return ""
    return PASSWORD_AND_APP if second_factor.has_second_factor(user) else PASSWORD_ONLY


def recommended(user):
    """The key of the recommended method: the app for users (D-106); a passkey for administrators once it exists."""
    if is_privileged(user):
        return PASSKEY
    return PASSWORD_AND_APP


def title_of(key):
    return _METHODS[key].title
