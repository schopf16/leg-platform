"""Template context of the instance configuration (docs/DESIGN.md, section 3.2)."""

from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from leg_platform.accounts.login_protection import LOGIN_COOLOFF
from leg_platform.accounts.portal_settings import legal_links


def instance(request):
    # The addresses of the footer are read from the database only when a template uses them: an
    # error page, which does not show the footer, must not need the database (D-054, D-056).
    links = SimpleLazyObject(lambda: legal_links(request))
    return {
        "instance_name": settings.INSTANCE_NAME,
        # The addresses of the footer: maintained in the portal settings (the privacy address
        # falls back to the instance configuration), D-056.
        "instance_privacy_url": SimpleLazyObject(lambda: links.privacy_url),
        "instance_imprint_url": SimpleLazyObject(lambda: links.imprint_url),
        "instance_website_url": settings.INSTANCE_WEBSITE_URL,
        # Only the host is shown as the text of the link (validated https URL, so it has one).
        "instance_website_host": urlsplit(settings.INSTANCE_WEBSITE_URL).hostname or "",
        "instance_logo_light": settings.INSTANCE_BRANDING.logo_light,
        "instance_logo_dark": settings.INSTANCE_BRANDING.logo_dark,
        "print_date": timezone.localdate(),
        # The duration of a temporary lockout, for the lockout page (the same value the abuse
        # protection uses, docs/SECURITY.md, section 3).
        "login_cooloff_minutes": int(LOGIN_COOLOFF.total_seconds() // 60),
    }
