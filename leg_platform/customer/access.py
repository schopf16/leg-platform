"""The one server-side access check of every page of the customer's participation (D-103).

A request reaches a customer view only if the account

* is logged in (otherwise: sent to the login page; a POST gets the plain login address),
* is active, has a customer number and is **not** a staff account (otherwise: 404, the answer for a page that does not
  exist). Staff accounts use the management area (D-086); taking part with the same account would mix the roles, so a
  staff member who wants to take part uses a separate ordinary account.

The second-factor middleware (``accounts.second_factor``) is unchanged: an account that must still replace its
password or complete its second factor cannot reach these pages either.

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
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from leg_platform.community.application_common import ApplicationError

MARKER = "customer_protected"
# Codes that never explain anything: a foreign id and a missing one look the same.
NOT_FOUND_CODES = ("not_found", "not_allowed")


def is_customer(user):
    """Whether the account takes part as a customer: active, with a customer number, neither staff nor superuser. The one
    predicate: the customer pages, the menu tiles and the profile use it."""
    return bool(
        user.is_authenticated
        and user.is_active
        and not user.is_staff
        and not user.is_superuser
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
