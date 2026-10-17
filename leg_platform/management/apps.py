from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ManagementConfig(AppConfig):
    """Staff area under ``/management/`` (D-077); the domain is in ``leg_platform.community``."""

    name = "leg_platform.management"
    label = "management"
    verbose_name = _("Management")
