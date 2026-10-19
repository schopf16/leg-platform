from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CustomerConfig(AppConfig):
    """The pages of the customer's participation (D-103): buildings, the registration wizard, the contract download.

    No models: the rules are in ``leg_platform.community`` (``applications``, ``buildings``, ``contracts``,
    ``own_iban``). The pages call these services and never re-implement a rule."""

    name = "leg_platform.customer"
    label = "customer"
    verbose_name = _("Customer area")
