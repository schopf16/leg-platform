"""The way back from the profile card "Bank or postal connection" to the page that sent the customer there (D-105).

The wizard (the row "Bank or postal connection") and the deregistration (a feed-in needs an IBAN) lead to the profile and
must lead back afterwards. The way back travels in the address (``?next=``), which anyone can write, so it is accepted
only if it is a path **of this application's customer pages**: a relative path (no scheme, no host, no ``//``) that
resolves to a route of the namespace ``customer``. Anything else is dropped (the card then returns to the profile).
"""

from urllib.parse import quote, urlsplit

from django.urls import Resolver404, resolve, reverse
from django.utils.http import url_has_allowed_host_and_scheme

NAMESPACE = "customer"
MAX_LENGTH = 500


def safe_return_path(value):
    """The path (with its query) if it is a page of the customer's participation, else ``""``. Never raises."""
    if not isinstance(value, str) or not value or len(value) > MAX_LENGTH:
        return ""
    # Relative only: no host is allowed at all (a path such as ``//host`` or ``/\\host`` is refused by the check).
    if not url_has_allowed_host_and_scheme(value, allowed_hosts=None, require_https=False):
        return ""
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment or not parts.path.startswith("/") or parts.path.startswith("//"):
        return ""
    try:
        match = resolve(parts.path)
    except Resolver404:
        return ""
    if match.namespaces != [NAMESPACE]:
        return ""
    return value


def bank_change_url(return_to=""):
    """The profile with the card "Bank or postal connection" open, and the way back if ``return_to`` is a safe path."""
    url = f"{reverse('accounts:profile')}?edit=bank"
    safe = safe_return_path(return_to)
    if safe:
        url += f"&next={quote(safe, safe='')}"
    return f"{url}#card-bank"
