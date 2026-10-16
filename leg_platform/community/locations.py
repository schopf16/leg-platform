"""Locations: the directory check and the decision about its suggestion (D-077).

The check runs when the address is created or changed (``refresh_suggestion``); the result is
stored on the location, so that the list can show and filter the problem "address correction open"
without asking the directory per row. A person with the right accepts the suggestion or does not;
"not accept" is remembered for the address as it is, and the warning returns only if the address
changes (``standorte/adresskorrektur/VORGABEN.md``).
"""

from django.db import transaction

from leg_platform.community import addressing, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Location


class SuggestionError(Exception):
    """``code``: ``no_open_suggestion`` or ``address_in_use`` (the suggested address is the address
    of another location)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def refresh_suggestion(location):
    """Ask the directory about the address of the location and store the answer. Saves only the
    suggestion columns. Call it after an address was created or changed."""
    suggestion = addressing.suggest_correction(*location.current_address())
    location.apply_suggestion(suggestion)
    if location.pk:
        location.save(
            update_fields=[
                "suggested_street", "suggested_house_number", "suggested_postal_code",
                "suggested_town", "address_correction_open", "updated_at",
            ]
        )
    return suggestion


def accept_suggestion(location, actor):
    """Replace the address by the suggestion; the entry goes to the history with the actor and the
    time. Refused if there is no open suggestion or if another location has that address (the
    merge of two locations is a separate, confirmed action)."""
    with transaction.atomic():
        fresh = Location.objects.select_for_update().get(pk=location.pk)
        suggestion = fresh.suggestion
        if suggestion is None or not fresh.address_correction_open:
            raise SuggestionError("no_open_suggestion")
        other = Location.find_by_address(*suggestion.as_tuple())
        if other is not None and other.pk != fresh.pk:
            raise SuggestionError("address_in_use")
        fresh.street, fresh.house_number, fresh.postal_code, fresh.town = suggestion.as_tuple()
        fresh.clear_suggestion()
        fresh.save()
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, actor)
    return fresh


def dismiss_suggestion(location, actor):
    """"Do not accept": the address stays, the warning is gone until the address changes."""
    with transaction.atomic():
        fresh = Location.objects.select_for_update().get(pk=location.pk)
        if fresh.suggestion is None or not fresh.address_correction_open:
            raise SuggestionError("no_open_suggestion")
        fresh.dismiss_suggestion()
        fresh.save(update_fields=["suggestion_dismissed_for", "address_correction_open", "updated_at"])
        history.record(fresh, HistoryEvent.ADDRESS_CORRECTION_DISMISSED, actor)
    return fresh
