"""The rule of D-085: an archived location or LEG is read-only, like an archived metering point (D-079).

Every change of it is refused on the server (a direct POST included); the only action that is allowed on
it is "restore" (``set_archived`` with ``archived=False``). The services raise ``Archived`` under the row
lock, so that a stale page cannot change an object that was archived in the meantime.
"""

LOCATION = "location"
LEG = "leg"


class Archived(Exception):
    """The object is archived and cannot be changed. ``kind`` is ``LOCATION`` or ``LEG``."""

    def __init__(self, kind):
        super().__init__(f"archived_{kind}")
        self.kind = kind


def ensure_open(obj, kind):
    """Raise ``Archived`` if the (freshly locked) object is archived."""
    if obj.is_archived:
        raise Archived(kind)
