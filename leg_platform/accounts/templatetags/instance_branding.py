"""The instance name and the logos for pages that must render without a request.

The error pages (``error_base.html``) cannot rely on a context processor: Django renders the page
of a server error without a request. The values come straight from the settings, so no session
and no database are needed (D-054, D-056).
"""

from types import SimpleNamespace

from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def instance_branding():
    """The name and the two logo files of the instance (the logos may be empty)."""
    branding = settings.INSTANCE_BRANDING
    return SimpleNamespace(
        name=settings.INSTANCE_NAME,
        logo_light=branding.logo_light,
        logo_dark=branding.logo_dark,
    )
