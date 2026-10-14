"""Local-only advisory matching; never mutate or reject a stored profile."""

import logging

from django.db import DatabaseError
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from .models import Address, Directory, READ_ALIAS

ATTRIBUTION = gettext_lazy("Federal Office of Topography swisstopo")
FIELDS = ("street", "house_number", "postal_code", "locality")
LIMITS = {"street": 255, "house_number": 32, "postal_code": 4, "locality": 255, "country": 2}
RESULT_LIMIT = 10
logger = logging.getLogger(__name__)


def clean_input(values):
    cleaned = {}
    for name, limit in LIMITS.items():
        value = values.get(name, "CH" if name == "country" else "")
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid address input.")
        cleaned[name] = value.strip()
    if cleaned["postal_code"] and not cleaned["postal_code"].isascii():
        raise ValueError("Invalid postal code.")
    if cleaned["postal_code"] and not cleaned["postal_code"].isdigit():
        raise ValueError("Invalid postal code.")
    return cleaned


def directory_status():
    result = {
        "status": "unavailable", "data_date": None, "stale": False,
        "warning": _("The address directory is unavailable. You can continue with the address as entered."),
        "attribution": ATTRIBUTION,
    }
    try:
        directory = Directory.objects.using(READ_ALIAS).filter(pk=1).first()
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory status is unavailable.")
        return result, None
    if directory is None or directory.imported_at is None or directory.row_count == 0:
        result["status"] = "missing"
        return result, None
    result.update(
        status="available",
        data_date=directory.data_date.isoformat() if directory.data_date else None,
        stale=directory.is_stale,
        warning=_("The address directory is more than 92 days old. You can continue with the address as entered.")
        if directory.is_stale else "",
    )
    return result, directory


def lookup(values, *, mode="suggest"):
    values = clean_input(values)
    if mode not in {"suggest", "check"}:
        raise ValueError("Invalid lookup mode.")
    if values["country"] != "CH" or not values["street"]:
        return {
            "status": "not_checked", "data_date": None, "stale": False,
            "warning": "", "attribution": ATTRIBUTION, "results": [], "match": None,
        }
    result, directory = directory_status()
    result.update(results=[], match=None)
    if directory is None:
        return result
    try:
        query = Address.objects.using(READ_ALIAS).filter(generation=directory.generation)
        if mode == "check":
            if len(values["postal_code"]) != 4 or not values["locality"]:
                result["match"] = False
            else:
                result["match"] = query.filter(
                    street__iexact=values["street"], house_number__iexact=values["house_number"],
                    postal_code=values["postal_code"], locality__iexact=values["locality"],
                ).exists()
            if not result["match"]:
                result["warning"] = _("This address was not found in the official directory. Please check it. You can continue with the address as entered.")
        elif len(values["street"]) >= 2:
            query = query.filter(street__istartswith=values["street"])
            if values["postal_code"]:
                query = query.filter(postal_code__startswith=values["postal_code"])
            if values["house_number"]:
                query = query.filter(house_number__istartswith=values["house_number"])
            if values["locality"]:
                query = query.filter(locality__istartswith=values["locality"])
            result["results"] = list(query.values(*FIELDS).order_by(*FIELDS).distinct()[:RESULT_LIMIT])
    except (DatabaseError, ConnectionDoesNotExist):
        logger.warning("Address directory lookup is unavailable.")
        result.update(
            status="unavailable", results=[], match=None,
            warning=_("The address directory is unavailable. You can continue with the address as entered."),
        )
    return result


def check_address(*, street="", house_number="", postal_code="", locality="", country="CH"):
    """Form integration: return a warning dict, not a ValidationError.

    Call only after the host form's normal validation. Do not alter cleaned_data
    or add_error(); display the returned warning even if the profile is saved.
    """
    try:
        return lookup({
            "street": street, "house_number": house_number,
            "postal_code": str(postal_code or ""), "locality": locality,
            "country": str(country),
        }, mode="check")
    except ValueError:
        return {
            "status": "not_checked", "match": None, "results": [],
            "data_date": None, "stale": False, "attribution": ATTRIBUTION,
            "warning": _("The address could not be checked. You can continue with the address as entered."),
        }
