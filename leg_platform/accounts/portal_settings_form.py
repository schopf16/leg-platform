"""The form of the portal settings in the administration (D-073).

Name, website, legal notice addresses, colours, the two logos and the favicon. The logos and the favicon are uploads checked by
``logo_images``; files live in the database and the upload's name is never used.
"""

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.html import format_html
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor, security_log
from leg_platform.accounts.logo_images import (
    FAVICON_MAX_UPLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    LogoRejected,
    validate_favicon,
    validate_logo,
)
from leg_platform.accounts.models import PortalSettings, SecurityEvent, validate_hex_colour
from leg_platform.accounts.portal_settings import effective_colours, environment_values
from leg_platform.branding import COLOUR_FIELDS, colour_problems, floor_ratio

LOGOS = ("light", "dark")

ONLY_ONE_LOGO = _(
    "Upload both logos or none. The portal needs one version for light and one for dark mode."
)
REMOVE_AND_UPLOAD = _(
    "Choose either new logos or the removal of the uploaded logos, not both."
)
REMOVE_FAVICON_AND_UPLOAD = _(
    "Choose either a new icon or the removal of the uploaded icon, not both."
)
FAVICON_HELP = _(
    "PNG or SVG, up to 128 KB (PNG) or 64 KB (SVG). A PNG should be square and at least 192 px wide. "
    "It is shown in the tab of the browser, for light and dark mode alike. "
    "Leave empty to use the built-in icon."
)
ACCENT_TOO_WEAK = {
    "light": _(
        "This accent colour is too weak on the light background (contrast %(ratio)s:1, "
        "required 4.5:1). Choose a darker colour."
    ),
    "dark": _(
        "This accent colour is too weak on the dark background (contrast %(ratio)s:1, "
        "required 4.5:1). Choose a lighter colour."
    ),
}
TEXT_TOO_WEAK = _(
    "The text colour on the accent colour is too weak (contrast %(ratio)s:1, required 4.5:1)."
)
HIGHLIGHT_TOO_WEAK = _(
    "The highlight colour is too weak with dark text (contrast %(ratio)s:1, required 4.5:1)."
)
LOGO_HELP = {
    "light": _(
        "PNG or SVG, up to 1 MB (PNG) or 200 KB (SVG). A PNG should be at least 720 px wide. "
        "Use the version with dark lettering."
    ),
    "dark": _(
        "PNG or SVG, up to 1 MB (PNG) or 200 KB (SVG). A PNG should be at least 720 px wide. "
        "Use the version with light lettering."
    ),
}


def colour_message(problem):
    """The message for a failed colour rule (``branding.ColourProblem``) of the form."""
    ratio = f"{floor_ratio(problem.ratio):.2f}" if problem.ratio is not None else ""
    if problem.code == "accent_contrast":
        mode = "dark" if problem.field.endswith("_dark") else "light"
        return ACCENT_TOO_WEAK[mode] % {"ratio": ratio}
    if problem.code == "text_contrast":
        return TEXT_TOO_WEAK % {"ratio": ratio}
    return HIGHLIGHT_TOO_WEAK % {"ratio": ratio}


