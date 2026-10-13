from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    name = "leg_platform.accounts"
    label = "accounts"
    verbose_name = _("Accounts")

    def ready(self):
        # Connects the receiver that starts the timeouts of a session at login (D-041).
        from leg_platform.accounts import session_timeout  # noqa: F401
