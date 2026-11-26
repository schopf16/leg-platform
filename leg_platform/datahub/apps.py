from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DatahubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leg_platform.datahub"
    verbose_name = _("Swisseldex Datahub")
