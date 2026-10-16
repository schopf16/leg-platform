"""What a staff user chose for a list: column widths and expanded locations (D-077).

Stored per user and list (``ListPreference``), never trusted: the key of the list must be one of
``LIST_KEYS`` and the widths are checked (``LISTEN-STANDARD.md`` section 1: at least 90 px).
"""

import re

from leg_platform.community.models import ListPreference

# One line per list; a later page adds its key here.
LIST_KEYS = (
    "persons",
    "locations",
    "location_metering_points",
    "legs",
    "leg_metering_points",
    "metering_points",
)
MIN_COLUMN_WIDTH = 90
MAX_COLUMN_WIDTH = 2000
MAX_COLUMNS = 12
MAX_EXPANDED = 2000
COLUMN_KEY = re.compile(r"[a-z][a-z0-9_]{0,29}")


def _check_list_key(list_key):
    if list_key not in LIST_KEYS:
        raise ValueError(f"Unknown list: {list_key!r}")


def get_preference(user, list_key):
    """The row of the user for the list; an unsaved row with the defaults if there is none."""
    _check_list_key(list_key)
    return ListPreference.objects.filter(user=user, list_key=list_key).first() or ListPreference(
        user=user, list_key=list_key
    )


def clean_column_widths(widths):
    """``{column: width}`` with valid column names and whole pixel widths; raises ``ValueError``."""
    if not isinstance(widths, dict) or len(widths) > MAX_COLUMNS:
        raise ValueError("Invalid column widths.")
    cleaned = {}
    for column, width in widths.items():
        if (
            not isinstance(column, str)
            or COLUMN_KEY.fullmatch(column) is None
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not MIN_COLUMN_WIDTH <= width <= MAX_COLUMN_WIDTH
        ):
            raise ValueError("Invalid column widths.")
        cleaned[column] = width
    return cleaned


def save_column_widths(user, list_key, widths):
    _check_list_key(list_key)
    cleaned = clean_column_widths(widths)
    preference, _created = ListPreference.objects.update_or_create(
        user=user, list_key=list_key, defaults={"column_widths": cleaned}
    )
    return preference


def save_expanded_locations(user, location_ids):
    """The ids of the locations that are expanded in the list of locations."""
    ids = list(location_ids)
    if len(ids) > MAX_EXPANDED or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
        raise ValueError("Invalid locations.")
    preference, _created = ListPreference.objects.update_or_create(
        user=user, list_key="locations", defaults={"expanded_locations": sorted(set(ids))}
    )
    return preference
