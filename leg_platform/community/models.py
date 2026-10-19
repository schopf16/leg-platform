"""The domain models of the community (D-077). Rules live in the neighbouring modules.

Exactly compared identifier columns use the binary collation (D-031). Staff-only person data is
the side record ``PersonRecord``, never a column of the account model.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import EXACT_COLLATION, SWISS_POSTAL_CODE, SWISS_POSTAL_CODE_MESSAGE
from leg_platform.community import addressing, numbers
from leg_platform.community import iban as iban_module
from leg_platform.community.status import MeteringPointStatus, derive_status, status_filter

# Art. 19e para. 1 StromVV: a constant, not a setting.
LEGAL_MINIMUM_QUOTA_PERCENT = 5
DEFAULT_WARNING_THRESHOLD_PERCENT = 10

SUBJECT_MAX_LENGTH = 20
ADDRESS_SIGNATURE_LENGTH = 400
# The PDF prints a remark in full: ``management.printing.pdf.MAX_FACT_CHARS`` must not be smaller (D-084).
REMARK_MAX_LENGTH = 5000
REMARK_VALIDATORS = [MaxLengthValidator(REMARK_MAX_LENGTH)]


def validate_postal_code(value):
    """A Swiss postal code with four digits (the rule of the profile)."""
    if SWISS_POSTAL_CODE.fullmatch(value or "") is None:
        raise ValidationError(SWISS_POSTAL_CODE_MESSAGE, code="invalid_postal_code")


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(_("created"), auto_now_add=True)
    updated_at = models.DateTimeField(_("changed"), auto_now=True)

    class Meta:
        abstract = True


class Direction(models.TextChoices):
    CONSUMPTION = "consumption", _("Consumption")
    FEED_IN = "feed_in", _("Feed-in")


class TransformerCircuit(TimestampedModel):
    """A grid circuit; the operator's number is unique after normalisation, the name is free."""

    grid_number = models.CharField(
        _("grid number"),
        max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        unique=True,
        db_collation=EXACT_COLLATION,
        validators=[numbers.validate_transformer_circuit_number],
    )
    name = models.CharField(_("name"), max_length=120, blank=True)

    class Meta:
        # Only "view", for the plain tables of the Django administration (D-091); the management area uses none (D-086).
        default_permissions = ("view",)
        verbose_name = _("transformer circuit")
        verbose_name_plural = _("transformer circuits")
        ordering = ("grid_number",)

    def __str__(self):
        return self.grid_number

    def save(self, *args, **kwargs):
        self.grid_number = numbers.normalise_transformer_circuit_number(self.grid_number)
        self.name = addressing.collapse(self.name)
        super().save(*args, **kwargs)


class AddressSuggestionMixin(models.Model):
    """State of the directory suggestion for an address; the subclass provides ``current_address``.

    ``address_correction_open`` is stored (suggestion exists and was not dismissed for the current
    address) so that lists can filter by it.
    """

    # The verbose names are shown by the raw tables of the administration (D-091).
    suggested_street = models.CharField(_("suggested street"), max_length=255, blank=True, editable=False)
    suggested_house_number = models.CharField(
        _("suggested house number"), max_length=32, blank=True, editable=False
    )
    suggested_postal_code = models.CharField(
        _("suggested postal code"), max_length=4, blank=True, editable=False
    )
    suggested_town = models.CharField(_("suggested town"), max_length=255, blank=True, editable=False)
    suggestion_dismissed_for = models.CharField(
        _("suggestion dismissed for"), max_length=ADDRESS_SIGNATURE_LENGTH, blank=True, editable=False
    )
    address_correction_open = models.BooleanField(_("address correction open"), default=False, editable=False)

    class Meta:
        abstract = True

    def current_address(self):
        raise NotImplementedError

    def current_address_signature(self):
        return addressing.address_signature(*self.current_address())

    @property
    def suggestion(self):
        if not (self.suggested_street or self.suggested_town):
            return None
        return addressing.AddressSuggestion(
            self.suggested_street, self.suggested_house_number,
            self.suggested_postal_code, self.suggested_town,
        )

    def apply_suggestion(self, suggestion):
        """Store a directory check result and update ``address_correction_open``. Does not save."""
        values = suggestion.as_tuple() if suggestion else ("", "", "", "")
        (self.suggested_street, self.suggested_house_number,
         self.suggested_postal_code, self.suggested_town) = values
        self.address_correction_open = (
            suggestion is not None
            and self.current_address_signature() != self.suggestion_dismissed_for
        )

    def dismiss_suggestion(self):
        """"Do not accept": the warning returns only if the address changes. Does not save."""
        self.suggestion_dismissed_for = self.current_address_signature()
        self.address_correction_open = False

    def clear_suggestion(self):
        """Forget suggestion and dismissal (e.g. after acceptance). Does not save."""
        self.suggestion_dismissed_for = ""
        self.apply_suggestion(None)


