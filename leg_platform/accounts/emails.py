"""Emails of the account functions (docs/ARCHITECTURE.md, "Email").

Sent through Django's email framework (the configured backend decides about delivery, D-006, D-028),
as plain text, in the instance's default language. Links are built from the canonical base URL of
the instance configuration, never from the request (docs/SECURITY.md, section 4). Neither messages
nor tokens are logged.
"""

import math
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext

from leg_platform.accounts import email_texts, greeting
from leg_platform.accounts.models import EmailText
from leg_platform.accounts.portal_settings import portal_values

Key = EmailText.Key


def absolute_url(path):
    """Return the link for a path of the application, built from the instance configuration."""
    return f"{settings.INSTANCE_BASE_URL}{path}"


def _values(specific, key=None, greeting_name=""):
    # The portal name is re-checked for control characters when read, so it cannot inject a header (D-073).
    portal = portal_values()
    values = {
        "instance_name": portal.name,
        "contact_url": portal.website_url or "",
        "privacy_url": portal.privacy_url or "",
        "login_url": absolute_url(reverse("accounts:login")),
        **specific,
    }
    # Only emails that offer the greeting placeholder get a value (no name can reach another email,
    # D-068); the deleted-account notice is always neutral, also in preview and test. Run with the email's language active.
    if key is not None and any(
        placeholder.name == email_texts.GREETING.name
        for placeholder in email_texts.DEFINITIONS[key].placeholders
    ):
        if key == Key.ACCOUNT_DELETED:
            greeting_name = ""
        values[email_texts.GREETING.name] = greeting.render(greeting_name)
    return values


def _send(address, key, values, greeting_name=""):
    """Send the email; ``values`` holds this email's specific placeholders, ``greeting_name`` the cleaned stored name (D-068)."""
    with translation.override(settings.LANGUAGE_CODE):
        subject, body = email_texts.render(key, _values(values, key, greeting_name))
    _deliver(address, subject, body)


def _deliver(address, subject, body):
    # An automatic message that answers no email (RFC 3834, section 5.2).
    EmailMessage(
        subject=subject, body=body, to=[address], headers={"Auto-Submitted": "auto-generated"}
    ).send(fail_silently=False)


# The personal links of a preview and of a test email carry a token that does not exist.
EXAMPLE_TOKEN = "x" * 43
_SAMPLE_LINK_VIEWS = {
    email_texts.VERIFICATION_URL: "accounts:registration_verify",
    email_texts.RESET_URL: "accounts:password_reset_verify",
    email_texts.EMAIL_CHANGE_URL: "accounts:email_change_verify",
    email_texts.STOP_URL: "accounts:email_change_stop_verify",
    email_texts.TWO_FACTOR_RESET_URL: "accounts:two_factor_reset_verify",
}


def _sample_values(key, greeting_name=""):
    specific = {}
    for placeholder in email_texts.DEFINITIONS[key].placeholders:
        if placeholder is email_texts.LIFETIME_HOURS:
            specific[placeholder.name] = "24"
        elif placeholder is email_texts.LIFETIME_DAYS:
            specific[placeholder.name] = "7"
        elif placeholder in _SAMPLE_LINK_VIEWS:
            specific[placeholder.name] = absolute_url(
                reverse(_SAMPLE_LINK_VIEWS[placeholder], kwargs={"token": EXAMPLE_TOKEN})
            )
    return _values(specific, key, greeting_name)


def preview(key, subject, body):
    """Return the subject and body as they would be sent, with example links and the example greeting name (D-039, D-068)."""
    with translation.override(settings.LANGUAGE_CODE):
        return email_texts.compose(key, subject, body, _sample_values(key, greeting.EXAMPLE_NAME))


def send_test(address, key, subject, body, greeting_name=""):
    """Send the given text with example links to the administrator's address (D-039).

    ``greeting_name`` is the administrator's own cleaned name (D-068): the real greeting rule applies.
    """
    with translation.override(settings.LANGUAGE_CODE):
        subject, body = email_texts.compose(
            key, subject, body, _sample_values(key, greeting_name)
        )
        notice = gettext("This is a test email. The personal link in it does not work.")
        subject = f"[{gettext('Test')}] {subject}"
    _deliver(address, subject, f"{notice}\n\n{body}")


