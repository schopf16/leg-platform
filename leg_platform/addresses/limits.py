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
        user = getattr(request, "user", None)
        pk = getattr(user, "pk", None)
        if not admin and pk is not None and getattr(user, "is_authenticated", False):
            # A signed-in account has its own budget (D-111): an office behind one address does not share one budget,
            # and no account can use more than this however many addresses it comes from.
            first = (ACCOUNT_SCOPE, str(pk), ACCOUNT_LOOKUP_LIMITS)
        else:
            first = (scope + "-ip", ip_key(address), limits)
        checks = [
            first,
            (scope + "-global", "instance", ((20 if admin else 600, timedelta(minutes=1)),)),
        ]
        # Read-only pre-check first: a refused client costs no bucket row (D-066).
        if exhausted(checks):
            return False
        return all(consume_all(checks))
    except (ValueError, DatabaseError):
        logger.warning("Address request limit check failed; request refused.")
        return False


# The typing suggestions of the forms (D-111): per signed-in account, three kinds of lookup (street, postal code, town).
ACCOUNT_SCOPE = "address-lookup-account"
ACCOUNT_LOOKUP_LIMITS = ((120, timedelta(minutes=1)),)


# Outbound building searches at swisstopo (``buildings``, D-066): a budget per signed-in account and one for the whole
# instance, so that no account can use the portal as a proxy to the service. A person types one address; a repeat is
# answered from the stored result and costs nothing. Proposals, not measured.
SCOPE_EXTERNAL_USER = "address-external-user"
SCOPE_EXTERNAL_GLOBAL = "address-external-global"
EXTERNAL_USER_LIMITS = ((6, timedelta(minutes=1)), (10, timedelta(hours=1)))
EXTERNAL_GLOBAL_LIMITS = ((30, timedelta(minutes=1)), (300, timedelta(hours=1)))
# Circuit breaker of the service: this many failures (timeout, error status, invalid answer) within the window open
# it; while open nobody asks swisstopo (and no budget is used), and the window sliding on closes it again. A success
# clears the failures. Counted in the same transactional store as the budgets, so every worker sees it.
SCOPE_EXTERNAL_FAILURES = "address-external-failures"
CIRCUIT_LIMITS = ((5, timedelta(minutes=5)),)
_CIRCUIT_CHECK = (SCOPE_EXTERNAL_FAILURES, "instance", CIRCUIT_LIMITS)


def circuit_open():
    """Whether the search at swisstopo is paused after repeated failures (fail closed on a database error)."""
    from leg_platform.accounts.rate_limits import exhausted

    try:
        return exhausted([_CIRCUIT_CHECK])
    except (ValueError, DatabaseError):
        logger.warning("Address search circuit breaker check failed; the external search is not made.")
        return True


def record_failure():
    """Count one failure of the search at swisstopo."""
    from leg_platform.accounts.rate_limits import consume_all

    try:
        consume_all([_CIRCUIT_CHECK])
    except (ValueError, DatabaseError):
        logger.warning("A failure of the address search could not be counted.")


def record_success():
    """A trustworthy answer closes the breaker: the failures so far are forgotten."""
    from leg_platform.accounts.digests import keyed_digest
    from leg_platform.accounts.models import RateLimitEvent
    from leg_platform.accounts.rate_limits import RATE_LIMIT_PURPOSE

    try:
        digest = keyed_digest(f"{_CIRCUIT_CHECK[0]}\n{_CIRCUIT_CHECK[1]}", RATE_LIMIT_PURPOSE)
        RateLimitEvent.objects.filter(bucket_id=digest).delete()
    except (ValueError, DatabaseError):
        logger.warning("The failures of the address search could not be cleared.")


def allow_external_lookup(user):
    """Count one outbound building search of a signed-in account; False when a budget is used up (fail closed)."""
    from leg_platform.accounts.rate_limits import consume_all, exhausted

    try:
        pk = getattr(user, "pk", None)
        if pk is None or not getattr(user, "is_authenticated", False):
            return False
        checks = [
            (SCOPE_EXTERNAL_USER, str(pk), EXTERNAL_USER_LIMITS),
            (SCOPE_EXTERNAL_GLOBAL, "instance", EXTERNAL_GLOBAL_LIMITS),
        ]
        # Read-only pre-check first: a refused account costs no bucket row (as in ``allow_request``).
        if exhausted(checks):
            return False
        return all(consume_all(checks))
    except (ValueError, DatabaseError):
        logger.warning("Address search limit check failed; the external search is not made.")
        return False
