from django.http import JsonResponse
from django.utils.cache import patch_cache_control
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from leg_platform.community.addressing import home_area

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
    if not allow_request(request):
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
            # The LEG's own area comes first in the answers (the check needs no ranking).
            home = home_area() if mode != "check" else None
            result = lookup(
                request.POST, mode=mode, home=home, minimum=TYPEAHEAD_MINIMUM,
                approximate=lambda: allow_approximate(request),
            )
            response = JsonResponse(result)
        except ValueError:
            response = JsonResponse({"status": "invalid", "results": [], "match": None, "warning": ""}, status=400)
    patch_cache_control(response, no_store=True, private=True)
    return response
