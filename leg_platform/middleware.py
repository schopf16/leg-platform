from django.conf import settings
from django.http import Http404


class LocalAdminOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.APP_ENV != "local" and (
            request.path_info == "/admin" or request.path_info.startswith("/admin/")
        ):
            raise Http404
        return self.get_response(request)