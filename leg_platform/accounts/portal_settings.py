"""The one accessor of the values the administrator maintains in the portal settings (D-056, D-073).

Per field the database value wins if not empty, else the instance configuration (environment), then
the platform default. The accessor never raises (error pages, emails and the footer must work
without the database); a stored value that fails the checks when read gives a warning and the server
value (fail safe). Works without a request; with one the database is read at most once per request
and nothing is kept between requests.
"""

import logging
import re
from typing import NamedTuple
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse

from leg_platform.branding import (
    COLOUR_FIELDS,
    HEX_COLOUR,
    auto_text_colour,
    colour_problems,
)
from leg_platform.environment import is_valid_instance_name

logger = logging.getLogger(__name__)

# Request attribute that keeps the result for the rest of one request.
_REQUEST_ATTRIBUTE = "_portal_values"
# The columns that are read (never the logo files).
_ROW_FIELDS = (
    "name",
    "website_url",
    "privacy_url",
    "imprint_url",
    *COLOUR_FIELDS,
    "logo_light_type",
    "logo_light_sha256",
    "logo_dark_type",
    "logo_dark_sha256",
    "favicon_type",
    "favicon_sha256",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOGO_TYPES = ("png", "svg")
# The content type of an uploaded image by its stored type: the one fixed table (served by ``presentation``).
IMAGE_CONTENT_TYPES = {"png": "image/png", "svg": "image/svg+xml"}
# Digest length in a logo address: the address changes with the file.
LOGO_VERSION_LENGTH = 16


class LegalLinks(NamedTuple):
    privacy_url: str
    imprint_url: str


class PortalValues(NamedTuple):
    """The effective values (immutable). ``logo_light``/``logo_dark`` are ``<img src>`` addresses ("" = no logo: the name is shown)."""

    name: str
    website_url: str
    privacy_url: str
    imprint_url: str
    accent_light: str
    text_light: str
    accent_dark: str
    text_dark: str
    highlight: str
    logo_light: str
    logo_dark: str
    # Whether the logos are the uploaded ones.
    uploaded_logos: bool
    # The uploaded favicon: its address and content type; "" = none, the pages use the built-in icon.
    favicon: str = ""
    favicon_type: str = ""
    # The format of the light logo ("png", "svg" or ""), derived from the file (not a setting): a mail shows only a PNG.
    logo_light_type: str = ""

    @property
    def website_host(self):
        """The host of the website (the footer link text), "" without a website."""
        return (urlsplit(self.website_url).hostname or "") if self.website_url else ""


def _safe_link(value):
    """Re-check an address before printing it as a link (a direct write can bypass the form): only a clean https:// address, else no link."""
    from leg_platform.accounts.models import validate_https_address

    if not value:
        return ""
    try:
        validate_https_address(value)
    except ValidationError:
        logger.warning("An address of the portal settings is not a clean https address.")
        return ""
    return value


def _static_url(path):
    if not path:
        return ""
    try:
        return static(path)
    except Exception:  # noqa: BLE001 - a missing manifest entry must not break a page
        logger.warning("A logo file of the server configuration has no address.")
        return ""


def logo_url(variant, digest):
    """The address of an uploaded logo with the digest prefix, so a cache never shows an old logo."""
    return reverse("instance_logo", kwargs={"variant": variant}) + (
        f"?v={digest[:LOGO_VERSION_LENGTH]}"
    )


def favicon_url(digest):
    """The address of the uploaded favicon with the digest prefix, so a cache never shows an old icon."""
    return reverse("instance_favicon") + f"?v={digest[:LOGO_VERSION_LENGTH]}"


def effective_colours(stored, branding=None):
    """The five colours from the stored (database) colours and the server configuration.

    A stored colour wins; an empty text colour next to a stored accent is chosen automatically (white or dark, the higher contrast).
    """
    branding = branding if branding is not None else settings.INSTANCE_BRANDING
    colours = {field: stored.get(field) or getattr(branding, field) for field in COLOUR_FIELDS}
    for mode in ("light", "dark"):
        accent = stored.get(f"accent_{mode}") or ""
        if accent and not stored.get(f"text_{mode}") and HEX_COLOUR.fullmatch(accent):
            colours[f"text_{mode}"] = auto_text_colour(accent)
    return colours


def environment_values():
    """The values of the server configuration alone (environment, then defaults); needs no database or request."""
    branding = settings.INSTANCE_BRANDING
    return PortalValues(
        name=settings.INSTANCE_NAME,
        website_url=_safe_link(settings.INSTANCE_WEBSITE_URL),
        privacy_url=_safe_link(settings.INSTANCE_PRIVACY_URL),
        imprint_url="",
        accent_light=branding.accent_light,
        text_light=branding.text_light,
        accent_dark=branding.accent_dark,
        text_dark=branding.text_dark,
        highlight=branding.highlight,
        logo_light=_static_url(branding.logo_light),
        logo_dark=_static_url(branding.logo_dark),
        uploaded_logos=False,
        logo_light_type=_path_type(branding.logo_light),
    )


def _path_type(path):
    """The logo format by the file extension of a static path, "" if it is neither png nor svg."""
    suffix = str(path or "").rsplit(".", 1)[-1].lower()
    return suffix if suffix in _LOGO_TYPES else ""


def _stored_logos(row):
    """The addresses of the uploaded logos, or None if there are none (both or none)."""
    ready = {
        variant: row[f"logo_{variant}_type"] in _LOGO_TYPES
        and _SHA256.fullmatch(row[f"logo_{variant}_sha256"] or "") is not None
        for variant in ("light", "dark")
    }
    if not any(ready.values()):
        return None
    if not all(ready.values()):
        logger.warning("The portal settings hold only one logo; the logos of the server are used.")
        return None
    try:
        return (
            logo_url("light", row["logo_light_sha256"]),
            logo_url("dark", row["logo_dark_sha256"]),
        )
    except NoReverseMatch:
        return None


def _stored_favicon(row):
    """(address, content type) of the uploaded favicon, or None if there is none (independent of the logos)."""
    if row["favicon_type"] not in _LOGO_TYPES or _SHA256.fullmatch(row["favicon_sha256"] or "") is None:
        return None
    try:
        return favicon_url(row["favicon_sha256"]), IMAGE_CONTENT_TYPES[row["favicon_type"]]
    except NoReverseMatch:
        return None


def _resolve(row):
    values = environment_values()
    if not row:
        return values
    name = row["name"] or ""
    if name and not (is_valid_instance_name(name) and name == name.strip()):
        logger.warning("The name in the portal settings is not valid; the server name is used.")
        name = ""
    changes = {
        "name": name or values.name,
        "website_url": _safe_link(row["website_url"]) or values.website_url,
        "privacy_url": _safe_link(row["privacy_url"]) or values.privacy_url,
        "imprint_url": _safe_link(row["imprint_url"]),
    }
    stored_colours = {field: row[field] or "" for field in COLOUR_FIELDS}
    if any(stored_colours.values()):
        colours = effective_colours(stored_colours)
        if colour_problems(colours):
            logger.warning(
                "The colours of the portal settings are not valid; the server colours are used."
            )
        else:
            changes.update(colours)
    logos = _stored_logos(row)
    if logos is not None:
        changes.update(
            logo_light=logos[0], logo_dark=logos[1], uploaded_logos=True, logo_light_type=row["logo_light_type"]
        )
    favicon = _stored_favicon(row)
    if favicon is not None:
        changes.update(favicon=favicon[0], favicon_type=favicon[1])
    return values._replace(**changes)


def _read_row():
    # Imported here: the model module needs the app registry, this module is imported earlier.
    from leg_platform.accounts.models import PortalSettings

    return PortalSettings.objects.filter(pk=1).values(*_ROW_FIELDS).first()


def portal_values(request=None):
    """Return the effective ``PortalValues``; never raises (see the module text)."""
    if request is not None:
        cached = getattr(request, _REQUEST_ATTRIBUTE, None)
        if cached is not None:
            return cached
    try:
        row = _read_row()
    except Exception:  # noqa: BLE001 - any failure of the read: the server configuration applies
        logger.warning("The portal settings could not be read.")
        row = None
    try:
        values = _resolve(row)
    except Exception as error:  # noqa: BLE001 - fail safe: the page must still be shown
        logger.warning(
            "The portal settings could not be applied (%s); the server configuration is used.",
            type(error).__name__,
        )
        values = environment_values()
    if request is not None:
        try:
            setattr(request, _REQUEST_ATTRIBUTE, values)
        except AttributeError:
            pass
    return values


def legal_links(request=None):
    """Return the addresses of the privacy notice and the legal notice ("" means no address).

    With a request, the database is read at most once per request.
    """
    values = portal_values(request)
    return LegalLinks(values.privacy_url, values.imprint_url)
