from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    name = "leg_platform.accounts"
    label = "accounts"
    # Same as the start page group, so the admin breadcrumb reads "Users > Accounts".
    verbose_name = _("Users")

    def ready(self):
        # Side-effect import: connects the receiver that starts a session's timeouts at login (D-041).
        from leg_platform.accounts import session_timeout  # noqa: F401
        from leg_platform.language import install_english_fallback

        # No German text on a French or Italian page for a text that its catalogue lacks.
        install_english_fallback()
