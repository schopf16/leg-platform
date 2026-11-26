"""The FTPES access of the instance to the Swisseldex Datahub (D-139). One row (id 1), system administrators only."""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from leg_platform.accounts.encryption import decrypt, encrypt, is_current, reencrypt

from .eic import validate_eic

# HKDF label of the password; one label per purpose (docs/SECURITY.md, section 7; D-006, D-040).
PASSWORD_PURPOSE = b"LEG-Platform Swisseldex Datahub FTPES password v1"
DEFAULT_HOST = "datahub.swisseldex.ch"
DEFAULT_PORT = 21
PASSWORD_MAX_LENGTH = 128

HOST_NAME = re.compile(r"(?=.{1,253}\Z)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
# FTP commands end at CR LF: a line break or another control character in a value could add a command.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def validate_host_name(value):
    if not HOST_NAME.fullmatch(value or ""):
        raise ValidationError(
            _("Enter the host name only, for example datahub.swisseldex.ch (no ftpes://, no path, no port)."),
            code="invalid_host",
        )


def validate_no_control(value):
    if CONTROL.search(value or ""):
        raise ValidationError(_("The value must not contain line breaks or control characters."), code="control")


class DatahubConfiguration(models.Model):
    """The connection data of the instance. The password is write-only and stored encrypted."""

    class TestOutcome(models.TextChoices):
        # Fixed codes: the security log and the page never show a text of the server (D-139).
        SUCCEEDED = "succeeded", _("Connection succeeded")
        INCOMPLETE = "incomplete", _("Configuration incomplete")
        PASSWORD_UNREADABLE = "password_unreadable", _("Stored password cannot be read")
        CONNECTION_FAILED = "connection_failed", _("Server not reachable")
        TLS_FAILED = "tls_failed", _("Encrypted connection failed (certificate or TLS)")
        LOGIN_FAILED = "login_failed", _("Login refused")
        DIRECTORY_FAILED = "directory_failed", _("Directory not found")
        LISTING_FAILED = "listing_failed", _("Directory could not be listed")
        TIMED_OUT = "timed_out", _("No answer in time")
        FAILED = "failed", _("Other error")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    host = models.CharField(
        _("FTPES server"),
        max_length=253,
        default=DEFAULT_HOST,
        validators=[validate_host_name],
        help_text=_("The host name of the Datahub, without ftpes://."),
    )
    port = models.PositiveIntegerField(
        _("port"),
        default=DEFAULT_PORT,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
        help_text=_("Explicit TLS (FTPES) on the control port; 21 according to the Datahub manual."),
    )
    username = models.CharField(
        _("FTPES user name"),
        max_length=150,
        blank=True,
        validators=[validate_no_control],
    )
    # Write-only: never shown, never in a form, never in a raw table (``community.raw_tables.NEVER_EDITABLE``).
    encrypted_password = models.TextField(blank=True, editable=False)
    directory = models.CharField(
        _("directory on the server"),
        max_length=255,
        blank=True,
        validators=[validate_no_control],
        help_text=_(
            "Empty: the start directory after the login. The Datahub keeps all messages in the main directory."
        ),
    )
    own_name = models.CharField(
        _("own market partner name"),
        max_length=100,
        blank=True,
        validators=[validate_no_control],
        help_text=_("The name of the LEG as registered on the Datahub."),
    )
    own_eic = models.CharField(
        _("own EIC"),
        max_length=16,
        blank=True,
        validators=[validate_eic],
        help_text=_("The Energy Identification Code of the LEG on the Datahub (16 characters)."),
    )
    grid_operator_eic = models.CharField(
        _("EIC of the distribution system operator"),
        max_length=16,
        blank=True,
        validators=[validate_eic],
        help_text=_("The Energy Identification Code of the grid operator that sends the load profiles."),
    )
    collection_enabled = models.BooleanField(
        _("collection active"),
        default=False,
        help_text=_(
            "Only one server may collect: collected messages are later deleted on the Datahub. "
            "It can be switched on only where the server configuration allows it."
        ),
    )
    last_test_at = models.DateTimeField(_("last connection test"), null=True, editable=False)
    last_test_outcome = models.CharField(
        _("result of the last connection test"), max_length=30, choices=TestOutcome, blank=True, editable=False
    )
    last_test_file_count = models.PositiveIntegerField(_("files found"), null=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        default_permissions = ()
        verbose_name = _("Swisseldex Datahub")
        verbose_name_plural = _("Swisseldex Datahub")
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="datahub_configuration_singleton"),
        ]

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        configuration, _created = cls.objects.get_or_create(id=1)
        return configuration

    @property
    def has_password(self):
        return bool(self.encrypted_password)

    @property
    def is_complete(self):
        """Everything a login needs."""
        return bool(self.host and self.port and self.username and self.encrypted_password)

    @property
    def collection_active(self):
        """Whether this server may collect now: the switch AND the server configuration (D-139).

        A database copied from another server keeps the switch; the server configuration of this server decides.
        Every later collection checks this property, never the switch alone.
        """
        return bool(self.collection_enabled and settings.DATAHUB_COLLECTION_ALLOWED and self.is_complete)

    @sensitive_variables("password")
    def set_password(self, password):
        """Store the password encrypted; an empty value removes it. The caller saves the row."""
        self.encrypted_password = encrypt(password.encode("utf-8"), PASSWORD_PURPOSE) if password else ""

    @sensitive_variables()
    def password(self):
        """The password as text; one under an earlier SECRET_KEY is encrypted again with the current key (D-040).

        ``DecryptionError`` when no key of the instance decrypts it.
        """
        value = decrypt(self.encrypted_password, PASSWORD_PURPOSE).decode("utf-8")
        if not is_current(self.encrypted_password, PASSWORD_PURPOSE):
            self.encrypted_password = reencrypt(self.encrypted_password, PASSWORD_PURPOSE)
            type(self).objects.filter(pk=self.pk).update(encrypted_password=self.encrypted_password)
        return value
