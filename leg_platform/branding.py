"""Validated public instance branding and WCAG 2.2 contrast checks."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


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
    # Defaults of the design package of the project owner (LEG-Design-Vorgaben/login/VORGABEN.md,
    # section 6; docs/DECISIONS.md, D-056). An instance can replace every value.
    defaults = {
        "INSTANCE_ACCENT_LIGHT": "#005BA2",
        "INSTANCE_ACCENT_TEXT_LIGHT": "#FFFFFF",
        "INSTANCE_ACCENT_DARK": "#6AA9E0",
        "INSTANCE_ACCENT_TEXT_DARK": "#0B1520",
        "INSTANCE_HIGHLIGHT": "#DDDD22",
    }
    colours = {}
    for name, default in defaults.items():
        value = env.get(name, "").strip() or default
        if value and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is None:
            raise ImproperlyConfigured(f"{name} must be a six-digit hexadecimal colour.")
        colours[name] = value
    # The surfaces are the tokens --panel and --bg of the design language (docs/DESIGN.md, 3.1).
    for suffix, surfaces in (("LIGHT", ("#FFFFFF", "#F6F7F8")), ("DARK", ("#191C1F", "#111315"))):
        accent = colours[f"INSTANCE_ACCENT_{suffix}"]
        foreground = colours[f"INSTANCE_ACCENT_TEXT_{suffix}"]
        if any(contrast(accent, surface) < 4.5 for surface in (*surfaces, foreground)):
            raise ImproperlyConfigured(f"INSTANCE_ACCENT_{suffix} must reach 4.5:1 contrast.")
    highlight = colours["INSTANCE_HIGHLIGHT"]
    if highlight and contrast(highlight, "#1C1917") < 4.5:
        raise ImproperlyConfigured("INSTANCE_HIGHLIGHT must reach 4.5:1 contrast with dark text.")
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
    return Branding(
        colours["INSTANCE_ACCENT_LIGHT"], colours["INSTANCE_ACCENT_TEXT_LIGHT"],
        colours["INSTANCE_ACCENT_DARK"], colours["INSTANCE_ACCENT_TEXT_DARK"],
        highlight, *logos,
    )
