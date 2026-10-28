"""The published plant register read per building (D-117): count, total power and mean coordinates of one EGID.

No network and no downloader here: web code may import this module (``plants`` is the import).
"""

from decimal import Decimal
from typing import NamedTuple

from django.db.models import Avg, Count, FloatField, Sum

from .models import Plant, PlantRegister

# The federal building identifier: a whole number of one to nine digits (as the plant register, ``plants.EGID_PATTERN``).
MIN_EGID = 1
MAX_EGID = 999_999_999
# Technology code of photovoltaics in the register (column SubCategory; ``Plant.subcategory``).
PHOTOVOLTAIC_SUBCATEGORY = "subcat_2"


class PlantSummary(NamedTuple):
    """The plants of one building in the published generation: count, total power in kW, mean LV95 coordinates."""

    count: int
    total_kw: Decimal
    x: float | None
    y: float | None


def pv_power_kw(egid):
    """The total power in kW of the photovoltaic plants of one building in the published register (None when the register
    is not published or the building has none); a hint to prefill a form, never a stored fact. Same ``egid`` rule as
    ``plant_summary``."""
    if type(egid) is not int or not MIN_EGID <= egid <= MAX_EGID:
        raise ValueError("The building identifier is invalid.")
    register = PlantRegister.objects.filter(pk=1).values("generation", "row_count").first()
    if register is None or register["row_count"] == 0:
        return None
    total = Plant.objects.filter(
        generation=register["generation"], egid=egid, subcategory=PHOTOVOLTAIC_SUBCATEGORY
    ).aggregate(total=Sum("total_power_kw"))["total"]
    return total or None


def plant_summary(egid):
    """The plants of one building in the published plant register, or None when nothing is published (D-117).

    ``count`` and ``total_kw`` cover every plant of the building (all technologies); the mean coordinates use the plants
    that have coordinates and are None when none has. ``ValueError`` for an ``egid`` that is not a whole number of one to
    nine digits (``bool`` and other types are refused too).
    """
    if type(egid) is not int or not MIN_EGID <= egid <= MAX_EGID:
        raise ValueError("The building identifier is invalid.")
    register = PlantRegister.objects.filter(pk=1).values("generation", "row_count").first()
    if register is None or register["row_count"] == 0:
        return None
    result = Plant.objects.filter(generation=register["generation"], egid=egid).aggregate(
        count=Count("pk"),
        total=Sum("total_power_kw"),
        x=Avg("x", output_field=FloatField()),
        y=Avg("y", output_field=FloatField()),
    )
    return PlantSummary(result["count"], result["total"] or Decimal("0"), result["x"], result["y"])
