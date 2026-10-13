"""Custom user model and customer numbers (docs/DECISIONS.md, D-016, D-021, D-031, D-032, D-034, D-036).

Invariants enforced here, independent of any view, form or admin:

* The normalised email column (the login identifier, ``USERNAME_FIELD``) is derived from the
  email address in one place and is NULL while the account is deactivated, so that a normal
  unique index enforces uniqueness among accounts that are not deactivated.
* ``is_active`` is derived from the status: only active accounts are active.
* Status, email address, ``is_staff``, ``is_superuser``, customer number and retention flag
  change only through the methods of the central account logic below; every save compares
  them with the row locked with ``SELECT ... FOR UPDATE``, which also rejects stale instances.
* Unverified accounts have neither a customer number nor a usable password; superusers are
  staff users (system administrators, D-012).
* A customer number is issued once from the table of all issued numbers, never changes and is
  never reused; the database only accepts issued numbers (foreign key).
* The retention obligation flag can only change from unset to set; accounts with it are not
  deleted.
* At least one active system administrator remains (D-016).
* Bulk changes of protected fields, bulk deletion and bulk creation are rejected.

The methods enforce the data rules only. Who may perform a change (e.g. only system
administrators, never on their own account) is checked by the views and the admin.
"""

import ipaddress
import logging
import re
import secrets
import time
import unicodedata
from base64 import b32encode
from contextlib import contextmanager
from urllib.parse import quote, urlencode

import idna
from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, MaxValueValidator, MinValueValidator
from django.db import IntegrityError, connections, models, router, transaction
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables
from django_countries.fields import CountryField
from django_otp.models import Device, DeviceManager, TimestampMixin
from django_otp.oath import TOTP
from phonenumber_field.modelfields import PhoneNumberField

from leg_platform.accounts.digests import keyed_digest, keyed_digests_with_fallbacks
from leg_platform.accounts.encryption import (
    DecryptionError,
    decrypt,
    encrypt,
    is_current,
    reencrypt,
)
from leg_platform.accounts.normalization import normalize_email_address

logger = logging.getLogger(__name__)

# Exact comparison for identifier columns (D-031).
EXACT_COLLATION = "utf8mb4_bin"

EMAIL_MAX_LENGTH = 254

CUSTOMER_NUMBER_DIGITS = 6
CUSTOMER_NUMBER_SPACE = 10**CUSTOMER_NUMBER_DIGITS
# With half of all numbers issued, 100 collisions in a row have a probability of 2**-100.
CUSTOMER_NUMBER_ATTEMPTS = 100
# From this share of issued numbers on, issuing logs a warning and the administration shows a
# notice, so that the operator can act before the numbers run out (D-021, D-038).
CUSTOMER_NUMBER_WARNING_PERCENT = 90

# Seconds create_superuser() waits for a concurrent call (MariaDB GET_LOCK).
SUPERUSER_LOCK_TIMEOUT = 10

SWITZERLAND = "CH"

# Swiss postal codes: four ASCII digits from 1000 to 9999 (D-032, D-036).
SWISS_POSTAL_CODE = re.compile(r"\A[1-9][0-9]{3}\Z")
SWISS_POSTAL_CODE_MESSAGE = _("Enter a postal code with 4 digits from 1000 to 9999.")


def parse_swiss_postal_code(value):
    """Return the Swiss postal code as an integer, or None unless it is four digits 1000-9999."""
    value = (value or "").strip()
    return int(value) if SWISS_POSTAL_CODE.match(value) else None


class AccountIntegrityError(Exception):
    """A change would violate an invariant of the account model."""


class CustomerNumberUnavailableError(AccountIntegrityError):
    """No unused customer number could be issued."""


