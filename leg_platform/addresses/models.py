"""Only directory metadata/entries live in the separate address database.

ImportRequest lives in the application database. It contains no profile, address,
user reference, download URL, credentials or arbitrary command arguments.
"""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

READ_ALIAS = "addresses"
WRITE_ALIAS = "address_import"
APPLICATION_ALIAS = "default"


class Directory(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    data_date = models.DateField(null=True, verbose_name=_("Data date"))
    imported_at = models.DateTimeField(null=True, verbose_name=_("Imported at"))
    checksum = models.CharField(max_length=64, blank=True)
    row_count = models.PositiveIntegerField(default=0, verbose_name=_("Address count"))

    class Meta:
        default_permissions = ()
        verbose_name = _("Address directory")
        verbose_name_plural = _("Address directory")

    @property
    def is_stale(self):
        return self.data_date is None or self.data_date < timezone.localdate() - timedelta(days=92)


class Address(models.Model):
    generation = models.UUIDField(editable=False)
    street = models.CharField(max_length=255)
    house_number = models.CharField(max_length=32, blank=True)
    postal_code = models.CharField(max_length=4)
    locality = models.CharField(max_length=255)

    class Meta:
        default_permissions = ()
        indexes = [
            models.Index(fields=["generation", "postal_code", "street"], name="addr_generation_postal_street"),
            models.Index(fields=["generation", "street"], name="addr_generation_street"),
        ]


class ImportRequest(models.Model):
    class State(models.TextChoices):
        IDLE = "idle", _("No import requested")
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Completed")
        FAILED = "failed", _("Failed")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    state = models.CharField(max_length=12, choices=State, default=State.IDLE, verbose_name=_("Status"))
    requested_at = models.DateTimeField(null=True, verbose_name=_("Requested at"))
    started_at = models.DateTimeField(null=True, verbose_name=_("Started at"))
    finished_at = models.DateTimeField(null=True, verbose_name=_("Finished at"))
    last_automatic_attempt_at = models.DateTimeField(
        null=True, verbose_name=_("Last automatic attempt at"),
    )
    is_automatic = models.BooleanField(default=False, editable=False)

    class Meta:
        default_permissions = ()
        permissions = [("request_directory_import", _("Can request an address directory import"))]
        verbose_name = _("Address directory import")
        verbose_name_plural = _("Address directory imports")

    def __str__(self):
        return str(_("Address directory import"))
