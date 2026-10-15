"""Display filter for the breadcrumbs of the administration.

Django 6.1 ships the template library ``admin_filters`` with the filter ``to_object_display_value``.
Django 6.0, the declared minimum (requirements/minimum-constraints.txt), does not have that library,
so the templates of the administration load this library instead. The filter does what the Django
filter does: it shows the object with ``display_for_value`` and the empty-value marker ``-``.
"""

from django import template
from django.contrib.admin.utils import display_for_value

register = template.Library()

# Django's default empty-value marker of the administration (AdminSite.empty_value_display).
EMPTY_VALUE_DISPLAY = "-"


@register.filter
def to_object_display_value(value):
    return display_for_value(str(value), EMPTY_VALUE_DISPLAY)
