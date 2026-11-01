"""The EGID of a location follows its address (D-135).

A location's EGID is not typed by anyone: it is the building the address directory lists at the address. ``resolve``
sets it whenever an address is saved by a service of this app, and the scheduled command ``fill_location_egids`` fills
the locations that have none (an address saved while the directory was missing, an upload of the directory later).
Rules, for the buildings the directory lists at the address (``addresses.buildings.local_egids``):

* the directory cannot answer, or lists nothing there: nothing changes (an EGID that came from the external search is
  never removed because the local copy lacks the building);
* the current EGID is one of them: it stays (a customer chose it when several buildings share the address);
* exactly one: that one is set;
* several, none of them the current EGID: the EGID is cleared (it belongs to another building), nobody has to guess;
* an EGID used by another location is never set twice (``Location.egid`` is unique): nothing changes, a WARNING is logged.

The caller holds the row lock of the location inside its transaction.
"""

import logging

from django.db import transaction

from leg_platform.addresses import buildings as directory_buildings

from . import history
from .history import HistoryEvent
from .models import Location

logger = logging.getLogger(__name__)


def resolve(location, actor=None):
    """Set ``location.egid`` from its address; True when it changed (saved, one History entry)."""
    if location.is_archived:
        return False
    found = directory_buildings.local_egids(location.street, location.house_number, location.postal_code)
    if not found:
        return False
    old = location.egid
    if old in found:
        return False
    new = next(iter(found)) if len(found) == 1 else None
    if new is not None and Location.objects.filter(egid=new).exclude(pk=location.pk).exists():
        logger.warning("The EGID of location %s is used by another location; it was not set.", location.pk)
        return False
    if new == old:
        return False
    location.egid = new
    location.save(update_fields=["egid", "updated_at"])
    history.record(location, HistoryEvent.EGID_CHANGED, actor, {"old_egid": old, "new_egid": new})
    return True


def fill_missing():
    """``resolve`` for every open location without an EGID, one transaction each; returns the number that got one."""
    filled = 0
    for pk in Location.objects.filter(egid__isnull=True, is_archived=False).order_by("pk").values_list("pk", flat=True):
        with transaction.atomic():
            location = Location.objects.select_for_update().filter(pk=pk, egid__isnull=True).first()
            if location is not None and resolve(location):
                filled += 1
    return filled
