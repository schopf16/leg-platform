from django.http import HttpResponse
from django.utils.cache import add_never_cache_headers

# Maximum request body in bytes by path prefix (D-052, D-073); the longest matching prefix decides.
# Checked before the CSRF middleware, which reads the form body of a POST request. The portal
# settings also take uploads (two logos of at most 1 MB each and a favicon of at most 128 KB): the limit keeps a request without a
# login from making Django write a large body to a temporary file before the CSRF check. It is a
# prefix and not one path because the admin routes the object id as ``<path:object_id>``, so
# spellings such as ``01``, ``+1``, ``%201`` or ``_31`` (the admin's own ``_XX`` escape) and the
# routes ``add`` and ``history`` all reach the same admin and must be limited as well. The
# prefix has no trailing slash, so that the spelling without it is covered too.
BODY_LIMITS = (
    ("/addresses/lookup/", 4096),
    # The language menu: two short fields and the CSRF token (the address of the page can be long).
    ("/language/", 8192),
    ("/admin/accounts/portalsettings", 3 * 1024 * 1024),
)


def body_limit(path):
    """The limit in bytes for a path (``request.path_info``), or None if the path has none."""
    matching = [(prefix, limit) for prefix, limit in BODY_LIMITS if path.startswith(prefix)]
    return max(matching, key=lambda entry: len(entry[0]))[1] if matching else None


class NoStoreForManagementMiddleware:
    """No response of the management area is cached, whatever it is: also the 403 and 404 pages and
    the CSRF refusal, which the view decorators never see (security review of block 1, L-3). The
    pages show customer data and the answer to "may I see this" must not be kept by a browser or
    a proxy. Outermost of the project's own middleware, so that every response of the prefix passes.
    """

    PREFIX = "/management/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path_info.startswith(self.PREFIX) or request.path_info == self.PREFIX.rstrip("/"):
            add_never_cache_headers(response)
        return response


class RequestBodyLimitMiddleware:
    """Refuse a body above the limit of the route before any form parsing.

    Django's WSGI request reads only as many bytes as ``Content-Length`` states, so an invalid
    header is refused as well. A web server or reverse proxy should still limit the request
    body of the instance (docs/INSTALLATION.md).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = body_limit(request.path_info)
        if limit is not None and request.method in {"POST", "PUT", "PATCH"}:
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                return HttpResponse(status=400)
            if length < 0:
                return HttpResponse(status=400)
            if length > limit:
                return HttpResponse(status=413)
        return self.get_response(request)
