"""Email delivery: the mail outbox, the editable email texts and the E-Mail chapter (Microsoft Graph settings,
sender and contact address; D-006, D-116)."""

from datetime import timedelta

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from leg_platform.accounts.encryption import decrypt, encrypt, is_current, reencrypt
from leg_platform.accounts.models.common import EMAIL_MAX_LENGTH, validate_sender_address
from leg_platform.accounts.models.user import User

# The longest cleaned greeting name a job may hold: first name (30) and last name (30) with one
# space need 61; a longer result gives the neutral greeting (D-068).
GREETING_NAME_MAX_LENGTH = 64


class MailJob(models.Model):
    """A request for an email that a background worker processes (outbox, D-037).

    The request path only stores the job, so it does the same work for every address. The
    address of a person without an account is stored only until the job is processed.
    """

    class Kind(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"
        EMAIL_CHANGE = "email_change", "Email address change"
        EMAIL_CHANGE_REQUEST = "email_change_request", "Email address change request"
        PASSWORD_CHANGED = "password_changed", "Password changed"
        SIGN_IN_CHANGED = "sign_in_changed", "Sign-in method or passkeys changed"
        METHOD_RETURN = "method_return", "Confirm sign-in method return"
        RECOVERY_GUIDANCE = "recovery_guidance", "Sign-in recovery instructions"
        EMAIL_CHANGED = "email_changed", "Email address changed"
        EMAIL_CHANGE_EXISTING_ACCOUNT = "email_change_existing_account", (
            "Email change: target address already has an account"
        )
        TWO_FACTOR_ENABLED = "two_factor_enabled", "Two-factor authentication set up"
        TWO_FACTOR_DISABLED = "two_factor_disabled", "Two-factor authentication removed"
        RECOVERY_CODES_RENEWED = "recovery_codes_renewed", "Recovery codes renewed"
        RECOVERY_CODE_USED = "recovery_code_used", "Recovery code used"
        CODE_ATTEMPTS_WARNING = "code_attempts_warning", "Many wrong codes"
        TWO_FACTOR_RESET_LINK = "two_factor_reset_link", "Link to reset two-factor authentication"
        TWO_FACTOR_RESET = "two_factor_reset", "Two-factor authentication reset"
        SECONDARY_EMAIL_REQUEST = "secondary_email_request", "Second email address request"
        EMAIL_CHANGE_NOTICE = "email_change_notice", "Email address change requested"
        ACCOUNT_DELETED = "account_deleted", "Account deleted"
        SECONDARY_EMAIL_CHANGED = "secondary_email_changed", "Second email address changed"
        SESSION_ENDED_WRONG_CODES = "session_ended_wrong_codes", "Session ended after wrong codes"
        ALL_SESSIONS_ENDED = "all_sessions_ended", "All sessions ended after wrong entries"
        INVITATION = "invitation", "Invitation"
        # Onboarding of customers (D-103): notifications to the addresses of an account.
        APPLICATION_SUBMITTED = "application_submitted", "Application submitted"
        APPLICATION_REMINDER = "application_reminder", "Application not completed"
        APPLICATION_ROLLED_BACK = "application_rolled_back", "Application rolled back"
        CONTRACT_REMINDER = "contract_reminder", "Contract not accepted"
        CONTRACT_ROLLED_BACK = "contract_rolled_back", "Application rolled back: contract"
        BANK_CONNECTION_MISSING = "bank_connection_missing", "Bank connection missing"
        DEREGISTRATION_SUBMITTED = "deregistration_submitted", "Deregistration submitted"
        DEREGISTRATION_EFFECTIVE = "deregistration_effective", "Deregistration effective"
        IBAN_CHANGED = "iban_changed", "IBAN changed"
        OPERATOR_CONFIRMATION_LAPSED = "operator_confirmation_lapsed", "Confirmation by the grid operator missing"
        APPLICATION_RETURNED = "application_returned", "Application returned for correction"
        APPLICATION_REJECTED = "application_rejected", "Application not accepted"
        APPLICATION_REJECTED_OPERATOR = "application_rejected_operator", "Application not accepted by the grid operator"

    # Notifications about a change of the account, as opposed to the requests of a person who
    # may have no account (D-038, D-040).
    NOTIFICATION_KINDS = (
        Kind.SIGN_IN_CHANGED,
        Kind.METHOD_RETURN,
        Kind.PASSWORD_CHANGED,
        Kind.EMAIL_CHANGED,
        Kind.SECONDARY_EMAIL_CHANGED,
        Kind.SESSION_ENDED_WRONG_CODES,
        Kind.ALL_SESSIONS_ENDED,
        Kind.TWO_FACTOR_ENABLED,
        Kind.TWO_FACTOR_DISABLED,
        Kind.RECOVERY_CODES_RENEWED,
        Kind.RECOVERY_CODE_USED,
        Kind.CODE_ATTEMPTS_WARNING,
        Kind.TWO_FACTOR_RESET_LINK,
        Kind.TWO_FACTOR_RESET,
        Kind.EMAIL_CHANGE_NOTICE,
        Kind.ACCOUNT_DELETED,
        Kind.APPLICATION_SUBMITTED,
        Kind.APPLICATION_REMINDER,
        Kind.APPLICATION_ROLLED_BACK,
        Kind.CONTRACT_REMINDER,
        Kind.CONTRACT_ROLLED_BACK,
        Kind.BANK_CONNECTION_MISSING,
        Kind.DEREGISTRATION_SUBMITTED,
        Kind.DEREGISTRATION_EFFECTIVE,
        Kind.IBAN_CHANGED,
        Kind.OPERATOR_CONFIRMATION_LAPSED,
        Kind.APPLICATION_RETURNED,
        Kind.APPLICATION_REJECTED,
        Kind.APPLICATION_REJECTED_OPERATOR,
    )

    kind = models.CharField(max_length=32, choices=Kind, editable=False)
    # Empty for a suppressed job: it is discarded unseen.
    address = models.EmailField(max_length=EMAIL_MAX_LENGTH, blank=True, editable=False)
    # Set for a job about an existing account: the worker finds the account by its key, not by
    # the address, which may have changed since the request (D-044).
    account = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        editable=False,
        related_name="mail_jobs",
    )
    # Cleaned name for the greeting of a notification (D-068); empty means the neutral greeting.
    # Personal data: it lives and dies with the job. Never logged.
    greeting_name = models.CharField(
        max_length=GREETING_NAME_MAX_LENGTH, blank=True, default="", editable=False
    )
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

    One row per email, created empty; an empty subject or body means the default text. Plain
    text with ``{placeholder}`` markers, validated when saved.
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
        SIGN_IN_CHANGED = "sign_in_changed", _("Notification: sign-in method or passkeys changed")
        METHOD_RETURN = "method_return", _("Sign-in method: confirmation link")
        RECOVERY_GUIDANCE = "recovery_guidance", _("Sign-in recovery instructions")
        EMAIL_CHANGE_VERIFICATION = "email_change_verification", _(
            "Email address change: link to confirm the new address"
        )
        EMAIL_CHANGE_EXISTING_ACCOUNT = "email_change_existing_account", _(
            "Email address change: the new address already has an account"
        )
        EMAIL_CHANGED = "email_changed", _("Notification: email address changed")
        SECONDARY_EMAIL_CHANGED = "secondary_email_changed", _(
            "Notification: second email address changed"
        )
        TWO_FACTOR_ENABLED = "two_factor_enabled", _(
            "Notification: two-factor authentication set up"
        )
        TWO_FACTOR_DISABLED = "two_factor_disabled", _(
            "Notification: two-factor authentication turned off"
        )
        RECOVERY_CODES_RENEWED = "recovery_codes_renewed", _(
            "Notification: recovery codes renewed"
        )
        RECOVERY_CODE_USED = "recovery_code_used", _("Notification: recovery code used")
        CODE_ATTEMPTS_WARNING = "code_attempts_warning", _(
            "Notification: many wrong codes entered"
        )
        TWO_FACTOR_RESET_LINK = "two_factor_reset_link", _(
            "Lost second factor: link to remove it"
        )
        TWO_FACTOR_RESET = "two_factor_reset", _(
            "Notification: two-factor authentication reset"
        )
        SECONDARY_EMAIL_VERIFICATION = "secondary_email_verification", _(
            "Second email address: link to confirm the address"
        )
        EMAIL_CHANGE_NOTICE = "email_change_notice", _(
            "Notification: email address change requested (with the link to stop it)"
        )
        ACCOUNT_DELETED = "account_deleted", _("Notification: account deleted")
        SESSION_ENDED_WRONG_CODES = "session_ended_wrong_codes", _(
            "Notification: session ended after wrong codes"
        )
        ALL_SESSIONS_ENDED = "all_sessions_ended", _(
            "Notification: all sessions ended after wrong entries"
        )
        INVITATION = "invitation", _("Invitation: link to confirm the address and choose a password")
        APPLICATION_SUBMITTED = "application_submitted", _("Application: confirmation of the submission")
        APPLICATION_REMINDER = "application_reminder", _("Application: reminder, not completed yet")
        APPLICATION_ROLLED_BACK = "application_rolled_back", _("Application: rolled back")
        CONTRACT_REMINDER = "contract_reminder", _("Application: reminder, contract not accepted yet")
        CONTRACT_ROLLED_BACK = "contract_rolled_back", _(
            "Application: rolled back because the contract was not accepted"
        )
        BANK_CONNECTION_MISSING = "bank_connection_missing", _("Application: bank connection missing")
        DEREGISTRATION_SUBMITTED = "deregistration_submitted", _("Deregistration: confirmation of the request")
        DEREGISTRATION_EFFECTIVE = "deregistration_effective", _("Deregistration: has taken effect")
        IBAN_CHANGED = "iban_changed", _("Notification: IBAN changed")
        OPERATOR_CONFIRMATION_LAPSED = "operator_confirmation_lapsed", _(
            "Staff: confirmation by the grid operator is missing, withdraw the registration"
        )
        APPLICATION_RETURNED = "application_returned", _("Application: returned for correction")
        APPLICATION_REJECTED = "application_rejected", _("Application: not accepted by the LEG")
        APPLICATION_REJECTED_OPERATOR = "application_rejected_operator", _(
            "Application: not accepted by the grid operator"
        )

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
    """A saved state of an email text, so that an earlier text can be restored (D-039).

    The account that made the change is kept for the audit, not shown, and cleared when the
    account is deleted.
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


# Microsoft Graph (D-006): the application authenticates with a certificate (no client secret).
# The private key is encrypted like the other stored secrets, never shown again and only
# decrypted to request a token.
GRAPH_CERTIFICATE_KEY_PURPOSE = b"LEG-Platform Microsoft Graph certificate key v1"
GRAPH_MIN_RSA_KEY_BITS = 2048
# A certificate that expires within this many days is shown as expiring (a project decision).
GRAPH_CERTIFICATE_WARNING_DAYS = 30
CERTIFICATE_NONE = "none"
CERTIFICATE_VALID = "valid"
CERTIFICATE_EXPIRING = "expiring"
CERTIFICATE_EXPIRED = "expired"


class GraphConfiguration(models.Model):
    """The E-Mail chapter of the administration: the Microsoft Graph settings, the sender and the contact address
    (D-116). The table holds exactly one row (id 1). The sender is the Graph mailbox: the Graph backend refuses any
    other sender, so the one value serves both (``leg_platform.accounts.mail_addresses``)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    tenant_id = models.UUIDField(_("tenant ID"), null=True, blank=True)
    client_id = models.UUIDField(_("client ID"), null=True, blank=True)
    sender_address = models.EmailField(
        _("sender address"),
        max_length=254,
        blank=True,
        validators=[validate_sender_address],
        help_text=_(
            "The address all emails are sent from. It is the mailbox of the Microsoft Graph application. Without it "
            "no email is sent. Use an address of the instance that does not invite a reply, for example "
            "noreply@example.org."
        ),
    )
    contact_address = models.EmailField(
        _("address for the messages of the contact form"),
        max_length=254,
        blank=True,
        validators=[validate_sender_address],
        help_text=_(
            "The LEG receives the messages of the contact form here. Without it the contact form is not offered."
        ),
    )
    certificate_pem = models.TextField(blank=True, editable=False)
    encrypted_private_key = models.TextField(blank=True, editable=False)
    certificate_expires_on = models.DateTimeField(null=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Email")
        verbose_name_plural = _("Email")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="accounts_graph_configuration_singleton"
            )
        ]

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        configuration, _created = cls.objects.get_or_create(id=1)
        return configuration

    @property
    def is_configured(self):
        return bool(
            self.tenant_id
            and self.client_id
            and self.sender_address
            and self.certificate_pem
            and self.encrypted_private_key
        )

    @staticmethod
    @sensitive_variables("private_key_pem")
    def check_certificate(private_key_pem, certificate_pem):
        """Check a certificate and its private key (PEM bytes) and return the expiry time.

        The key must be RSA with at least GRAPH_MIN_RSA_KEY_BITS, belong to the certificate, and
        the certificate must not have expired; otherwise ``ValidationError``.
        """
        # Non-ASCII input is not PEM; this keeps set_certificate() and private_key_pem() from
        # failing on the text conversion.
        if not private_key_pem.isascii() or not certificate_pem.isascii():
            raise ValidationError(
                _("The certificate or the private key cannot be read (PEM, no password).")
            )
        try:
            private_key = serialization.load_pem_private_key(private_key_pem, password=None)
            certificate = x509.load_pem_x509_certificate(certificate_pem)
            certificate_key = certificate.public_key()
        except (TypeError, ValueError, UnsupportedAlgorithm):
            raise ValidationError(
                _("The certificate or the private key cannot be read (PEM, no password).")
            ) from None
        if (
            not isinstance(private_key, rsa.RSAPrivateKey)
            or private_key.key_size < GRAPH_MIN_RSA_KEY_BITS
        ):
            raise ValidationError(
                _("The private key must be an RSA key with at least 2048 bits.")
            )
        if certificate_key.public_numbers() != private_key.public_key().public_numbers():
            raise ValidationError(_("The private key does not belong to the certificate."))
        expires_on = certificate.not_valid_after_utc
        if expires_on <= timezone.now():
            raise ValidationError(_("The certificate has expired."))
        return expires_on

    @sensitive_variables("private_key_pem")
    def set_certificate(self, private_key_pem, certificate_pem):
        """Store a certificate and its private key (PEM bytes) after ``check_certificate``."""
        expires_on = self.check_certificate(private_key_pem, certificate_pem)
        self.certificate_pem = certificate_pem.decode("ascii")
        self.encrypted_private_key = encrypt(private_key_pem, GRAPH_CERTIFICATE_KEY_PURPOSE)
        self.certificate_expires_on = expires_on
        self.save()

    def certificate_status(self, now=None):
        """Return CERTIFICATE_NONE, CERTIFICATE_VALID, CERTIFICATE_EXPIRING or CERTIFICATE_EXPIRED."""
        if self.certificate_expires_on is None:
            return CERTIFICATE_NONE
        now = now or timezone.now()
        if self.certificate_expires_on <= now:
            return CERTIFICATE_EXPIRED
        if self.certificate_expires_on - now <= timedelta(days=GRAPH_CERTIFICATE_WARNING_DAYS):
            return CERTIFICATE_EXPIRING
        return CERTIFICATE_VALID

    def certificate_details(self):
        """The subject and the SHA-1 and SHA-256 thumbprints of the stored public certificate."""
        certificate = x509.load_pem_x509_certificate(self.certificate_pem.encode("ascii"))
        return {
            "subject": certificate.subject.rfc4514_string(),
            "sha1": certificate.fingerprint(hashes.SHA1()).hex().upper(),
            "sha256": certificate.fingerprint(hashes.SHA256()).hex().upper(),
        }

    @sensitive_variables()
    def private_key_pem(self):
        """Return the private key as PEM bytes; a key encrypted with an earlier SECRET_KEY is
        encrypted again with the current one (D-040). ``DecryptionError`` if no key decrypts it."""
        pem = decrypt(self.encrypted_private_key, GRAPH_CERTIFICATE_KEY_PURPOSE)
        if not is_current(self.encrypted_private_key, GRAPH_CERTIFICATE_KEY_PURPOSE):
            self.encrypted_private_key = reencrypt(
                self.encrypted_private_key, GRAPH_CERTIFICATE_KEY_PURPOSE
            )
            self.save(update_fields=["encrypted_private_key"])
        return pem
