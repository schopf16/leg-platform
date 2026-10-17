"""The one server-side access check of every page of the management area (D-077).

A request reaches a management view only if the account

* is logged in (otherwise: sent to the login page),
* is active and a staff user (otherwise: 404, the answer for a page that does not exist; this does
  not hide the area completely: the redirect to the login for a visitor who is not logged in and the
  CSRF refusal of a POST are other answers, and the area is public in the open source code),
* has completed the second factor in this session (the second-factor middleware already sends an
  account without it to the setup; this is the second line, ``second_factor.is_second_factor_complete``).

That is the whole policy (D-086, the owner's decision of 2026-10-17, an approved deviation from D-012):
**every active staff account may use every page of the management area**; there is no permission of
the area to hold, and a page, a column, a filter, a search, a count, a print scope or a menu entry is never
shown or hidden by a Django permission. System administrators are staff. The Django administration is not
part of this and keeps its permission model.

Use ``ManagementAccessMixin`` for class-based views and ``management_view`` for functions. Every
callback of ``leg_platform.management.urls`` must carry the marker ``management_protected``; a test
walks the URL configuration and checks it (the equivalent of the rule for admin URLs: "wrap every
admin URL in ``admin_view()``").
"""

import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache

from leg_platform.accounts.second_factor import is_second_factor_complete
from leg_platform.management.returns import return_url

logger = logging.getLogger(__name__)

MARKER = "management_protected"


def gone(request):
    """The answer for an object that a service did not find any more (deleted between the form and the save, L-C of the
    review of 2026-10-17): a POST gets the normal refusal, a message and the way back (``next``, else the overview),
    anything else is a 404. Nothing was changed: the service runs in one transaction that the exception rolled back."""
    logger.warning("A management request named an object that does not exist any more (%s).", request.method)
    if request.method != "POST":
        raise Http404
    messages.error(request, gettext("This entry does not exist any more."))
    return redirect(return_url(request, reverse("management:overview")))


def is_management_user(user):
    """Whether the account may use the management area at all: active and staff (the second factor of
    the session is checked by ``check_access``). The one test that the menu and the views share."""
    return bool(user.is_authenticated and user.is_active and user.is_staff)


def check_access(request):
    """Return None if the request may proceed, a redirect response for an anonymous visitor, or
    raise ``Http404`` / ``PermissionDenied``."""
    user = request.user
    if not user.is_authenticated:
        if request.method in ("GET", "HEAD"):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        # A POST target is no page to return to.
        return redirect(settings.LOGIN_URL)
    if not is_management_user(user):
        raise Http404
    if not is_second_factor_complete(user):
        raise PermissionDenied
    return None


class ManagementAccessMixin:
    """For class-based views: every staff account with the completed second factor may use the view."""

    management_protected = True

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        denied = check_access(request)
        if denied is not None:
            return denied
        if request.method.lower() in self.http_method_names:
            refused = self.refusal(request, *args, **kwargs)
            if refused is not None:
                return refused
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return gone(request)

    def refusal(self, request, *args, **kwargs):
        """A hook after the access check and before the handler: a response that refuses the request (for
        example the change of an archived object, D-085), or None. The default refuses nothing."""
        return None


def management_view(view):
    """For function views: ``@management_view``."""

    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        denied = check_access(request)
        if denied is not None:
            return denied
        try:
            return view(request, *args, **kwargs)
        except ObjectDoesNotExist:
            return gone(request)

    setattr(wrapped, MARKER, True)
    return wrapped


def is_protected(callback):
    """Whether a URL callback (a function or the function of a class-based view) is protected."""
    view_class = getattr(callback, "view_class", None)
    return bool(getattr(callback, MARKER, False) or getattr(view_class, MARKER, False))
