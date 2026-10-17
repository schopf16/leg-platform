"""An archived location or LEG is read-only except "restore" (D-085); services raise ``Archived`` under the row lock."""

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
