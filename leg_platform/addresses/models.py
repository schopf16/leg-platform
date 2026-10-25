"""Address directory (own SQLite file, alias ``addresses``, public data) and the import queue row (application database); see ``router`` (D-075)."""

import uuid
from datetime import time, timedelta

from django.core.validators import RegexValidator
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
    # The day (instance time zone) on which the published file last changed, that is its checksum differed from the one
    # before (D-117, hint "stale"). Kept when the same file is downloaded again; null before the first publication.
    content_changed_at = models.DateField(null=True, editable=False)
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
    # LV95 (EPSG:2056) position of the address point (columns ADR_EASTING and ADR_NORTHING; D-049 amendment of
    # 2026-10-25). Null: the file gives none. Used for the transformer station match (D-118).
    easting = models.DecimalField(max_digits=10, decimal_places=3, null=True, editable=False)
    northing = models.DecimalField(max_digits=10, decimal_places=3, null=True, editable=False)

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

    class Source(models.TextChoices):
        ADDRESS = "address", _("Address directory")
        PLANT = "plant", _("Plant register")
        TRANSFORMER = "transformer", _("Transformer stations")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    state = models.CharField(max_length=12, choices=State, default=State.IDLE, verbose_name=_("Status"))
    # The import that holds this one claim (D-117): the address directory or the plant register. Both share the claim
    # (D-090): no import starts while the other one runs.
    source = models.CharField(max_length=12, choices=Source, default=Source.ADDRESS, editable=False)
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
    # The day of the last daily run of the plant register (``schedule.run_plant_daily``, D-117); its own day, so that
    # the address run and the plant run do not use up each other's day.
    last_plant_scheduled_run_on = models.DateField(null=True, editable=False)
    # The result of the last plant run (D-117, administration block "PV-Anlagen (BFE)"): kept apart from the fields above,
    # which the address import overwrites. Set by ``jobs`` when a plant run ends: ``State.SUCCEEDED`` or ``State.FAILED``,
    # the time, and the fixed failure text ("" after a success).
    last_plant_state = models.CharField(max_length=12, blank=True, default="", editable=False)
    last_plant_finished_at = models.DateTimeField(null=True, editable=False)
    last_plant_error = models.CharField(max_length=255, blank=True, default="", editable=False)
    # The weekly transformer station scan (D-118, ``schedule.run_transformer_weekly``): the day of its last run, the
    # result of the last run (``last_transformer_state``, time, fixed failure text) and the statistic of the last success.
    last_transformer_scheduled_run_on = models.DateField(null=True, editable=False)
    last_transformer_state = models.CharField(max_length=12, blank=True, default="", editable=False)
    last_transformer_finished_at = models.DateTimeField(null=True, editable=False)
    last_transformer_error = models.CharField(max_length=255, blank=True, default="", editable=False)
    last_transformer_postal_codes = models.CharField(max_length=255, blank=True, default="", editable=False)
    last_transformer_buildings = models.PositiveIntegerField(null=True, editable=False)
    last_transformer_with_name = models.PositiveIntegerField(null=True, editable=False)

    class Meta:
        default_permissions = ()
        permissions = [("request_directory_import", _("Can request an address directory import"))]
        verbose_name = _("Data import")
        verbose_name_plural = _("Data imports")

    def __str__(self):
        return str(_("Data import"))


class ServicePostalCode(models.Model):
    """A postal code of the area the LEG serves; the transformer station scan covers these postal codes (D-118).

    Instance data: entered by staff in the administration (right ``addresses.manage_service_areas``), never in code.
    """

    postal_code = models.CharField(
        max_length=4, unique=True, verbose_name=_("Postal code"),
        validators=[RegexValidator(r"^[1-9][0-9]{3}$", message=_("Enter a Swiss postal code with four digits."))],
    )

    class Meta:
        default_permissions = ()
        ordering = ["postal_code"]
        permissions = [("manage_service_areas", _("Can manage the postal codes of the service area"))]
        verbose_name = _("Postal code of the service area")
        verbose_name_plural = _("Postal codes of the service area")

    def __str__(self):
        return self.postal_code


class TransformerStation(models.Model):
    """The transformer station of one building, from the public BKW high-discount map (D-118). In the address directory
    file (alias ``addresses``): one row per building (EGID), with no foreign key to the address generation. A weekly scan
    replaces the rows of the postal codes it scanned; a lookup joins them to the current generation by EGID.
    """

    class Match(models.TextChoices):
        EXACT = "exact", _("Exact")
        FALLBACK = "fallback", _("Nearest dot")
        NONE = "none", _("No dot")

    egid = models.PositiveIntegerField(unique=True, editable=False)
    postal_code = models.CharField(max_length=4, db_index=True, editable=False)
    # The cleaned name only (``transformers.CLEAN_NAME``), never the raw text of the map; "" when the map shows none.
    name = models.CharField(max_length=64, blank=True, default="", editable=False)
    match = models.CharField(max_length=8, choices=Match, editable=False)
    scanned_at = models.DateTimeField(editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Transformer station")
        verbose_name_plural = _("Transformer stations")


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


class PlantRegister(models.Model):
    """The published generation of the plant register (BFE, D-117): one row, in the addresses file (alias ``addresses``)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    data_date = models.DateField(null=True, editable=False)
    imported_at = models.DateTimeField(null=True, editable=False)
    # SHA-256 of the downloaded ZIP. Not compared with an official value (none is published; D-117).
    checksum = models.CharField(max_length=64, blank=True, editable=False)
    # The day on which the published file last changed (same rule as ``Directory.content_changed_at``, D-117).
    content_changed_at = models.DateField(null=True, editable=False)
    row_count = models.PositiveIntegerField(default=0, editable=False)
    rejected_count = models.PositiveIntegerField(default=0, editable=False)
    # Plants without an EGID: skipped, not stored (D-117).
    skipped_count = models.PositiveIntegerField(default=0, editable=False)
    # Statistic of the last successful import (D-117), not used for any decision and not shown yet. The address count
    # of the published directory when the plants were published, and the rows of the plant file (row_count plus
    # rejected_count plus skipped_count). A failed or refused import leaves all of these values as they were.
    address_count = models.PositiveIntegerField(default=0, editable=False)
    file_row_count = models.PositiveIntegerField(default=0, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Plant register")
        verbose_name_plural = _("Plant register")


class Plant(models.Model):
    """One plant of the plant register (BFE, D-117): the fields the portal uses only, public register data.

    One building (EGID) can have several plants: the relation is one to many.
    """

    generation = models.UUIDField(editable=False)
    # Identifier of the source (column xtf_id), unique within a generation.
    xtf_id = models.CharField(max_length=64, editable=False)
    egid = models.PositiveIntegerField(editable=False)
    # Technology code of the source (column SubCategory, e.g. subcat_2 for photovoltaics).
    subcategory = models.CharField(max_length=16, editable=False)
    # Total power in kW (column TotalPower: first commissioning plus all extensions).
    total_power_kw = models.DecimalField(max_digits=11, decimal_places=3, editable=False)
    # LV95 (EPSG:2056) coordinates of the plant; null when the source gives none.
    x = models.DecimalField(max_digits=10, decimal_places=3, null=True, editable=False)
    y = models.DecimalField(max_digits=10, decimal_places=3, null=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Plant of the register")
        verbose_name_plural = _("Plants of the register")
        constraints = [
            models.UniqueConstraint(fields=["generation", "xtf_id"], name="plant_generation_xtf_id"),
        ]
        indexes = [
            # Plants of one building.
            models.Index(fields=["generation", "egid"], name="plant_generation_egid"),
        ]
