"""Problem detection per list (D-077): location with open address correction; metering point without
location, LEG or person; LEG problems (``legs``); persons have none. Archived objects have none.

A metering point "has" a person or LEG while an assignment is valid now or starts later.
"""

from django.db import models
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community import legs
from leg_platform.community.models import (
    Leg,
    LegAssignment,
    Location,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
)


class LocationProblem(models.TextChoices):
    ADDRESS_CORRECTION_OPEN = "address_correction_open", _("Problem: Address correction open")


class MeteringPointProblem(models.TextChoices):
    # Banner sentences of the detail page; also the hint of the triangle.
    NO_LOCATION = "no_location", _("This metering point is not assigned to a location.")
    NO_LEG = "no_leg", _("This metering point is not assigned to a LEG.")
    NO_PERSON = "no_person", _("This metering point is not assigned to a person.")


def locations_with_problem():
    """Locations with an open address correction (not archived)."""
    return Location.objects.filter(is_archived=False, address_correction_open=True)


def location_problems(location):
    return [LocationProblem.ADDRESS_CORRECTION_OPEN] if location.address_correction_open else []


def annotate_metering_points(queryset, today=None):
    """Adds ``has_person`` and ``has_leg`` to each metering point."""
    today = today or timezone.localdate()
    return queryset.annotate(
        has_person=Exists(
            PersonAssignment.objects.not_ended(today).filter(metering_point=OuterRef("pk"))
        ),
        has_leg=Exists(
            LegAssignment.objects.not_ended(today).filter(metering_point=OuterRef("pk"))
        ),
    )


def metering_points_with_problem(today=None, queryset=None):
    """Metering points (not archived) without location, without LEG or without person."""
    queryset = MeteringPoint.objects.all() if queryset is None else queryset
    return annotate_metering_points(queryset.filter(is_archived=False), today).filter(
        Q(location__isnull=True) | Q(has_person=False) | Q(has_leg=False)
    )


def metering_point_problems(metering_point, today=None):
    """Problems of one metering point in banner order; empty if archived. Uses annotated ``has_person``/``has_leg`` if present."""
    if metering_point.is_archived:
        return []
    today = today or timezone.localdate()
    has_person = getattr(metering_point, "has_person", None)
    if has_person is None:
        has_person = PersonAssignment.objects.not_ended(today).filter(metering_point=metering_point).exists()
    has_leg = getattr(metering_point, "has_leg", None)
    if has_leg is None:
        has_leg = LegAssignment.objects.not_ended(today).filter(metering_point=metering_point).exists()
    problems = []
    if metering_point.location_id is None:
        problems.append(MeteringPointProblem.NO_LOCATION)
    if not has_leg:
        problems.append(MeteringPointProblem.NO_LEG)
    if not has_person:
        problems.append(MeteringPointProblem.NO_PERSON)
    return problems


def legs_with_problem(today=None, threshold_percent=None):
    """``[(leg, summary)]`` of the LEGs (not archived) that have at least one problem."""
    all_legs = list(Leg.objects.filter(is_archived=False))
    found = legs.summaries(all_legs, threshold_percent, today)
    return [(leg, found[leg.pk]) for leg in all_legs if found[leg.pk].problems]


def problem_counts(today=None, threshold_percent=None, only=None):
    """Entries with a problem per list: ``{"locations": n, "metering_points": n, "legs": n}``;
    ``only`` limits which lists are computed (others are left out)."""
    wanted = {"locations", "metering_points", "legs"} if only is None else set(only)
    counts = {}
    if "locations" in wanted:
        counts["locations"] = locations_with_problem().count()
    if "metering_points" in wanted:
        counts["metering_points"] = metering_points_with_problem(today).count()
    if "legs" in wanted:
        counts["legs"] = len(legs_with_problem(today, threshold_percent))
    return counts


def describe(problem, threshold_percent=None):
    """The text of any problem (triangle hint, banner line)."""
    if problem in legs.LegProblem.values:
        if threshold_percent is None:
            threshold_percent = ManagementSettings.load().warning_threshold_percent
        return legs.problem_text(problem, threshold_percent)
    for kind in (LocationProblem, MeteringPointProblem):
        if problem in kind.values:
            return str(kind(problem).label)
    raise ValueError(f"Unknown problem: {problem!r}")
