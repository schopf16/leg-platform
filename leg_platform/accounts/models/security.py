"""Abuse-protection state and the security log (D-017, D-018, D-037, D-041, D-062)."""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models.common import EXACT_COLLATION, AccountIntegrityError
from leg_platform.accounts.models.user import User


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


class SessionCodeFailure(models.Model):
    """The wrong app codes entered in the confirmations of one session of an account (D-062).

    Counted per session, not against the account budget that also pauses the login; the limit
    ends the session. Found by a keyed digest of the session key; kept in the database (not the
    session) because parallel requests work on copies of the session; changed only under the
    lock of the account row.
    """

    account = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="session_code_failures", editable=False
    )
    session_digest = models.CharField(max_length=64, db_collation=EXACT_COLLATION, editable=False)
    failures = models.PositiveSmallIntegerField(default=0, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("account", "session_digest"), name="accounts_scf_account_session"
            )
        ]
        indexes = [models.Index(fields=("updated_at",), name="accounts_scf_updated")]


class ConfirmationFailure(models.Model):
    """One wrong code or password in a profile confirmation, counted across all sessions of the
    account for the cap that ends all of them (D-062). Never pauses the login."""

    account = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="confirmation_failures", editable=False
    )
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [models.Index(fields=("account", "occurred_at"), name="accounts_cf_account_time")]


class RateLimitBucket(models.Model):
    """A pseudonymous lock row for one key (D-037): an HMAC of the scope and the normalised
    address or client IP, never the address or IP itself."""

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


class SecurityEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AccountIntegrityError("Security log entries cannot be changed.")

    def delete(self):
        raise AccountIntegrityError("Security log entries are deleted only by the retention.")

    def bulk_create(self, objs, *args, **kwargs):
        raise AccountIntegrityError("Security log entries are written only with record().")

    def bulk_update(self, *args, **kwargs):
        raise AccountIntegrityError("Security log entries cannot be changed.")

    def _delete_unprotected(self):
        return super().delete()


class SecurityEventManager(models.Manager.from_queryset(SecurityEventQuerySet)):
    def record(self, event, *, account=None, actor=None, long_retention=False):
        """Write one entry; call it in the transaction of the change that it records."""
        return self.create(
            event=event,
            account_id=getattr(account, "pk", account),
            actor_id=getattr(actor, "pk", actor),
            long_retention=long_retention or event in SecurityEvent.LONG_RETENTION,
        )

    def delete_expired(self, now=None):
        """Delete the entries whose retention has passed (D-018); return their number."""
        now = now or timezone.now()
        expired = models.Q(
            long_retention=False,
            occurred_at__lt=now - timedelta(days=settings.SECURITY_LOG_RETENTION_DAYS),
        ) | models.Q(
            long_retention=True,
            occurred_at__lt=now - timedelta(days=settings.SECURITY_LOG_LONG_RETENTION_DAYS),
        )
        deleted, _unused = self.get_queryset().filter(expired)._delete_unprotected()
        return deleted


