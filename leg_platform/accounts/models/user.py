"""The account model and its central account logic (D-016, D-021, D-031, D-032, D-034, D-036).

Invariants enforced here, independent of any view, form or admin:

* The normalised email column (``USERNAME_FIELD``) is derived from the email address in one place
  and is NULL while the account is deactivated, so a unique index covers the other accounts.
* ``is_active`` is derived from the status: only active accounts are active.
* Status, email, ``is_staff``, ``is_superuser``, customer number and retention flag change only
  through the methods below; every save compares them with the row locked with
  ``SELECT ... FOR UPDATE``, which also rejects stale instances.
* Unverified accounts have neither a customer number nor a usable password; superusers are staff.
* The retention obligation flag can only change from unset to set; accounts with it are not
  deleted. At least one active system administrator remains (D-016).
* Bulk changes of protected fields, bulk deletion and bulk creation are rejected.

Who may perform a change is checked by the views and the admin, not here.
"""

from contextlib import contextmanager

import phonenumbers
from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.hashers import identify_hasher, is_password_usable
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, router, transaction
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django_countries.fields import CountryField
from phonenumber_field.modelfields import PhoneNumberField

from leg_platform.accounts.models.common import (
    EMAIL_MAX_LENGTH,
    EXACT_COLLATION,
    SWISS_POSTAL_CODE_MESSAGE,
    SWITZERLAND,
    AccountIntegrityError,
    validate_account_email,
)
from leg_platform.accounts.models.customer_numbers import (
    IssuedCustomerNumber,
    format_customer_number,
)
from leg_platform.accounts.models.user_manager import UserManager
from leg_platform.accounts.normalization import normalize_email_address


