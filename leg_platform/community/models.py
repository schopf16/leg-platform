"""The domain models of the community (D-077).

Locations, transformer circuits, metering points, LEGs, the assignments of persons and of LEGs to
metering points, the history, the management settings and the staff-only side record of a
person. The rules (status, assignment changes, discount tier, problems) are in the neighbouring
modules, not here; a model only keeps its own data consistent.

Identifier columns that are compared exactly use the binary collation (D-031). A person is an
account of ``accounts.User``; the staff-only data about a person is a side record, never a column
of the guarded account model.
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

# The legal minimum of the production quota (Art. 19e para. 1 StromVV, ``legs/VORGABEN.md``): a
# constant, not a setting.
LEGAL_MINIMUM_QUOTA_PERCENT = 5
DEFAULT_WARNING_THRESHOLD_PERCENT = 10

SUBJECT_MAX_LENGTH = 20
ADDRESS_SIGNATURE_LENGTH = 400
# The longest remark or note (a text column has no limit of its own; the validator and the forms cut it, and
# the PDF prints a remark in full: ``management.printing.pdf.MAX_FACT_CHARS`` is not smaller, D-084).
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


# Transformer circuits ------------------------------------------------------------------------


class TransformerCircuit(TimestampedModel):
    """A circuit of the grid, identified by the number that the grid operator assigns.

    The number is unique after conversion to capital letters and removal of surrounding white
    space; the name is free and not unique (``standorte/VORGABEN.md``, "Nachtrag: Trafokreis")."""

    grid_number = models.CharField(
        _("grid number"),
        max_length=numbers.TRANSFORMER_CIRCUIT_MAX_LENGTH,
        unique=True,
        db_collation=EXACT_COLLATION,
        validators=[numbers.validate_transformer_circuit_number],
    )
    name = models.CharField(_("name"), max_length=120, blank=True)

    class Meta:
        # No rights of the Django permission system: every staff account may use the management area (D-086).
        default_permissions = ()
        verbose_name = _("transformer circuit")
        verbose_name_plural = _("transformer circuits")
        ordering = ("grid_number",)

    def __str__(self):
        return self.grid_number

    def save(self, *args, **kwargs):
        self.grid_number = numbers.normalise_transformer_circuit_number(self.grid_number)
        self.name = addressing.collapse(self.name)
        super().save(*args, **kwargs)


# Addresses -----------------------------------------------------------------------------------


class AddressSuggestionMixin(models.Model):
    """The state of the directory suggestion for the address of an object (``adresskorrektur``).

    ``address_correction_open`` is the stored result: a suggestion exists and it was not dismissed
    for the address as it is now. Lists filter by this column; nobody asks the directory per row.
    The subclass names its address through ``current_address``.
    """

    suggested_street = models.CharField(max_length=255, blank=True, editable=False)
    suggested_house_number = models.CharField(max_length=32, blank=True, editable=False)
    suggested_postal_code = models.CharField(max_length=4, blank=True, editable=False)
    suggested_town = models.CharField(max_length=255, blank=True, editable=False)
    # The address (``addressing.address_signature``) for which "do not accept" was chosen.
    suggestion_dismissed_for = models.CharField(
        max_length=ADDRESS_SIGNATURE_LENGTH, blank=True, editable=False
    )
    address_correction_open = models.BooleanField(default=False, editable=False)

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
        """Store the result of a directory check (``addressing.suggest_correction``) and update
        ``address_correction_open``. Does not save."""
        values = suggestion.as_tuple() if suggestion else ("", "", "", "")
        (self.suggested_street, self.suggested_house_number,
         self.suggested_postal_code, self.suggested_town) = values
        self.address_correction_open = (
            suggestion is not None
            and self.current_address_signature() != self.suggestion_dismissed_for
        )

    def dismiss_suggestion(self):
        """"Do not accept": remember the address; the warning returns only if it changes. Does not save."""
        self.suggestion_dismissed_for = self.current_address_signature()
        self.address_correction_open = False

    def clear_suggestion(self):
        """Forget suggestion and dismissal, for example after the suggestion was accepted. Does not save."""
        self.suggestion_dismissed_for = ""
        self.apply_suggestion(None)


class Location(AddressSuggestionMixin, TimestampedModel):
    """A physical address. It exists independently of persons; a metering point belongs to one
    (``standorte/VORGABEN.md``, section 1). Unique after normalisation of the address."""

    street = models.CharField(_("street"), max_length=150)
    house_number = models.CharField(_("house number"), max_length=30)
    postal_code = models.CharField(_("postal code"), max_length=4, validators=[validate_postal_code])
    town = models.CharField(_("town"), max_length=100)
    # Not part of the address that must be unique and not checked against the directory.
    address_addition = models.CharField(_("address addition"), max_length=150, blank=True)
    # The SHA-256 (hex) of the normalised address with case and accents ignored; maintained by
    # ``save``. It makes the address unique (``addressing.address_digest``).
    # The SHA-256 (hex) of the normalised address with case and accents ignored; maintained by
    # ``save``. It makes the address unique (``addressing.address_digest``).
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
    # Internal; never searched (``LISTEN-STANDARD.md``, section 4).
    remark = models.TextField(_("remark"), blank=True, validators=REMARK_VALIDATORS)
    is_archived = models.BooleanField(_("archived"), default=False)
    # Created by a customer and not yet confirmed by the staff ("neu").
    needs_confirmation = models.BooleanField(_("not yet confirmed"), default=False)

    class Meta:
        default_permissions = ()
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
        # The digest is checked below with a message that names the address, not the column.
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


# Metering points -----------------------------------------------------------------------------


class MeteringPointQuerySet(models.QuerySet):
    def with_status(self, status, today=None):
        return self.filter(status_filter(status, today or timezone.localdate()))


class MeteringPoint(TimestampedModel):
    # Empty until the grid operator has assigned it ("Fehlt sie, ist der Messpunkt angelegt, aber beim Netzbetreiber
    # noch nicht bekannt", personen/detail/VORGABEN.md); several rows may have none.
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
        default_permissions = ()
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


# LEGs ----------------------------------------------------------------------------------------


class Leg(TimestampedModel):
    """A virtual grid association (a local electricity community in the sense of the law).

    The production quota is read from the grid operator's LEG portal on ``quota_checked_on``; the date belongs to
    the value and does not change by itself (``legs/VORGABEN.md``, "Geprüft am")."""

    name = models.CharField(_("name"), max_length=120, unique=True)
    production_quota_percent = models.DecimalField(
        _("production quota (%)"), max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    quota_checked_on = models.DateField(_("checked on"), null=True, blank=True)
    remark = models.TextField(_("remark"), blank=True, validators=REMARK_VALIDATORS)
    is_archived = models.BooleanField(_("archived"), default=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("LEG")
        verbose_name_plural = _("LEGs")
        ordering = ("name",)
        constraints = [
            # A quota needs its date (the date is mandatory when a quota is entered).
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


# Assignments ---------------------------------------------------------------------------------


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
    """A metering point is assigned to a person or a LEG for a period.

    The period includes both days; ``valid_to`` empty means open. The rules for changes (the
    previous assignment ends the day before the new one, deletion only if it never took effect)
    are in ``leg_platform.community.assignments``. The two ``previous_*`` columns remember what a
    change of the previous assignment was, so that deleting the new, not yet effective assignment
    can undo it."""

    valid_from = models.DateField(_("valid from"))
    valid_to = models.DateField(_("valid to"), null=True, blank=True)
    previous_assignment = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False
    )
    previous_valid_to = models.DateField(null=True, blank=True, editable=False)

    objects = AssignmentQuerySet.as_manager()

    class Meta:
        abstract = True
        # No permissions of its own (D-077, D-086).
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
        MeteringPoint, on_delete=models.PROTECT, related_name="person_assignments"
    )
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="metering_point_assignments"
    )

    class Meta(AssignmentBase.Meta):
        verbose_name = _("person assignment")
        verbose_name_plural = _("person assignments")
        ordering = ("valid_from",)


class LegAssignment(AssignmentBase):
    metering_point = models.ForeignKey(
        MeteringPoint, on_delete=models.PROTECT, related_name="leg_assignments"
    )
    leg = models.ForeignKey(Leg, on_delete=models.PROTECT, related_name="assignments")

    class Meta(AssignmentBase.Meta):
        verbose_name = _("LEG assignment")
        verbose_name_plural = _("LEG assignments")
        ordering = ("valid_from",)


# History -------------------------------------------------------------------------------------


class HistoryEntry(models.Model):
    """One event in the history of a location, metering point, LEG, person or the settings.

    Append only: an entry is never changed (``save`` refuses an existing row). ``subject_type`` and
    ``subject_id`` name the object without a foreign key, so that an entry outlives it. The actor
    is the account that did it (empty if the account is gone); ``detail`` holds small facts such as
    the old and the new value, never a name or an address of a customer. See
    ``leg_platform.community.history``."""

    subject_type = models.CharField(max_length=SUBJECT_MAX_LENGTH)
    subject_id = models.PositiveBigIntegerField()
    event = models.CharField(_("event"), max_length=40)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(_("time"), default=timezone.now, editable=False)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        default_permissions = ()
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


# Settings, persons, preferences ----------------------------------------------------------------


class ManagementSettings(models.Model):
    """The settings of the management area. The table holds exactly one row (id 1).

    The country and the identifier of the metering points, the warning threshold of the
    production quota and the sender address of the invoices (``einstellungen/VORGABEN.md``). The
    values that belong to the instance (identifier, sender address) are empty until the staff
    enters them; nothing is hard-coded. Read through ``ManagementSettings.load()``."""

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
    # The sender address for which "do not accept" was chosen (``addressing.address_signature``).
    sender_suggestion_dismissed_for = models.CharField(
        max_length=ADDRESS_SIGNATURE_LENGTH, blank=True, editable=False
    )
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        # The table has the one row. No Django rights: every staff account uses the management area (D-086).
        default_permissions = ()
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
    """The grid operator of this instance (D-086): the company whose portal the LEG is registered in and whose
    name the texts of the management area use ("bei der %(operator)s angemeldet").

    The table holds exactly one row (id 1), the current operator of the instance; a system administrator
    maintains it in the Django administration. Nothing is hard-coded: the name is empty until it is entered, and
    the texts then say "grid operator" (``leg_platform.community.grid_operator``). Meant to become a catalogue of
    operators later (the owner's plan); only the name exists now. Read through ``GridOperator.load()``."""

    NAME_MAX_LENGTH = 120

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(_("name"), max_length=NAME_MAX_LENGTH, blank=True)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        # Maintained in the Django administration by system administrators only (``community.admin``).
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
    """What the staff keep about a person (an account) besides the account itself.

    The account is the customer's own data and not editable by the staff; this side record holds
    the staff-only note, the optional customer number at the grid operator, the IBAN (encrypted) and the paper
    invoice wish (the staff may change them for support, D-087; the customer maintains them later) and the state of the address
    suggestion; the shares of the cooperative are the entries of ``ShareChange``. Created when needed."""

    person = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="person_record"
    )
    staff_note = models.TextField(_("note"), blank=True, validators=REMARK_VALIDATORS)
    operator_customer_number = models.CharField(_("customer number at the grid operator"), max_length=30, blank=True)
    # The IBAN is stored encrypted (D-087, ``community.iban``); the attribute ``iban`` reads and writes it as
    # text. Nothing queries it: it is no search field and has no lookup.
    iban_encrypted = models.TextField(_("IBAN"), blank=True, editable=False)
    paper_invoice = models.BooleanField(_("paper invoice"), default=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("person")
        verbose_name_plural = _("persons")

    def __str__(self):
        return f"{self.person}"

    @property
    def iban(self):
        """The IBAN in its normal form, or "". Raises ``DecryptionError`` if no key of the instance decrypts it
        (``iban.readable`` gives "" and logs the account number for display)."""
        return iban_module.decrypt_iban(self.iban_encrypted)

    @iban.setter
    def iban(self, value):
        self.iban_encrypted = iban_module.encrypt_iban(value)

    def current_address(self):
        person = self.person
        return (
            person.street, person.house_number,
            str(person.swiss_postal_code or ""), person.town,
        )


class ShareChange(models.Model):
    """One entry in the history of the cooperative shares of a person (D-086): the number of shares that holds
    from a day on. There is no period: the number in force on a day is the one of the latest entry whose
    ``valid_from`` is not after that day (``leg_platform.community.persons.current_shares``). The services only add entries (a
    correction is a new entry); the actor and the time of the entry are kept."""

    record = models.ForeignKey(PersonRecord, on_delete=models.CASCADE, related_name="share_changes")
    quantity = models.PositiveIntegerField(_("number of shares"))
    valid_from = models.DateField(_("valid from"))
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(_("time"), default=timezone.now, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("share change")
        verbose_name_plural = _("share changes")
        ordering = ("-valid_from", "-pk")
        indexes = [models.Index(fields=["record", "valid_from"], name="community_sharechange_record")]

    def __str__(self):
        return f"{self.quantity} from {self.valid_from}"


class ListPreference(models.Model):
    """What a staff user chose for a list: the widths of the columns and, for the list of
    locations, which ones are expanded (``LISTEN-STANDARD.md``, section 1). One row per user and
    list; written through ``leg_platform.community.preferences``."""

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
