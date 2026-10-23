"""The one server-side access check of every page of the customer's participation (D-103).

A request reaches a customer view only if the account

* is logged in (otherwise: sent to the login page; a POST gets the plain login address),
* is active, has a customer number and the status "active" (otherwise: 404, the answer for a page that does not exist).
  Staff accounts and system administrators take part with the same account as everybody else (owner decision
  2026-10-23, D-113, which reverses the exclusion of D-103 and D-105): they use the participant pages here and the
  staff area (management and administration) in a second browser tab. An account without a customer number, an
  inactive or a not active one still gets the 404,
* has completed the second factor in this session if the account must use one (staff and system administrators,
  D-004): otherwise ``PermissionDenied`` (403), the same second line as ``management.access.check_access`` and
  ``PortalAdminSite.has_permission``.

The second-factor middleware (``accounts.second_factor``) is the first line and is unchanged: an account that must
still replace its password or complete its second factor is redirected before a view runs and cannot reach these pages.

Every object id in an address is resolved **through the logged-in account** by the services of
``leg_platform.community`` (an application or a building of another account is "not found", the same answer as a
missing one): ``ObjectDoesNotExist`` and the ``ApplicationError`` codes ``not_found`` / ``not_allowed`` are a 404 here.
No response is cached (``never_cache``): the pages show private data.

Use ``CustomerAccessMixin`` for class-based views. Every callback of ``leg_platform.customer.urls`` must carry the
marker ``customer_protected``; ``customer/tests/test_access.py`` walks the URL configuration and checks it.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from leg_platform.accounts.second_factor import is_second_factor_complete, requires_second_factor
from leg_platform.community.application_common import ApplicationError

MARKER = "customer_protected"
# Codes that never explain anything: a foreign id and a missing one look the same.
NOT_FOUND_CODES = ("not_found", "not_allowed")


def is_customer(user):
    """Whether the account takes part as a customer: active, with a customer number and the status "active". Staff and
    system administrators are customers too (D-113). The one predicate: the customer pages, the menu, the start page, the
    tiles and the profile use it."""
    return bool(
        user.is_authenticated
        and user.is_active
        and user.customer_number_id is not None
        and user.status == get_user_model().Status.ACTIVE
    )


def check_access(request):
    """None if the request may proceed, a redirect for a visitor who is not logged in, else ``Http404``."""
    user = request.user
    if not user.is_authenticated:
        if request.method in ("GET", "HEAD"):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        # A POST target is no page to return to.
        return redirect(settings.LOGIN_URL)
    if not is_customer(user):
        raise Http404
    # The second line of the second factor, as in the management area and the administration (D-004, D-042): the
    # middleware (``accounts.second_factor``) is the first line and redirects; this makes the pages refuse an account
    # that must use a second factor and has not completed it in this session even if the middleware were changed.
    # Staff accounts and system administrators take part too (D-113), so the rule reaches this area.
    if requires_second_factor(user) and not is_second_factor_complete(user):
        raise PermissionDenied
    return None


class CustomerAccessMixin:
    """For class-based views: the account must be an active customer; ids are resolved through the account."""

    customer_protected = True

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        denied = check_access(request)
        if denied is not None:
            return denied
        try:
            return super().dispatch(request, *args, **kwargs)
        except ObjectDoesNotExist:
            raise Http404 from None
        except ApplicationError as error:
            if error.code in NOT_FOUND_CODES:
                raise Http404 from None
            raise


def is_protected(callback):
    """Whether a URL callback (the function of a class-based view) carries the access check."""
    view_class = getattr(callback, "view_class", None)
    return bool(getattr(callback, MARKER, False) or getattr(view_class, MARKER, False))