class PortalSettingsForm(forms.ModelForm):
    # Set by ``PortalSettingsAdmin.get_form``: a rejected upload is a security event (D-073). None outside the
    # administration.
    actor = None
    request = None

    logo_light_file = forms.FileField(
        required=False,
        label=_("logo for light mode"),
        help_text=LOGO_HELP["light"],
        widget=forms.FileInput(attrs={"accept": ".png,.svg,image/png,image/svg+xml"}),
    )
    logo_dark_file = forms.FileField(
        required=False,
        label=_("logo for dark mode"),
        help_text=LOGO_HELP["dark"],
        widget=forms.FileInput(attrs={"accept": ".png,.svg,image/png,image/svg+xml"}),
    )
    remove_logos = forms.BooleanField(
        required=False,
        label=_(
            "Remove the uploaded logos and use the server configuration. The files are deleted "
            "and cannot be restored."
        ),
    )
    reset_colours = forms.BooleanField(
        required=False, label=_("Use the server colours again")
    )
    favicon_file = forms.FileField(
        required=False,
        label=_("icon of the browser tab"),
        help_text=FAVICON_HELP,
        widget=forms.FileInput(attrs={"accept": ".png,.svg,image/png,image/svg+xml"}),
    )
    remove_favicon = forms.BooleanField(
        required=False,
        label=_(
            "Remove the uploaded icon and use the built-in icon. The file is deleted "
            "and cannot be restored."
        ),
    )

    class Meta:
        model = PortalSettings
        fields = (
            "name",
            "website_url",
            "privacy_url",
            "imprint_url",
            *COLOUR_FIELDS,
        )
        widgets = {
            field: forms.TextInput(
                attrs={
                    "class": "portal-colour",
                    "size": 9,
                    "maxlength": 7,
                    "pattern": "#[0-9A-Fa-f]{6}",
                    "inputmode": "text",
                    "autocomplete": "off",
                    "spellcheck": "false",
                }
            )
            for field in COLOUR_FIELDS
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Accepted logos by variant, the accepted favicon, and the reasons of rejected uploads.
        self.new_logos = {}
        self.new_favicon = None
        self.rejections = []
        self._add_server_hints()

    def _add_server_hints(self):
        """Under every field with a server value: what an empty field means (D-073). Only public values; escaped."""
        server = environment_values()
        hints = {
            "name": server.name,
            "website_url": server.website_url,
            "privacy_url": server.privacy_url,
            **{field: getattr(server, field) for field in COLOUR_FIELDS},
        }
        for name, value in hints.items():
            self._append_hint(name, value or gettext("not set"))
        for variant in LOGOS:
            logo = getattr(server, f"logo_{variant}")
            self._append_hint(
                f"logo_{variant}_file", gettext("logo files set") if logo else gettext("not set")
            )

    def _append_hint(self, name, value):
        field = self.fields.get(name)
        if field is None:
            return
        hint = gettext("Server configuration: %(value)s") % {"value": value}
        field.help_text = format_html(
            '{}<br><span class="portal-hint">{}</span>', field.help_text, hint
        )

    def _read_logo(self, name, variant):
        upload = self.cleaned_data.get(name)
        if not upload:
            return
        # Never more than the largest allowed size is read.
        data = upload.read(MAX_UPLOAD_BYTES)
        try:
            self.new_logos[variant] = validate_logo(data)
        except LogoRejected as rejected:
            self.rejections.append(rejected.reason)
            self.add_error(name, ValidationError(rejected.message, params=rejected.params, code="logo"))

    def _read_favicon(self):
        upload = self.cleaned_data.get("favicon_file")
        if not upload:
            return
        # Never more than the largest allowed size is read.
        data = upload.read(FAVICON_MAX_UPLOAD_BYTES)
        try:
            self.new_favicon = validate_favicon(data)
        except LogoRejected as rejected:
            self.rejections.append(f"favicon {rejected.reason}")
            self.add_error(
                "favicon_file", ValidationError(rejected.message, params=rejected.params, code="logo")
            )

    def _check_favicon(self):
        self._read_favicon()
        if "favicon_file" in self._errors:
            return
        if self.cleaned_data.get("remove_favicon") and self.new_favicon is not None:
            raise ValidationError(REMOVE_FAVICON_AND_UPLOAD, code="remove_and_upload")

    def clean(self):
        cleaned = super().clean()
        for field in COLOUR_FIELDS:
            if cleaned.get(field):
                cleaned[field] = cleaned[field].upper()
        if cleaned.get("reset_colours"):
            # Emptying the colours is the reset; a wrong typed value is dropped.
            for field in COLOUR_FIELDS:
                cleaned[field] = ""
                self._errors.pop(field, None)
        elif not any(field in self._errors for field in COLOUR_FIELDS):
            self._check_colours(cleaned)
        for check in (lambda: self._check_logos(cleaned), self._check_favicon):
            try:
                check()
            except ValidationError as error:
                self.add_error(None, error)
        if self.rejections and self.actor is not None:
            # One event for the whole request, with reason codes only (never the content).
            with transaction.atomic():
                security_log.record(
                    SecurityEvent.Event.PORTAL_UPLOAD_REJECTED,
                    actor=self.actor,
                    detail=", ".join(self.rejections),
                )
        return cleaned

    def _check_colours(self, cleaned):
        stored = {field: cleaned.get(field) or "" for field in COLOUR_FIELDS}
        if not any(stored.values()):
            return
        for problem in colour_problems(effective_colours(stored)):
            if problem.code == "format":
                # A typed value that is no colour: the field's message, no ratio.
                try:
                    validate_hex_colour(stored.get(problem.field, ""))
                except ValidationError as error:
                    self.add_error(problem.field, error)
                continue
            self.add_error(problem.field, colour_message(problem))

    def _check_logos(self, cleaned):
        self._read_logo("logo_light_file", "light")
        self._read_logo("logo_dark_file", "dark")
        if any(name in self._errors for name in ("logo_light_file", "logo_dark_file")):
            return
        if cleaned.get("remove_logos"):
            if self.new_logos:
                raise ValidationError(REMOVE_AND_UPLOAD, code="remove_and_upload")
            return
        stored = {
            variant: bool(getattr(self.instance, f"logo_{variant}_sha256")) for variant in LOGOS
        }
        after = {variant: variant in self.new_logos or stored[variant] for variant in LOGOS}
        if after["light"] != after["dark"]:
            raise ValidationError(ONLY_ONE_LOGO, code="one_logo")

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get("remove_logos"):
            for variant in LOGOS:
                setattr(instance, f"logo_{variant}_data", None)
                setattr(instance, f"logo_{variant}_type", "")
                setattr(instance, f"logo_{variant}_sha256", "")
        for variant, logo in self.new_logos.items():
            setattr(instance, f"logo_{variant}_data", logo.data)
            setattr(instance, f"logo_{variant}_type", logo.kind)
            setattr(instance, f"logo_{variant}_sha256", logo.sha256)
        if self.cleaned_data.get("remove_favicon"):
            instance.favicon_data = None
            instance.favicon_type = ""
            instance.favicon_sha256 = ""
        if self.new_favicon is not None:
            instance.favicon_data = self.new_favicon.data
            instance.favicon_type = self.new_favicon.kind
            instance.favicon_sha256 = self.new_favicon.sha256
        if commit:
            instance.save()
        return instance
