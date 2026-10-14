"""The addresses of the privacy notice and the legal notice (D-056).

They are maintained by the administrator in the portal settings. The privacy address falls back
to ``INSTANCE_PRIVACY_URL`` of the instance configuration while the setting is empty. The lookup
never fails: error pages and the footer must work when the database does not answer, so a database
error gives no link (fail safe) instead of an exception.
"""

import logging
from typing import NamedTuple

from django.conf import settings
from django.db import Error as DatabaseError

logger = logging.getLogger(__name__)

# The attribute that keeps the result for the rest of one request.
_REQUEST_ATTRIBUTE = "_portal_legal_links"


class LegalLinks(NamedTuple):
    privacy_url: str
    imprint_url: str


def legal_links(request=None):
    """Return the addresses of the privacy notice and the legal notice ("" means no address).

    With a request, the database is read at most once per request.
    """
    if request is not None:
        cached = getattr(request, _REQUEST_ATTRIBUTE, None)
        if cached is not None:
            return cached
    # Imported here: the model module needs the app registry, this module is imported earlier.
    from leg_platform.accounts.models import PortalSettings

    privacy_url = imprint_url = ""
    try:
        row = PortalSettings.objects.filter(pk=1).values("privacy_url", "imprint_url").first()
    except DatabaseError:
        # Fail safe: the instance configuration still gives the privacy address, the legal
        # notice has no link.
        logger.warning("The portal settings could not be read.")
        row = None
    if row:
        privacy_url, imprint_url = row["privacy_url"], row["imprint_url"]
    links = LegalLinks(privacy_url or settings.INSTANCE_PRIVACY_URL, imprint_url)
    if request is not None:
        try:
            setattr(request, _REQUEST_ATTRIBUTE, links)
        except AttributeError:
            pass
    return links
