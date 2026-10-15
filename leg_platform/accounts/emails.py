"""Emails of the account functions (docs/ARCHITECTURE.md, "Email").

All sending goes through Django's email framework, so that the configured backend decides
about delivery (console or Graph, D-006, D-028) and tests use the in-memory backend. Messages
are plain text. Links are built from the canonical base URL of the instance configuration,
never from the request (docs/SECURITY.md, section 4). Neither messages nor tokens are logged.
"""

import math
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext

from leg_platform.accounts import email_texts
from leg_platform.accounts.models import EmailText
from leg_platform.accounts.portal_settings import legal_links

Key = EmailText.Key

# The emails are written in the default language of the instance, not in the language of the
# request that triggered them.


def absolute_url(path):
    """Return the link for a path of the application, built from the instance configuration."""
    return f"{settings.INSTANCE_BASE_URL}{path}"


def _values(specific):
    return {
        "instance_name": settings.INSTANCE_NAME,
        "contact_url": settings.INSTANCE_WEBSITE_URL or "",
        "privacy_url": legal_links().privacy_url or "",
        "login_url": absolute_url(reverse("accounts:login")),
        **specific,
    }


def _send(address, key, values):
    """Send the email; ``values`` holds the placeholders that are specific to this email."""
    with translation.override(settings.LANGUAGE_CODE):
        subject, body = email_texts.render(key, _values(values))
    _deliver(address, subject, body)


def _deliver(address, subject, body):
    # Marks an automatic message that answers no email (RFC 3834, section 5.2), so that
    # automatic replies are not sent to it.
    EmailMessage(
        subject=subject, body=body, to=[address], headers={"Auto-Submitted": "auto-generated"}
    ).send(fail_silently=False)


# The personal links of a preview and of a test email carry a token that does not exist.
EXAMPLE_TOKEN = "x" * 43


def _sample_values(key):
    specific = {}
    for placeholder in email_texts.DEFINITIONS[key].placeholders:
        if placeholder is email_texts.LIFETIME_HOURS:
            specific[placeholder.name] = "24"
        elif placeholder is email_texts.VERIFICATION_URL:
            specific[placeholder.name] = absolute_url(
                reverse("accounts:registration_verify", kwargs={"token": EXAMPLE_TOKEN})
            )
        elif placeholder is email_texts.RESET_URL:
            specific[placeholder.name] = absolute_url(
                reverse("accounts:password_reset_verify", kwargs={"token": EXAMPLE_TOKEN})
            )
        elif placeholder is email_texts.EMAIL_CHANGE_URL:
            specific[placeholder.name] = absolute_url(
                reverse("accounts:email_change_verify", kwargs={"token": EXAMPLE_TOKEN})
            )
        elif placeholder is email_texts.STOP_URL:
            specific[placeholder.name] = absolute_url(
                reverse("accounts:email_change_stop_verify", kwargs={"token": EXAMPLE_TOKEN})
            )
        elif placeholder is email_texts.TWO_FACTOR_RESET_URL:
            specific[placeholder.name] = absolute_url(
                reverse("accounts:two_factor_reset_verify", kwargs={"token": EXAMPLE_TOKEN})
            )
    return _values(specific)


def preview(key, subject, body):
    """Return the subject and body as they would be sent, with example links (D-039)."""
    with translation.override(settings.LANGUAGE_CODE):
        return email_texts.compose(key, subject, body, _sample_values(key))


def send_test(address, key, subject, body):
    """Send the given text with example links to the address of the administrator (D-039)."""
    with translation.override(settings.LANGUAGE_CODE):
        subject, body = email_texts.compose(key, subject, body, _sample_values(key))
        notice = gettext("This is a test email. The personal link in it does not work.")
        subject = f"[{gettext('Test')}] {subject}"
    _deliver(address, subject, f"{notice}\n\n{body}")


def graph_test_message(address, sender_address):
    """Return the test email of the Microsoft Graph configuration (D-006). It contains no secret.

    The sender is the configured mailbox. The message is sent with GraphEmailBackend by the
    administration, not through the email framework.
    """
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


def send_registration_existing_account(address):
    """Tell the owner of an address with an account that someone tried to register it.

    Contains no link that changes anything (docs/PROJECT.md, "Registration"). Nobody is asked
    to reply: third parties can trigger this email.
    """
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


def send_password_changed(address):
    """Tell the owner that the password was changed; contains no link (docs/SECURITY.md)."""
    _send(address, Key.PASSWORD_CHANGED, {})


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


def send_email_changed(address):
    """Notify the former address that the account email changed; contains no link."""
    _send(address, Key.EMAIL_CHANGED, {})


def send_secondary_email_changed(address):
    """Tell every address that the second address was added or removed; contains no link and no
    word about logging in again (the sessions stay)."""
    _send(address, Key.SECONDARY_EMAIL_CHANGED, {})


def send_session_ended_wrong_codes(address):
    """Tell every address that a session was ended after wrong codes; contains no link."""
    _send(address, Key.SESSION_ENDED_WRONG_CODES, {})


def send_two_factor_enabled(address):
    """Tell the owner that two-factor authentication was set up; contains no link."""
    _send(address, Key.TWO_FACTOR_ENABLED, {})


def send_two_factor_disabled(address):
    """Tell the owner that two-factor authentication was removed; contains no link."""
    _send(address, Key.TWO_FACTOR_DISABLED, {})


def send_recovery_codes_renewed(address):
    """Tell the owner that new recovery codes were created; contains no link."""
    _send(address, Key.RECOVERY_CODES_RENEWED, {})


def send_recovery_code_used(address):
    """Tell the owner that a recovery code was used to log in; contains no link."""
    _send(address, Key.RECOVERY_CODE_USED, {})


def send_code_attempts_warning(address):
    """Tell the owner that many wrong codes were entered after the correct password."""
    _send(address, Key.CODE_ATTEMPTS_WARNING, {})


def send_two_factor_reset_link(address, raw_token, lifetime: timedelta):
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
    )


def send_two_factor_reset(address):
    """Tell the owner that the second factor was reset; contains no link."""
    _send(address, Key.TWO_FACTOR_RESET, {})


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


def send_email_change_notice(address, raw_token, lifetime: timedelta):
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
    )


def send_account_deleted(address):
    """Tell an address of the account that the account was deleted; contains no link (D-059)."""
    _send(address, Key.ACCOUNT_DELETED, {})