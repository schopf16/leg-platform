from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CommunityConfig(AppConfig):
    """The domain of the local electricity community: locations, metering points, LEGs and the
    assignments between them (D-077). Views and forms are in ``leg_platform.management``."""

    name = "leg_platform.community"
    label = "community"
    verbose_name = _("Community")
