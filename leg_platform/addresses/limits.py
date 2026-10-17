"""Request limits of the address endpoints, on the application's transactional rate limiter."""

import logging
from datetime import timedelta

from django.db import DatabaseError

logger = logging.getLogger(__name__)


def allow_request(request, *, admin=False):
    from leg_platform.accounts.login_protection import axes_client_ip
    from leg_platform.accounts.rate_limits import consume_all, exhausted, ip_key

    # Same trusted proxy handling and IPv6 /64 treatment as registration.
    try:
        address = axes_client_ip(request)
        if address is None:
            return False
        scope = "address-import" if admin else "address-lookup"
        limits = ((5 if admin else 60, timedelta(minutes=1)),)
        checks = [
            (scope + "-ip", ip_key(address), limits),
            (scope + "-global", "instance", ((20 if admin else 600, timedelta(minutes=1)),)),
        ]
        # Read-only pre-check first: a refused client costs no bucket row (D-066).
        if exhausted(checks):
            return False
        return all(consume_all(checks))
    except (ValueError, DatabaseError):
        logger.warning("Address request limit check failed; request refused.")
        return False
