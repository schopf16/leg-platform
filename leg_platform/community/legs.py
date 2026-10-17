"""LEG figures: members, consumer-to-producer ratio, discount tier and quota problems (D-077).

Members are the non-archived metering points with a LEG assignment valid on the day. ``summaries`` computes the
figures for all LEGs asked for in one query. The "stale" quota rule is the one of the LEG software
(``app/domain/quality_checks.py``).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.community.models import (
    DEFAULT_WARNING_THRESHOLD_PERCENT,
    LEGAL_MINIMUM_QUOTA_PERCENT,
    Direction,
    LegAssignment,
    ManagementSettings,
)


class DiscountTier(models.TextChoices):
    """Derived from the data, never entered; the grid operator decides the real discount."""

    NONE = "none", _("none")
    BASIC = "basic", _("Basic discount")
    OPTIMISED = "optimised", _("Price optimised")


class LegProblem(models.TextChoices):
    QUOTA_BELOW_LEGAL = "quota_below_legal", _("Production quota below 5 % (required by law)")
    QUOTA_BELOW_WARNING = "quota_below_warning", _("Production quota below the warning threshold")
    QUOTA_STALE = "quota_stale", _("Metering points added since the value was read, read the value again")
    NO_PRODUCER = "no_producer", _("No producer in the LEG")
    NO_CONSUMER = "no_consumer", _("No consumer in the LEG")


class QuotaLevel:
    """Colour level of the quota line: green sufficient, yellow below the warning value, red below the legal 5 %."""

    OK = "ok"
    WARNING = "warning"
    LEGAL = "legal"


def quota_level_text(level, threshold_percent=DEFAULT_WARNING_THRESHOLD_PERCENT):
    """The quota line text: "Sufficient", "Below 10 %" or "Below 5 %"."""
    if level == QuotaLevel.OK:
        return gettext("Sufficient")
    if level == QuotaLevel.WARNING:
        return gettext("Below %(threshold)s %%") % {"threshold": threshold_percent}
    return gettext("Below %(threshold)s %%") % {"threshold": LEGAL_MINIMUM_QUOTA_PERCENT}


def quota_level(quota_percent, threshold_percent):
    """The ``QuotaLevel`` of a quota, or None without a quota."""
    if quota_percent is None:
        return None
    quota = Decimal(quota_percent)
    if quota < LEGAL_MINIMUM_QUOTA_PERCENT:
        return QuotaLevel.LEGAL
    if quota < threshold_percent:
        return QuotaLevel.WARNING
    return QuotaLevel.OK


def quota_number(quota_percent):
    """"37.6" for a field or text; trailing zeros of the stored two decimals are dropped ("40", "8.2")."""
    if quota_percent is None:
        return ""
    return format(Decimal(quota_percent).normalize(), "f")


def format_quota(quota_percent):
    """"37.6 %"; None (no quota) gives None."""
    if quota_percent is None:
        return None
    return f"{quota_number(quota_percent)} %"


def discount_tier(circuit_ids):
    """The tier from one circuit id per metering point (None: no location or no circuit).

    No metering points: none. All in one and the same known circuit: optimised. Else basic."""
    circuit_ids = list(circuit_ids)
    if not circuit_ids:
        return DiscountTier.NONE
    if None not in circuit_ids and len(set(circuit_ids)) == 1:
        return DiscountTier.OPTIMISED
    return DiscountTier.BASIC


def ratio_text(consumers, producers):
    """"3 : 1" (consumers to producers)."""
    return f"{consumers} : {producers}"


def quota_problems(quota_percent, checked_on, member_created_on, threshold_percent):
    """The ``LegProblem`` list of the quota (empty if none).

    A quota below the legal minimum replaces the warning (it would say the same). Stale: a member was created
    after the day the quota was checked (the same day is not stale).
    """
    if quota_percent is None:
        return []
    problems = []
    level = quota_level(quota_percent, threshold_percent)
    if level == QuotaLevel.LEGAL:
        problems.append(LegProblem.QUOTA_BELOW_LEGAL)
    elif level == QuotaLevel.WARNING:
        problems.append(LegProblem.QUOTA_BELOW_WARNING)
    if checked_on is not None and any(created > checked_on for created in member_created_on):
        problems.append(LegProblem.QUOTA_STALE)
    return problems


@dataclass(frozen=True)
class LegSummary:
    leg_id: int
    metering_points: int = 0
    consumers: int = 0
    producers: int = 0
    circuit_ids: frozenset = frozenset()
    tier: str = DiscountTier.NONE
    problems: tuple = field(default=())

    @property
    def ratio(self):
        """The ratio text, or None for a LEG without metering points."""
        return ratio_text(self.consumers, self.producers) if self.metering_points else None


def build_summary(leg, rows, threshold_percent):
    """The summary of one LEG from its member rows ``(direction, circuit_id, created_on)``; an archived LEG has no problems."""
    consumers = sum(1 for direction, _circuit, _created in rows if direction == Direction.CONSUMPTION)
    producers = sum(1 for direction, _circuit, _created in rows if direction == Direction.FEED_IN)
    problems = []
    if not leg.is_archived:
        problems = quota_problems(
            leg.production_quota_percent,
            leg.quota_checked_on,
            [created for _direction, _circuit, created in rows],
            threshold_percent,
        )
        if rows and not producers:
            problems.append(LegProblem.NO_PRODUCER)
        if rows and not consumers:
            problems.append(LegProblem.NO_CONSUMER)
    circuits = [circuit for _direction, circuit, _created in rows]
    return LegSummary(
        leg_id=leg.pk,
        metering_points=len(rows),
        consumers=consumers,
        producers=producers,
        circuit_ids=frozenset(c for c in circuits if c is not None),
        tier=discount_tier(circuits),
        problems=tuple(problems),
    )


def member_rows(leg_ids, on=None):
    """``{leg_id: [(direction, circuit_id, created_on), ...]}`` for the members on the day: one query."""
    on = on or timezone.localdate()
    rows = {leg_id: [] for leg_id in leg_ids}
    query = (
        LegAssignment.objects.in_force(on)
        .filter(leg_id__in=list(rows), metering_point__is_archived=False)
        .values_list(
            "leg_id",
            "metering_point__direction",
            "metering_point__location__transformer_circuit_id",
            "metering_point__created_at",
        )
    )
    for leg_id, direction, circuit_id, created_at in query:
        created_on = timezone.localtime(created_at).date()
        rows[leg_id].append((direction, circuit_id, created_on))
    return rows


def summaries(legs, threshold_percent=None, on=None):
    """``{leg_id: LegSummary}`` for ``Leg`` objects; the threshold defaults to the management settings."""
    legs = list(legs)
    if threshold_percent is None:
        threshold_percent = ManagementSettings.load().warning_threshold_percent
    rows = member_rows([leg.pk for leg in legs], on)
    return {leg.pk: build_summary(leg, rows[leg.pk], threshold_percent) for leg in legs}


def problem_text(problem, threshold_percent=DEFAULT_WARNING_THRESHOLD_PERCENT):
    """The problem text for the warning triangle hint and the banner; the warning names the threshold."""
    if problem == LegProblem.QUOTA_BELOW_WARNING:
        return gettext("Problem: Production quota below %(threshold)s %%") % {"threshold": threshold_percent}
    return gettext("Problem: %(problem)s") % {"problem": LegProblem(problem).label}
