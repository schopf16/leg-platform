"""Template context of the instance configuration (docs/DESIGN.md, section 3.2)."""

from django.conf import settings
from django.utils import timezone

from leg_platform.accounts.login_protection import LOGIN_COOLOFF
from leg_platform.presentation import colour_mode


def instance(request):
    return {
        "instance_name": settings.INSTANCE_NAME,
        "instance_privacy_url": settings.INSTANCE_PRIVACY_URL,
        "instance_website_url": settings.INSTANCE_WEBSITE_URL,
        "instance_logo_light": settings.INSTANCE_BRANDING.logo_light,
        "instance_logo_dark": settings.INSTANCE_BRANDING.logo_dark,
        "colour_mode": colour_mode(request),
        "print_date": timezone.localdate(),
        # The duration of a temporary lockout, for the lockout page (the same value the abuse
        # protection uses, docs/SECURITY.md, section 3).
        "login_cooloff_minutes": int(LOGIN_COOLOFF.total_seconds() // 60),
    }