def graph_test_message(address, sender_address):
    """The test email of the Microsoft Graph configuration (D-006), without a secret; sent by the administration with GraphEmailBackend."""
    with translation.override(settings.LANGUAGE_CODE):
        subject = gettext("Test email of the Microsoft Graph configuration")
        body = gettext(
            "This is a test email of the Microsoft Graph email delivery. No action is needed."
        )
    return EmailMessage(subject=subject, body=body, from_email=sender_address, to=[address])


def send_registration_verification(address, raw_token, lifetime: timedelta):
    """Send the link with which the person verifies the address and sets the password."""
    _send(
        address,
        Key.REGISTRATION_VERIFICATION,
        {
            "verification_url": absolute_url(
                reverse("accounts:registration_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
    )


def send_invitation(address, raw_token, lifetime: timedelta):
    """Send the registration link to a person invited by staff (D-087); the text has no name or account data."""
    _send(
        address,
        Key.INVITATION,
        {
            "verification_url": absolute_url(
                reverse("accounts:registration_verify", kwargs={"token": raw_token})
            ),
            "lifetime_days": str(math.ceil(lifetime.total_seconds() / 86400)),
        },
    )


def send_registration_existing_account(address):
    """Tell the owner of an address that someone tried to register it; no link, no reply requested (third parties can trigger it)."""
    _send(address, Key.REGISTRATION_EXISTING_ACCOUNT, {})


def send_password_reset(address, raw_token, lifetime: timedelta):
    """Send the link with which the owner of an account chooses a new password."""
    _send(
        address,
        Key.PASSWORD_RESET,
        {
            "reset_url": absolute_url(
                reverse("accounts:password_reset_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
    )


def send_password_changed(address, greeting_name=""):
    """Tell the owner that the password was changed; contains no link (docs/SECURITY.md)."""
    _send(address, Key.PASSWORD_CHANGED, {}, greeting_name)


def send_email_change_verification(address, raw_token, lifetime: timedelta):
    """Send the single-use link that verifies the proposed address."""
    _send(
        address,
        Key.EMAIL_CHANGE_VERIFICATION,
        {
            "email_change_url": absolute_url(
                reverse("accounts:email_change_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
    )


def send_email_change_existing_account(address):
    """Notify the target address that an email change was attempted; no reply is requested."""
    _send(address, Key.EMAIL_CHANGE_EXISTING_ACCOUNT, {})


def send_email_changed(address, greeting_name=""):
    """Notify the former address that the account email changed; contains no link."""
    _send(address, Key.EMAIL_CHANGED, {}, greeting_name)


def send_secondary_email_changed(address, greeting_name=""):
    """Tell every address that the second address was added or removed; no link."""
    _send(address, Key.SECONDARY_EMAIL_CHANGED, {}, greeting_name)


def send_session_ended_wrong_codes(address, greeting_name=""):
    """Tell every address that a session was ended after wrong codes; contains no link."""
    _send(address, Key.SESSION_ENDED_WRONG_CODES, {}, greeting_name)


def send_all_sessions_ended(address, greeting_name=""):
    """Tell every address that all sessions were ended after wrong entries; contains no link."""
    _send(address, Key.ALL_SESSIONS_ENDED, {}, greeting_name)


def send_two_factor_enabled(address, greeting_name=""):
    """Tell the owner that two-factor authentication was set up; contains no link."""
    _send(address, Key.TWO_FACTOR_ENABLED, {}, greeting_name)


def send_two_factor_disabled(address, greeting_name=""):
    """Tell the owner that two-factor authentication was removed; contains no link."""
    _send(address, Key.TWO_FACTOR_DISABLED, {}, greeting_name)


def send_recovery_codes_renewed(address, greeting_name=""):
    """Tell the owner that new recovery codes were created; contains no link."""
    _send(address, Key.RECOVERY_CODES_RENEWED, {}, greeting_name)


def send_recovery_code_used(address, greeting_name=""):
    """Tell the owner that a recovery code was used to log in; contains no link."""
    _send(address, Key.RECOVERY_CODE_USED, {}, greeting_name)


def send_code_attempts_warning(address, greeting_name=""):
    """Tell the owner that many wrong codes were entered after the correct password."""
    _send(address, Key.CODE_ATTEMPTS_WARNING, {}, greeting_name)


def send_two_factor_reset_link(address, raw_token, lifetime: timedelta, greeting_name=""):
    """Send the link with which the owner confirms the removal of the second factor (D-004)."""
    _send(
        address,
        Key.TWO_FACTOR_RESET_LINK,
        {
            "two_factor_reset_url": absolute_url(
                reverse("accounts:two_factor_reset_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
        greeting_name,
    )


def send_two_factor_reset(address, greeting_name=""):
    """Tell the owner that the second factor was reset; contains no link."""
    _send(address, Key.TWO_FACTOR_RESET, {}, greeting_name)


def send_secondary_email_verification(address, raw_token, lifetime: timedelta):
    """Send the single-use link that confirms a requested second address (D-059)."""
    _send(
        address,
        Key.SECONDARY_EMAIL_VERIFICATION,
        {
            "email_change_url": absolute_url(
                reverse("accounts:email_change_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
    )


def send_email_change_notice(address, raw_token, lifetime: timedelta, greeting_name=""):
    """Tell an address of the account that a change was requested, with the link that stops it
    without a login (D-059)."""
    _send(
        address,
        Key.EMAIL_CHANGE_NOTICE,
        {
            "stop_url": absolute_url(
                reverse("accounts:email_change_stop_verify", kwargs={"token": raw_token})
            ),
            "lifetime_hours": str(math.ceil(lifetime.total_seconds() / 3600)),
        },
        greeting_name,
    )


def send_account_deleted(address):
    """Tell an address of the account that the account was deleted; contains no link (D-059).

    Always the neutral greeting: no name of a deleted account is stored or used (D-068).
    """
    _send(address, Key.ACCOUNT_DELETED, {})


# Onboarding of customers (D-103): the text carries no address, IBAN or metering point number, only the greeting and
# the link to the login page (``{login_url}``, built from the instance configuration).


def send_application_submitted(address, greeting_name=""):
    """Confirm the submission of an application."""
    _send(address, Key.APPLICATION_SUBMITTED, {}, greeting_name)


def send_application_reminder(address, greeting_name=""):
    """Remind the customer that an application is not completed."""
    _send(address, Key.APPLICATION_REMINDER, {}, greeting_name)


def send_application_rolled_back(address, greeting_name=""):
    """Tell the customer that an application was rolled back and its details deleted."""
    _send(address, Key.APPLICATION_ROLLED_BACK, {}, greeting_name)


def send_contract_reminder(address, greeting_name=""):
    """Remind the customer to approve the contract."""
    _send(address, Key.CONTRACT_REMINDER, {}, greeting_name)


def send_contract_rolled_back(address, greeting_name=""):
    """Tell the customer that an application was rolled back because the contract was not approved in time."""
    _send(address, Key.CONTRACT_ROLLED_BACK, {}, greeting_name)


def send_bank_connection_missing(address, greeting_name=""):
    """Ask for the bank or postal account that a feed-in needs."""
    _send(address, Key.BANK_CONNECTION_MISSING, {}, greeting_name)


def send_deregistration_submitted(address, greeting_name=""):
    """Confirm a deregistration request."""
    _send(address, Key.DEREGISTRATION_SUBMITTED, {}, greeting_name)


def send_deregistration_effective(address, greeting_name=""):
    """Tell the customer that a deregistration has taken effect."""
    _send(address, Key.DEREGISTRATION_EFFECTIVE, {}, greeting_name)


def send_iban_changed(address, greeting_name=""):
    """Tell every address of the account that the IBAN was changed; the IBAN itself is never in the text."""
    _send(address, Key.IBAN_CHANGED, {}, greeting_name)


def send_operator_confirmation_lapsed(address, greeting_name=""):
    """Tell a staff account that an application was rolled back because the grid operator's confirmation is missing."""
    _send(address, Key.OPERATOR_CONFIRMATION_LAPSED, {}, greeting_name)


def send_application_returned(address, greeting_name=""):
    """Tell the customer that the LEG returned the application for correction; the reason is only in the portal."""
    _send(address, Key.APPLICATION_RETURNED, {}, greeting_name)


def send_application_rejected(address, greeting_name=""):
    """Tell the customer that the LEG did not accept the application (the details were deleted, the account stays)."""
    _send(address, Key.APPLICATION_REJECTED, {}, greeting_name)


def send_application_rejected_operator(address, greeting_name=""):
    """Tell the customer that the grid operator did not accept the application."""
    _send(address, Key.APPLICATION_REJECTED_OPERATOR, {}, greeting_name)