class Location(AddressSuggestionMixin, TimestampedModel):
    """A physical address, independent of persons; unique after normalisation."""

    street = models.CharField(_("street"), max_length=150)
    house_number = models.CharField(_("house number"), max_length=30)
    postal_code = models.CharField(_("postal code"), max_length=4, validators=[validate_postal_code])
    town = models.CharField(_("town"), max_length=100)
    # Not part of the unique address and not checked against the directory.
    address_addition = models.CharField(_("address addition"), max_length=150, blank=True)
    # SHA-256 of the normalised address (``addressing.address_digest``); maintained by ``save``.
    address_digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )
    transformer_circuit = models.ForeignKey(
        TransformerCircuit,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="locations",
        verbose_name=_("transformer circuit"),
    )
    # Internal; never searched.
    remark = models.TextField(_("remark"), blank=True, validators=REMARK_VALIDATORS)
    is_archived = models.BooleanField(_("archived"), default=False)
    # Created by a customer and not yet confirmed by the staff ("neu").
    needs_confirmation = models.BooleanField(_("not yet confirmed"), default=False)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("location")
        verbose_name_plural = _("locations")
        ordering = ("street", "house_number")

    def __str__(self):
        return f"{self.street} {self.house_number}, {self.postal_code} {self.town}"

    def current_address(self):
        return (self.street, self.house_number, self.postal_code, self.town)

    def normalise(self):
        self.street, self.house_number, self.postal_code, self.town = addressing.normalise_address(
            *self.current_address()
        )
        self.address_digest = addressing.address_digest(*self.current_address())
        self.address_addition = addressing.collapse(self.address_addition)

    def clean_fields(self, exclude=None):
        self.normalise()
        super().clean_fields(exclude=exclude)

    def validate_unique(self, exclude=None):
        # The digest is checked below with a message that names the address.
        super().validate_unique(exclude={*(exclude or ()), "address_digest"})
        self.normalise()
        clash = Location.objects.filter(address_digest=self.address_digest).exclude(pk=self.pk)
        if clash.exists():
            raise ValidationError(
                _("This address already exists."), code="duplicate_address"
            )

    def save(self, *args, **kwargs):
        self.normalise()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and (
            {"street", "house_number", "postal_code", "town"} & set(update_fields)
        ):
            kwargs["update_fields"] = {*update_fields, "address_digest"}
        super().save(*args, **kwargs)

    @classmethod
    def find_by_address(cls, street, house_number, postal_code, town):
        """The location with this address (normalised comparison), or None."""
        return cls.objects.filter(
            address_digest=addressing.address_digest(street, house_number, postal_code, town)
        ).first()


class MeteringPointQuerySet(models.QuerySet):
    def with_status(self, status, today=None):
        return self.filter(status_filter(status, today or timezone.localdate()))


