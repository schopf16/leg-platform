"""The one server-side access check of every page of the management area (D-077).

A request reaches a management view only if the account

* is logged in (otherwise: sent to the login page),
* is active and a staff user (otherwise: 404, so that a person without access does not learn that
  the area exists),
* has completed the second factor in this session (the second-factor middleware already sends an
  account without it to the setup; this is the second line, ``second_factor.is_second_factor_complete``),
* and holds the permissions of the page (otherwise: 403, the project's page "You have no access
  here").

Permissions are checked, never group names (D-012); system administrators hold all of them
(``is_superuser``). A view that names no permission is a configuration error and fails closed.

Use ``ManagementAccessMixin`` for class-based views and ``management_view`` for functions. Every
callback of ``leg_platform.management.urls`` must carry the marker ``management_protected``; a test
walks the URL configuration and checks it (the equivalent of the rule for admin URLs: "wrap every
admin URL in ``admin_view()``").
"""

from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from leg_platform.accounts.second_factor import is_second_factor_complete

MARKER = "management_protected"


def check_access(request, permissions=(), any_of=()):
    """Return None if the request may proceed, a redirect response for an anonymous visitor, or
    raise ``Http404`` / ``PermissionDenied``.

    ``permissions`` are all required; ``any_of`` needs at least one. Both empty is an error."""
    permissions, any_of = tuple(permissions), tuple(any_of)
    if not permissions and not any_of:
        raise ImproperlyConfigured("A management view must name the permissions it needs.")
    user = request.user
    if not user.is_authenticated:
        if request.method in ("GET", "HEAD"):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        # A POST target is no page to return to.
        return redirect(settings.LOGIN_URL)
    if not (user.is_active and user.is_staff):
        raise Http404
    if not is_second_factor_complete(user):
        raise PermissionDenied
    if permissions and not user.has_perms(permissions):
        raise PermissionDenied
    if any_of and not any(user.has_perm(permission) for permission in any_of):
        raise PermissionDenied
    return None


class ManagementAccessMixin:
    """For class-based views: set ``permission_required`` (all of) or ``any_permission_required``
    (at least one), or override ``get_required_permissions`` to depend on the method (for example
    view for GET and change for POST)."""

    permission_required = ()
    any_permission_required = ()
    management_protected = True

    def get_required_permissions(self):
        return tuple(self.permission_required)

    def get_any_permissions(self):
        return tuple(self.any_permission_required)

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        denied = check_access(request, self.get_required_permissions(), self.get_any_permissions())
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)


def management_view(*permissions, any_of=()):
    """For function views: ``@management_view("community.view_leg")``."""

    def decorator(view):
        @wraps(view)
        @never_cache
        def wrapped(request, *args, **kwargs):
            denied = check_access(request, permissions, any_of)
            if denied is not None:
                return denied
            return view(request, *args, **kwargs)

        setattr(wrapped, MARKER, True)
        return wrapped

    return decorator


def is_protected(callback):
    """Whether a URL callback (a function or the function of a class-based view) is protected."""
    view_class = getattr(callback, "view_class", None)
    return bool(getattr(callback, MARKER, False) or getattr(view_class, MARKER, False))
