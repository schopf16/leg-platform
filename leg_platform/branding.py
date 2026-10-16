"""Validated public instance branding and WCAG 2.2 contrast checks.

The colour rules live in ONE function (``colour_problems``). The environment loader raises
``ImproperlyConfigured`` from its result at the start, the form of the portal settings turns it
into ``ValidationError`` when the administrator saves, and the accessor of the portal settings
checks the effective values again when they are read (D-051, D-073).
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

HEX_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")
MIN_CONTRAST = 4.5
# The surfaces are the tokens --panel and --bg of the design language (docs/DESIGN.md, 3.1).
LIGHT_SURFACES = ("#FFFFFF", "#F6F7F8")
DARK_SURFACES = ("#191C1F", "#111315")
# The highlight is only a dot next to dark text (docs/DESIGN.md, 3.2).
HIGHLIGHT_TEXT = "#1C1917"
# The two candidates for an automatically chosen text colour on the accent colour.
LIGHT_TEXT_DEFAULT = "#FFFFFF"
DARK_TEXT_DEFAULT = "#0B1520"
# Defaults of the design package of the project owner (LEG-Design-Vorgaben/login/VORGABEN.md,
# section 6; docs/DECISIONS.md, D-056). An instance can replace every value.
COLOUR_DEFAULTS = {
    "accent_light": "#005BA2",
    "text_light": LIGHT_TEXT_DEFAULT,
    "accent_dark": "#6AA9E0",
    "text_dark": DARK_TEXT_DEFAULT,
    "highlight": "#DDDD22",
}
COLOUR_FIELDS = tuple(COLOUR_DEFAULTS)
COLOUR_ENVIRONMENT_NAMES = {
    "accent_light": "INSTANCE_ACCENT_LIGHT",
    "text_light": "INSTANCE_ACCENT_TEXT_LIGHT",
    "accent_dark": "INSTANCE_ACCENT_DARK",
    "text_dark": "INSTANCE_ACCENT_TEXT_DARK",
    "highlight": "INSTANCE_HIGHLIGHT",
}


def luminance(colour: str) -> float:
    channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def floor_ratio(ratio: float) -> float:
    """The ratio cut (not rounded) to two decimals, so that 4.499 is never shown as 4.50."""
    return math.floor(ratio * 100) / 100


def auto_text_colour(accent: str) -> str:
    """The text colour on an accent colour when none is chosen: white or the dark default,
    whichever has the higher contrast (D-073)."""
    return max((LIGHT_TEXT_DEFAULT, DARK_TEXT_DEFAULT), key=lambda text: contrast(accent, text))


@dataclass(frozen=True)
class ColourProblem:
    """One failed colour rule. ``field`` is the colour that has to change; ``code`` is
    ``format``, ``accent_contrast`` (accent too weak on the surfaces), ``text_contrast`` (text too
    weak on the accent) or ``highlight_contrast``; ``ratio`` is the contrast that was found."""

    field: str
    code: str
    ratio: float | None = None


def colour_problems(colours: Mapping[str, str]) -> list[ColourProblem]:
    """Check a complete set of colours (keys ``COLOUR_FIELDS``) and return the problems found.

    Every value is ``#RRGGBB``. The accent reaches 4.5:1 on both surfaces of its mode and with
    its text colour; the highlight reaches 4.5:1 with dark text (WCAG 2.2, success criterion
    1.4.3). A format problem stops the contrast checks. The same rules apply to the environment,
    to the form of the administrator and to the values read from the database.
    """
    problems = [
        ColourProblem(field, "format")
        for field in COLOUR_FIELDS
        if HEX_COLOUR.fullmatch(colours.get(field) or "") is None
    ]
    if problems:
        return problems
    for suffix, surfaces in (("light", LIGHT_SURFACES), ("dark", DARK_SURFACES)):
        accent = colours[f"accent_{suffix}"]
        weakest = min(contrast(accent, surface) for surface in surfaces)
        if weakest < MIN_CONTRAST:
            problems.append(ColourProblem(f"accent_{suffix}", "accent_contrast", weakest))
        ratio = contrast(accent, colours[f"text_{suffix}"])
        if ratio < MIN_CONTRAST:
            problems.append(ColourProblem(f"text_{suffix}", "text_contrast", ratio))
    ratio = contrast(colours["highlight"], HIGHLIGHT_TEXT)
    if ratio < MIN_CONTRAST:
        problems.append(ColourProblem("highlight", "highlight_contrast", ratio))
    return problems


def instance_static_directory(env: Mapping[str, str]) -> Path | None:
    value = env.get("INSTANCE_STATIC_DIRECTORY", "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ImproperlyConfigured("INSTANCE_STATIC_DIRECTORY must be an existing absolute directory.")
    return path


@dataclass(frozen=True)
class Branding:
    accent_light: str
    text_light: str
    accent_dark: str
    text_dark: str
    highlight: str
    logo_light: str
    logo_dark: str


def load_branding(env: Mapping[str, str]) -> Branding:
    colours = {
        field: env.get(COLOUR_ENVIRONMENT_NAMES[field], "").strip() or default
        for field, default in COLOUR_DEFAULTS.items()
    }
    for problem in colour_problems(colours):
        name = COLOUR_ENVIRONMENT_NAMES[problem.field]
        if problem.code == "format":
            raise ImproperlyConfigured(f"{name} must be a six-digit hexadecimal colour.")
        if problem.code == "highlight_contrast":
            raise ImproperlyConfigured(
                "INSTANCE_HIGHLIGHT must reach 4.5:1 contrast with dark text."
            )
        # The accent and its text colour are reported under the name of the accent.
        suffix = problem.field.rsplit("_", 1)[1].upper()
        raise ImproperlyConfigured(f"INSTANCE_ACCENT_{suffix} must reach 4.5:1 contrast.")
    logos = []
    for name in ("INSTANCE_LOGO_LIGHT", "INSTANCE_LOGO_DARK"):
        value = env.get(name, "").strip()
        if value and (
            re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(?:svg|png)", value) is None
        ):
            raise ImproperlyConfigured(f"{name} must be a relative static SVG or PNG path.")
        logos.append(value)
    if bool(logos[0]) != bool(logos[1]):
        raise ImproperlyConfigured("Both INSTANCE_LOGO_LIGHT and INSTANCE_LOGO_DARK must be set.")
    return Branding(*(colours[field] for field in COLOUR_FIELDS), *logos)
