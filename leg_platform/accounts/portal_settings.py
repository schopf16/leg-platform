"""The one accessor of the values that the administrator maintains in the portal settings (D-056,
D-073): name, website, privacy notice, legal notice, colours and logos.

Per field the database value wins if it is not empty; otherwise the value of the instance
configuration (the environment) applies, then the default of the platform. While the row is empty
this is exactly the behaviour of the environment alone, so no data is migrated.

The accessor never raises: error pages, emails and the footer must work when the database does not
answer, and a value that was written past the form (or that no longer fits the environment, for
example a text colour of the environment with an accent colour of the database) is checked again
when it is read. A value that fails gives a warning and the server configuration (fail safe). It
reads ``settings.INSTANCE_*`` when it is called, and it works without a request (emails, commands).
With a request the database is read at most once per request, and nothing is kept between requests,
so a change is visible at once.
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

# The attribute that keeps the result for the rest of one request.
_REQUEST_ATTRIBUTE = "_portal_values"
# The columns that are read (never the files of the logos).
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
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOGO_TYPES = ("png", "svg")
# The length of the digest in the address of a logo, which makes the address change with the file.
LOGO_VERSION_LENGTH = 16


class LegalLinks(NamedTuple):
    privacy_url: str
    imprint_url: str


class PortalValues(NamedTuple):
    """The effective values (immutable). ``logo_light`` and ``logo_dark`` are addresses that can
    be used in ``<img src>`` ("" means no logo: the name is shown as text)."""

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
    # Whether the logos are the uploaded ones (and not the files of the server configuration).
    uploaded_logos: bool

    @property
    def website_host(self):
        """The host of the website, the text of its link in the footer ("" without a website)."""
        return (urlsplit(self.website_url).hostname or "") if self.website_url else ""


def _safe_link(value):
    """The address again checked before it is printed as a link: only a clean https:// address
    (the value of the database was written by the administrator, the rule of the form can have
    been bypassed by a direct write, security review of phase 2, E-1). Otherwise no link."""
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
    """The address of an uploaded logo: the view and the first characters of the digest of the file
    (the address changes with the file, so a cache never shows an old logo)."""
    return reverse("instance_logo", kwargs={"variant": variant}) + (
        f"?v={digest[:LOGO_VERSION_LENGTH]}"
    )


def effective_colours(stored, branding=None):
    """The five colours that result from the stored (database) colours and the server
    configuration: a stored colour wins; an empty text colour next to a stored accent colour is
    chosen automatically (white or the dark default, the one with the higher contrast)."""
    branding = branding if branding is not None else settings.INSTANCE_BRANDING
    colours = {field: stored.get(field) or getattr(branding, field) for field in COLOUR_FIELDS}
    for mode in ("light", "dark"):
        accent = stored.get(f"accent_{mode}") or ""
        if accent and not stored.get(f"text_{mode}") and HEX_COLOUR.fullmatch(accent):
            colours[f"text_{mode}"] = auto_text_colour(accent)
    return colours


def environment_values():
    """The values of the server configuration alone (the environment, then the defaults). Needs no
    database and no request."""
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
    )


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
        changes.update(logo_light=logos[0], logo_dark=logos[1], uploaded_logos=True)
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
