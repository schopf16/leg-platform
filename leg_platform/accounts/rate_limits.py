"""Request limits for the functions that send email (docs/SECURITY.md, section 3; D-037).

Registration, password reset and, later, email change share one budget per normalised address,
so that the total number of emails that one address can receive is bounded; a second budget
limits the requests of one client IP address. Counters are rows in the database, serialised by a
row lock per key like the login IP counter (D-017), so that parallel requests cannot exceed a
limit. Keys are keyed digests (D-023); neither addresses nor IP addresses are stored here.

The values follow common practice for this kind of application; OWASP names the controls (a
limit per account, no lockout of the account) but no numbers (Forgot Password Cheat Sheet).
A person at the device may need several attempts (mail not received, link expired, typing
errors), so the hourly per-address budget is generous and the daily one twice as large. Offices
behind one address share the IP budget, so it is larger than a single person needs.
"""

from datetime import timedelta
from ipaddress import IPv6Address, IPv6Network, ip_address

from django.db import transaction
from django.utils import timezone

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.models import RateLimitBucket

RATE_LIMIT_PURPOSE = b"LEG-Platform rate limit v1"

# (number of requests, window): a request is allowed only if every window has room.
MAIL_ADDRESS_LIMITS = ((12, timedelta(hours=1)), (24, timedelta(hours=24)))
MAIL_IP_LIMITS = ((20, timedelta(hours=1)),)
# An instance-wide cap on requests that send email, so that many distinct victims cannot be
# mail-bombed from many client addresses and the sender's reputation is protected. Generous for
# one municipality; reaching it is logged (D-037).
MAIL_GLOBAL_LIMITS = ((300, timedelta(hours=1)),)
# The information email to the owner of an existing address is static, so repeating it within a
# day adds nothing; it is checked in the background job, invisible to the requester.
EXISTING_ACCOUNT_NOTICE_LIMITS = ((1, timedelta(hours=24)),)
# Test emails of the email text editor go to the address of the administrator who asks (D-039).
TEST_EMAIL_LIMITS = ((10, timedelta(hours=1)),)

SCOPE_TEST_EMAIL = "test-email"

SCOPE_MAIL_ADDRESS = "mail-address"
SCOPE_MAIL_IP = "mail-ip"
SCOPE_MAIL_GLOBAL = "mail-global"
SCOPE_EXISTING_ACCOUNT_NOTICE = "existing-account-notice"

# Buckets that were not used for longer than the longest window are deleted by the cleanup.
LONGEST_WINDOW = timedelta(hours=24)


def ip_key(address):
    """Return the key under which a client IP address is counted.

    An IPv6 address is counted by its /64 prefix: a normal end-site assignment is a /64 or
    larger (RFC 6177), so a client controls 2**64 addresses and a limit per full address would
    never apply. An IPv4 address is counted as it is. Without an address, requests share one
    bucket (fail closed).
    """
    if not address:
        return "unknown"
    parsed = ip_address(address)
    if isinstance(parsed, IPv6Address):
        return str(IPv6Network((parsed, 64), strict=False))
    return str(parsed)


def consume(scope, value, limits):
    """Count one request for one key; return whether it is within every limit."""
    return consume_all([(scope, value, limits)])[0]


def consume_all(checks):
    """Count one request against several keys at once; return one boolean per check.

    ``checks`` is a list of ``(scope, value, limits)``. The request is counted at every key only
    if it is within the limits of all of them, so that a request refused by one key (for example
    the client IP address) never uses up the budget of another (for example a victim's address).
    The buckets are locked in a fixed order, so that parallel requests neither exceed a limit nor
    deadlock. A refused request is not counted, so that the window slides and a persistent sender
    cannot keep a key blocked beyond the limit. The last use of every bucket is written in both
    cases; a refused request inserts no event, which shows at most that a budget is used up,
    never whether an account exists, because no limit depends on an account. If a bucket cannot
    be locked, the request is refused (fail closed).
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