class MeteringPoint(TimestampedModel):
    # Empty until the grid operator has assigned it; several rows may have none.
    number = models.CharField(
        _("metering point number"),
        max_length=numbers.METERING_POINT_LENGTH,
        unique=True,
        null=True,
        blank=True,
        db_collation=EXACT_COLLATION,
        validators=[numbers.validate_metering_point_number],
    )
    direction = models.CharField(_("direction"), max_length=11, choices=Direction)
    location = models.ForeignKey(
        Location,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="metering_points",
        verbose_name=_("location"),
    )
    pv_power_kwp = models.DecimalField(
        _("PV power (kWp)"), max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    battery_kwh = models.DecimalField(
        _("battery (kWh)"), max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    wallbox_kw = models.DecimalField(
        _("wallbox (kW)"), max_digits=8, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    remark = models.TextField(_("remark"), blank=True, validators=REMARK_VALIDATORS)
    is_archived = models.BooleanField(_("archived"), default=False)
    # The dates that drive the status (``community.status``).
    registered_with_operator_on = models.DateField(_("notified on"), null=True, blank=True)
    confirmed_on = models.DateField(_("confirmed on"), null=True, blank=True)
    start_date = models.DateField(_("start date"), null=True, blank=True)
    terminated_on = models.DateField(_("terminated on"), null=True, blank=True)

    objects = MeteringPointQuerySet.as_manager()

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("metering point")
        verbose_name_plural = _("metering points")
        ordering = ("number",)

    def __str__(self):
        return self.number or "–"

    def save(self, *args, **kwargs):
        # An empty number is stored as NULL, so that several metering points may have none.
        self.number = numbers.normalise_metering_point_number(self.number) or None
        self.remark = self.remark.strip()
        super().save(*args, **kwargs)

    def status(self, today=None):
        return derive_status(
            registered_with_operator_on=self.registered_with_operator_on,
            confirmed_on=self.confirmed_on,
            start_date=self.start_date,
            terminated_on=self.terminated_on,
            today=today or timezone.localdate(),
        )

    def get_status_display(self, today=None):
        return MeteringPointStatus(self.status(today)).label


class Leg(TimestampedModel):
    """A local electricity community. ``quota_checked_on`` is the date the quota was read from the operator's portal."""

    name = models.CharField(_("name"), max_length=120, unique=True)
    # No upper bound of 100 (owner, 2026-10-17: a LEG may produce more than its members consume, e.g. 204.1 %);
    # the column holds up to 999.99 (max_digits=5, decimal_places=2).
    production_quota_percent = models.DecimalField(
        _("production quota (%)"), max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0)],
    )
    quota_checked_on = models.DateField(_("checked on"), null=True, blank=True)
    remark = models.TextField(_("remark"), blank=True, validators=REMARK_VALIDATORS)
    is_archived = models.BooleanField(_("archived"), default=False)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("LEG")
        verbose_name_plural = _("LEGs")
        ordering = ("name",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(production_quota_percent__isnull=True)
                | models.Q(quota_checked_on__isnull=False),
                name="community_leg_quota_has_date",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.production_quota_percent is not None and self.quota_checked_on is None:
            raise ValidationError({"quota_checked_on": _("This field is required.")})

    def save(self, *args, **kwargs):
        self.name = addressing.collapse(self.name)
        super().save(*args, **kwargs)


class AssignmentQuerySet(models.QuerySet):
    def in_force(self, on=None):
        """Assignments that are valid on the day."""
        on = on or timezone.localdate()
        return self.filter(valid_from__lte=on).filter(
            models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=on)
        )

    def not_ended(self, on=None):
        """Assignments that are valid now or start later (not yet ended)."""
        on = on or timezone.localdate()
        return self.filter(models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=on))


class AssignmentBase(TimestampedModel):
    """A metering point assigned to a person or a LEG for a period (both days included; open if ``valid_to`` is empty).

    Change rules: ``leg_platform.community.assignments``. The ``previous_*`` columns let deleting a
    not yet effective assignment undo the change of its predecessor."""

    valid_from = models.DateField(_("valid from"))
    valid_to = models.DateField(_("valid to"), null=True, blank=True)
    previous_assignment = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False,
        verbose_name=_("previous assignment"),
    )
    previous_valid_to = models.DateField(_("previous valid to"), null=True, blank=True, editable=False)

    objects = AssignmentQuerySet.as_manager()

    class Meta:
        abstract = True
        default_permissions = ()
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True) | models.Q(valid_to__gte=models.F("valid_from")),
                name="%(app_label)s_%(class)s_period",
            ),
            models.UniqueConstraint(
                fields=["metering_point", "valid_from"], name="%(app_label)s_%(class)s_start_unique"
            ),
        ]


