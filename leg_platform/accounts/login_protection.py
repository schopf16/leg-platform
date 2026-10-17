"""Privacy-preserving identifiers for login abuse protection (D-017, D-023)."""

import ipaddress
from datetime import timedelta

from django.conf import settings

from leg_platform.accounts.digests import keyed_digest
from leg_platform.accounts.normalization import normalize_email_address

EMAIL_MAX_LENGTH = 254
LOGIN_FAILURE_LIMIT = 5
LOGIN_COOLOFF = timedelta(minutes=15)
IP_ATTEMPT_WINDOW = LOGIN_COOLOFF


_identifier_digest = keyed_digest


def axes_username(request, credentials):
    """Return a keyed digest of the normalised login email for Axes storage."""
    username = (credentials or {}).get("username")
    if username is None or len(str(username).strip()) > EMAIL_MAX_LENGTH:
        return None
    normalized = normalize_email_address(username)
    if normalized is None or len(normalized) > EMAIL_MAX_LENGTH:
        return None
    return _identifier_digest(normalized, b"LEG-Platform Axes username v1")


def client_ip_digest(address):
    """Return a keyed digest of a canonical IP address without retaining the address."""
    if not address:
        return None
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return _identifier_digest(str(parsed), b"LEG-Platform login client IP v1")


def axes_client_ip(request):
    """Resolve a client IP without trusting forwarded headers by default."""
    if settings.AXES_IPWARE_PROXY_COUNT == 0:
        address = request.META.get("REMOTE_ADDR")
    else:
        from ipware.ip import get_client_ip

        address, _is_routable = get_client_ip(
            request,
            proxy_order=settings.AXES_IPWARE_PROXY_ORDER,
            proxy_count=settings.AXES_IPWARE_PROXY_COUNT,
            proxy_trusted_ips=settings.AXES_IPWARE_PROXY_TRUSTED_IPS,
            request_header_order=settings.AXES_IPWARE_META_PRECEDENCE_ORDER,
        )
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return str(parsed)


def reserve_client_ip_attempt(request):
    """Reserve one concurrent login slot, or refuse an IP at the failure limit."""
    from django.db import transaction
    from django.utils import timezone

    from leg_platform.accounts.models import LoginIpBucket

    digest = client_ip_digest(getattr(request, "axes_ip_address", None))
    if digest is None:
        return False

    now = timezone.now()
    cutoff = now - IP_ATTEMPT_WINDOW
    for _attempt in range(3):
        LoginIpBucket.objects.get_or_create(digest=digest)
        try:
            with transaction.atomic():
                bucket = LoginIpBucket.objects.select_for_update().get(digest=digest)
                bucket.attempts.filter(attempted_at__lt=cutoff).delete()
                if bucket.attempts.count() >= LOGIN_FAILURE_LIMIT:
                    return False
                attempt = bucket.attempts.create(attempted_at=now)
                request.login_ip_attempt_id = attempt.pk
            return True
        except LoginIpBucket.DoesNotExist:
            continue
    return False


def settle_client_ip_attempt(request, *, succeeded):
    """Release a successful slot or retain a failed attempt until its window expires.

    The bucket row is locked first, like ``reserve_client_ip_attempt`` and the cleanup: a delete or
    update by primary key and the reservation's index scan lock in opposite orders and could
    deadlock (MariaDB 1213 as HTTP 500; D-072).
    """
    from django.db import transaction

    from leg_platform.accounts.models import LoginIpAttempt, LoginIpBucket

    attempt_id = getattr(request, "login_ip_attempt_id", None)
    if attempt_id is None:
        return
    bucket_id = (
        LoginIpAttempt.objects.filter(pk=attempt_id).values_list("bucket_id", flat=True).first()
    )
    if bucket_id is not None:
        with transaction.atomic():
            # Only the lock matters; a bucket removed by the cleanup has no attempts left.
            LoginIpBucket.objects.select_for_update().filter(pk=bucket_id).first()
            attempts = LoginIpAttempt.objects.filter(pk=attempt_id, pending=True)
            if succeeded:
                attempts.delete()
            else:
                attempts.update(pending=False)
    del request.login_ip_attempt_id
