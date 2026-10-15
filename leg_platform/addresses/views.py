from django.http import JsonResponse
from django.utils.cache import patch_cache_control
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .limits import allow_request
from .services import lookup


@require_POST
@csrf_protect
def address_lookup(request):
    # POST avoids putting a profile address in access-log query strings. Normal
    # CSRF protection also applies during anonymous registration. The answers carry no text for
    # the person (a "warning" is only ever the not-found notice of a check, D-066).
    # CSRF middleware may already have parsed multipart bodies. Reject that
    # unsupported encoding before accessing request.body (RawPostDataException).
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
            # Limit the complete payload as well as individual input fields.
            if len(request.body) > 4096:
                raise ValueError("Address payload too large.")
            if any(len(request.POST.getlist(key)) != 1 for key in request.POST):
                raise ValueError("Repeated address parameters.")
            result = lookup(request.POST, mode=request.POST.get("mode", "suggest"))
            response = JsonResponse(result)
        except ValueError:
            response = JsonResponse({"status": "invalid", "results": [], "match": None, "warning": ""}, status=400)
    patch_cache_control(response, no_store=True, private=True)
    return response
