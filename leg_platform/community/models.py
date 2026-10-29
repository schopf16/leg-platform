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
# Defaults of the deadlines (days; owner examples, KONZEPT 5.2, 5.8, 5.9; the staff changes them).
DEFAULT_REMINDER_AFTER_DAYS = 2
DEFAULT_ROLLBACK_AFTER_DAYS = 14
DEFAULT_CONTRACT_DEADLINE_DAYS = 14
DEFAULT_OPERATOR_CONFIRMATION_DAYS = 30
DEFAULT_IBAN_MISSING_REMINDER_DAYS = 7

SUBJECT_MAX_LENGTH = 20
ADDRESS_SIGNATURE_LENGTH = 400
# The PDF prints a remark in full: ``management.printing.pdf.MAX_FACT_CHARS`` must not be smaller (D-084).
REMARK_MAX_LENGTH = 5000
REMARK_VALIDATORS = [MaxLengthValidator(REMARK_MAX_LENGTH)]
# Names the customer gives to a building and to a metering point; free text, never searched (D-103).
BUILDING_NAME_MAX_LENGTH = 80
BUILDING_DESCRIPTION_MAX_LENGTH = 200
METERING_POINT_NAME_MAX_LENGTH = 80
# The reason of a return for correction and the note of a rejection (free text of the staff, D-103).
RETURN_REASON_MAX_LENGTH = 500
REJECTION_NOTE_MAX_LENGTH = 300
# The federal building identifier (EGID) has at most nine digits (Federal Register of Buildings and Dwellings).
EGID_MAX = 999_999_999


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
    # The federal building identifier: the same EGID is the same building, never a second location (D-103). A building
    # is shared by all customers of it; each customer's name and description of it are ``CustomerBuilding``.
    # Empty for buildings the staff entered without one; the unique index allows several empty values.
    egid = models.PositiveIntegerField(
        _("EGID"), null=True, blank=True, unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(EGID_MAX)],
    )
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
    # Chosen by the customer (D-103); free text, never searched, never in the history.
    name = models.CharField(_("name"), max_length=METERING_POINT_NAME_MAX_LENGTH, blank=True)
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
    # Reminders and deadlines of the applications, whole days from 1, no upper limit of our own (D-103;
    # ``community.deadline_settings`` changes them with history).
    reminder_after_days = models.PositiveIntegerField(
        _("reminder after (days)"), default=DEFAULT_REMINDER_AFTER_DAYS, validators=[MinValueValidator(1)]
    )
    rollback_after_days = models.PositiveIntegerField(
        _("rollback after the reminder (days)"), default=DEFAULT_ROLLBACK_AFTER_DAYS,
        validators=[MinValueValidator(1)],
    )
    contract_deadline_days = models.PositiveIntegerField(
        _("contract deadline (days)"), default=DEFAULT_CONTRACT_DEADLINE_DAYS, validators=[MinValueValidator(1)]
    )
    operator_confirmation_days = models.PositiveIntegerField(
        _("confirmation by the grid operator (days)"), default=DEFAULT_OPERATOR_CONFIRMATION_DAYS,
        validators=[MinValueValidator(1)],
    )
    iban_missing_reminder_days = models.PositiveIntegerField(
        _("reminder: bank connection missing (days)"), default=DEFAULT_IBAN_MISSING_REMINDER_DAYS,
        validators=[MinValueValidator(1)],
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


# ---- Applications, contract and deregistration (D-103) ----


class Application(TimestampedModel):
    """A customer's application ("Anmeldung") to take part with buildings and metering points (D-103).

    The buildings and metering points of an application are **draft rows** (``ApplicationBuilding``,
    ``ApplicationMeteringPoint``), not rows of ``Location`` and ``MeteringPoint``: a draft is invisible in the
    management lists, its numbers block nobody, and a rollback deletes only draft rows. The real rows are created by
    ``community.application_staff.complete``. Rules: ``community.applications`` (the customer's side),
    ``community.application_staff`` (the staff's side), ``community.application_deadlines`` (the scheduler)."""

    class State(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PAUSED = "paused", _("Paused")
        SUBMITTED = "submitted", _("Submitted")
        IN_REVIEW = "in_review", _("In review")
        # Static labels: a model label never reads the database (system checks, migrate). The operator's name is
        # filled in at display time by ``community.labels.application_state_label``.
        OPERATOR_STARTED = "operator_started", _("Registration at the grid operator started")
        OPERATOR_CONFIRMED = "operator_confirmed", _("Confirmed by the grid operator")
        COMPLETED = "completed", _("Completed")

    class StartChoice(models.TextChoices):
        # Nothing is chosen at first: the start is never prefilled (README 2a).
        ASAP = "asap", _("As soon as possible")
        DATE = "date", _("Desired date")

    # The customer edits and the idle deadlines run.
    DRAFT_STATES = (State.DRAFT, State.PAUSED)
    # Submitted and not yet at the grid operator: the customer may still withdraw.
    WITHDRAWABLE_STATES = (State.SUBMITTED, State.IN_REVIEW)
    # Not completed: the application still occupies buildings and numbers.
    OPEN_STATES = (
        State.DRAFT, State.PAUSED, State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED,
    )
    # Submitted, i.e. visible to the staff and holding its numbers and buildings against other customers.
    IN_FLIGHT_STATES = (State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED)
    # The staff may correct the address and the metering points (D-109): while the LEG works on the application. Not a
    # draft (the customer edits it), not completed (the rows are real then; the pages of the locations and metering
    # points change them).
    CORRECTABLE_STATES = IN_FLIGHT_STATES

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="applications")
    state = models.CharField(max_length=20, choices=State, default=State.DRAFT)
    # The step of the wizard the customer is on (server side, D-103); a key of ``applications.STEPS``.
    wizard_step = models.CharField(max_length=20, default="building", editable=False)
    start_choice = models.CharField(max_length=4, choices=StartChoice, blank=True)
    desired_start_date = models.DateField(null=True, blank=True)
    # Whether a bank connection was already stored when the application was started ("already on file").
    bank_known_at_start = models.BooleanField(default=False, editable=False)
    # The idle clock of a draft: every change of the customer sets it and ends a pause.
    last_activity_at = models.DateTimeField(default=timezone.now)
    reminded_at = models.DateTimeField(null=True, blank=True)
    # Since when everything but the contract is complete (the contract deadline runs from then).
    contract_pending_since = models.DateTimeField(null=True, blank=True)
    # The reminder "bank connection missing" is sent once per period of missing.
    iban_reminded_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    # The reason the staff gave when the application was returned for correction (free text, never in the history);
    # shown to the customer on the draft and cleared when the application is submitted again.
    returned_reason = models.CharField(max_length=RETURN_REASON_MAX_LENGTH, blank=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    operator_started_at = models.DateTimeField(null=True, blank=True)
    operator_started_on = models.DateField(null=True, blank=True)
    operator_confirmed_on = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ("pk",)
        indexes = [models.Index(fields=["state", "last_activity_at"], name="community_application_idle")]

    def __str__(self):
        return f"application {self.pk}"


class CustomerBuilding(TimestampedModel):
    """One customer's own name and description of a building (D-103). A ``Location`` is shared by every customer of the
    building, so these words are per account: the row is read and written only through the account, and nobody sees
    another account's row. Free text, never searched, never in the history."""

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="building_labels")
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="customer_labels")
    name = models.CharField(max_length=BUILDING_NAME_MAX_LENGTH, blank=True)
    description = models.CharField(max_length=BUILDING_DESCRIPTION_MAX_LENGTH, blank=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["person", "location"], name="community_customerbuilding_unique"),
        ]

    def __str__(self):
        return f"building label of account {self.person_id}"


