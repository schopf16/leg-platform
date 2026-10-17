"""Display filter for the breadcrumbs of the administration.

Django 6.1 ships ``admin_filters.to_object_display_value``; Django 6.0 (the declared minimum) does
not, so the admin templates load this equivalent library instead.
"""

from django import template
from django.contrib.admin.utils import display_for_value

register = template.Library()

# Django's default empty-value marker of the administration.
EMPTY_VALUE_DISPLAY = "-"


@register.filter
def to_object_display_value(value):
    return display_for_value(str(value), EMPTY_VALUE_DISPLAY)
