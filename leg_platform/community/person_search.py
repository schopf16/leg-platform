"""Finding a person for an assignment (D-079): active accounts with a customer number, every word must match.

The result is always limited; the e-mail address is searched but never shown."""

import re
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db.models import Q

from leg_platform.accounts.models import format_customer_number

LIMIT = 10
SEPARATOR = " · "
CUSTOMER_NUMBER = re.compile(r"(\d{3}) ?(\d{3})")


def candidates():
    user_model = get_user_model()
    return user_model.objects.filter(
        status=user_model.Status.ACTIVE, customer_number__isnull=False
    ).order_by("last_name", "first_name", "organisation_name", "pk")


def label(person):
    """The text of a suggestion: the name and the customer number ("Anna Muster · 123 456")."""
    return f"{person.display_name}{SEPARATOR}{format_customer_number(person.customer_number_id)}"


def search(text, limit=LIMIT):
    """At most ``limit`` accounts that match every word of the text."""
    queryset = candidates()
    for word in (text or "").split():
        condition = (
            Q(first_name__icontains=word) | Q(last_name__icontains=word) | Q(organisation_name__icontains=word)
            | Q(normalized_email__contains=word.lower())
        )
        digits = word.replace(" ", "")
        if digits.isascii() and digits.isdigit():
            condition |= Q(customer_number__number__startswith=digits)
        queryset = queryset.filter(condition)
    return list(queryset[:limit])


@dataclass(frozen=True)
class Found:
    """``person`` is the account or None; ``code`` is "" for one match, else ``none`` or ``several``."""

    person: object = None
    code: str = ""


def resolve(text):
    """The one person that the text names: a suggestion text, customer number, e-mail address or unique name."""
    text = (text or "").strip()
    if not text:
        return Found(code="none")
    if SEPARATOR in text:
        # A suggestion text: the customer number after the separator decides.
        match = CUSTOMER_NUMBER.fullmatch(text.rsplit(SEPARATOR, 1)[1].strip())
        if match:
            person = candidates().filter(customer_number="".join(match.groups())).first()
            return Found(person, "" if person else "none")
    match = CUSTOMER_NUMBER.fullmatch(text)
    if match:
        person = candidates().filter(customer_number="".join(match.groups())).first()
        if person:
            return Found(person)
    if "@" in text:
        person = candidates().filter(normalized_email=text.lower()).first()
        if person:
            return Found(person)
    found = search(text, limit=2)
    if not found:
        return Found(code="none")
    if len(found) > 1:
        return Found(code="several")
    return Found(found[0])