class SecurityEvent(models.Model):
    """Append-only within the application; entries leave only through the retention (D-018).

    An entry names accounts by their internal number and holds no other personal data.
    """

    class Event(models.TextChoices):
        PASSWORD_CHANGED = "password_changed", _("Password changed")
        PASSWORD_RESET = "password_reset", _("Password reset by link")
        PASSKEYS_CHANGED = "passkeys_changed", _("Passkeys changed")
        SIGN_IN_METHOD_CHANGED = "sign_in_method_changed", _("Sign-in method changed")
        EMAIL_CHANGE_REQUESTED = "email_change_requested", _("Email address change requested")
        EMAIL_CHANGED = "email_changed", _("Email address changed")
        ACCOUNT_BLOCKED = "account_blocked", _("Account blocked")
        ACCOUNT_UNBLOCKED = "account_unblocked", _("Account unblocked")
        ACCOUNT_DEACTIVATED = "account_deactivated", _("Account deleted (retention obligation)")
        ACCOUNT_REACTIVATED = "account_reactivated", _("Account restored")
        ACCOUNT_DELETED = "account_deleted", _("Account deleted")
        TWO_FACTOR_ENABLED = "two_factor_enabled", _("Two-factor authentication set up")
        HOUSEKEEPING_RUN = "housekeeping_run", _("Housekeeping of the databases")
        LOG_NOTIFICATION_CHANGED = "log_notification_changed", _("Log notification changed")
        HOUSEKEEPING_ERRORS_ACKNOWLEDGED = "housekeeping_errors_acknowledged", _("Housekeeping errors acknowledged")
        TWO_FACTOR_DISABLED = "two_factor_disabled", _(
            "Two-factor authentication removed by the account itself"
        )
        TWO_FACTOR_RESET = "two_factor_reset", _(
            "Two-factor authentication reset by link or by a system administrator"
        )
        TWO_FACTOR_RESET_REQUESTED = "two_factor_reset_requested", _(
            "Link for removing two-factor authentication requested"
        )
        RECOVERY_CODES_RENEWED = "recovery_codes_renewed", _("Recovery codes renewed")
        RECOVERY_CODE_USED = "recovery_code_used", _("Recovery code used")
        CODE_ATTEMPTS_WARNING = "code_attempts_warning", _("Many wrong codes entered")
        CODE_ATTEMPTS_PAUSED = "code_attempts_paused", _("Code entry paused")
        LOGIN_LOCKOUT = "login_lockout", _("Account locked after failed logins")
        PRIVILEGES_CHANGED = "privileges_changed", _("Access to the administration changed")
        ACCESS_RELATIONS_CHANGED = "access_relations_changed", _("Roles or permissions changed")
        INITIAL_ADMINISTRATOR_CREATED = "initial_administrator_created", _(
            "Initial system administrator created"
        )
        EMERGENCY_TWO_FACTOR_RESET = "emergency_two_factor_reset", _(
            "Two-factor authentication reset by the emergency command"
        )
        EMERGENCY_ACCOUNT_UNBLOCKED = "emergency_account_unblocked", _(
            "Account unblocked by the emergency command"
        )
        EMERGENCY_KEYS_REENCRYPTED = "emergency_keys_reencrypted", _(
            "Second factor keys encrypted again by the emergency command"
        )
        GRAPH_CONFIGURATION_CHANGED = "graph_configuration_changed", _(
            "Microsoft Graph configuration changed"
        )
        GRAPH_CERTIFICATE_UPLOADED = "graph_certificate_uploaded", _(
            "Microsoft Graph certificate uploaded"
        )
        GRAPH_TEST_EMAIL_REQUESTED = "graph_test_email_requested", _(
            "Microsoft Graph test email requested"
        )
        PORTAL_SETTINGS_CHANGED = "portal_settings_changed", _("Portal settings changed")
        PORTAL_UPLOAD_REJECTED = "portal_upload_rejected", _("Portal upload rejected")
        EMAIL_CHANGE_CANCELLED = "email_change_cancelled", _(
            "Requested email address change cancelled by the account"
        )
        EMAIL_CHANGE_STOPPED = "email_change_stopped", _(
            "Requested email address change stopped with the link of the notice"
        )
        SECONDARY_EMAIL_CHANGED = "secondary_email_changed", _("Second email address changed")
        REGISTRATION_COMPLETED = "registration_completed", _("Registration completed")
        SESSION_ENDED_WRONG_CODES = "session_ended_wrong_codes", _(
            "Session ended after wrong codes"
        )
        ALL_SESSIONS_ENDED = "all_sessions_ended", _("All sessions ended after wrong entries")
        TWO_FACTOR_REMOVED_PHONE_LOST = "two_factor_removed_phone_lost", _(
            "Two-factor authentication removed with a recovery code (phone lost)"
        )
        ADDRESS_IMPORT_REQUESTED = "address_import_requested", _(
            "Address directory import requested"
        )
        RETENTION_OBLIGATION_SET = "retention_obligation_set", _("Retention obligation set")
        LIST_PRINTED = "list_printed", _("List printed")
        PERSON_INVITED = "person_invited", _("Person created and invited by staff")
        INVITATION_RESENT = "invitation_resent", _("Invitation sent again by staff")
        ACCOUNT_DATA_CHANGED_BY_STAFF = "account_data_changed_by_staff", _("Account data changed by staff")
        GRID_OPERATOR_CHANGED = "grid_operator_changed", _("Grid operator changed")
        ADDRESS_IMPORT_SUCCEEDED = "address_import_succeeded", _("Address directory import completed")
        ADDRESS_IMPORT_FAILED = "address_import_failed", _("Address directory import failed")
        # The plant register import (D-117): one event per outcome, the reason in the detail (fixed words).
        PV_IMPORT_SUCCEEDED = "pv_import_succeeded", _("Plant register import completed")
        PV_IMPORT_FAILED = "pv_import_failed", _("Plant register import failed")
        # The transformer station scan (D-118): one event per outcome, the count or the fixed reason in the detail.
        TRANSFORMER_SCAN_SUCCEEDED = "transformer_scan_succeeded", _("Transformer station scan completed")
        TRANSFORMER_SCAN_FAILED = "transformer_scan_failed", _("Transformer station scan failed")
        # The postal codes of the service area (D-118): the codes only, in the log line; no other text of the input.
        SERVICE_AREA_CHANGED = "service_area_changed", _("Postal codes of the service area changed")
        LOG_VIEWED = "log_viewed", _("Log file viewed")
        LOG_DOWNLOADED = "log_downloaded", _("Log file downloaded")
        PERSISTENT_SESSION_STARTED = "persistent_session_started", _(
            "Long session (stay signed in) started for a staff or administrator account"
        )
        ACCOUNT_SESSIONS_ENDED_BY_ADMIN = "account_sessions_ended_by_admin", _(
            "All sessions of an account ended by a system administrator"
        )
        MANAGEMENT_DATA_EDITED = "management_data_edited", _("Management data edited in the administration")
        IBAN_CHANGED = "iban_changed", _("IBAN changed by the account")
        CONTRACT_VERSION_CHANGED = "contract_version_changed", _("Contract version uploaded, made current or deleted")

    # Kept for the long period: misuse is noticed late (D-018, D-006, D-056, D-084, D-087, D-092).
    LONG_RETENTION = frozenset(
        {
            Event.IBAN_CHANGED,
            Event.CONTRACT_VERSION_CHANGED,
            Event.PRIVILEGES_CHANGED,
            Event.ACCESS_RELATIONS_CHANGED,
            Event.INITIAL_ADMINISTRATOR_CREATED,
            Event.EMERGENCY_TWO_FACTOR_RESET,
            Event.EMERGENCY_ACCOUNT_UNBLOCKED,
            Event.EMERGENCY_KEYS_REENCRYPTED,
            Event.GRAPH_CONFIGURATION_CHANGED,
            Event.GRAPH_CERTIFICATE_UPLOADED,
            Event.PORTAL_SETTINGS_CHANGED,
            Event.TWO_FACTOR_REMOVED_PHONE_LOST,
            Event.LIST_PRINTED,
            Event.ACCOUNT_DATA_CHANGED_BY_STAFF,
            Event.GRID_OPERATOR_CHANGED,
            Event.LOG_VIEWED,
            Event.LOG_DOWNLOADED,
            Event.PERSISTENT_SESSION_STARTED,
            Event.ACCOUNT_SESSIONS_ENDED_BY_ADMIN,
            Event.MANAGEMENT_DATA_EDITED,
        }
    )

    occurred_at = models.DateTimeField(_("time"), default=timezone.now, db_index=True, editable=False)
    event = models.CharField(_("event"), max_length=40, choices=Event, editable=False)
    # Internal numbers, not foreign keys: the entries outlive the accounts (D-018, D-036).
    account_id = models.PositiveBigIntegerField(_("account"), null=True, editable=False)
    actor_id = models.PositiveBigIntegerField(_("performed by"), null=True, editable=False)
    long_retention = models.BooleanField(
        _("kept for the long period"), default=False, editable=False
    )

    objects = SecurityEventManager()

    class Meta:
        base_manager_name = "objects"
        default_permissions = ()
        ordering = ("-occurred_at", "-id")
        verbose_name = _("security log entry")
        verbose_name_plural = _("security log")

    def save(self, **kwargs):
        if not self._state.adding or kwargs.get("update_fields") is not None:
            raise AccountIntegrityError("Security log entries cannot be changed.")
        kwargs["force_insert"] = True
        super().save(**kwargs)

    def delete(self, *args, **kwargs):
        raise AccountIntegrityError("Security log entries are deleted only by the retention.")
