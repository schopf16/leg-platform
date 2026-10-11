"""Check that requirements/minimum-constraints.txt matches the lower bounds in pyproject.toml.

The "minimum" CI job installs exactly the versions pinned in minimum-constraints.txt
(docs/DECISIONS.md, D-015, D-026). If a pin differs from the ">=" lower bound declared in
pyproject.toml (e.g. after an automated update of only one of the files), CI would test a
different version than the declared minimum. This script fails in that case.

Usage: python scripts/check_minimum_constraints.py
Exit code 0 if both files agree, 1 otherwise. Standard library only (Python >= 3.12, the declared minimum).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CONSTRAINTS = ROOT / "requirements" / "minimum-constraints.txt"

_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*?)\s*$")


def normalize_name(name: str) -> str:
    """Normalize a distribution name as defined in PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def lower_bounds(pyproject_text: str) -> tuple[dict[str, str], list[str]]:
    """Return {name: lower bound} of the project dependencies and a list of errors."""
    data = tomllib.loads(pyproject_text)
    bounds: dict[str, str] = {}
    errors: list[str] = []
    for requirement in data["project"]["dependencies"]:
        spec = requirement.split(";", 1)[0]
        match = _REQUIREMENT.match(spec)
        if not match:
            errors.append(f"pyproject.toml: cannot parse {requirement!r}")
            continue
        name = normalize_name(match.group(1))
        lower = [
            part.strip()[2:].strip()
            for part in match.group(3).split(",")
            if part.strip().startswith(">=")
        ]
        if len(lower) != 1:
            errors.append(f"pyproject.toml: {requirement!r} needs exactly one '>=' lower bound")
            continue
        if name in bounds:
            errors.append(f"pyproject.toml: {name} is listed more than once")
            continue
        bounds[name] = lower[0]
    return bounds, errors


def pins(constraints_text: str) -> tuple[dict[str, str], list[str]]:
    """Return {name: pinned version} of the constraints file and a list of errors."""
    result: dict[str, str] = {}
    errors: list[str] = []
    for line in constraints_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, separator, version = line.partition("==")
        if not separator or not name.strip() or not version.strip():
            errors.append(f"minimum-constraints.txt: {line!r} is not of the form name==version")
            continue
        normalized = normalize_name(name.strip())
        if normalized in result:
            errors.append(f"minimum-constraints.txt: {normalized} is pinned more than once")
            continue
        result[normalized] = version.strip()
    return result, errors


def compare(pyproject_text: str, constraints_text: str) -> list[str]:
    """Return a list of differences; empty if the files agree."""
    bounds, errors = lower_bounds(pyproject_text)
    pinned, pin_errors = pins(constraints_text)
    errors.extend(pin_errors)
    for name in sorted(bounds.keys() | pinned.keys()):
        if name not in pinned:
            errors.append(f"{name}: lower bound {bounds[name]} has no pin in minimum-constraints.txt")
        elif name not in bounds:
            errors.append(f"{name}: pinned {pinned[name]} but not a dependency in pyproject.toml")
        elif bounds[name] != pinned[name]:
            errors.append(
                f"{name}: pyproject.toml declares >={bounds[name]}, "
                f"minimum-constraints.txt pins =={pinned[name]}"
            )
    return errors


def main() -> int:
    errors = compare(
        PYPROJECT.read_text(encoding="utf-8"), CONSTRAINTS.read_text(encoding="utf-8")
    )
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("minimum-constraints.txt matches the lower bounds in pyproject.toml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