class PersonAssignment(AssignmentBase):
    metering_point = models.ForeignKey(
        MeteringPoint, on_delete=models.PROTECT, related_name="person_assignments",
        verbose_name=_("metering point"),
    )
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="metering_point_assignments",
        verbose_name=_("person"),
    )

    class Meta(AssignmentBase.Meta):
        default_permissions = ("view",)
        verbose_name = _("person assignment")
        verbose_name_plural = _("person assignments")
        ordering = ("valid_from",)


class LegAssignment(AssignmentBase):
    metering_point = models.ForeignKey(
        MeteringPoint, on_delete=models.PROTECT, related_name="leg_assignments",
        verbose_name=_("metering point"),
    )
    leg = models.ForeignKey(Leg, on_delete=models.PROTECT, related_name="assignments", verbose_name=_("LEG"))

    class Meta(AssignmentBase.Meta):
        default_permissions = ("view",)
        verbose_name = _("LEG assignment")
        verbose_name_plural = _("LEG assignments")
        ordering = ("valid_from",)


class HistoryEntry(models.Model):
    """Append-only history event (``save`` refuses an existing row); see ``leg_platform.community.history``.

    ``subject_type``/``subject_id`` name the object without a foreign key, so an entry outlives it.
    ``detail`` holds small facts (old/new value), never a customer name or address."""

    subject_type = models.CharField(_("subject type"), max_length=SUBJECT_MAX_LENGTH)
    subject_id = models.PositiveBigIntegerField(_("subject ID"))
    event = models.CharField(_("event"), max_length=40)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("performed by"),
    )
    created_at = models.DateTimeField(_("time"), default=timezone.now, editable=False)
    detail = models.JSONField(_("detail"), default=dict, blank=True)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("history entry")
        verbose_name_plural = _("history entries")
        ordering = ("created_at", "pk")
        indexes = [models.Index(fields=["subject_type", "subject_id", "created_at"], name="community_history_subject")]

    def __str__(self):
        return f"{self.subject_type}:{self.subject_id} {self.event}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("A history entry is never changed.")
        super().save(*args, **kwargs)


