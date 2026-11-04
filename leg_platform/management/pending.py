"""Buildings and metering points of applications in flight, shown on the lists of locations and metering points.

An application is not a location yet (D-103), but the administrator looks for its address on the page of the locations and
for its metering points on the page of the metering points. These rows show them there, marked as "in registration", with
the dialogs of the correction (D-109) one click away. Only submitted applications (``Application.CORRECTABLE_STATES``)
appear; a draft is the customer's. The search words of the list apply to the address, the numbers and the applicant. The
views show the block only on the first page of a list without filters; at most ``MAX_ROWS`` rows, the rest is counted and
the line below leads to the processes."""

from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import ngettext

from leg_platform.community import labels, numbers, process_list
from leg_platform.community.models import Application, ApplicationBuilding, ApplicationMeteringPoint, Direction
from leg_platform.management.returns import with_next

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


def _block(rows, total, text, more_text):
    """The rows of the block and, when the cap cut some, the line that leads to the applications in the processes."""
    more = total - len(rows)
    query = {"kind": process_list.KIND_APPLICATION, **({"q": text} if text else {})}
    return {
        "rows": rows,
        "more_text": more_text(more) if more > 0 else "",
        "more_url": f"{reverse('management:processes')}?{urlencode(query)}",
    }


def _application_link(application, request):
    return {
        "application_url": with_next(reverse("management:processes_application", args=[application.pk]), request),
        "applicant": _applicant(application.person),
        "applicant_url": reverse("management:persons_detail", args=[application.person_id]),
        "state": labels.application_state_label(application.state),
    }


def _point_row(point, application, request):
    return {
        "number": point.number, "direction": point.direction, "name": point.name,
        "edit_url": with_next(reverse("management:processes_application_point", args=[application.pk, point.pk]), request),
    }


def new_buildings(request, text=""):
    """The new buildings (no stored location yet) of the applications in flight, with their metering points."""
    buildings = _search(
        ApplicationBuilding.objects.filter(application__state__in=Application.CORRECTABLE_STATES, location__isnull=True),
        text, number="metering_points__number", person="application__person__",
        places=("street", "house_number", "postal_code", "town", "name"),
    ).distinct()
    total = buildings.count()
    rows = []
    found = (
        buildings.select_related("application__person").prefetch_related("metering_points")
        .order_by("street", "house_number", "pk")[:MAX_ROWS]
    )
    for building in found:
        rows.append(
            {
                "address": f"{building.street} {building.house_number}".strip(),
                "town": f"{building.postal_code} {building.town}".strip(),
                "egid": building.egid,
                "edit_url": with_next(
                    reverse("management:processes_application_building", args=[building.application_id, building.pk]),
                    request,
                ),
                "points": [_point_row(point, building.application, request) for point in building.metering_points.all()],
                **_application_link(building.application, request),
            }
        )
    return _block(
        rows, total, text,
        lambda more: ngettext(
            "%(count)d more building in registration: see the processes",
            "%(count)d more buildings in registration: see the processes", more,
        ) % {"count": more},
    )


def points(request, text=""):
    """The metering points of the applications in flight (also at a stored building: "add a metering point")."""
    places = [
        f"building__{prefix}{name}" for prefix in ("", "location__")
        for name in ("street", "house_number", "postal_code", "town")
    ]
    found = _search(
        ApplicationMeteringPoint.objects.filter(
            building__application__state__in=Application.CORRECTABLE_STATES, metering_point__isnull=True
        ),
        text, number="number", person="building__application__person__", places=(*places, "name"),
    )
    total = found.count()
    rows = []
    found = found.select_related("building__application__person", "building__location").order_by("number", "pk")
    for point in found[:MAX_ROWS]:
        building = point.building
        application = building.application
        address = building.current_address()
        rows.append(
            {
                **_point_row(point, application, request),
                "address": f"{address[0]} {address[1]}".strip(),
                "direction_label": Direction(point.direction).label,
                **_application_link(application, request),
            }
        )
    return _block(
        rows, total, text,
        lambda more: ngettext(
            "%(count)d more metering point in registration: see the processes",
            "%(count)d more metering points in registration: see the processes", more,
        ) % {"count": more},
    )
