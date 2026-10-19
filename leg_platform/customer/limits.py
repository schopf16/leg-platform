"""Request limits of the customer's pages, on the application's transactional rate limiter (``accounts.rate_limits``).

Counted **per account**, never per address or per client IP: an office or a household shares one IP address, and the
account is the one that is logged in. Keys are keyed digests (no account data is stored in the bucket). The pages refuse
a POST beyond a limit with 429 and ``Retry-After``; a GET costs nothing. The numbers are proposals, not measured: a
person fills in a wizard step in a few attempts, far below them.

* ``allow_wizard``: every POST of the wizard and of the edit pages.
* ``allow_search``: additionally every POST that asks the address directory (the building search; it may cause one
  request to swisstopo, which has its own budget per account and per instance in ``addresses.limits``).
"""

import logging
from datetime import timedelta

from django.db import DatabaseError

from leg_platform.accounts import rate_limits

logger = logging.getLogger(__name__)

SCOPE_WIZARD = "customer-wizard"
SCOPE_SEARCH = "customer-building-search"
WIZARD_LIMITS = ((60, timedelta(minutes=1)), (600, timedelta(hours=1)))
SEARCH_LIMITS = ((12, timedelta(minutes=1)), (60, timedelta(hours=1)))
# What the refusal tells the browser to wait (seconds).
RETRY_AFTER = 60


def _allow(user, scope, limits):
    try:
        return bool(rate_limits.consume(scope, str(user.pk), limits))
    except DatabaseError:
        # Fail closed, without any account data in the log.
        logger.warning("A request limit of the customer pages could not be checked; the request is refused.")
        return False


def allow_wizard(user):
    """Count one POST of the wizard or an edit page; False when the budget is used up."""
    return _allow(user, SCOPE_WIZARD, WIZARD_LIMITS)


def allow_search(user):
    """Count one building search; False when the budget is used up."""
    return _allow(user, SCOPE_SEARCH, SEARCH_LIMITS)