class ApplicationBuilding(TimestampedModel):
    """A building of an application: an existing ``Location`` (``location`` set, "add a metering point") or the
    address of a new one, which becomes a ``Location`` only at completion."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="buildings")
    # Protected: a location that a draft refers to is not deleted under it.
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, null=True, blank=True, related_name="application_buildings"
    )
    egid = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(EGID_MAX)])
    street = models.CharField(max_length=150, blank=True)
    house_number = models.CharField(max_length=30, blank=True)
    postal_code = models.CharField(max_length=4, blank=True)
    town = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=BUILDING_NAME_MAX_LENGTH, blank=True)
    description = models.CharField(max_length=BUILDING_DESCRIPTION_MAX_LENGTH, blank=True)
    # Chosen by "add a metering point" on the building page: the building is fixed.
    preselected = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(fields=["application", "egid"], name="community_appbuilding_egid_unique"),
            models.UniqueConstraint(fields=["application", "location"], name="community_appbuilding_location_unique"),
        ]

    def __str__(self):
        return f"building of application {self.application_id}"

    def current_address(self):
        if self.location_id is not None:
            return self.location.current_address()
        return (self.street, self.house_number, self.postal_code, self.town)


class ApplicationMeteringPoint(TimestampedModel):
    """A metering point of an application (draw or feed-in); a real ``MeteringPoint`` only after completion."""

    building = models.ForeignKey(ApplicationBuilding, on_delete=models.CASCADE, related_name="metering_points")
    direction = models.CharField(max_length=11, choices=Direction)
    number = models.CharField(
        max_length=numbers.METERING_POINT_LENGTH, db_index=True, db_collation=EXACT_COLLATION,
        validators=[numbers.validate_metering_point_number],
    )
    name = models.CharField(max_length=METERING_POINT_NAME_MAX_LENGTH, blank=True)
    pv_power_kwp = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                       validators=[MinValueValidator(0)])
    battery_kwh = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                      validators=[MinValueValidator(0)])
    wallbox_kw = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                     validators=[MinValueValidator(0)])
    # Set at completion.
    metering_point = models.ForeignKey(
        MeteringPoint, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False
    )

    class Meta:
        default_permissions = ()
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(fields=["building", "direction"], name="community_appmeteringpoint_direction"),
        ]

    def __str__(self):
        return f"{self.direction} of application building {self.building_id}"


class ApplicationNotice(models.Model):
    """A notice for the staff that the customer was refused something without being told why (D-103): the building
    or number belongs to someone else. Rows hold what the staff needs to act; ``numbers`` is emptied when resolved."""

    class Kind(models.TextChoices):
        BUILDING_UNAVAILABLE = "building_unavailable", _("Building not available to the customer")
        NUMBER_UNAVAILABLE = "number_unavailable", _("Metering point number not available to the customer")
        # Static (see ``Application.State``); ``community.labels.notice_kind_label`` names the operator at display time.
        OPERATOR_CONFIRMATION_LAPSED = "operator_lapsed", _(
            "Confirmation by the grid operator did not arrive: withdraw the registration there"
        )
        # A correction after the registration at the grid operator started (D-109): the data now differs from it.
        OPERATOR_DATA_CHANGED = "operator_changed", _(
            "Data changed after the registration at the grid operator: check the registration again"
        )
        # The customer deleted the account while the registration was at the grid operator: withdraw it there, as if the
        # customer had deregistered the buildings and points himself (owner decision 2026-10-24).
        ACCOUNT_DELETED = "account_deleted", _(
            "Account deleted: withdraw the registration at the grid operator"
        )

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind)
    egid = models.PositiveIntegerField(null=True, blank=True)
    # The metering point numbers concerned; staff data, deleted when the notice is resolved.
    numbers = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        default_permissions = ()
        ordering = ("created_at", "pk")
        indexes = [models.Index(fields=["resolved_at", "kind"], name="community_appnotice_open")]

    def __str__(self):
        return f"{self.kind} of account {self.person_id}"


class CustomerMessage(models.Model):
    """A one-time message for a customer after an event that removed or changed an application (D-103): shown once on
    the next visit (``community.customer_messages``). Holds the kind key, the optional short note the staff wrote and
    the times; no other data. Seen messages are deleted after 30 days, unseen ones after 180."""

    class Kind(models.TextChoices):
        # Static labels (see ``Application.State``); the pages choose their own words per kind.
        APPLICATION_REJECTED = "application_rejected", _("Application not accepted by the LEG")
        APPLICATION_REJECTED_OPERATOR = "application_rejected_operator", _("Application not accepted by the grid operator")
        APPLICATION_RETURNED = "application_returned", _("Application returned for correction")
        OPERATOR_NOT_CONFIRMED = "operator_not_confirmed", _("Confirmation by the grid operator did not arrive")

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="customer_messages")
    kind = models.CharField(max_length=30, choices=Kind)
    text = models.CharField(max_length=REJECTION_NOTE_MAX_LENGTH, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ("created_at", "pk")
        indexes = [models.Index(fields=["person", "seen_at"], name="community_custmsg_person_seen")]

    def __str__(self):
        return f"{self.kind} for account {self.person_id}"


class ContractVersionManager(models.Manager):
    def current(self):
        """The current version, or None while none is chosen."""
        return self.filter(current_marker=True).first()


class ContractVersion(models.Model):
    """One uploaded contract PDF (D-103), kept in the database like the logos (``community.contracts``).

    Exactly one version is current: ``current_marker`` is True for it and NULL for every other, and the unique index
    allows one True. A version that was never current and has no acceptance may be deleted; every other stays as
    evidence."""

    class State(models.TextChoices):
        UPLOADED = "uploaded", _("Uploaded")
        CURRENT = "current", _("Current")
        REPLACED = "replaced", _("Replaced")

    version_date = models.DateField()
    pdf = models.BinaryField(editable=False)
    sha256 = models.CharField(max_length=64, unique=True, editable=False)
    size = models.PositiveIntegerField(editable=False)
    current_marker = models.BooleanField(null=True, unique=True, default=None, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False
    )
    uploaded_at = models.DateTimeField(default=timezone.now, editable=False)
    made_current_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = ContractVersionManager()

    class Meta:
        default_permissions = ()
        ordering = ("-version_date", "-pk")

    def __str__(self):
        return f"contract {self.version_date}"

    @property
    def is_current(self):
        return self.current_marker is True

    @property
    def state(self):
        if self.is_current:
            return self.State.CURRENT
        return self.State.UPLOADED if self.made_current_at is None else self.State.REPLACED


class AppendOnlyQuerySet(models.QuerySet):
    """No update and no delete through the application (a cascade of an account deletion does not use these)."""

    def update(self, **kwargs):
        raise ValueError("These rows are never changed.")

    def delete(self):
        raise ValueError("These rows are never deleted by the application.")

    def bulk_update(self, *args, **kwargs):
        raise ValueError("These rows are never changed.")


class ContractAcceptance(models.Model):
    """The consent of a person to one contract version (D-103): append-only, once per person and version.

    Stores the time, the version, the SHA-256 of the accepted file and the second tick (data exchange with the grid
    operator) as its own value. No IP address, no device data, no password."""

    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contract_acceptances"
    )
    version = models.ForeignKey(ContractVersion, on_delete=models.PROTECT, related_name="acceptances")
    accepted_at = models.DateTimeField(default=timezone.now, editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    data_exchange_accepted = models.BooleanField()

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        ordering = ("accepted_at", "pk")
        constraints = [
            models.UniqueConstraint(fields=["person", "version"], name="community_contractacceptance_once"),
        ]

    def __str__(self):
        return f"acceptance of account {self.person_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("An acceptance is never changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("An acceptance is never deleted by the application.")


class Deregistration(TimestampedModel):
    """A customer's request to end whole buildings or single metering points (D-103; Gesellschaftsvertrag item 4)."""

    class State(models.TextChoices):
        SUBMITTED = "submitted", _("In deregistration")
        CONFIRMED = "confirmed", _("Date confirmed")
        EFFECTIVE = "effective", _("Deregistered")

    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deregistrations"
    )
    state = models.CharField(max_length=10, choices=State, default=State.SUBMITTED)
    # The earliest possible day on the day of the request, and the day the customer asked for (never before it).
    earliest_date = models.DateField()
    desired_date = models.DateField()
    wants_earliest = models.BooleanField(default=True)
    confirmed_date = models.DateField(null=True, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    effective_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ()
        ordering = ("pk",)

    def __str__(self):
        return f"deregistration {self.pk}"


class DeregistrationItem(models.Model):
    deregistration = models.ForeignKey(Deregistration, on_delete=models.CASCADE, related_name="items")
    metering_point = models.ForeignKey(
        MeteringPoint, on_delete=models.PROTECT, related_name="deregistration_items"
    )

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["deregistration", "metering_point"], name="community_deregistrationitem_unique"
            ),
        ]

    def __str__(self):
        return f"metering point {self.metering_point_id} in deregistration {self.deregistration_id}"
