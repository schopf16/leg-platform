"""The instance name and logos for pages that must render without the database.

A server error page is rendered without a request, so then the values come straight from the
settings (no session, no database; D-054, D-056); with a request the portal settings accessor is used
(falls back to the settings if the database does not answer, D-073).
"""

from types import SimpleNamespace

from django import template

from leg_platform.accounts.portal_settings import environment_values, portal_values

register = template.Library()


@register.simple_tag(takes_context=True)
def instance_branding(context):
    """The name and the addresses of the two logos of the instance (the logos may be empty)."""
    request = context.get("request")
    values = environment_values() if request is None else portal_values(request)
    return SimpleNamespace(
        name=values.name, logo_light=values.logo_light, logo_dark=values.logo_dark
    )
