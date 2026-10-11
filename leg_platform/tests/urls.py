"""URL configuration used only by the response-level tests.

The project has no application views yet; these views let the tests exercise Django's
middleware (security headers, HTTPS redirect, CSRF protection, error handling) with the
project's settings without a database.
"""

from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.urls import path

# Marker in an exception message; the 500 response must not contain it.
INTERNAL_DETAIL = "internal-detail-that-must-not-be-shown"


def ok_view(request):
    # get_token() makes CsrfViewMiddleware set the CSRF cookie on the response.
    get_token(request)
    return HttpResponse("ok")


def error_view(request):
    raise RuntimeError(INTERNAL_DETAIL)


urlpatterns = [
    path("ok/", ok_view),
    path("error/", error_view),
]