class User(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        UNVERIFIED = "unverified", _("Unverified")
        ACTIVE = "active", _("Active")
        BLOCKED = "blocked", _("Blocked")
        DEACTIVATED = "deactivated", _("Deleted (retention obligation)")

    class Salutation(models.TextChoices):
        MRS = "mrs", _("Mrs")
        MR = "mr", _("Mr")
        FAMILY = "family", _("Family")

    class AuthenticationMethod(models.TextChoices):
        PASSWORD = "password", _("Password")
        PASSKEY = "passkey", _("Passkey")
        RECOVERY = "recovery", _("Sign-in method setup")

    authentication_method = models.CharField(
        _("authentication method"), max_length=8, choices=AuthenticationMethod, default=AuthenticationMethod.PASSWORD,
        editable=False,
    )

    email = models.EmailField(_("email address"), max_length=EMAIL_MAX_LENGTH)
    # Information and notifications only (D-059): never a login name, unique or used for a
    # password reset. Stored once confirmed; a requested address waits in ``PendingEmailChange``.
    secondary_email = models.EmailField(
        _("second email address"), max_length=EMAIL_MAX_LENGTH, blank=True, editable=False
    )
    # The unique message must NOT be shown by the public registration: it answers duplicates
    # generically (no account enumeration, docs/SECURITY.md, section 3).
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
    # A foreign key, so that the database accepts only issued numbers.
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
    # The person must replace the password before anything else (D-005, D-041).
    password_change_required = models.BooleanField(
        _("password change required"), default=False, editable=False
    )
    # Set when staff created and invited the person (D-087); written only by
    # ``UserManager.create_invited_user``; stays after the registration is completed.
    invited_at = models.DateTimeField(_("invited at"), null=True, blank=True, editable=False)
    # Part of the session hash: raising it ends every session of the account (D-041).
    session_version = models.PositiveIntegerField(
        _("session version"), default=0, editable=False
    )

    # Name and postal address: structure and lengths of eCH-0010 (D-032). Unused fields are kept
    # but not shown (D-036).
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

    phone_number = PhoneNumberField(_("phone number"), blank=True)

    # Consent to the privacy notice at the end of the registration (D-058): time and address
    # of the notice shown then; written once.
    privacy_accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    privacy_accepted_url = models.CharField(max_length=500, blank=True, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "normalized_email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["email"]

    # Fields that change only through the account logic, with their values at creation.
    _CONTROLLED_DEFAULTS = {
        "authentication_method": AuthenticationMethod.PASSWORD,
        "status": Status.UNVERIFIED,
        "email": None,
        "is_staff": False,
        "is_superuser": False,
        "customer_number": None,
        "retention_obligation": False,
        "invited_at": None,
    }
    # Written together whenever one of them is written, so derived values never go stale.
    _DERIVATION_FIELDS = frozenset({"email", "status", "normalized_email", "is_active"})

    _authorized_fields = frozenset()
    _persisted_pk = None

    class Meta:
        base_manager_name = "objects"  # _base_manager must not bypass the bulk protection
        verbose_name = _("account")
        verbose_name_plural = _("accounts")

    def __str__(self):
        # The customer number identifies the account for support, never the email or the name
        # (D-036); read from the row, no query.
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
    def display_name(self):
        """First and last name, or the company name for an account without a personal name."""
        return f"{self.first_name} {self.last_name}".strip() or self.organisation_name

    @property
    def street_address_display(self):
        """The street and the house number (or the post office box) for one line, or ''."""
        if self.street.strip():
            return f"{self.street} {self.house_number}".strip()
        if self.post_office_box_number is not None:
            return f"{gettext('Post office box')} {self.post_office_box_number}"
        return ""

    @property
    def postal_code_display(self):
        """The postal code, whichever of the two eCH-0010 fields holds it."""
        if self.swiss_postal_code is not None:
            return str(self.swiss_postal_code)
        return self.foreign_postal_code

    @property
    def phone_number_display(self):
        """National format for the default region (Switzerland), else international (D-034)."""
        number = self.phone_number
        if not number:
            return ""
        if not number.is_valid():
            return str(number)
        if phonenumbers.region_code_for_number(number) == settings.PHONENUMBER_DEFAULT_REGION:
            return number.as_national
        return number.as_international

    def set_password(self, raw_password):
        if raw_password is not None and not raw_password:
            raise AccountIntegrityError("An empty password is not allowed.")
        super().set_password(raw_password)

    def _get_session_auth_hash(self, secret=None):
        # Django's hash covers the password; the version ends every session without one (D-041).
        return salted_hmac(
            "leg_platform.accounts.models.User.session_auth_hash",
            f"{self.password}:{self.session_version}",
            secret=secret,
            algorithm="sha256",
        ).hexdigest()

    def end_all_sessions(self):
        """End every session of the account; call it in the transaction of the change.

        A session that must go on takes the new hash with ``update_session_auth_hash()``.
        """
        type(self)._base_manager.filter(pk=self.pk).increase_session_version()
        self.refresh_from_db(fields=["session_version"])

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
        """Store the canonical email address (D-035) and derive is_active and the normalised
        column, which is NULL while the account is deactivated. The only place that sets them."""
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
        method = values["authentication_method"]
        if method not in self.AuthenticationMethod.values:
            raise AccountIntegrityError("Unknown authentication method.")
        if method != self.AuthenticationMethod.PASSWORD and self._has_usable_password(values["password"]):
            raise AccountIntegrityError("Passkey and recovery accounts have no usable password.")
        if (
            status == self.Status.ACTIVE
            and method == self.AuthenticationMethod.PASSWORD
            and not self._has_usable_password(values["password"])
        ):
            raise AccountIntegrityError("Active accounts need a usable password.")
        if status == self.Status.ACTIVE and method == self.AuthenticationMethod.PASSKEY:
            if self._state.adding or not self.passkeys.filter(active=True).exists():
                raise AccountIntegrityError("Passkey accounts need an active credential.")

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

        errors.update(self.name_errors())
        errors.update(self.address_errors())

        if errors:
            raise ValidationError(errors)

    def name_errors(self):
        """{field: message} for the name rule: a first and a last name, or a company name.

        Separate from ``clean()``, so a form that edits only the names checks only this (D-087).
        """
        errors = {}
        if not self.organisation_name.strip():
            if not self.first_name.strip():
                errors["first_name"] = _("Enter a first name or a company name.")
            if not self.last_name.strip():
                errors["last_name"] = _("Enter a last name or a company name.")
        return errors

    def address_errors(self):
        """{field: message} for street or post office box, town and country (not the postal
        code, see ``postal_code_errors()``). Separate from ``clean()`` like ``name_errors()``."""
        errors = {}
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
        return errors

    def postal_code_errors(self):
        """{field: message} for the two postal code fields (D-032, D-036).

        Switzerland uses the Swiss postal code, every other country (Liechtenstein included)
        the foreign one. Without a country nothing is checked.
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
        # Checked here because only clean_fields() receives the fields a form excludes: a form
        # with one postal code field checks the rules itself (D-036).
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
        """Delete the account unless a retention obligation or the last-administrator rule applies
        (such accounts are deactivated instead, D-016)."""
        using = using or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["retention_obligation"] or self._has_person_assignment(using):
                raise AccountIntegrityError(
                    "Accounts with a retention obligation are deactivated, not deleted."
                )
            if self._is_system_administrator(stored):
                self._check_other_system_administrator(using)
            # The greeting name must not outlive the account in a queued job (D-068).
            self.erase_greeting_names(using)
            return super().delete(using=using, keep_parents=keep_parents)

    def discard_email_change_requests(self, using=None):
        """Delete the open address-change requests and every link that belongs to them.

        Called where the person's hold on the account changes (new password, status other than
        active, reset of the second factor), so that an earlier request cannot be confirmed.
        """
        from leg_platform.accounts.models.tokens import PendingEmailChange, VerificationToken

        using = using or self._state.db or router.db_for_write(type(self), instance=self)
        PendingEmailChange.objects.using(using).filter(user_id=self.pk).delete()
        VerificationToken.objects.using(using).filter(
            user_id=self.pk,
            purpose__in=(
                VerificationToken.Purpose.EMAIL_CHANGE,
                VerificationToken.Purpose.SECONDARY_EMAIL,
                VerificationToken.Purpose.EMAIL_CHANGE_STOP,
            ),
        ).delete()

    def set_status(self, status):
        """Change the account status and end its existing sessions (D-016)."""
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            account = type(self)._base_manager.using(using).select_for_update().get(pk=self.pk)
            account._change(status=status)
            account.end_all_sessions()
            if status != self.Status.ACTIVE:
                account.discard_email_change_requests(using)
            self.refresh_from_db(using=using)

    def erase_greeting_names(self, using=None):
        """Blank the greeting name in the queued notifications to the addresses of the account
        (D-068). The notices are still sent, with the neutral greeting."""
        from leg_platform.accounts.models.mail import MailJob

        using = using or self._state.db or router.db_for_write(type(self), instance=self)
        row = (
            type(self)
            ._base_manager.using(using)
            .filter(pk=self.pk)
            .values("email", "secondary_email")
            .first()
        )
        if row is None:
            return 0
        wanted = {
            normalize_email_address(address)
            for address in (row["email"], row["secondary_email"])
            if address
        }
        job_ids = [
            pk
            for pk, address in MailJob.objects.using(using)
            .exclude(greeting_name="")
            .values_list("pk", "address")
            if normalize_email_address(address) in wanted
        ]
        return MailJob.objects.using(using).filter(pk__in=job_ids).update(greeting_name="")

    def _has_person_assignment(self, using):
        """Whether an assignment of a person to a metering point refers to the account (D-077):
        such an account is deactivated, not deleted. The relation comes from the community app."""
        related = getattr(self, "metering_point_assignments", None)
        return related is not None and related.using(using).exists()

    def delete_or_deactivate(self):
        """Apply the account deletion rule: retain-and-deactivate, otherwise erase (D-016)."""
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            account = type(self)._base_manager.using(using).select_for_update().get(pk=self.pk)
            # Before the second address goes, also for an account kept under retention (D-068).
            account.erase_greeting_names(using)
            # The second address and the requests for new addresses (and their links) are no
            # longer needed, also for a retained account (D-059).
            account.discard_email_change_requests(using)
            if account.secondary_email:
                account.secondary_email = ""
                account.save(using=using, update_fields=["secondary_email"])
            if account.retention_obligation or account._has_person_assignment(using):
                account._change(status=self.Status.DEACTIVATED)
                account.end_all_sessions()
                self.refresh_from_db(using=using)
                return "deactivated"
            account.delete(using=using)
            return "deleted"

    def reactivate(self):
        """Reactivate a retained account if its email address is available (D-016)."""
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            account = type(self)._base_manager.using(using).select_for_update().get(pk=self.pk)
            if account.status != self.Status.DEACTIVATED:
                raise AccountIntegrityError("Only deactivated accounts can be reactivated.")
            duplicate = (
                type(self)
                ._base_manager.using(using)
                .select_for_update()
                .filter(
                    normalized_email=account.normalized_email
                    or normalize_email_address(account.email)
                )
                .exclude(pk=account.pk)
                .order_by("pk")
                .first()
            )
            if duplicate is not None and duplicate.status == self.Status.UNVERIFIED:
                duplicate.delete(using=using)
            elif duplicate is not None:
                raise AccountIntegrityError("The email address is already in use.")
            try:
                with transaction.atomic(using=using):
                    account._change(status=self.Status.ACTIVE)
            except IntegrityError:
                raise AccountIntegrityError(
                    "The email address is already in use."
                ) from None
            account.end_all_sessions()
            self.refresh_from_db(using=using)

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

    def notification_addresses(self):
        """The login address and the confirmed second address, without repetition (D-059)."""
        addresses = [self.email]
        if self.secondary_email and self.secondary_email not in addresses:
            addresses.append(self.secondary_email)
        return addresses

    def set_secondary_email(self, address):
        """Store the confirmed second address, or remove it with an empty value (D-059).

        It must differ from the login address; the caller has checked the confirmation.
        """
        address = normalize_email_address(address) if address else ""
        if address:
            validate_account_email(address)
            if address == self.email:
                raise AccountIntegrityError("The second address differs from the login address.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            self._locked_row(using)
            self.secondary_email = address
            self.save(using=using, update_fields=["secondary_email"])

    def assign_customer_number(self):
        """Issue the customer number when the registration is completed (D-021).

        Refused for unsaved accounts, accounts that are not active and accounts that have one.
        """
        if self.pk is None:
            raise AccountIntegrityError("Save the account before assigning a customer number.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["customer_number"] is not None             or self.customer_number_id is not None:
                raise AccountIntegrityError("The account already has a customer number.")
            if stored["status"] != self.Status.ACTIVE:
                raise AccountIntegrityError("Only active accounts receive a customer number.")
            number = IssuedCustomerNumber.objects.db_manager(using).issue()
            self._change(customer_number=number)
        return self.customer_number_id

    def set_retention_obligation(self):
        """Set the retention obligation flag; it can never be unset (D-016)."""
        self._change(retention_obligation=True)

    def complete_registration(self, raw_password=None, *, password_hash=None, passkey_binding=None):
        """Activate an unverified account with the sign-in method chosen after verification.

        Password (or passkey), status and customer number are written together (D-016, D-021). The password
        is given as text or as the hash made when it was chosen (D-058, a known algorithm). A passkey account
        (``passkey_binding``: the session binding under which its keys were created, ``passkeys.binding``) has
        no password: its inactive keys of that binding are switched on in the same transaction.
        """
        if self.pk is None:
            raise AccountIntegrityError("Save the account before completing its registration.")
        by_passkey = passkey_binding is not None
        if by_passkey != (raw_password is None and password_hash is None):
            raise AccountIntegrityError("Give either the password, its hash or the binding of the passkey.")
        if raw_password is not None and password_hash is not None:
            raise AccountIntegrityError("Give either the password or its hash.")
        if password_hash is not None and not self._has_usable_password(password_hash):
            raise AccountIntegrityError("The password hash is not usable.")
        using = self._state.db or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            stored = self._locked_row(using)
            if stored["status"] != self.Status.UNVERIFIED:
                raise AccountIntegrityError("Only unverified accounts complete a registration.")
            previous_password = self.password
            previous_status = self.status
            previous_method = self.authentication_method
            if by_passkey:
                # The keys first: an active passkey account needs an active credential (``_check_state``).
                if not self.passkeys.filter(active=False, setup_binding=passkey_binding).update(
                    active=True, setup_binding=""
                ):
                    raise AccountIntegrityError("No passkey was created for this registration.")
                self.set_unusable_password()
                self.authentication_method = self.AuthenticationMethod.PASSKEY
            elif password_hash is not None:
                self.password = password_hash
            else:
                self.set_password(raw_password)
            self.status = self.Status.ACTIVE
            try:
                with self._account_change("status", "authentication_method"):
                    self.save(using=using, update_fields=["status", "password", "authentication_method"])
            except BaseException:
                self.password = previous_password
                self.status = previous_status
                self.authentication_method = previous_method
                self._apply_derived_fields()
                raise
            self.assign_customer_number()
