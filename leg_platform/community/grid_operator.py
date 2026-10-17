"""The name of the grid operator in the texts of the management area (D-086).

The grid operator (the company that assigns the metering point numbers and whose portal the LEG is registered
in) belongs to the instance and is never written into the code: the specification's texts name one operator, the
product says "bei der %(operator)s angemeldet" and fills in the name that a system administrator entered in
the Django administration (``community.GridOperator``). While no name is entered the neutral word "grid
operator" ("Netzbetreiber") stands in its place.

A source text with a placeholder is used in one of two ways:

* inside a function: ``_("... %(operator)s ...") % {"operator": grid_operator.name()}``;
* where a lazy text is needed at the level of a module or a class: ``grid_operator.text(gettext_lazy("..."))``,
  which fills the name in when the text is used (every time, so that a change of the name shows at once).
"""

from django.utils.functional import lazy
from django.utils.translation import gettext_lazy

FALLBACK = gettext_lazy("grid operator")


def name():
    """The name of the grid operator, or the neutral word while none is entered. One small query."""
    from leg_platform.community.models import GridOperator

    value = GridOperator.objects.filter(pk=1).values_list("name", flat=True).first()
    return value or str(FALLBACK)


def _filled(message):
    return str(message) % {"operator": name()}


_lazy_filled = lazy(_filled, str)


def text(message):
    """A lazy text whose ``%(operator)s`` is replaced by the name of the grid operator when the text is used."""
    return _lazy_filled(message)
