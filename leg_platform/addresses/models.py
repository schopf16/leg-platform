"""Address directory (own SQLite file, alias ``addresses``, public data) and the import queue row (application database); see ``router`` (D-075)."""

import uuid
from datetime import time, timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models.common import EXACT_COLLATION

from .keys import key
from .storage import DIRECTORY_ALIAS  # noqa: F401  (the alias of the directory file, D-075)

# Default time of day of the daily address directory run (``schedule.import_run_time``).
DEFAULT_RUN_TIME = time(3, 0)


class Directory(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    data_date = models.DateField(null=True, verbose_name=_("Data date"))
    imported_at = models.DateTimeField(null=True, verbose_name=_("Imported at"))
    checksum = models.CharField(max_length=64, blank=True)
    row_count = models.PositiveIntegerField(default=0, verbose_name=_("Address count"))
    # Whether the published generation carries the building identifier (EGID, D-049). A generation published
    # before that was added has none: it is replaced by the next import even if the checksum is unchanged.
    has_egid = models.BooleanField(default=False, editable=False)
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
    # Federal building identifier (column BDG_EGID of the official file; D-049). One row per row of the file:
    # an address with several buildings (entrances) has several rows, a building with several addresses
    # shares its EGID over several rows. Null: the file gives the address no building.
    egid = models.PositiveIntegerField(null=True, editable=False)
    # Entrance of the building (column ADR_EDID, "Eingangsidentifikator"; D-049): with the EGID it tells the
    # entrances of one building (or the buildings of one address) apart. Null: the file gives none.
    entrance_id = models.PositiveSmallIntegerField(null=True, editable=False)

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
            # Towns and their postal codes (D-111): prefix range on the town key, covering, in answer order. The
            # postal code comes last, so that the check of a complete address still prefers the index of the postal code.
            models.Index(
                fields=["generation", "locality_key", "locality", "postal_code"],
                name="addr_generation_locality",
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
    # The update flag (KONZEPT 3a, D-066): set once when a building could not be found in the directory nor at
    # swisstopo; the daily run (``schedule``) downloads the directory and a successful import that started
    # after the flag was set clears it. Never holds an address.
    update_flag_set_at = models.DateTimeField(null=True, editable=False)
    # When the last import ended because the official file lacks a required column ("Missing EGID column.",
    # "Missing entrance column."): the daily run then waits a week before it downloads again (schedule); cleared by a
    # successful import. The button is not held back.
    columns_missing_at = models.DateTimeField(null=True, editable=False)
    # The daily run (``schedule``): time of day (instance time zone) from which it may start the import, and the
    # day of the last run (at most one download per day). Read through ``schedule.import_run_time``.
    run_time = models.TimeField(default=DEFAULT_RUN_TIME, editable=False)
    last_scheduled_run_on = models.DateField(null=True, editable=False)

    class Meta:
        default_permissions = ()
        permissions = [("request_directory_import", _("Can request an address directory import"))]
        verbose_name = _("Address directory import")
        verbose_name_plural = _("Address directory imports")

    def __str__(self):
        return str(_("Address directory import"))


class BuildingLookup(models.Model):
    """The answer of the external building search for one address, kept so that it is not asked again (``buildings``).

    Lives in the application database, not in the directory file: an import does not replace it and the
    request threads may write it. The address itself is never stored, only a keyed digest of its normalised
    form; a found building is public data of the official directory. An empty list is a negative answer
    that expires after ``buildings.NEGATIVE_CACHE_DAYS``.
    """

    digest = models.CharField(max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False)
    # From when the answer is no longer used (``buildings.POSITIVE_CACHE_DAYS``, ``NEGATIVE_CACHE_DAYS``); the
    # scheduled cleanup deletes it then.
    expires_at = models.DateTimeField(db_index=True, editable=False)
    # [{"egid": 1, "street": "..", "house_number": "..", "postal_code": "..", "locality": ".."}, ...]
    buildings = models.JSONField(default=list, editable=False)

    class Meta:
        default_permissions = ()
