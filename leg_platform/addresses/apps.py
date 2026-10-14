from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AddressesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "leg_platform.addresses"
    verbose_name = _("Address directory")
