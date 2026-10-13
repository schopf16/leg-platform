"""Template context of the instance configuration (docs/DESIGN.md, section 3.2)."""

from django.conf import settings


def instance(request):
    return {
        "instance_name": settings.INSTANCE_NAME,
        "instance_privacy_url": settings.INSTANCE_PRIVACY_URL,
        "instance_website_url": settings.INSTANCE_WEBSITE_URL,
    }
