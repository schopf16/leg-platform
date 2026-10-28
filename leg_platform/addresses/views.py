from django.http import JsonResponse
from django.utils.cache import patch_cache_control
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from leg_platform.community.addressing import home_area
from leg_platform.customer.access import has_customer_access

from .limits import allow_approximate, allow_request
from .services import TYPEAHEAD_MINIMUM, lookup


@require_POST
@csrf_protect
def address_lookup(request):
    # The typing suggestions of every address form (D-111): street, postal code and town. No login: the details step
    # of the registration is used before the account exists (D-066); the answer is public register data only.
    # POST keeps addresses out of access logs. Reject non-urlencoded bodies (multipart may already
    # be parsed by the CSRF middleware) before touching request.body (RawPostDataException).
    if request.content_type != "application/x-www-form-urlencoded":
        response = JsonResponse({"status": "invalid", "results": [], "match": None, "warning": ""}, status=400)
        patch_cache_control(response, no_store=True, private=True)
        return response
    # The check (mode "check") reads all rows of a street and is dearer than a suggestion: for an anonymous client (the
    # registration) it counts as two requests of the address budget, so the limits of D-111 bound the work it can cause.
    anonymous = not getattr(getattr(request, "user", None), "is_authenticated", False)
    expensive = request.POST.get("mode") == "check" and anonymous
    if not allow_request(request) or (expensive and not allow_request(request)):
        response = JsonResponse(
            {"status": "rate_limited", "results": [], "match": None, "warning": ""}, status=429
        )
        response["Retry-After"] = "60"
    else:
        try:
            if len(request.body) > 4096:
                raise ValueError("Address payload too large.")
            if any(len(request.POST.getlist(key)) != 1 for key in request.POST):
                raise ValueError("Repeated address parameters.")
            mode = request.POST.get("mode", "suggest")
            # The LEG's own area comes first in the answers, also among the addresses of a fix (mode "check").
            home = home_area()
            result = lookup(
                request.POST, mode=mode, home=home, minimum=TYPEAHEAD_MINIMUM,
                approximate=lambda: allow_approximate(request),
                # The status line of the customer wizard (D-124) also asks for the EGID of a match: customers only
                # (active, customer number, second factor), the same rule as the customer pages.
                building=request.POST.get("building") == "1" and has_customer_access(request),
            )
            response = JsonResponse(result)
        except ValueError:
            response = JsonResponse({"status": "invalid", "results": [], "match": None, "warning": ""}, status=400)
    patch_cache_control(response, no_store=True, private=True)
    return response
