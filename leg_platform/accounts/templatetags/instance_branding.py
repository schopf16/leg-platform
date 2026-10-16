"""The instance name and the logos for pages that must render without the database.

The error pages (``error_base.html``) cannot rely on a context processor: Django renders the page
of a server error without a request. Without a request in the context the values come straight
from the settings, so no session and no database are needed (D-054, D-056). The pages of a client
error (403, 404, failed CSRF check) are rendered with the request and use the accessor of the
portal settings, which falls back to the settings if the database does not answer (D-073).
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
