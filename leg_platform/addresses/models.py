"""Address directory (own SQLite file, alias ``addresses``, public data) and the import queue row (application database); see ``router`` (D-075)."""

import uuid
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .keys import key
from .storage import DIRECTORY_ALIAS  # noqa: F401  (the alias of the directory file, D-075)


class Directory(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    data_date = models.DateField(null=True, verbose_name=_("Data date"))
    imported_at = models.DateTimeField(null=True, verbose_name=_("Imported at"))
    checksum = models.CharField(max_length=64, blank=True)
    row_count = models.PositiveIntegerField(default=0, verbose_name=_("Address count"))
    # Import lease against concurrent imports (importer.py).
    import_token = models.UUIDField(null=True, editable=False)
    import_lease_until = models.DateTimeField(null=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Address directory")
        verbose_name_plural = _("Address directory")

    @property
    def is_stale(self):
        return self.data_date is None or self.data_date < timezone.localdate() - timedelta(days=92)


class AddressQuerySet(models.QuerySet):
    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        for address in objs:
            address.fill_keys()
        return super().bulk_create(objs, *args, **kwargs)


class Address(models.Model):
    generation = models.UUIDField(editable=False)
    street = models.CharField(max_length=255)
    house_number = models.CharField(max_length=32, blank=True)
    postal_code = models.CharField(max_length=4)
    locality = models.CharField(max_length=255)
    # Folded search copies (``keys``), filled on save and in bulk_create.
    street_key = models.CharField(max_length=255, editable=False, default="")
    house_number_key = models.CharField(max_length=32, editable=False, default="", blank=True)
    locality_key = models.CharField(max_length=255, editable=False, default="")

    objects = AddressQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        indexes = [
            # Check of a complete address, candidate streets of the approximate search, addresses of one street.
            models.Index(
                fields=["generation", "postal_code", "street_key", "street", "house_number_key"],
                name="addr_generation_postal_street",
            ),
            # Suggestions: prefix range on the street key, in answer order.
            models.Index(
                fields=["generation", "street_key", "street", "house_number", "postal_code", "locality"],
                name="addr_generation_street",
            ),
        ]

    def fill_keys(self):
        self.street_key = key(self.street)
        self.house_number_key = key(self.house_number)
        self.locality_key = key(self.locality)

    def save(self, *args, **kwargs):
        self.fill_keys()
        super().save(*args, **kwargs)


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
    # Last manual repair that ended without an intact directory; the next one waits (importer.
    # REPAIR_RETRY_INTERVAL, D-076). Kept here because the directory file is what is damaged.
    last_failed_repair_at = models.DateTimeField(null=True, editable=False)
    # Progress of the running import (D-090): the runner's heartbeat writes these every few seconds. A
    # RUNNING job whose heartbeat is old belongs to a process that is gone (restart, kill) and is taken over.
    claim_token = models.UUIDField(null=True, editable=False)
    heartbeat_at = models.DateTimeField(null=True, editable=False)
    # One of ``progress.PHASES`` while running; "unchanged" after a success without a download.
    phase = models.CharField(max_length=16, blank=True, default="", editable=False)
    bytes_done = models.PositiveBigIntegerField(null=True, editable=False)
    bytes_total = models.PositiveBigIntegerField(null=True, editable=False)
    rows_done = models.PositiveIntegerField(null=True, editable=False)
    # The safe, fixed text of the last failure (``importer.ImportFailure``); never upstream data.
    last_error = models.CharField(max_length=255, blank=True, default="", editable=False)

    class Meta:
        default_permissions = ()
        permissions = [("request_directory_import", _("Can request an address directory import"))]
        verbose_name = _("Address directory import")
        verbose_name_plural = _("Address directory imports")

    def __str__(self):
        return str(_("Address directory import"))