class ManagementSettings(models.Model):
    """Settings of the management area; exactly one row (id 1), read through ``load()``.

    Instance values (identifier, sender address) stay empty until the staff enters them."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    country_code = models.CharField(
        _("country"), max_length=2, default="CH", validators=[numbers.validate_country]
    )
    identifier = models.CharField(
        _("identifier"), max_length=numbers.IDENTIFIER_LENGTH, blank=True,
        validators=[numbers.validate_identifier],
    )
    warning_threshold_percent = models.PositiveSmallIntegerField(
        _("warning threshold"),
        default=DEFAULT_WARNING_THRESHOLD_PERCENT,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    sender_street = models.CharField(_("street"), max_length=150, blank=True)
    sender_house_number = models.CharField(_("house number"), max_length=30, blank=True)
    sender_address_addition = models.CharField(_("address addition"), max_length=150, blank=True)
    sender_postal_code = models.CharField(_("postal code"), max_length=4, blank=True)
    sender_town = models.CharField(_("town"), max_length=100, blank=True)
    sender_suggestion_dismissed_for = models.CharField(
        _("sender suggestion dismissed for"), max_length=ADDRESS_SIGNATURE_LENGTH, blank=True, editable=False
    )
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("management settings")
        verbose_name_plural = _("management settings")
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="community_settings_singleton")
        ]

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        row, _created = cls.objects.get_or_create(id=1)
        return row

    @property
    def metering_point_prefix(self):
        """Country and identifier: the first 13 characters of every metering point number."""
        return f"{self.country_code}{self.identifier}"

    def sender_address(self):
        return (self.sender_street, self.sender_house_number, self.sender_postal_code, self.sender_town)


class GridOperator(models.Model):
    """The grid operator of this instance (D-086); exactly one row (id 1), read through ``load()``.

    Maintained by system administrators in the Django administration. While the name is empty the
    texts say "grid operator" (``leg_platform.community.grid_operator``)."""

    NAME_MAX_LENGTH = 120

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(_("name"), max_length=NAME_MAX_LENGTH, blank=True)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("grid operator")
        verbose_name_plural = _("grid operators")
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="community_grid_operator_singleton")
        ]

    def __str__(self):
        return self.name or str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        row, _created = cls.objects.get_or_create(id=1)
        return row

    def save(self, *args, **kwargs):
        self.name = addressing.collapse(self.name)
        super().save(*args, **kwargs)


class PersonRecord(AddressSuggestionMixin, TimestampedModel):
    """Staff-side data about a person's account: note, operator customer number, encrypted IBAN, paper invoice
    wish (D-087) and address suggestion state. Created when needed; shares are ``ShareChange`` entries."""

    person = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="person_record",
        verbose_name=_("person"),
    )
    staff_note = models.TextField(_("note"), blank=True, validators=REMARK_VALIDATORS)
    operator_customer_number = models.CharField(_("customer number at the grid operator"), max_length=30, blank=True)
    # Encrypted (``community.iban``); use the ``iban`` property. Never queried.
    iban_encrypted = models.TextField(_("IBAN"), blank=True, editable=False)
    paper_invoice = models.BooleanField(_("paper invoice"), default=False)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("person")
        verbose_name_plural = _("persons")

    def __str__(self):
        return f"{self.person}"

    @property
    def iban(self):
        """The IBAN in normal form, or "". Raises ``DecryptionError`` if no key decrypts it or it was copied
        from another person's record (``iban.readable`` is the display variant)."""
        return iban_module.decrypt_iban(self.iban_encrypted, self.person_id)

    @iban.setter
    def iban(self, value):
        if self.person_id is None and iban_module.compact(value):
            raise ValueError("Set the person before the IBAN: the IBAN is bound to the account.")
        self.iban_encrypted = iban_module.encrypt_iban(value, self.person_id)

    def current_address(self):
        person = self.person
        return (
            person.street, person.house_number,
            str(person.swiss_postal_code or ""), person.town,
        )


class ShareChange(models.Model):
    """Cooperative shares of a person from ``valid_from`` on (D-086); the latest entry not after a day
    applies (``community.persons.current_shares``). Entries are only added; a correction is a new entry."""

    record = models.ForeignKey(
        PersonRecord, on_delete=models.CASCADE, related_name="share_changes", verbose_name=_("person data")
    )
    quantity = models.PositiveIntegerField(_("number of shares"))
    valid_from = models.DateField(_("valid from"))
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("performed by"),
    )
    created_at = models.DateTimeField(_("time"), default=timezone.now, editable=False)

    class Meta:
        default_permissions = ("view",)
        verbose_name = _("share change")
        verbose_name_plural = _("share changes")
        ordering = ("-valid_from", "-pk")
        indexes = [models.Index(fields=["record", "valid_from"], name="community_sharechange_record")]

    def __str__(self):
        return f"{self.quantity} from {self.valid_from}"


class ListPreference(models.Model):
    """A staff user's column widths (and expanded locations) per list; see ``community.preferences``."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    list_key = models.CharField(max_length=40)
    column_widths = models.JSONField(default=dict, blank=True)
    expanded_locations = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        verbose_name = _("list preference")
        verbose_name_plural = _("list preferences")
        constraints = [
            models.UniqueConstraint(fields=["user", "list_key"], name="community_list_preference_unique")
        ]

    def __str__(self):
        return self.list_key
