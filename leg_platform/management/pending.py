"""Buildings and metering points of applications in flight, shown on the lists of locations and metering points.

An application is not a location yet (D-103), but the administrator looks for its address on the page of the locations and
for its metering points on the page of the metering points. These rows show them there, marked as "in registration", with
the dialogs of the correction (D-109) one click away. Only submitted applications (``Application.CORRECTABLE_STATES``)
appear; a draft is the customer's. The search words of the list apply to the address, the numbers and the applicant."""

from django.urls import reverse
from django.utils.translation import gettext as _

from leg_platform.community import labels
from leg_platform.community.models import Application, ApplicationBuilding, ApplicationMeteringPoint, Direction
from leg_platform.management.returns import with_next


def _applicant(person):
    return person.organisation_name or f"{person.first_name} {person.last_name}".strip()


def _matches(text, parts):
    haystack = " ".join(str(part) for part in parts if part).casefold()
    return all(word.casefold() in haystack for word in (text or "").split())


def _application_link(application):
    return {
        "application_url": reverse("management:processes_application", args=[application.pk]),
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
    rows = []
    buildings = (
        ApplicationBuilding.objects.filter(application__state__in=Application.CORRECTABLE_STATES, location__isnull=True)
        .select_related("application__person").prefetch_related("metering_points")
        .order_by("street", "house_number", "pk")
    )
    for building in buildings:
        points = list(building.metering_points.all())
        if not _matches(
            text, [building.street, building.house_number, building.postal_code, building.town, building.name,
                   _applicant(building.application.person)] + [point.number for point in points]
        ):
            continue
        rows.append(
            {
                "address": f"{building.street} {building.house_number}".strip(),
                "town": f"{building.postal_code} {building.town}".strip(),
                "egid": building.egid,
                "edit_url": with_next(
                    reverse("management:processes_application_building", args=[building.application_id, building.pk]),
                    request,
                ),
                "points": [_point_row(point, building.application, request) for point in points],
                **_application_link(building.application),
            }
        )
    return rows


def points(request, text=""):
    """The metering points of the applications in flight (also at a stored building: "add a metering point")."""
    rows = []
    found = (
        ApplicationMeteringPoint.objects.filter(
            building__application__state__in=Application.CORRECTABLE_STATES, metering_point__isnull=True
        )
        .select_related("building__application__person", "building__location").order_by("number", "pk")
    )
    for point in found:
        building = point.building
        application = building.application
        address = building.current_address()
        if not _matches(text, [point.number, point.name, *address, _applicant(application.person)]):
            continue
        rows.append(
            {
                **_point_row(point, application, request),
                "address": f"{address[0]} {address[1]}".strip(),
                "direction_label": Direction(point.direction).label,
                **_application_link(application),
            }
        )
    return rows
