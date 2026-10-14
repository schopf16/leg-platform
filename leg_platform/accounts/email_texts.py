"""Editable texts of the emails (docs/SECURITY.md, section 3; D-039).

Every email has a default text (the Django template and the subject in the application) and
may have a text that the administrator wrote in the administration (``EmailText``). The written
text is plain text with ``{placeholder}`` markers. It is never run through the template engine:
the markers are replaced in one pass by a fixed list of values, so a text cannot read anything
else, and every marker is checked when the text is saved.

Rules that protect the person who receives the email:

* only the placeholders of this email are allowed;
* the personal links of the email are required, otherwise the function would not work;
* no web or email address can be typed, so that a text cannot lead to another site than the
  links that the application puts in (the instance's own addresses are available as
  placeholders). The check is a deny-list of the usual forms and cannot be complete; the
  permission to edit the texts is the protection against a person who wants to bypass it.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable

from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import EmailText

logger = logging.getLogger(__name__)

Key = EmailText.Key


@dataclass(frozen=True)
class Placeholder:
    name: str
    description: str


INSTANCE_NAME = Placeholder("instance_name", _("Name of this portal."))
CONTACT_URL = Placeholder(
    "contact_url",
    _("Address of the website with the contact information; empty if the instance has none."),
)
PRIVACY_URL = Placeholder("privacy_url", _("Address of the privacy notice."))
LOGIN_URL = Placeholder("login_url", _("Address of the login page."))
VERIFICATION_URL = Placeholder(
    "verification_url",
    _("Personal link to confirm the email address and to choose a password."),
)
RESET_URL = Placeholder("reset_url", _("Personal link to choose a new password."))
EMAIL_CHANGE_URL = Placeholder(
    "email_change_url", _("Personal link to confirm the new email address.")
)
TWO_FACTOR_RESET_URL = Placeholder(
    "two_factor_reset_url", _("Personal link to remove two-factor authentication.")
)
LIFETIME_HOURS = Placeholder(
    "lifetime_hours", _("Number of hours for which the personal link can be used.")
)

COMMON_PLACEHOLDERS = (INSTANCE_NAME, CONTACT_URL, PRIVACY_URL, LOGIN_URL)
# Only the name of the portal is available in the subject.
SUBJECT_PLACEHOLDERS = (INSTANCE_NAME,)


@dataclass(frozen=True)
class Definition:
    template: str
    default_subject: Callable[[], str]
    placeholders: tuple[Placeholder, ...]
    required: tuple[Placeholder, ...]


DEFINITIONS = {
    Key.REGISTRATION_VERIFICATION: Definition(
        "accounts/email/registration_verification.txt",
        lambda: gettext("Confirm your registration at %(instance_name)s"),
        (VERIFICATION_URL, LIFETIME_HOURS),
        (VERIFICATION_URL,),
    ),
    Key.REGISTRATION_EXISTING_ACCOUNT: Definition(
        "accounts/email/registration_existing_account.txt",
        lambda: gettext("Your account at %(instance_name)s"),
        (),
        (),
    ),
    Key.PASSWORD_RESET: Definition(
        "accounts/email/password_reset.txt",
        lambda: gettext("Choose a new password for %(instance_name)s"),
        (RESET_URL, LIFETIME_HOURS),
        (RESET_URL,),
    ),
    Key.EMAIL_CHANGE_VERIFICATION: Definition(
        "accounts/email/email_change_verification.txt",
        lambda: gettext("Confirm your new email address at %(instance_name)s"),
        (EMAIL_CHANGE_URL, LIFETIME_HOURS),
        (EMAIL_CHANGE_URL,),
    ),
    Key.EMAIL_CHANGE_EXISTING_ACCOUNT: Definition(
        "accounts/email/email_change_existing_account.txt",
        lambda: gettext("An email address change was attempted at %(instance_name)s"),
        (CONTACT_URL,),
        (),
    ),
    Key.EMAIL_CHANGED: Definition(
        "accounts/email/email_changed.txt",
        lambda: gettext("Your email address at %(instance_name)s was changed"),
        (CONTACT_URL,),
        (),
    ),
    Key.PASSWORD_CHANGED: Definition(
        "accounts/email/password_changed.txt",
        lambda: gettext("Your password at %(instance_name)s was changed"),
        (),
        (),
    ),
    Key.TWO_FACTOR_ENABLED: Definition(
        "accounts/email/two_factor_enabled.txt",
        lambda: gettext("Two-factor authentication was set up at %(instance_name)s"),
        (),
        (),
    ),
    Key.TWO_FACTOR_DISABLED: Definition(
        "accounts/email/two_factor_disabled.txt",
        lambda: gettext("Two-factor authentication was removed at %(instance_name)s"),
        (),
        (),
    ),
    Key.RECOVERY_CODES_RENEWED: Definition(
        "accounts/email/recovery_codes_renewed.txt",
        lambda: gettext("New recovery codes were created at %(instance_name)s"),
        (),
        (),
    ),
    Key.RECOVERY_CODE_USED: Definition(
        "accounts/email/recovery_code_used.txt",
        lambda: gettext("A recovery code was used at %(instance_name)s"),
        (),
        (),
    ),
    Key.CODE_ATTEMPTS_WARNING: Definition(
        "accounts/email/code_attempts_warning.txt",
        lambda: gettext("Wrong codes were entered for your account at %(instance_name)s"),
        (),
        (),
    ),
    Key.TWO_FACTOR_RESET_LINK: Definition(
        "accounts/email/two_factor_reset_link.txt",
        lambda: gettext("Remove two-factor authentication at %(instance_name)s"),
        (TWO_FACTOR_RESET_URL, LIFETIME_HOURS),
        (TWO_FACTOR_RESET_URL,),
    ),
    Key.TWO_FACTOR_RESET: Definition(
        "accounts/email/two_factor_reset.txt",
        lambda: gettext("Two-factor authentication was reset at %(instance_name)s"),
        (),
        (),
    ),
}

MAX_BODY_LENGTH = 5000

_TOKEN = re.compile(r"\{([^{}\n]*)\}")
_SCHEMES = (
    "http", "https", "ftp", "ftps", "sftp", "ssh", "file", "mailto", "tel", "sms",
    "javascript", "data", "vbscript", "ws", "wss", "ldap", "smb",
)
# Anything that looks like a web or email address that was typed instead of a placeholder: a
# scheme, "//", a backslash, "@", or a word with a dot and at least two letters after it
# ("www.example.org", "bit.ly/x"). Dots of other scripts are mapped to "." before the check.
_ADDRESS = re.compile(
    r"(?i)(?:\b(?:" + "|".join(_SCHEMES) + r"):|[a-z][a-z0-9+.\-]*://|//|\\|@|[\w\-]\.[^\W\d_]{2,})"
)
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_DOTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\uff0e": "."})


def _looks_like_an_address(text):
    # NFKC maps full-width and look-alike forms to plain ones; format characters (zero-width
    # characters, bidi controls) are removed so that they cannot split a scheme.
    text = unicodedata.normalize("NFKC", text).translate(_DOTS)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return bool(_ADDRESS.search(text))


def _has_forbidden_characters(text, allowed=""):
    """Return whether the text has control, format, separator or unassigned characters."""
    return any(
        char not in allowed and unicodedata.category(char) in _FORBIDDEN_CATEGORIES
        for char in text
    )


def placeholders_of(key):
    """Return the placeholders that the body of this email may use."""
    return COMMON_PLACEHOLDERS + DEFINITIONS[key].placeholders


def required_placeholders(key):
    return DEFINITIONS[key].required


def default_texts(key):
    """Return the default subject and body with the placeholders as markers (stripped).

    Written in the default language of the instance, like the emails themselves.
    """
    definition = DEFINITIONS[key]
    with translation.override(settings.LANGUAGE_CODE):
        values = {p.name: "{%s}" % p.name for p in placeholders_of(key)}
        # Without a website the sent default has no contact branch, so neither does the text.
        if not settings.INSTANCE_WEBSITE_URL:
            values[CONTACT_URL.name] = ""
        subject = definition.default_subject() % values
        body = render_to_string(definition.template, values)
    return subject.strip(), body.strip()


def overview():
    """Return every placeholder with the emails it can be used in and where it is required."""
    rows = {}
    for key in Key.values:
        for placeholder in placeholders_of(key):
            row = rows.setdefault(placeholder.name, (placeholder, [], []))
            row[1].append(Key(key).label)
            if placeholder in required_placeholders(key):
                row[2].append(Key(key).label)
    return list(rows.values())


def _check_placeholders(text, allowed):
    """Return the placeholders used in the text and the error messages about its markers."""
    used = set()
    errors = []
    for name in _TOKEN.findall(text):
        if name in allowed:
            used.add(name)
        else:
            errors.append(
                gettext("The placeholder {%(name)s} does not exist or cannot be used here.")
                % {"name": name}
            )
    if "{" in _TOKEN.sub("", text) or "}" in _TOKEN.sub("", text):
        errors.append(
            gettext("The text contains a curly bracket that is not part of a placeholder.")
        )
    return used, errors


def _address_message():
    return gettext(
        "Do not type web or email addresses. Use a placeholder such as {login_url} instead."
    )


def validate_subject(key, subject):
    """Raise a ValidationError if the subject cannot be used; an empty subject is the default."""
    if not subject:
        return
    errors = []
    if "\n" in subject or "\r" in subject:
        errors.append(gettext("The subject must be a single line."))
    elif _has_forbidden_characters(subject):
        errors.append(gettext("The text contains control characters."))
    if _looks_like_an_address(subject):
        errors.append(_address_message())
    _used, marker_errors = _check_placeholders(
        subject, {placeholder.name for placeholder in SUBJECT_PLACEHOLDERS}
    )
    errors += marker_errors
    if errors:
        raise ValidationError(errors)


def validate_body(key, body):
    """Raise a ValidationError if the body cannot be used; an empty body is the default."""
    if not body:
        return
    errors = []
    if len(body) > MAX_BODY_LENGTH:
        errors.append(
            gettext("The text must not be longer than %(limit)d characters.")
            % {"limit": MAX_BODY_LENGTH}
        )
    if _has_forbidden_characters(body, allowed="\n\t"):
        errors.append(gettext("The text contains control characters."))
    if _looks_like_an_address(body):
        errors.append(_address_message())
    used, marker_errors = _check_placeholders(
        body, {placeholder.name for placeholder in placeholders_of(key)}
    )
    errors += marker_errors
    for placeholder in required_placeholders(key):
        if placeholder.name not in used:
            errors.append(
                gettext("The placeholder {%(name)s} is required, otherwise the email is useless.")
                % {"name": placeholder.name}
            )
    if errors:
        raise ValidationError(errors)


def _substitute(text, values):
    # One pass: a value is never searched for further markers.
    return _TOKEN.sub(lambda match: values.get(match.group(1), match.group(0)), text)


def _usable(key, field, text, validate):
    if not text:
        return False
    try:
        validate(key, text)
    except ValidationError:
        # The code can change after a text was saved; the default is safer than a broken link.
        logger.warning("The saved %s of the email text %s is invalid; the default is used.", field, key)
        return False
    return True


def compose(key, subject, body, values):
    """Return the subject and body for the given texts; an empty text is the default.

    The texts must have passed the validation. Must run with the default language of the
    instance active.
    """
    definition = DEFINITIONS[key]
    if subject:
        subject = _substitute(subject, values)
    else:
        subject = definition.default_subject() % values
    if body:
        body = _substitute(body, values) + "\n"
    else:
        body = render_to_string(definition.template, values)
    return subject, body


def render(key, values):
    """Return the subject and body of the email; ``values`` maps every placeholder to a string.

    Uses the saved text of the email where it is valid. Must run with the default language of
    the instance active.
    """
    stored = EmailText.objects.filter(key=key).first()
    subject = body = ""
    if stored is not None:
        if _usable(key, "subject", stored.subject, validate_subject):
            subject = stored.subject
        if _usable(key, "body", stored.body, validate_body):
            body = stored.body
    return compose(key, subject, body, values)
