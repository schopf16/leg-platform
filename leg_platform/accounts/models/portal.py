"""The portal settings of the administrator and the hidden task tiles of a person."""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models.common import (
    EXACT_COLLATION,
    validate_hex_colour,
    validate_https_address,
    validate_instance_name,
)
from leg_platform.accounts.models.user import User
from leg_platform.environment import INSTANCE_NAME_MAX_LENGTH


def validate_instance_origin(value):
    """Retired with the base address (D-116 amendment). No field uses it; it only keeps migration 0041 loadable, which
    names it. It refuses every value, so that it could never accept an address by mistake."""
    raise ValidationError("This address is no longer stored in the portal settings.", code="retired")


class PortalSettings(models.Model):
    """The portal settings that the administrator maintains. The table holds exactly one row (id 1).

    Name, website, privacy notice, legal notice, the two logos, the favicon and the colours (D-073). An empty
    text or colour means "use the server configuration": the precedence is decided per field by
    ``leg_platform.accounts.portal_settings.portal_values``, the one accessor. The logos and the favicon are
    stored in the table (validated file, type, SHA-256 digest), written only by the form of the
    administration (``logo_images``) and served by ``presentation.instance_logo`` and ``instance_favicon``;
    queries that do not need the files must not load them (``LOGO_DATA_FIELDS``).
    """

    class LogoType(models.TextChoices):
        PNG = "png", "PNG"
        SVG = "svg", "SVG"

    LOGO_DATA_FIELDS = ("logo_light_data", "logo_dark_data", "favicon_data")
    COLOUR_FIELDS = ("accent_light", "text_light", "accent_dark", "text_dark", "highlight")

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    name = models.CharField(
        _("name of the portal"),
        max_length=INSTANCE_NAME_MAX_LENGTH,
        blank=True,
        validators=[validate_instance_name],
        help_text=_(
            "Shown in the header, in page titles, in the subject of emails and in the "
            "administration. At most 100 characters. Leave empty to use the name from the "
            "server configuration."
        ),
    )
    website_url = models.URLField(
        _("address of the website"),
        max_length=500,
        blank=True,
        validators=[validate_https_address],
        help_text=_(
            "The website of your community with the contact information. It is linked in the "
            "footer and used as the contact address in emails. It must start with https://. "
            "Leave empty to use the server configuration."
        ),
    )
    privacy_url = models.URLField(
        _("address of the privacy notice"),
        max_length=500,
        blank=True,
        validators=[validate_https_address],
        help_text=_(
            "Linked in the footer, at the registration and when deleting the account. "
            "Leave empty to show the text without a link."
        ),
    )
    imprint_url = models.URLField(
        _("address of the legal notice"),
        max_length=500,
        blank=True,
        validators=[validate_https_address],
        help_text=_(
            "Linked in the footer. Leave empty to show the text without a link."
        ),
    )
    accent_light = models.CharField(
        _("accent colour, light mode"), max_length=7, blank=True, validators=[validate_hex_colour]
    )
    text_light = models.CharField(
        _("text colour on the accent colour, light mode"),
        max_length=7,
        blank=True,
        validators=[validate_hex_colour],
        help_text=_("Leave empty to choose white or dark text automatically."),
    )
    accent_dark = models.CharField(
        _("accent colour, dark mode"), max_length=7, blank=True, validators=[validate_hex_colour]
    )
    text_dark = models.CharField(
        _("text colour on the accent colour, dark mode"),
        max_length=7,
        blank=True,
        validators=[validate_hex_colour],
        help_text=_("Leave empty to choose white or dark text automatically."),
    )
    highlight = models.CharField(
        _("highlight colour"),
        max_length=7,
        blank=True,
        validators=[validate_hex_colour],
        help_text=_(
            "Used only for the small dots in the lists of the login page, never for text."
        ),
    )
    logo_light_data = models.BinaryField(null=True, editable=False)
    logo_light_type = models.CharField(max_length=3, choices=LogoType, blank=True, editable=False)
    logo_light_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    logo_dark_data = models.BinaryField(null=True, editable=False)
    logo_dark_type = models.CharField(max_length=3, choices=LogoType, blank=True, editable=False)
    logo_dark_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    # The browser tab icon: the same storage and checks as a logo, one file for both colour modes.
    favicon_data = models.BinaryField(null=True, editable=False)
    favicon_type = models.CharField(max_length=3, choices=LogoType, blank=True, editable=False)
    favicon_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    class Meta:
        # View and change are assignable to a role at runtime (D-012, D-073); no add, no delete.
        default_permissions = ("view", "change")
        verbose_name = _("portal settings")
        verbose_name_plural = _("portal settings")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1), name="accounts_portal_settings_singleton"
            )
        ]

    def __str__(self):
        return str(self._meta.verbose_name)

    @classmethod
    def load(cls):
        settings_row, _created = cls.objects.get_or_create(id=1)
        return settings_row

    @classmethod
    def load_without_logo_files(cls):
        """The row without the logo files; saving such an instance writes only loaded columns."""
        settings_row, _created = cls.objects.defer(*cls.LOGO_DATA_FIELDS).get_or_create(id=1)
        return settings_row


class HiddenTask(models.Model):
    """A task tile that a person hid on the start page (D-057); final, stored per account."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="hidden_tasks", editable=False
    )
    task_key = models.CharField(max_length=32, db_collation=EXACT_COLLATION, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["user", "task_key"], name="accounts_hidden_task_unique")
        ]

    def __str__(self):
        return self.task_key
