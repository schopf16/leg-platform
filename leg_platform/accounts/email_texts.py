"""Editable texts of the emails (D-039): a default plus an optional administrator text (``EmailText``).

The written text is plain text with ``{placeholder}`` markers, never run through the template
engine: markers are replaced in one pass from a fixed list and checked when the text is saved.
Rules: only this email's placeholders; the personal links are required; no typed web or email
address (a deny-list, which cannot be complete; the edit permission is the protection).
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
from leg_platform.accounts.portal_settings import portal_values

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
LIFETIME_DAYS = Placeholder(
    "lifetime_days", _("Number of days for which the personal link can be used.")
)
STOP_URL = Placeholder(
    "stop_url", _("Personal link that stops the requested change of an email address.")
)
# Only in notifications to an account's addresses (``MailJob.NOTIFICATION_KINDS``, D-068).
GREETING = Placeholder(
    "greeting",
    _(
        "Greeting with the name of the person the account belongs to, or a neutral greeting if "
        "no suitable name is known. Best written on a line of its own at the start."
    ),
)
# The notice about a deleted account never uses a name.
GREETING_NEUTRAL = Placeholder(
    "greeting",
    _(
        "Neutral greeting without a name: the name of a deleted account is not kept. The preview "
        "and the test email show the same. Best written on a line of its own at the start."
    ),
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
        lambda: gettext("Your registration at %(instance_name)s"),
        (VERIFICATION_URL, LIFETIME_HOURS),
        (VERIFICATION_URL,),
    ),
    Key.INVITATION: Definition(
        "accounts/email/invitation.txt",
        lambda: gettext("Your account at %(instance_name)s"),
        (VERIFICATION_URL, LIFETIME_DAYS),
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
        (GREETING, CONTACT_URL),
        (),
    ),
    Key.SECONDARY_EMAIL_CHANGED: Definition(
        "accounts/email/secondary_email_changed.txt",
        lambda: gettext("The second email address at %(instance_name)s was changed"),
        (GREETING, CONTACT_URL),
        (),
    ),
    Key.PASSWORD_CHANGED: Definition(
        "accounts/email/password_changed.txt",
        lambda: gettext("Your password at %(instance_name)s was changed"),
        (GREETING,),
        (),
    ),
    Key.TWO_FACTOR_ENABLED: Definition(
        "accounts/email/two_factor_enabled.txt",
        lambda: gettext("Two-factor authentication was set up at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.TWO_FACTOR_DISABLED: Definition(
        "accounts/email/two_factor_disabled.txt",
        lambda: gettext("Two-factor authentication was turned off at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.RECOVERY_CODES_RENEWED: Definition(
        "accounts/email/recovery_codes_renewed.txt",
        lambda: gettext("New recovery codes were created at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.RECOVERY_CODE_USED: Definition(
        "accounts/email/recovery_code_used.txt",
        lambda: gettext("A recovery code was used at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.CODE_ATTEMPTS_WARNING: Definition(
        "accounts/email/code_attempts_warning.txt",
        lambda: gettext("Wrong codes were entered for your account at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.TWO_FACTOR_RESET_LINK: Definition(
        "accounts/email/two_factor_reset_link.txt",
        lambda: gettext("Remove two-factor authentication at %(instance_name)s"),
        (GREETING, TWO_FACTOR_RESET_URL, LIFETIME_HOURS),
        (TWO_FACTOR_RESET_URL,),
    ),
    Key.TWO_FACTOR_RESET: Definition(
        "accounts/email/two_factor_reset.txt",
        lambda: gettext("Two-factor authentication was reset at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.SECONDARY_EMAIL_VERIFICATION: Definition(
        "accounts/email/secondary_email_verification.txt",
        lambda: gettext("Confirm your second email address at %(instance_name)s"),
        (EMAIL_CHANGE_URL, LIFETIME_HOURS),
        (EMAIL_CHANGE_URL,),
    ),
    Key.EMAIL_CHANGE_NOTICE: Definition(
        "accounts/email/email_change_notice.txt",
        lambda: gettext("An email address change was requested at %(instance_name)s"),
        (GREETING, STOP_URL, LIFETIME_HOURS),
        (STOP_URL,),
    ),
    Key.ACCOUNT_DELETED: Definition(
        "accounts/email/account_deleted.txt",
        lambda: gettext("Your account at %(instance_name)s was deleted"),
        (GREETING_NEUTRAL,),
        (),
    ),
    Key.SESSION_ENDED_WRONG_CODES: Definition(
        "accounts/email/session_ended_wrong_codes.txt",
        lambda: gettext("A session at %(instance_name)s was ended"),
        (GREETING, CONTACT_URL),
        (),
    ),
    Key.ALL_SESSIONS_ENDED: Definition(
        "accounts/email/all_sessions_ended.txt",
        lambda: gettext("All sessions at %(instance_name)s were ended"),
        (GREETING, CONTACT_URL),
        (),
    ),
    # Onboarding of customers (D-103): no address, IBAN or metering point number, only the link to the portal.
    Key.APPLICATION_SUBMITTED: Definition(
        "accounts/email/application_submitted.txt",
        lambda: gettext("Your application at %(instance_name)s was submitted"),
        (GREETING,),
        (),
    ),
    Key.APPLICATION_REMINDER: Definition(
        "accounts/email/application_reminder.txt",
        lambda: gettext("Your application at %(instance_name)s is not completed yet"),
        (GREETING,),
        (),
    ),
    Key.APPLICATION_ROLLED_BACK: Definition(
        "accounts/email/application_rolled_back.txt",
        lambda: gettext("Your application at %(instance_name)s was deleted"),
        (GREETING,),
        (),
    ),
    Key.CONTRACT_REMINDER: Definition(
        "accounts/email/contract_reminder.txt",
        lambda: gettext("Please approve the contract at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.CONTRACT_ROLLED_BACK: Definition(
        "accounts/email/contract_rolled_back.txt",
        lambda: gettext("Your application at %(instance_name)s was deleted"),
        (GREETING,),
        (),
    ),
    Key.BANK_CONNECTION_MISSING: Definition(
        "accounts/email/bank_connection_missing.txt",
        lambda: gettext("Bank connection needed at %(instance_name)s"),
        (GREETING,),
        (),
    ),
    Key.DEREGISTRATION_SUBMITTED: Definition(
        "accounts/email/deregistration_submitted.txt",
        lambda: gettext("Your deregistration at %(instance_name)s was received"),
        (GREETING,),
        (),
    ),
    Key.DEREGISTRATION_EFFECTIVE: Definition(
        "accounts/email/deregistration_effective.txt",
        lambda: gettext("Your deregistration at %(instance_name)s has taken effect"),
        (GREETING,),
        (),
    ),
    Key.IBAN_CHANGED: Definition(
        "accounts/email/iban_changed.txt",
        lambda: gettext("The IBAN of your account at %(instance_name)s was changed"),
        (GREETING, CONTACT_URL),
        (),
    ),
    Key.OPERATOR_CONFIRMATION_LAPSED: Definition(
        "accounts/email/operator_confirmation_lapsed.txt",
        lambda: gettext("Registration at the grid operator to be withdrawn: %(instance_name)s"),
        (GREETING,),
        (),
    ),
    # Action required of the customer, so it is sent: the reason is in the portal, never in the mail.
    Key.APPLICATION_RETURNED: Definition(
        "accounts/email/application_returned.txt",
        lambda: gettext("Your application at %(instance_name)s needs a correction"),
        (GREETING,),
        (),
    ),
    Key.APPLICATION_REJECTED: Definition(
        "accounts/email/application_rejected.txt",
        lambda: gettext("Your application at %(instance_name)s was not accepted"),
        (GREETING,),
        (),
    ),
    Key.APPLICATION_REJECTED_OPERATOR: Definition(
        "accounts/email/application_rejected_operator.txt",
        lambda: gettext("Your application at %(instance_name)s was not accepted"),
        (GREETING,),
        (),
    ),
}

MAX_BODY_LENGTH = 5000

_TOKEN = re.compile(r"\{([^{}\n]*)\}")
_SCHEMES = (
    "http", "https", "ftp", "ftps", "sftp", "ssh", "file", "mailto", "tel", "sms",
    "javascript", "data", "vbscript", "ws", "wss", "ldap", "smb",
)
# A typed web or email address: a scheme, "//", a backslash, "@", or a word with a dot and two
# letters after it ("www.example.org"). Dots of other scripts are mapped to "." before the check.
_ADDRESS = re.compile(
    r"(?i)(?:\b(?:" + "|".join(_SCHEMES) + r"):|[a-z][a-z0-9+.\-]*://|//|\\|@|[\w\-]\.[^\W\d_]{2,})"
)
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
_DOTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\uff0e": "."})


def looks_like_an_address(text):
    """Return whether the text looks like a web or email address (also used for names, D-068)."""
    # NFKC maps look-alike forms to plain ones; format characters (zero-width, bidi) are removed so they cannot split a scheme.
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
    """Return the default subject and body with the placeholders as markers (stripped), in the instance's default language."""
    definition = DEFINITIONS[key]
    with translation.override(settings.LANGUAGE_CODE):
        values = {p.name: "{%s}" % p.name for p in placeholders_of(key)}
        # Without a website the sent default has no contact branch, so neither does the text.
        if not portal_values().website_url:
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
    if looks_like_an_address(subject):
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
    if looks_like_an_address(body):
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

    The texts must have passed validation; the instance's default language must be active.
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
