"""The sign-in methods a person can choose between, and which of them one account is offered (D-106, "Guidance").

The options are decided here, never in a template: ``available(user)`` is the one list that the choice
page shows and the one list the posted choice is validated against (``forms.LoginMethodForm``). The
methods route into the existing flows (``profile_views.LoginMethodView``); this module holds no security logic.

* ``PASSWORD_ONLY``: password without a second factor. Users only (D-004: the second factor is mandatory for
  staff users and system administrators).
* ``PASSWORD_AND_APP``: password and a code from an authenticator app (TOTP).
* ``PASSKEY``: discoverable, user-verified credential without a password or app.
"""

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor

PASSWORD_ONLY = "password"
PASSWORD_AND_APP = "app"
PASSKEY = "passkey"


@dataclass(frozen=True)
class Method:
    """One card of the choice: texts in the reading order of the card (title, benefit, "You need", "If you lose it")."""

    key: str
    title: object
    benefit: object
    need: object
    lose: object
    # The line of the decision helper ("Which fits me?").
    helper: object
    # A sentence shown inside the card, for the consequence of the choice; empty for none.
    note: object = ""


_METHODS = {
    PASSWORD_ONLY: Method(
        key=PASSWORD_ONLY,
        title=_("Password only"),
        benefit=_("The simplest way. Less protected: whoever knows your password can sign in."),
        need=_("Your password."),
        lose=_("If you forget your password, we send a link to your email address."),
        helper=_("I just want to sign in as before"),
    ),
    PASSWORD_AND_APP: Method(
        key=PASSWORD_AND_APP,
        title=_("Password and code from an app"),
        benefit=_("Safer: after your password you enter a 6-digit code from an app on your phone."),
        need=_("A smartphone with a free authenticator app."),
        lose=_("Use one of your 10 recovery codes. Without them, contact the LEG administrator."),
        helper=_("I have a smartphone and can install an app"),
    ),
    PASSKEY: Method(
        key=PASSKEY,
        title=_("Passkey"),
        benefit=_(
            "Quick and safe: you sign in with fingerprint, face or screen lock of your phone or computer. "
            "No password, no codes."
        ),
        need=_("A phone or computer with a screen lock. We recommend a second device."),
        lose=_(
            "Sign in with your second device. Without one, you send an ID document by email to the LEG "
            "administrator, who resets your account."
        ),
        helper=_("My phone or computer unlocks with fingerprint, face or PIN"),
        note=_("Your password and app are deleted. You cannot combine a password with a passkey."),
    ),
}


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
