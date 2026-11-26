"""Buildings and metering points of applications in flight, shown on the lists of locations and metering points.

An application is not a location yet (D-103), but the administrator looks for its address on the page of the locations and
for its metering points on the page of the metering points. These rows show them there, marked as "in registration", with
the dialogs of the correction (D-109) one click away. Only submitted applications (``Application.CORRECTABLE_STATES``)
appear; a draft is the customer's. The search words of the list apply to the address, the numbers and the applicant. The
views put the rows among the stored ones (``location_views.pending_entries``, ``metering_point_views.pending_entries``)
only on the first page of a list without filters; at most ``MAX_ROWS`` rows, the rest is counted and the line below the
list leads to the processes. A metering point added at a stored location is a row under that location."""

from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import ngettext

from leg_platform.community import numbers, process_list

MAX_ROWS = 20


def _applicant(person):
    return person.organisation_name or f"{person.first_name} {person.last_name}".strip()


def _search(queryset, text, *, number, places, person):
    """Every word must match the number, a part of the address or the applicant's name; a number copied from a page (its
    blocks stand apart, D-109) is searched as one number (as ``metering_point_list.apply_search``)."""
    pasted = numbers.pasted_number(text)
    if pasted is not None and queryset.filter(**{f"{number}__contains": pasted}).exists():
        return queryset.filter(**{f"{number}__contains": pasted})
    for word in (text or "").split():
        condition = Q(**{f"{number}__contains": word.upper()})
        for name in (*places, f"{person}first_name", f"{person}last_name", f"{person}organisation_name"):
            condition |= Q(**{f"{name}__icontains": word})
        queryset = queryset.filter(condition)
    return queryset


def _more(total, shown, text, more_text):
    """The line below a list when the cap cut rows of applications (``{"text", "url"}``, the url leads to the
    applications in the processes with the search words), or None."""
    more = total - shown
    if more <= 0:
        return None
    query = {"kind": process_list.KIND_APPLICATION, **({"q": text} if text else {})}
    return {"text": more_text(more), "url": f"{reverse('management:processes')}?{urlencode(query)}"}


def more_buildings(total, shown, text=""):
    return _more(
        total, shown, text,
        lambda more: ngettext(
            "%(count)d more building in registration: see the processes",
            "%(count)d more buildings in registration: see the processes", more,
        ) % {"count": more},
    )


def more_points(total, shown, text=""):
    return _more(
        total, shown, text,
        lambda more: ngettext(
            "%(count)d more metering point in registration: see the processes",
            "%(count)d more metering points in registration: see the processes", more,
        ) % {"count": more},
    )
