from django.http import HttpResponse

# Maximum request body in bytes of endpoints that are reachable without login (D-052). Checked
# before the CSRF middleware, which reads the form body of a POST request.
BODY_LIMITS = {"/addresses/lookup/": 4096}


class RequestBodyLimitMiddleware:
    """Refuse a body above the limit of the route before any form parsing.

    Django's WSGI request reads only as many bytes as ``Content-Length`` states, so an invalid
    header is refused as well. A web server or reverse proxy should still limit the request
    body of the instance (docs/INSTALLATION.md).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = BODY_LIMITS.get(request.path_info)
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
