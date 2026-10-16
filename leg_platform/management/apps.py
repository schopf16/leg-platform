from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ManagementConfig(AppConfig):
    """The management area for staff users: views, forms, templates and static files of the pages
    under ``/management/`` (D-077). The domain is in ``leg_platform.community``."""

    name = "leg_platform.management"
    label = "management"
    verbose_name = _("Management")
