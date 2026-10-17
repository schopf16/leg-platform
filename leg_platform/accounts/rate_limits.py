"""Request limits for the functions that send email (D-037).

Registration, password reset and email change share one budget per normalised address (bounding the
emails one address can receive); a second budget limits one client IP address. Counters are database
rows serialised by a row lock per key (like the login IP counter, D-017), so parallel requests cannot
exceed a limit. Keys are keyed digests (D-023); no addresses or IPs are stored. The numbers are
practice, not OWASP: a person may need several attempts, and offices share one IP budget.
"""

from datetime import timedelta
from ipaddress import IPv6Address, IPv6Network, ip_address

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import RateLimitBucket

RATE_LIMIT_PURPOSE = b"LEG-Platform rate limit v1"

# (number of requests, window): a request is allowed only if every window has room. The short
# window (D-058) stops a burst that the hourly budget alone would allow ("send again" waits 60 seconds).
MAIL_ADDRESS_LIMITS = ((12, timedelta(hours=1)), (24, timedelta(hours=24)), (3, timedelta(minutes=10)))
MAIL_IP_LIMITS = ((20, timedelta(hours=1)),)
# Instance-wide cap on requests that send email (anti mail-bombing, sender reputation); reaching it is logged (D-037).
MAIL_GLOBAL_LIMITS = ((300, timedelta(hours=1)),)
# The info email to the owner of an existing address is static, so repeating it within a day adds
# nothing; checked in the background job, invisible to the requester.
EXISTING_ACCOUNT_NOTICE_LIMITS = ((1, timedelta(hours=24)),)
# Requests that store nothing (bot check hit, repeat within the pause) still write the session; counted
# per client address so they cannot create sessions without limit.
MAIL_SILENT_LIMITS = ((30, timedelta(hours=1)),)
SCOPE_MAIL_SILENT = "mail-silent"
SCOPE_MAIL_ADDRESS = "mail-address"
SCOPE_MAIL_IP = "mail-ip"
SCOPE_MAIL_GLOBAL = "mail-global"
SCOPE_EXISTING_ACCOUNT_NOTICE = "existing-account-notice"
# Staff invitations (D-087): no client-IP limit (an office shares one address), but one per staff
# account and a pause per invited account (a double click sends one email); address and instance limits apply.
SCOPE_INVITATION_ACTOR = "invitation-actor"
SCOPE_INVITATION_ACCOUNT = "invitation-account"
INVITATION_ACTOR_LIMITS = ((60, timedelta(hours=1)),)
INVITATION_ACCOUNT_LIMITS = ((1, timedelta(seconds=60)),)

# Buckets that were not used for longer than the longest window are deleted by the cleanup.
LONGEST_WINDOW = timedelta(hours=24)


def ip_key(address):
    """Return the key under which a client IP address is counted.

    IPv6 is counted by its /64 prefix (an end site has 2**64 addresses, RFC 6177); IPv4 as is.
    Without an address, requests share one bucket (fail closed).
    """
    if not address:
        return "unknown"
    parsed = ip_address(address)
    if isinstance(parsed, IPv6Address):
        return str(IPv6Network((parsed, 64), strict=False))
    return str(parsed)


def exhausted(checks):
    """Whether any of the keys has no room left, read only; ``checks`` is a list of ``(scope, value, limits)``.

    Nothing is written and no bucket is made (a refused client costs no row). A key counted at the
    same moment may pass here and be refused by ``consume_all`` afterwards.
    """
    now = timezone.now()
    for scope, value, limits in checks:
        digest = keyed_digest(f"{scope}\n{value}", RATE_LIMIT_PURPOSE)
        bucket = RateLimitBucket.objects.filter(digest=digest).first()
        if bucket is None:
            continue
        for count, window in limits:
            if bucket.events.filter(occurred_at__gt=now - window).count() >= count:
                return True
    return False


def consume(scope, value, limits):
    """Count one request for one key; return whether it is within every limit."""
    return consume_all([(scope, value, limits)])[0]


def consume_all(checks):
    """Count one request against several keys at once; return one boolean per check (``(scope, value, limits)``).

    Counted at every key only if within all limits, so a request refused by one key (the client IP)
    never uses up another's budget (a victim's address). Buckets are locked in a fixed order (no
    deadlock, no excess). A refused request is not counted (the window slides); the last use of every
    bucket is written either way; no limit depends on an account, so nothing reveals whether one
    exists. If a bucket cannot be locked, the request is refused (fail closed).
    """
    now = timezone.now()
    keyed = sorted(
        (keyed_digest(f"{scope}\n{value}", RATE_LIMIT_PURPOSE), limits)
        for scope, value, limits in checks
    )
    digests = [digest for digest, _limits in keyed]
    for _attempt in range(3):
        for digest in digests:
            RateLimitBucket.objects.get_or_create(digest=digest, defaults={"last_seen_at": now})
        try:
            with transaction.atomic():
                results = {}
                buckets = []
                for digest, limits in keyed:
                    bucket = RateLimitBucket.objects.select_for_update().get(digest=digest)
                    longest = max(window for _count, window in limits)
                    bucket.events.filter(occurred_at__lte=now - longest).delete()
                    results[digest] = all(
                        bucket.events.filter(occurred_at__gt=now - window).count() < count
                        for count, window in limits
                    )
                    buckets.append(bucket)
                allowed = all(results.values())
                for bucket in buckets:
                    if allowed:
                        bucket.events.create(occurred_at=now)
                    bucket.last_seen_at = now
                    bucket.save(update_fields=["last_seen_at"])
                return [
                    True
                    if allowed
                    else results[keyed_digest(f"{scope}\n{value}", RATE_LIMIT_PURPOSE)]
                    for scope, value, _limits in checks
                ]
        except RateLimitBucket.DoesNotExist:
            continue
    return [False] * len(checks)

def delete_unused_buckets(now=None):
    """Delete buckets, with their events, that no request used within the longest window."""
    cutoff = (now or timezone.now()) - LONGEST_WINDOW
    _total, per_model = RateLimitBucket.objects.filter(last_seen_at__lt=cutoff).delete()
    return per_model.get(RateLimitBucket._meta.label, 0)
