from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CommunityConfig(AppConfig):
    """Locations, metering points, LEGs and their assignments (D-077); views and forms are in ``leg_platform.management``."""

    name = "leg_platform.community"
    label = "community"
    verbose_name = _("Community")
