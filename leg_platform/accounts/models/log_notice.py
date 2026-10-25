"""The e-mail notification of log events (the log menu, owner decision; D-120). One row, application database.

The row holds the setting only (switch, address, minimum severity) and the bookkeeping of the notifier: the last checked
row of each source, the time of the last mail per source (at most one per source and hour), the count of suppressed
events and the notices waiting for the next mail. A notice holds the time, the severity and the source label only.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class LogNotificationSettings(models.Model):
    class Severity(models.TextChoices):
        WARNING = "warning", _("Warning")
        ERROR = "error", _("Error")
        CRITICAL = "critical", _("Critical")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False, verbose_name=_("Send an email"))
    email = models.EmailField(blank=True, verbose_name=_("Email address"))
    minimum_severity = models.CharField(
        max_length=10, choices=Severity, default=Severity.ERROR, verbose_name=_("Minimum severity"),
    )
    state = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Log notification")
        verbose_name_plural = _("Log notification")

    def clean(self):
        super().clean()
        if self.enabled and not self.email:
            raise ValidationError({"email": _("Enter an email address, or switch the email off.")})
