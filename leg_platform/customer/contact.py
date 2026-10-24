"""The contact form of the customers (D-115): one message to the LEG, answered by mail or by phone.

The message goes from the sender address of the portal settings to the contact address of the portal settings (D-116). The answer goes
to the address in ``Reply-To``: the address the customer entered for a mail answer, otherwise the verified address of the
account. A copy, when asked, goes to the verified address of the account. Nothing of a message is logged; a failed
delivery raises and the view shows a refusal.
"""

import phonenumbers
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import translation
from django.utils.translation import gettext as _

from leg_platform.accounts.models import PortalSettings

REPLY_EMAIL = "email"
REPLY_PHONE = "phone"


def available():
    """Whether the form is offered: the contact address is set in the portal settings (D-116)."""
    return bool(PortalSettings.contact_recipient())


def swiss_phone(raw):
    """The number as E.164. Accepted: ``+41``, ``0041`` and the national form with its leading ``0`` (``079 123 45 67``);
    a number without that ``0`` is refused. ``ValueError`` when the number is not valid."""
    value = "".join(char for char in raw if char.isdigit() or char == "+")
    if not value.startswith(("+", "0")):
        raise ValueError("no country code and no leading 0")
    try:
        number = phonenumbers.parse(value, settings.PHONENUMBER_DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        raise ValueError("not a number") from None
    if not phonenumbers.is_valid_number(number):
        raise ValueError("not a valid number")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _international(e164):
    return phonenumbers.format_number(
        phonenumbers.parse(e164), phonenumbers.PhoneNumberFormat.INTERNATIONAL
    )


def send(person, data):
    """Send the message (and the copy, when asked) for ``data`` (the cleaned form); returns the E.164 number when the
    customer wants a phone answer, otherwise None. Raises when the delivery fails."""
    phone = data["phone"] if data["reply_by"] == REPLY_PHONE else None
    sender = PortalSettings.sender()
    recipient = PortalSettings.contact_recipient()
    reply_to = person.email if phone else data["email"]
    with translation.override(settings.LANGUAGE_CODE):
        if phone:
            way = _("Answer by phone: {number}").format(number=_international(phone))
        else:
            way = _("Answer by email to: {address}").format(address=data["email"])
        body = "\n".join([
            _("Customer number: {number}").format(number=person.customer_number_display),
            _("Name: {name}").format(name=person.display_name),
            way,
            _("Copy wanted: {answer}").format(answer=_("yes") if data["copy"] else _("no")),
            "",
            _("Message:"),
            data["message"],
        ])
        messages = [EmailMessage(
            subject=_("Contact form: message from customer {number}").format(
                number=person.customer_number_display,
            ),
            body=body,
            from_email=sender,
            to=[recipient],
            reply_to=[reply_to],
        )]
        if data["copy"]:
            messages.append(EmailMessage(
                subject=_("Copy of your message to the LEG"),
                from_email=sender,
                body=_("This is a copy of the message you sent to the LEG:") + "\n\n" + data["message"],
                to=[person.email],
            ))
    get_connection().send_messages(messages)
    return phone
