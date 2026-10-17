"""Django admin of accounts; Django's autodiscover imports this package, which registers all models."""

from django.contrib import admin
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _

from axes.models import AccessAttempt, AccessFailureLog

from leg_platform.accounts.admin.email_texts import EMAIL_TEXT_PROCESSES, EmailTextAdmin, EmailTextForm
from leg_platform.accounts.admin.graph import GraphConfigurationAdmin
from leg_platform.accounts.admin.groups import RestrictedGroupAdmin
from leg_platform.accounts.admin.portal import PortalSettingsAdmin
from leg_platform.accounts.admin.security import (
    ReadOnlyAxesAdmin,
    ReadOnlyAxesFailureAdmin,
    SecurityEventAdmin,
)
from leg_platform.accounts.admin.users import SECOND_FACTOR_REQUIRED_MESSAGE, UserAdmin, UserAdminForm
from leg_platform.accounts.models import (
    EmailText,
    GraphConfiguration,
    PortalSettings,
    SecurityEvent,
    User,
)

__all__ = (
    "EMAIL_TEXT_PROCESSES",
    "SECOND_FACTOR_REQUIRED_MESSAGE",
    "EmailTextForm",
    "PortalSettingsAdmin",
    "UserAdminForm",
)

# Plain, instance-neutral title instead of Django's default (D-036); the header and title of the
# site are the name of the instance (``PortalAdminSite.each_context``).
admin.site.index_title = _("Administration")

admin.site.unregister(Group)
admin.site.register(Group, RestrictedGroupAdmin)
admin.site.register(User, UserAdmin)
admin.site.register(EmailText, EmailTextAdmin)
admin.site.register(SecurityEvent, SecurityEventAdmin)
admin.site.register(GraphConfiguration, GraphConfigurationAdmin)
admin.site.register(PortalSettings, PortalSettingsAdmin)
admin.site.register(AccessAttempt, ReadOnlyAxesAdmin)
admin.site.register(AccessFailureLog, ReadOnlyAxesFailureAdmin)
