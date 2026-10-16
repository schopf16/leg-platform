"""The address directory (an own SQLite file) and the import queue (the application database).

The directory (``Directory``, ``Address``) is public data without a reference to any user or
profile. It lives in its own SQLite file (alias ``addresses``) that is not part of any backup; the
import replaces it as a whole and runs only in the worker process (``process_address_imports``,
D-075). ``ImportRequest`` is the queue row of the admin button; it stays in the application
database and contains no profile, address, user reference, download URL, credentials or arbitrary
command arguments. ``leg_platform.addresses.router`` keeps the two apart.
"""

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
    # A lease against two imports at the same time (the worker and the operator command): the
    # importer that holds it has the token; it expires, so that a killed importer does not block
    # the next import for ever (importer.py).
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
    # Folded copies of the three texts that are searched (leg_platform.addresses.keys): lower case
    # without accents. They are filled on save and in bulk_create; the searches use only them.
    street_key = models.CharField(max_length=255, editable=False, default="")
    house_number_key = models.CharField(max_length=32, editable=False, default="", blank=True)
    locality_key = models.CharField(max_length=255, editable=False, default="")

    objects = AddressQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        indexes = [
            # Equality on the postal code and the street, then a range on the house number: the
            # check of a complete address, the candidate streets of the approximate search (the
            # index covers the street, so no table row is read for them) and the addresses of one
            # street.
            models.Index(
                fields=["generation", "postal_code", "street_key", "street", "house_number_key"],
                name="addr_generation_postal_street",
            ),
            # A prefix range on the street key, in the order of the answer: the suggestions.
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

    class Meta:
        default_permissions = ()
        permissions = [("request_directory_import", _("Can request an address directory import"))]
        verbose_name = _("Address directory import")
        verbose_name_plural = _("Address directory imports")

    def __str__(self):
        return str(_("Address directory import"))
