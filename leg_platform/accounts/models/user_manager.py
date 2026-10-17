"""The queryset and manager of the account model: bulk protection and the creation functions."""

from django.contrib.auth import password_validation
from django.contrib.auth.base_user import BaseUserManager
from django.db import connections, models, router, transaction
from django.utils import timezone

from leg_platform.accounts.models.common import (
    EMAIL_MAX_LENGTH,
    AccountIntegrityError,
    validate_account_email,
)
from leg_platform.accounts.normalization import normalize_email_address

# Seconds create_superuser() waits for a concurrent call (MariaDB GET_LOCK).
SUPERUSER_LOCK_TIMEOUT = 10


class UserQuerySet(models.QuerySet):
    # Changes go through the account logic (D-016). Attribute names are listed as well, because
    # bulk_update() passes them to update().
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
            "session_version",
            "password_change_required",
            "invited_at",
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

    def increase_session_version(self):
        """End the sessions of the accounts in the queryset (D-041).

        Bypasses the protected-field check of ``update()`` on purpose; only
        ``User.end_all_sessions()`` calls it.
        """
        return models.QuerySet.update(self, session_version=models.F("session_version") + 1)

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
            "invited_at",
        }
    )
    # The profile data that staff enter when they create a person (D-087).
    INVITATION_PROFILE_FIELDS = frozenset(
        {
            "salutation",
            "first_name",
            "last_name",
            "organisation_name",
            "address_line_1",
            "street",
            "house_number",
            "post_office_box_number",
            "town",
            "swiss_postal_code",
            "foreign_postal_code",
            "country",
            "phone_number",
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
        """Create an unverified account without a usable password (D-016); a password is rejected."""
        email = self._prepare(email, extra_fields)
        if password is not None:
            raise ValueError("Unverified accounts receive their password after verification.")
        user = self.model(email=email, **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_invited_user(self, email, **profile):
        """Create an unverified account for a person whom staff invite (D-087).

        Only ``INVITATION_PROFILE_FIELDS`` are accepted; the data is validated with the model
        rules (``ValidationError``, nothing stored). ``invited_at`` marks the account; it has no
        customer number (issued at the end of the registration) and no usable password.
        """
        email = self._prepare(email, profile)
        unknown = set(profile) - self.INVITATION_PROFILE_FIELDS
        if unknown:
            raise ValueError(f"These fields cannot be set here: {', '.join(sorted(unknown))}.")
        user = self.model(email=email, invited_at=timezone.now(), **profile)
        user.set_unusable_password()
        user.full_clean(exclude=["password", "last_login"], validate_unique=False)
        with user._account_change("invited_at"):
            user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create the first system administrator (docs/PROJECT.md, "First System Administrator").

        Refused while any account with is_superuser exists, including blocked and deactivated
        ones. The email address counts as verified; the account is active and receives its
        customer number. Must run outside a transaction: a MariaDB named lock (GET_LOCK)
        serialises concurrent calls, because row locks cannot protect the check while no
        system administrator row exists.
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

    def _natural_key_lookup(self, username):
        # The raw length is checked before normalisation, as in validate_account_email().
        if username is not None and len(str(username).strip()) > EMAIL_MAX_LENGTH:
            raise self.model.DoesNotExist
        normalized = normalize_email_address(username)
        if normalized is None or len(normalized) > EMAIL_MAX_LENGTH:
            # A lookup with None would match deactivated accounts (IS NULL).
            raise self.model.DoesNotExist
        return {self.model.USERNAME_FIELD: normalized}

    def get_by_natural_key(self, username):
        return self.get(**self._natural_key_lookup(username))

    async def aget_by_natural_key(self, username):
        return await self.aget(**self._natural_key_lookup(username))
