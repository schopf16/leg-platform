"""Queries of the list of metering points (D-079; ``messpunkte/VORGABEN.md``, ``LISTEN-STANDARD.md``).

Search, quick filters with their counts, the filter by LEG, the problems, sorting. Pure query code,
no request: the page passes the values it parsed. The search finds what the list and the detail page
show: the number, the parts of the address, the name of the LEG and the name of the person; never the remarks
(``LISTEN-STANDARD.md``, section 4).
"""

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast
from django.utils import timezone

from leg_platform.community import problems
from leg_platform.community.models import LegAssignment, MeteringPoint, PersonAssignment
from leg_platform.community.status import MeteringPointStatus, status_filter

# The quick filters ("Schnellfilter"); "all" shows the status, the others the next step.
STEP_ALL = "all"
STEP_NOTIFY = "notify"
STEP_CONFIRM = "confirm"
STEP_START = "start"
STEP_ARCHIVED = "archived"
STEPS = (STEP_ALL, STEP_NOTIFY, STEP_CONFIRM, STEP_START, STEP_ARCHIVED)

SORT_KEYS = ("number", "location", "leg", "status")
DEFAULT_SORT = "number"

_STATUS_ORDER = (
    MeteringPointStatus.REGISTERED,
    MeteringPointStatus.NOTIFIED,
    MeteringPointStatus.CONFIRMED,
    MeteringPointStatus.ACTIVE,
    MeteringPointStatus.TERMINATED,
)


def base_queryset(today=None):
    """All metering points with ``has_person`` and ``has_leg`` (the problems), ``current_leg_id`` and
    ``current_leg_name`` (the LEG that is valid now, else the next one that starts later)."""
    today = today or timezone.localdate()
    current_leg = LegAssignment.objects.not_ended(today).filter(metering_point=OuterRef("pk")).order_by("valid_from")
    queryset = MeteringPoint.objects.select_related("location", "location__transformer_circuit").annotate(
        current_leg_id=Subquery(current_leg.values("leg_id")[:1]),
        current_leg_name=Subquery(current_leg.values("leg__name")[:1]),
    )
    return problems.annotate_metering_points(queryset, today)


def apply_leg(queryset, leg_id):
    """The filter by LEG: the metering points whose LEG (valid now, else the next one) is this one."""
    return queryset.filter(current_leg_id=leg_id) if leg_id else queryset


def apply_search(queryset, text, *, today=None):
    """Every word of the text must match something that the list or the page shows: a number, a part of the
    address, the name of the LEG or the name of the person. Letters match without regard to case or accents (the
    collation of the columns); the numbers of the exact collation are compared in capitals."""
    today = today or timezone.localdate()
    for word in (text or "").split():
        upper = word.upper()
        condition = (
            Q(number__contains=upper)
            | Q(location__street__icontains=word) | Q(location__house_number__icontains=word)
            | Q(location__postal_code__icontains=word) | Q(location__town__icontains=word)
            | Q(location__address_addition__icontains=word)
            | Exists(
                LegAssignment.objects.not_ended(today).filter(
                    metering_point=OuterRef("pk"), leg__name__icontains=word
                )
            )
            | Exists(
                PersonAssignment.objects.not_ended(today).filter(metering_point=OuterRef("pk")).filter(
                    Q(person__first_name__icontains=word) | Q(person__last_name__icontains=word)
                    | Q(person__organisation_name__icontains=word)
                )
            )
        )
        queryset = queryset.filter(condition)
    return queryset


def step_condition(step, today=None):
    """The condition of a quick filter. The first four never show archived metering points (they are
    out of sight, ``LISTEN-STANDARD.md``, section 4); "archived" shows only them."""
    today = today or timezone.localdate()
    if step == STEP_ARCHIVED:
        return Q(is_archived=True)
    live = Q(is_archived=False)
    if step == STEP_NOTIFY:
        return live & status_filter(MeteringPointStatus.REGISTERED, today)
    if step == STEP_CONFIRM:
        return live & status_filter(MeteringPointStatus.NOTIFIED, today)
    if step == STEP_START:
        return live & status_filter(MeteringPointStatus.CONFIRMED, today) & Q(start_date__isnull=True)
    return live


def apply_step(queryset, step, today=None):
    return queryset.filter(step_condition(step, today))


def step_counts(queryset, today=None):
    """``{step: n}``: how many metering points each quick filter would show in this view (the search
    and the other filters are already in ``queryset``), in one query."""
    today = today or timezone.localdate()
    return queryset.aggregate(
        **{step: Count("pk", filter=step_condition(step, today)) for step in STEPS}
    )


def problem_condition():
    return Q(is_archived=False) & (Q(location__isnull=True) | Q(has_person=False) | Q(has_leg=False))


def apply_problems(queryset):
    """Metering points without location, LEG or person (archived ones have no problem). The queryset
    comes from ``base_queryset``."""
    return queryset.filter(problem_condition())


def apply_sort(queryset, key, descending=False, today=None):
    """Sort by a column; the number is the tie breaker. Metering points without a value come last
    (a missing location, LEG or number)."""
    today = today or timezone.localdate()
    number = [F("number"), F("pk")]
    first = []
    if key == "location":
        first = [
            F("location__street"), Cast("location__house_number", IntegerField()),
            F("location__house_number"),
        ]
    elif key == "leg":
        first = [F("current_leg_name")]
    elif key == "status":
        queryset = queryset.annotate(
            status_rank=Case(
                *[When(status_filter(status, today), then=Value(rank)) for rank, status in enumerate(_STATUS_ORDER)],
                default=Value(len(_STATUS_ORDER)), output_field=IntegerField(),
            )
        )
        first = [F("status_rank")]
    return queryset.order_by(
        *(
            item.desc(nulls_last=True) if descending else item.asc(nulls_last=True)
            for item in first + number
        )
    )
