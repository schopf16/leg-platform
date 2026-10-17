"""LEG figures: members, consumer-to-producer ratio, discount tier and quota problems (D-077).

``legs/VORGABEN.md`` and ``legs/DETAIL-ENTWURF.md`` define the rules; the quota rule "stale" is the
one of the LEG software (``app/domain/quality_checks.py``), decided by the project owner.

The members of a LEG are the metering points with a LEG assignment that is valid on the day and
that are not archived. The figures come from one query for all LEGs asked for
(``summaries``), so that a list does not ask once per row.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.community.models import (
    DEFAULT_WARNING_THRESHOLD_PERCENT,
    LEGAL_MINIMUM_QUOTA_PERCENT,
    Direction,
    LegAssignment,
)


class DiscountTier(models.TextChoices):
    """Derived from the data, never entered (``legs/VORGABEN.md``, "Rabattstufe"); the grid operator decides
    the real discount."""

    NONE = "none", _("none")
    BASIC = "basic", _("Basic discount")
    OPTIMISED = "optimised", _("Price optimised")


class LegProblem(models.TextChoices):
    QUOTA_BELOW_LEGAL = "quota_below_legal", _("Production quota below 5 % (required by law)")
    QUOTA_BELOW_WARNING = "quota_below_warning", _("Production quota below the warning threshold")
    QUOTA_STALE = "quota_stale", _("Metering points added since the value was read, read the value again")
    # The banner of the one-circuit reference says "Problem: kein Produzent in der LEG" (D-080).
    NO_PRODUCER = "no_producer", _("No producer in the LEG")
    NO_CONSUMER = "no_consumer", _("No consumer in the LEG")


class QuotaLevel:
    """How the production quota stands, for the colour of the line on the card of the LEG
    (``legs/DETAIL-ENTWURF.md``: green sufficient, yellow tight = below the warning value, red below
    the legal 5 %)."""

    OK = "ok"
    WARNING = "warning"
    LEGAL = "legal"


def quota_level_text(level, threshold_percent=DEFAULT_WARNING_THRESHOLD_PERCENT):
    """The line on the card of the quota: "Ausreichend", "Unter 10 %" or "Unter 5 %" (the reference)."""
    from django.utils.translation import gettext

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
    """"37.6" for a field or a text; the stored two decimals lose their trailing zeros ("40", "8.2")."""
    if quota_percent is None:
        return ""
    return format(Decimal(quota_percent).normalize(), "f")


def format_quota(quota_percent):
    """"37.6 %" as the specification writes it; None (no quota) gives None."""
    if quota_percent is None:
        return None
    return f"{quota_number(quota_percent)} %"


def discount_tier(circuit_ids):
    """The tier of a LEG from the transformer circuits of its metering points.

    ``circuit_ids`` has one entry per metering point: the id of the circuit of its location, or
    None if the metering point has no location or the location no circuit. No metering points: no
    tier. All metering points in locations of one and the same circuit: price optimised. Anything
    else is the basic discount, also a metering point whose circuit is unknown (it cannot be
    shown to lie in the same circuit)."""
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
    """The problems of the production quota; a list of ``LegProblem`` (empty if there are none).

    * No quota: no problem.
    * Below the legal minimum of 5 %: that problem; it replaces the warning, which would only say
      the same (a quota below 5 % is also below the threshold).
    * Else below the warning threshold of the settings: the warning problem.
    * Stale: a metering point of the LEG was created after the day it was checked (the day must
      be later; on the same day the quota is not stale).
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
        """The ratio text, or None for a LEG without metering points ("–")."""
        return ratio_text(self.consumers, self.producers) if self.metering_points else None


def build_summary(leg, rows, threshold_percent):
    """The summary of one LEG from its member rows ``(direction, circuit_id, created_on)``. An archived
    LEG has no problems (it is out of sight, like every archived object: ``problems``)."""
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
    """``{leg_id: LegSummary}`` for the LEGs (a list of ``Leg``). The threshold defaults to the one
    of the management settings."""
    legs = list(legs)
    if threshold_percent is None:
        from leg_platform.community.models import ManagementSettings

        threshold_percent = ManagementSettings.load().warning_threshold_percent
    rows = member_rows([leg.pk for leg in legs], on)
    return {leg.pk: build_summary(leg, rows[leg.pk], threshold_percent) for leg in legs}


def problem_text(problem, threshold_percent=DEFAULT_WARNING_THRESHOLD_PERCENT):
    """The text of a problem for the hint of the warning triangle and for the banner
    (``legs/VORGABEN.md``). The warning text names the threshold of the settings."""
    from django.utils.translation import gettext

    if problem == LegProblem.QUOTA_BELOW_WARNING:
        return gettext("Problem: Production quota below %(threshold)s %%") % {"threshold": threshold_percent}
    return gettext("Problem: %(problem)s") % {"problem": LegProblem(problem).label}
