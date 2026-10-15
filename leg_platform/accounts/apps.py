from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    name = "leg_platform.accounts"
    label = "accounts"
    # The name of the app in the breadcrumb of the administration: the same as the group of the start
    # page, so that the path reads "Users > Accounts" and not "Accounts > Accounts" (P2-7).
    verbose_name = _("Users")

    def ready(self):
        # Connects the receiver that starts the timeouts of a session at login (D-041).
        from leg_platform.accounts import session_timeout  # noqa: F401
