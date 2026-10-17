"""The instance's grid operator name for texts of the management area (D-086); never in code.

Falls back to the neutral word "grid operator" while no name is entered. Use
``_("... %(operator)s ...") % {"operator": grid_operator.name()}`` in functions, or
``grid_operator.text(gettext_lazy("..."))`` for lazy texts at module/class level.
"""

from django.utils.functional import lazy
from django.utils.translation import gettext_lazy

FALLBACK = gettext_lazy("grid operator")


def name():
    """The grid operator name, or the neutral word while none is entered."""
    from leg_platform.community.models import GridOperator

    value = GridOperator.objects.filter(pk=1).values_list("name", flat=True).first()
    return value or str(FALLBACK)


def _filled(message):
    return str(message) % {"operator": name()}


_lazy_filled = lazy(_filled, str)


def text(message):
    """A lazy text whose ``%(operator)s`` is filled in each time it is used."""
    return _lazy_filled(message)