def validate_account_email(email):
    """Raise ValueError unless the address is valid and fits the columns after normalisation."""
    if email is not None and len(str(email).strip()) > EMAIL_MAX_LENGTH:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")
    value = unicodedata.normalize("NFKC", str(email)).strip().lower() if email is not None else ""
    if not value:
        raise ValueError("An email address is required.")
    if value.count("@") != 1:
        raise ValueError("The email address is not valid.")
    local_part, domain = value.rsplit("@", 1)
    if not local_part or not domain or not local_part.isascii():
        raise ValueError("The email address is not valid.")
    if '"' in local_part or "\\" in local_part:
        raise ValueError("The email address is not valid.")
    if len(value) > EMAIL_MAX_LENGTH or len(domain) > 253:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")
    # Category C, Z and M characters are rejected before the domain is mapped with IDNA (UTS #46).
    if any(unicodedata.category(character)[0] in {"C", "Z", "M"} for character in value):
        raise ValueError("The email address is not valid.")
    if domain == "localhost" or domain.startswith("[") or domain.endswith("]"):
        raise ValueError("The email address is not valid.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("The email address is not valid.")
    try:
        ascii_domain = idna.encode(
            domain, uts46=True, std3_rules=True, transitional=False
        ).decode("ascii").lower()
    except idna.IDNAError:
        raise ValueError("The email address is not valid.") from None
    normalized = f"{local_part}@{ascii_domain}"
    try:
        EmailValidator(allowlist=[])(normalized)
    except ValidationError:
        raise ValueError("The email address is not valid.") from None
    # NFKC and IDNA can lengthen an address, so the stored key is checked too.
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise ValueError(f"The email address is longer than {EMAIL_MAX_LENGTH} characters.")


def format_customer_number(number):
    """Return the display format of a customer number in two blocks of three digits."""
    if not number:
        return ""
    if len(number) != CUSTOMER_NUMBER_DIGITS or not number.isascii() or not number.isdigit():
        raise ValueError("A customer number consists of six digits.")
    return f"{number[:3]} {number[3:]}"


def customer_numbers_nearly_exhausted(issued):
    """Return whether the share of issued customer numbers reaches the warning level."""
    return issued * 100 >= CUSTOMER_NUMBER_SPACE * CUSTOMER_NUMBER_WARNING_PERCENT


class IssuedCustomerNumberQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AccountIntegrityError("Issued customer numbers cannot be changed.")

    def delete(self):
        raise AccountIntegrityError("Issued customer numbers are never deleted.")

    def bulk_create(self, objs, *args, **kwargs):
        raise AccountIntegrityError("Customer numbers are issued only with issue().")


class IssuedCustomerNumberManager(models.Manager.from_queryset(IssuedCustomerNumberQuerySet)):
    def issue(self):
        """Record and return a new random customer number that was never issued before.

        Must run inside a transaction together with the assignment to an account.
        """
        issued = self.count()
        if issued >= CUSTOMER_NUMBER_SPACE:
            raise CustomerNumberUnavailableError("All customer numbers have been issued.")
        if customer_numbers_nearly_exhausted(issued):
            logger.warning(
                "%d of %d customer numbers have been issued (%d%%).",
                issued,
                CUSTOMER_NUMBER_SPACE,
                issued * 100 // CUSTOMER_NUMBER_SPACE,
            )
        for _attempt in range(CUSTOMER_NUMBER_ATTEMPTS):
            candidate = f"{secrets.randbelow(CUSTOMER_NUMBER_SPACE):0{CUSTOMER_NUMBER_DIGITS}d}"
            try:
                with transaction.atomic(using=self.db):
                    self.create(number=candidate)
            except IntegrityError:
                continue
            return candidate
        raise CustomerNumberUnavailableError(
            f"No unused customer number found in {CUSTOMER_NUMBER_ATTEMPTS} attempts."
        )


class IssuedCustomerNumber(models.Model):
    """Every customer number ever issued, without any reference to a person (D-021).

    The unique key of this table guarantees that a number is never issued twice, also after
    the account that held it has been deleted.
    """

    number = models.CharField(
        _("customer number"),
        max_length=CUSTOMER_NUMBER_DIGITS,
        primary_key=True,
        db_collation=EXACT_COLLATION,
        editable=False,
    )

    objects = IssuedCustomerNumberManager()

    class Meta:
        # Related-object access and _base_manager use the protecting queryset as well; it
        # filters nothing, as Django requires for base managers.
        base_manager_name = "objects"
        verbose_name = _("issued customer number")
        verbose_name_plural = _("issued customer numbers")

    def __str__(self):
        # Never the number itself: this table has no reference to a person (D-021), and its
        # text could reach log entries or error output. Accounts show their customer number
        # for display only (User.__str__); admin log entries use the internal number (D-036).
        return str(self._meta.verbose_name)

    def save(self, **kwargs):
        if (
            not self._state.adding
            or kwargs.get("force_update")
            or kwargs.get("update_fields") is not None
        ):
            raise AccountIntegrityError("Issued customer numbers cannot be changed.")
        # Without force_insert, Django would accept an existing number as an update.
        kwargs["force_insert"] = True
        super().save(**kwargs)

    def delete(self, *args, **kwargs):
        raise AccountIntegrityError("Issued customer numbers are never deleted.")


class UserQuerySet(models.QuerySet):
    # Changes to these fields must go through the central account logic (D-016,
    # docs/SECURITY.md, section 4). is_active is derived from the status. Attribute names are
    # listed as well, because bulk_update() passes them to update().
    PROTECTED_FIELDS = frozenset(
        {
            "status",
            "is_active",
            "email",
            "normalized_email",
            "retention_obligation",
            "is_staff",
            "is_superuser",
            "customer_number",
            "customer_number_id",
            "password",
        }
    )

    def update(self, **kwargs):
        protected = self.PROTECTED_FIELDS & kwargs.keys()
        if protected:
            raise AccountIntegrityError(
                f"Bulk updates of {', '.join(sorted(protected))} are not allowed; "
                "change each account through the account logic."
            )
        return super().update(**kwargs)

    def delete(self):
        raise AccountIntegrityError(
            "Bulk deletion of accounts is not allowed; delete each account through the "
            "account logic."
        )

    def bulk_create(self, objs, *args, **kwargs):
        raise AccountIntegrityError(
            "Bulk creation of accounts is not allowed; it bypasses the account logic."
        )


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    # Fields that only the account logic sets; never accepted when an account is created.
    _CONTROLLED_FIELDS = frozenset(
        {
            "id",
            "pk",
            "status",
            "is_active",
            "is_staff",
            "is_superuser",
            "customer_number",
            "customer_number_id",
            "retention_obligation",
            "groups",
            "user_permissions",
            "password",
            "last_login",
        }
    )

    def _prepare(self, email, extra_fields):
        email = str(email).strip() if email is not None else email
        validate_account_email(email)
        normalized = normalize_email_address(email)
        given = extra_fields.pop(self.model.USERNAME_FIELD, None)
        if given is not None and normalize_email_address(given) != normalized:
            raise ValueError("The normalised email address does not match the email address.")
        controlled = self._CONTROLLED_FIELDS & extra_fields.keys()
        if controlled:
            raise ValueError(f"These fields cannot be set here: {', '.join(sorted(controlled))}.")
        return normalized

    def create_user(self, email, password=None, **extra_fields):
        """Create an unverified account without a usable password (D-016).

        The password is set only after the email address has been verified, so a password
        given here is rejected. New accounts have no privileges, groups or permissions.
        """
        email = self._prepare(email, extra_fields)
        if password is not None:
            raise ValueError("Unverified accounts receive their password after verification.")
        user = self.model(email=email, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create the first system administrator (docs/PROJECT.md, "First System Administrator").

        Refused while any account with is_superuser exists, including blocked and deactivated
        ones. The email address counts as verified; the account is active and receives its
        customer number. Must run outside a transaction: a MariaDB named lock (GET_LOCK)
        serialises concurrent calls until the account is committed, because row locks cannot
        protect the check while no system administrator row exists.
        """
        email = self._prepare(email, extra_fields)
        if not password:
            raise ValueError("A password is required.")
        using = self._db or router.db_for_write(self.model)
        if not transaction.get_autocommit(using=using):
            raise AccountIntegrityError("create_superuser() must not run inside a transaction.")
        user = self.model(
            email=email,
            status=self.model.Status.ACTIVE,
            is_staff=True,
            is_superuser=True,
            **extra_fields,
        )
        password_validation.validate_password(password, user)
        user.set_password(password)
        connection = connections[using]
        lock_name = f"{connection.settings_dict['NAME']}.create_superuser"
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s)", [lock_name, SUPERUSER_LOCK_TIMEOUT])
            (acquired,) = cursor.fetchone()
        if acquired != 1:
            raise AccountIntegrityError("Another system administrator is being created.")
        try:
            with transaction.atomic(using=using):
                if self.filter(is_superuser=True).exists():
                    raise AccountIntegrityError("A system administrator already exists.")
                with user._account_change("status", "is_staff", "is_superuser"):
                    user.save(using=using)
                user.assign_customer_number()
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
        return user

    def get_by_natural_key(self, username):
        # The raw length is checked before normalisation, as in validate_account_email().
        if username is not None and len(str(username).strip()) > EMAIL_MAX_LENGTH:
            raise self.model.DoesNotExist
        normalized = normalize_email_address(username)
        if normalized is None or len(normalized) > EMAIL_MAX_LENGTH:
            # A lookup with None would match deactivated accounts (IS NULL).
            raise self.model.DoesNotExist
        return self.get(**{self.model.USERNAME_FIELD: normalized})

    async def aget_by_natural_key(self, username):
        # The raw length is checked before normalisation, as in get_by_natural_key().
        if username is not None and len(str(username).strip()) > EMAIL_MAX_LENGTH:
            raise self.model.DoesNotExist
        normalized = normalize_email_address(username)
        if normalized is None or len(normalized) > EMAIL_MAX_LENGTH:
            raise self.model.DoesNotExist
        return await self.aget(**{self.model.USERNAME_FIELD: normalized})


class User(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        UNVERIFIED = "unverified", _("Unverified")
        ACTIVE = "active", _("Active")
        BLOCKED = "blocked", _("Blocked")
        DEACTIVATED = "deactivated", _("Deactivated")

    class Salutation(models.TextChoices):
        MRS = "mrs", _("Mrs")
        MR = "mr", _("Mr")
        FAMILY = "family", _("Family")

    # Account

    email = models.EmailField(_("email address"), max_length=EMAIL_MAX_LENGTH)
    # The unique message is intended for the later email change in the account's own profile
    # (the admin cannot change email addresses). Public registration must NOT show it: it must
    # answer duplicates generically, or it would reveal which addresses have an account
    # (account enumeration; security review finding L-9, docs/SECURITY.md, section 3).
    normalized_email = models.EmailField(
        _("email address"),
        max_length=EMAIL_MAX_LENGTH,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        db_collation=EXACT_COLLATION,
        error_messages={"unique": _("This email address is already in use.")},
    )
    # A foreign key, so that the database accepts only issued numbers; the column keeps the
    # number itself and the collation of the referenced key (utf8mb4_bin).
    customer_number = models.OneToOneField(
        IssuedCustomerNumber,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
        db_column="customer_number",
        verbose_name=_("customer number"),
    )
    status = models.CharField(
        _("status"), max_length=11, choices=Status, default=Status.UNVERIFIED
    )
    is_active = models.BooleanField(_("active"), default=False, editable=False)
    is_staff = models.BooleanField(_("access to administration"), default=False)
    retention_obligation = models.BooleanField(
        _("retention obligation"), default=False, editable=False
    )

    # Name and postal address: the structure and lengths of eCH-0010 (D-032). Fields that are
    # not used are kept but not shown (title, organisation name add-ons, address line 2,
    # dwelling number, post office box text, locality; D-036).

    salutation = models.CharField(
        _("salutation"), max_length=6, choices=Salutation, blank=True
    )
    title = models.CharField(_("title"), max_length=50, blank=True)
    first_name = models.CharField(_("first name"), max_length=30, blank=True)
    last_name = models.CharField(_("last name"), max_length=30, blank=True)
    organisation_name = models.CharField(_("company name"), max_length=60, blank=True)
    organisation_name_add_on_1 = models.CharField(
        _("organisation name add-on 1"), max_length=60, blank=True
    )
    organisation_name_add_on_2 = models.CharField(
        _("organisation name add-on 2"), max_length=60, blank=True
    )

    # addressLine1: e.g. a second person on the invoice or "c/o"; one account still belongs
    # to exactly one person (D-032).
    address_line_1 = models.CharField(
        _("address addition"),
        max_length=150,
        blank=True,
        help_text=_("For example a second person or c/o."),
    )
    address_line_2 = models.CharField(_("address line 2"), max_length=150, blank=True)
    street = models.CharField(_("street"), max_length=150, blank=True)
    house_number = models.CharField(_("house number"), max_length=30, blank=True)
    dwelling_number = models.CharField(_("dwelling number"), max_length=30, blank=True)
    post_office_box_number = models.PositiveIntegerField(
        _("post office box"),
        null=True,
        blank=True,
        validators=[
            MaxValueValidator(
                99_999_999, message=_("Enter a post office box of at most 8 digits.")
            )
        ],
    )
    post_office_box_text = models.CharField(_("post office box text"), max_length=15, blank=True)
    locality = models.CharField(_("locality"), max_length=40, blank=True)
    town = models.CharField(_("town"), max_length=40, blank=True)
    # Forms show one postal code field and write it into one of these two (D-036).
    swiss_postal_code = models.PositiveSmallIntegerField(
        _("Swiss postal code"),
        null=True,
        blank=True,
        validators=[
            MinValueValidator(1000, message=SWISS_POSTAL_CODE_MESSAGE),
            MaxValueValidator(9999, message=SWISS_POSTAL_CODE_MESSAGE),
        ],
    )
    foreign_postal_code = models.CharField(_("foreign postal code"), max_length=15, blank=True)
    country = CountryField(_("country"), default=SWITZERLAND, blank=True)

    # Contact

    phone_number = PhoneNumberField(_("phone number"), blank=True)

    objects = UserManager()

    USERNAME_FIELD = "normalized_email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["email"]

    # Fields that change only through the central account logic, with their values at creation.
    _CONTROLLED_DEFAULTS = {
        "status": Status.UNVERIFIED,
        "email": None,
        "is_staff": False,
        "is_superuser": False,
        "customer_number": None,
        "retention_obligation": False,
    }
    # Written together whenever one of them is written, so that derived values never go stale.
    _DERIVATION_FIELDS = frozenset({"email", "status", "normalized_email", "is_active"})

    _authorized_fields = frozenset()
    _persisted_pk = None

    class Meta:
        # See IssuedCustomerNumber.Meta: _base_manager must not bypass the bulk protection.
        base_manager_name = "objects"
        verbose_name = _("account")
        verbose_name_plural = _("accounts")

    def __str__(self):
        # For display (admin list, titles, error output): the customer number identifies the
        # account for support (D-036); never the email address or the name. It is read from the
        # row (customer_number_id), so no query is made. Admin log entries do not store this
        # text: UserAdmin stores a neutral text with the internal number instead (D-018, D-036).
        if self.customer_number_id:
            return gettext("Account %(customer_number)s") % {
                "customer_number": self.customer_number_display
            }
        return gettext("Unconfirmed account")

    @classmethod
    def from_db(cls, db, field_names, values, **kwargs):
        # Django 6.1 passes fetch_mode as a keyword argument, Django 6.0 does not.
        instance = super().from_db(db, field_names, values, **kwargs)
        instance._persisted_pk = instance.pk
        return instance

    @property
    def customer_number_display(self):
        return format_customer_number(self.customer_number_id)

    @property
    def postal_code_display(self):
        """The postal code of the address, whichever of the two eCH-0010 fields holds it."""
        if self.swiss_postal_code is not None:
            return str(self.swiss_postal_code)
        return self.foreign_postal_code

    def set_password(self, raw_password):
        if raw_password is not None and not raw_password:
            raise AccountIntegrityError("An empty password is not allowed.")
        super().set_password(raw_password)

    # Central account logic

    @contextmanager
    def _account_change(self, *names):
        previous = self._authorized_fields
        self._authorized_fields = previous | frozenset(names)
        try:
            yield
        finally:
            self._authorized_fields = previous

    def _change(self, **values):
        """Set and save controlled fields; restores the instance if the change is refused."""
        previous = {name: self._controlled_value(name) for name in values}
        for name, value in values.items():
            setattr(self, self._meta.get_field(name).attname, value)
        try:
            with self._account_change(*values):
                self.save(using=self._state.db, update_fields=list(values))
        except BaseException:
            for name, value in previous.items():
                setattr(self, self._meta.get_field(name).attname, value)
            self._apply_derived_fields()
            raise

    def _controlled_value(self, name):
        return getattr(self, self._meta.get_field(name).attname)

    def _apply_derived_fields(self):
        """Store the canonical email address and derive is_active and the normalised email column.

        The only place that sets these fields. The stored address is the canonical form (D-035):
        lower case, domain in IDNA ASCII. The normalised column repeats it while the account is
        not deactivated.
        """
        if self.email is not None:
            self.email = normalize_email_address(self.email) or self.email
        self.is_active = self.status == self.Status.ACTIVE
        if self.status == self.Status.DEACTIVATED:
            self.normalized_email = None
        else:
            self.normalized_email = normalize_email_address(self.email)

    @classmethod
    def _is_system_administrator(cls, values):
        return values["is_superuser"] and values["is_staff"] and values["status"] == cls.Status.ACTIVE

    def _locked_row(self, using):
        try:
            return (
                type(self)
                ._base_manager.using(using)
                .select_for_update()
                .values(*self._CONTROLLED_DEFAULTS, "password")
                .get(pk=self.pk)
            )
        except self.DoesNotExist:
            raise AccountIntegrityError("The account does not exist (any more).") from None

    def _check_other_system_administrator(self, using):
        """Refuse if no other active system administrator exists (D-016); locks their rows."""
        others = (
            type(self)
            ._base_manager.using(using)
            .select_for_update()
            .filter(is_superuser=True, is_staff=True, status=self.Status.ACTIVE)
            .exclude(pk=self.pk)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not list(others):
            raise AccountIntegrityError(
                "At least one active system administrator must remain."
            )

    @staticmethod
    def _has_usable_password(encoded):
        # Usable: not empty, no "!" marker, known hasher algorithm; hash format is not checked.
        if not encoded or not is_password_usable(encoded):
            return False
        try:
            identify_hasher(encoded)
        except ValueError:
            return False
        return True

    def _check_state(self, values):
        status = values["status"]
        if not values["password"]:
            # The field must hold a password hash or an unusable marker, never an empty value.
            raise AccountIntegrityError("The password field is empty.")
        if status not in self.Status.values:
            raise AccountIntegrityError("Unknown account status.")
        if values["is_superuser"] and not values["is_staff"]:
            raise AccountIntegrityError("A system administrator is a staff user.")
        if status == self.Status.UNVERIFIED:
            if values["customer_number"] is not None:
                raise AccountIntegrityError("Unverified accounts have no customer number.")
            if self._has_usable_password(values["password"]):
                raise AccountIntegrityError("Unverified accounts have no usable password.")
        if status == self.Status.ACTIVE and not self._has_usable_password(values["password"]):
            raise AccountIntegrityError("Active accounts need a usable password.")

    def _check_privilege_grant(self, values, previous):
        """Refuse to grant staff or administrator rights to an account that is not active."""
        granted = (values["is_staff"] and not previous["is_staff"]) or (
            values["is_superuser"] and not previous["is_superuser"]
        )
        if granted and values["status"] != self.Status.ACTIVE:
            raise AccountIntegrityError("Only active accounts can receive privileges.")

    def _check_against_stored(self, using, update_fields):
        def written(name):
            return (
                update_fields is None
                or name in update_fields
                or self._meta.get_field(name).attname in update_fields
            )

        own = {name: self._controlled_value(name) for name in self._CONTROLLED_DEFAULTS}
        own["password"] = self.password
        if self._state.adding:
            for name, default in self._CONTROLLED_DEFAULTS.items():
                if name == "email" or own[name] == default:
                    continue
                if name not in self._authorized_fields or name in {
                    "customer_number",
                    "retention_obligation",
                }:
                    raise AccountIntegrityError(
                        f"{name} is set only by the account logic, not at creation."
                    )
            self._check_state(own)
            self._check_privilege_grant(own, self._CONTROLLED_DEFAULTS)
            return
        stored = self._locked_row(using)
        result = {name: own[name] if written(name) else stored[name] for name in stored}
        for name in self._CONTROLLED_DEFAULTS:
            if result[name] != stored[name] and name not in self._authorized_fields:
                raise AccountIntegrityError(
                    f"{name} changes only through the account logic; the instance may be stale."
                )
        if stored["retention_obligation"] and not result["retention_obligation"]:
            raise AccountIntegrityError("The retention obligation can never be unset.")
        if stored["customer_number"] is not None and result["customer_number"] != stored[
            "customer_number"
        ]:
            raise AccountIntegrityError("A customer number never changes.")
        self._check_state(result)
        self._check_privilege_grant(result, stored)
        if self._is_system_administrator(stored) and not self._is_system_administrator(result):
            self._check_other_system_administrator(using)

    def clean(self):
        super().clean()
        self._apply_derived_fields()
        errors = {}

        if self.email:
            try:
                validate_account_email(self.email)
            except ValueError:
                errors["email"] = _("Enter a valid email address.")
        if len(self.normalized_email or "") > EMAIL_MAX_LENGTH:
            errors["email"] = _("Enter an email address of at most 254 characters.")

        if not self.organisation_name.strip():
            if not self.first_name.strip():
                errors["first_name"] = _("Enter a first name or a company name.")
            if not self.last_name.strip():
                errors["last_name"] = _("Enter a last name or a company name.")

        # Hidden fields are never required (D-036): a post office box number needs no text.
        has_post_office_box = (
            self.post_office_box_number is not None or bool(self.post_office_box_text.strip())
        )
        if not self.street.strip() and not has_post_office_box:
            errors["street"] = _("Enter a street or a post office box.")
        if not self.town.strip():
            errors["town"] = _("This field is required.")

        country = self.country.code if self.country else ""
        if not country:
            errors["country"] = _("This field is required.")

        if errors:
            raise ValidationError(errors)

    def postal_code_errors(self):
        """Return {field name: message} for the two postal code fields (D-032, D-036).

        Switzerland uses the Swiss postal code; every other country, Liechtenstein included,
        the foreign one. Without a country, nothing is checked (the country is reported).
        """
        country = self.country.code if self.country else ""
        errors = {}
        if country == SWITZERLAND:
            if self.swiss_postal_code is None:
                errors["swiss_postal_code"] = SWISS_POSTAL_CODE_MESSAGE
            if self.foreign_postal_code.strip():
                errors["foreign_postal_code"] = _(
                    "Leave the foreign postal code empty for an address in Switzerland."
                )
        elif country:
            if not self.foreign_postal_code.strip():
                errors["foreign_postal_code"] = _("This field is required.")
            if self.swiss_postal_code is not None:
                errors["swiss_postal_code"] = _(
                    "Leave the Swiss postal code empty for an address outside Switzerland."
                )
        return errors

    def clean_fields(self, exclude=None):
        # The postal code rules are checked here because only clean_fields() receives the
        # fields a form excludes: a form that shows one postal code field (instead of the two
        # model fields) checks the rules itself and attaches them to that field (D-036).
        # Django's documentation recommends this for rules on fields a form may exclude.
        errors = {}
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        excluded = set(exclude or ())
        for name, message in self.postal_code_errors().items():
            if name not in excluded and name not in errors:
                errors[name] = [message]

        if errors:
            raise ValidationError(errors)

    def save(self, **kwargs):
        if self._state.adding:
            if kwargs.get("force_update") or kwargs.get("update_fields") is not None:
                raise AccountIntegrityError("A new account is inserted, never updated.")
            # Otherwise Django would try an UPDATE first and overwrite an account with this pk.
            kwargs["force_insert"] = True
        elif self.pk is None or self.pk != self._persisted_pk:
            raise AccountIntegrityError("The primary key of an account never changes.")
        self._apply_derived_fields()
        try:
            validate_account_email(self.email)
        except ValueError as error:
            raise AccountIntegrityError(str(error)) from None
        if len(self.email or "") > EMAIL_MAX_LENGTH or len(
            self.normalized_email or ""
        ) > EMAIL_MAX_LENGTH:
            raise AccountIntegrityError(
                f"The email address is longer than {EMAIL_MAX_LENGTH} characters."
            )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if update_fields & self._DERIVATION_FIELDS:
                update_fields |= self._DERIVATION_FIELDS
            kwargs["update_fields"] = update_fields
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            self._check_against_stored(using, update_fields)
            super().save(**kwargs)
        self._persisted_pk = self.pk

    def delete(self, using=None, keep_parents=False):
        """Delete the account unless a retention obligation or the last-administrator rule applies.

        Accounts with a retention obligation are deactivated instead (D-016); the deletion
        function of a later step does this with set_status().
        """
        using = using or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["retention_obligation"]:
                raise AccountIntegrityError(
                    "Accounts with a retention obligation are deactivated, not deleted."
                )
            if self._is_system_administrator(stored):
                self._check_other_system_administrator(using)
            return super().delete(using=using, keep_parents=keep_parents)

    def set_status(self, status):
        """Change the account status (D-016); is_active and the normalised address follow it."""
        self._change(status=status)

    def set_privileges(self, *, is_staff, is_superuser):
        """Promote or demote the account (D-012); a system administrator is also a staff user."""
        self._change(is_staff=is_staff, is_superuser=is_superuser)

    def set_access_relations(self, *, groups, user_permissions):
        """Set group memberships and direct permissions through central account logic (D-012)."""
        if self.pk is None:
            raise AccountIntegrityError("Save the account before assigning access relations.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            self._locked_row(using)
            self.groups.set(groups)
            self.user_permissions.set(user_permissions)

    def change_email(self, email):
        """Replace the email address once the new address has been verified (D-016)."""
        email = str(email).strip() if email is not None else email
        validate_account_email(email)
        self._change(email=email)

    def assign_customer_number(self):
        """Issue the customer number when the registration is completed (D-021).

        Used by the verification flow and the setup command of the first system administrator.
        Refused for unsaved accounts, for accounts that are not active and for accounts that
        already have one.
        """
        if self.pk is None:
            raise AccountIntegrityError("Save the account before assigning a customer number.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["customer_number"] is not None or self.customer_number_id is not None:
                raise AccountIntegrityError("The account already has a customer number.")
            if stored["status"] != self.Status.ACTIVE:
                raise AccountIntegrityError("Only active accounts receive a customer number.")
            number = IssuedCustomerNumber.objects.db_manager(using).issue()
            self._change(customer_number=number)
        return self.customer_number_id

    def set_retention_obligation(self):
        """Set the retention obligation flag; it can never be unset (D-016)."""
        self._change(retention_obligation=True)

    def complete_registration(self, raw_password):
        """Activate an unverified account with the password chosen after verification.

        The password, the status and the customer number are written together (D-016, D-021):
        an account is never active without a usable password. The caller validates the
        password and decides whether the verification link is valid.
        """
        if self.pk is None:
            raise AccountIntegrityError("Save the account before completing its registration.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["status"] != self.Status.UNVERIFIED:
                raise AccountIntegrityError("Only unverified accounts complete a registration.")
            previous_password = self.password
            previous_status = self.status
            self.set_password(raw_password)
            self.status = self.Status.ACTIVE
            try:
                with self._account_change("status"):
                    self.save(using=using, update_fields=["status", "password"])
            except BaseException:
                self.password = previous_password
                self.status = previous_status
                self._apply_derived_fields()
                raise
            self.assign_customer_number()


class LoginIpBucket(models.Model):
    """A pseudonymous lock row that serializes login attempts from one client IP."""

    digest = models.CharField(
        max_length=64,
        primary_key=True,
        db_collation=EXACT_COLLATION,
        editable=False,
    )


class LoginIpAttempt(models.Model):
    """A short-lived pending or failed login attempt for one pseudonymous IP bucket."""

    bucket = models.ForeignKey(
        LoginIpBucket,
        on_delete=models.CASCADE,
        related_name="attempts",
        editable=False,
    )
    attempted_at = models.DateTimeField()
    pending = models.BooleanField(default=True, editable=False)

    class Meta:
        indexes = [models.Index(fields=("bucket", "attempted_at"), name="accounts_lia_bucket_time")]


# 256 bits from the operating system's random generator (Python "secrets"); the URL-safe text
# has 43 characters.
VERIFICATION_TOKEN_BYTES = 32
VERIFICATION_TOKEN_LENGTH = 43
VERIFICATION_TOKEN_PURPOSE = b"LEG-Platform verification token v1"


def verification_token_digest(raw_token):
    """Return the keyed digest under which a token is stored; the token itself never is."""
    return keyed_digest(raw_token, VERIFICATION_TOKEN_PURPOSE)


class VerificationTokenManager(models.Manager):
    def issue(self, user, purpose, lifetime):
        """Create a token for the user and return its raw value, which is stored nowhere.

        Only the keyed digest is stored, so a copy of the database yields no usable link.
        """
        raw_token = secrets.token_urlsafe(VERIFICATION_TOKEN_BYTES)
        self.create(
            user=user,
            purpose=purpose,
            digest=verification_token_digest(raw_token),
            expires_at=timezone.now() + lifetime,
        )
        return raw_token

    def find_valid(self, raw_token, purpose):
        """Return the unexpired token with this raw value and purpose, or None."""
        if len(raw_token or "") != VERIFICATION_TOKEN_LENGTH:
            return None
        return self.find_valid_by_digest(verification_token_digest(raw_token), purpose)

    def find_valid_by_digest(self, digest, purpose):
        return self.filter(
            digest=digest, purpose=purpose, expires_at__gt=timezone.now()
        ).select_related("user").first()


class VerificationToken(models.Model):
    """A single-use, time-limited token that a link sent by email carries (D-016).

    The token is deleted when it is used, together with the account it belongs to when that
    account is deleted.
    """

    class Purpose(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="verification_tokens", editable=False
    )
    purpose = models.CharField(max_length=20, choices=Purpose, editable=False)
    digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    objects = VerificationTokenManager()


class RateLimitBucket(models.Model):
    """A pseudonymous lock row that serialises the requests counted for one key (D-037).

    The key is an HMAC of the scope and the normalised address or client IP; the address or IP
    itself is never stored.
    """

    digest = models.CharField(
        max_length=64, primary_key=True, db_collation=EXACT_COLLATION, editable=False
    )
    last_seen_at = models.DateTimeField()


class RateLimitEvent(models.Model):
    """One counted request of a bucket; deleted when it is older than every window."""

    bucket = models.ForeignKey(
        RateLimitBucket, on_delete=models.CASCADE, related_name="events", editable=False
    )
    occurred_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=("bucket", "occurred_at"), name="accounts_rle_bucket_time")]


class MailJob(models.Model):
    """A request for an email that a background worker processes (outbox, D-037).

    The request path only stores the job, so that it does the same work for every address.
    The address of a person without an account is stored only until the job is processed.
    """

    class Kind(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        TWO_FACTOR_ENABLED = "two_factor_enabled", "Two-factor authentication set up"
        TWO_FACTOR_DISABLED = "two_factor_disabled", "Two-factor authentication removed"
        RECOVERY_CODES_RENEWED = "recovery_codes_renewed", "Recovery codes renewed"
        RECOVERY_CODE_USED = "recovery_code_used", "Recovery code used"

    # The notifications about a change of the account, as opposed to the requests of a person
    # who may have no account (D-038, D-040).
    NOTIFICATION_KINDS = (
        Kind.PASSWORD_CHANGED,
        Kind.TWO_FACTOR_ENABLED,
        Kind.TWO_FACTOR_DISABLED,
        Kind.RECOVERY_CODES_RENEWED,
        Kind.RECOVERY_CODE_USED,
    )

    kind = models.CharField(max_length=24, choices=Kind, editable=False)
    # Empty for a suppressed job: it is discarded unseen, so it need not keep the address of a
    # person who may have no account.
    address = models.EmailField(max_length=EMAIL_MAX_LENGTH, blank=True, editable=False)
    # A request above a limit is stored like any other and discarded by the worker.
    suppressed = models.BooleanField(default=False, editable=False)
    attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField(db_index=True)


class EmailTextManager(models.Manager):
    def ensure_all(self):
        """Create the missing rows, so that the administration lists every email text."""
        for key in EmailText.Key.values:
            self.get_or_create(key=key)


class EmailText(models.Model):
    """The subject and body that the administrator wrote for one email (D-039).

    One row per email, created empty. An empty subject or body means that the default text of
    the application is used. The texts are plain text with ``{placeholder}`` markers that are
    validated when saved and replaced without a template engine when the email is sent.
    """

    class Key(models.TextChoices):
        REGISTRATION_VERIFICATION = "registration_verification", _(
            "Registration: link to confirm the address"
        )
        REGISTRATION_EXISTING_ACCOUNT = "registration_existing_account", _(
            "Registration: the address already has an account"
        )
        PASSWORD_RESET = "password_reset", _("Password reset: link to choose a new password")
        PASSWORD_CHANGED = "password_changed", _("Notification: password changed")
        TWO_FACTOR_ENABLED = "two_factor_enabled", _(
            "Notification: two-factor authentication set up"
        )
        TWO_FACTOR_DISABLED = "two_factor_disabled", _(
            "Notification: two-factor authentication removed"
        )
        RECOVERY_CODES_RENEWED = "recovery_codes_renewed", _(
            "Notification: recovery codes renewed"
        )
        RECOVERY_CODE_USED = "recovery_code_used", _("Notification: recovery code used")

    key = models.CharField(
        _("email"), max_length=40, choices=Key, unique=True, editable=False
    )
    subject = models.CharField(_("subject"), max_length=150, blank=True)
    body = models.TextField(_("text"), max_length=5000, blank=True)
    updated_at = models.DateTimeField(_("last changed"), auto_now=True)

    objects = EmailTextManager()

    class Meta:
        default_permissions = ("view", "change")
        verbose_name = _("email text")
        verbose_name_plural = _("email texts")

    def __str__(self):
        return str(self.get_key_display())


class EmailTextVersion(models.Model):
    """A saved state of an email text, kept so that an earlier text can be restored (D-039).

    Written with every change of the subject or body. An empty subject and body is the
    default text. The account that made the change is kept for the audit and is not shown;
    it is cleared when the account is deleted.
    """

    email_text = models.ForeignKey(
        EmailText, on_delete=models.CASCADE, related_name="versions", editable=False
    )
    subject = models.CharField(max_length=150, blank=True, editable=False)
    body = models.TextField(max_length=5000, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    changed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        default_permissions = ()
        ordering = ("-created_at", "-id")

# Second factor (D-040). The devices follow the interface of django-otp (``Device``), so that its
# middleware and device functions work with them, but the secrets are not stored in plain text:
# the TOTP key is encrypted with a key derived from SECRET_KEY (like the Microsoft Graph secret,
# D-006) and a recovery code is stored only as a keyed digest. Neither can be read back from the
# database or shown again.

AUTHENTICATOR_SECRET_PURPOSE = b"LEG-Platform authenticator secret v1"
RECOVERY_CODE_PURPOSE = b"LEG-Platform recovery code v1"
AUTHENTICATOR_KEY_BYTES = 20


def recovery_code_digest(code):
    return keyed_digest(code, RECOVERY_CODE_PURPOSE)


AUTHENTICATOR_CODE = re.compile(r"[0-9]{6}")


class AuthenticatorDeviceManager(DeviceManager):
    def create_device(self, user, *, confirmed):
        """Create a device with a new random key (160 bits, RFC 4226, section 4)."""
        return self.create(
            user=user,
            name="authenticator app",
            confirmed=confirmed,
            encrypted_key=encrypt(secrets.token_bytes(AUTHENTICATOR_KEY_BYTES), AUTHENTICATOR_SECRET_PURPOSE),
        )


class AuthenticatorDevice(TimestampMixin, Device):
    """An authenticator app (TOTP, RFC 6238): 30 seconds, six digits, SHA-1.

    These are the values authenticator apps support. One step of tolerance in each direction; a
    code at or below the step of the last accepted one is refused, so that a code works once.
    Failures are limited by the abuse protection of the login (D-017), not by the device.
    """

    STEP = 30
    DIGITS = 6
    TOLERANCE = 1

    encrypted_key = models.CharField(max_length=255, editable=False)
    drift = models.SmallIntegerField(default=0, editable=False)
    last_t = models.BigIntegerField(default=-1, editable=False)

    objects = AuthenticatorDeviceManager()

    class Meta:
        default_permissions = ()
        # The account row lock keeps this, the constraint makes the database refuse a second one.
        constraints = [
            models.UniqueConstraint(fields=["user"], name="accounts_one_authenticator_per_user")
        ]

    @property
    def bin_key(self):
        return decrypt(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE)

    @sensitive_variables()
    def verify_token(self, token):
        # Exactly six ASCII digits: int() would also accept signs, underscores and other scripts.
        if not isinstance(token, str) or not AUTHENTICATOR_CODE.fullmatch(token):
            return False
        try:
            key = self.bin_key
        except DecryptionError:
            logger.error("An authenticator secret cannot be decrypted (SECRET_KEY changed?).")
            return False
        number = int(token)
        generator = TOTP(key, self.STEP, 0, self.DIGITS, self.drift)
        generator.time = time.time()
        if not generator.verify(number, self.TOLERANCE, self.last_t + 1):
            return False
        self.last_t = generator.t()
        self.drift = generator.drift
        if not is_current(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE):
            self.encrypted_key = reencrypt(self.encrypted_key, AUTHENTICATOR_SECRET_PURPOSE)
        self.set_last_used_timestamp(commit=False)
        self.save()
        return True

    @property
    @sensitive_variables()
    def config_url(self):
        """The ``otpauth`` URL of the key URI format that authenticator apps read from the QR code.

        https://github.com/google/google-authenticator/wiki/Key-Uri-Format
        """
        issuer = settings.INSTANCE_NAME.replace(":", "")
        label = f"{issuer}:{self.user.get_username()}"
        parameters = urlencode(
            {
                "secret": b32encode(self.bin_key),
                "algorithm": "SHA1",
                "digits": self.DIGITS,
                "period": self.STEP,
                "issuer": issuer,
            },
            quote_via=quote,
        )
        return f"otpauth://totp/{quote(label)}?{parameters}"


class RecoveryCodeDevice(TimestampMixin, Device):
    """The recovery codes of an account; each code is deleted when it is used."""

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["user"], name="accounts_one_recovery_device_per_user")
        ]

    def verify_token(self, token):
        # Digests under the keys of SECRET_KEY_FALLBACKS also match, so that a rotation of the
        # key does not invalidate the codes. The delete is one statement: of two parallel
        # requests with the same code only one deletes a row.
        digests = keyed_digests_with_fallbacks(token, RECOVERY_CODE_PURPOSE)
        deleted, _unused = self.codes.filter(digest__in=digests).delete()
        if not deleted:
            return False
        self.set_last_used_timestamp(commit=False)
        self.save()
        return True

    def add_codes(self, codes):
        RecoveryCode.objects.bulk_create(
            RecoveryCode(device=self, digest=recovery_code_digest(code)) for code in codes
        )


class RecoveryCode(models.Model):
    device = models.ForeignKey(
        RecoveryCodeDevice, on_delete=models.CASCADE, related_name="codes", editable=False
    )
    digest = models.CharField(
        max_length=64, unique=True, db_collation=EXACT_COLLATION, editable=False
    )

    class Meta:
        default_permissions = ()
