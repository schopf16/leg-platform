"""No-store, CSRF-protected browser entry points for WebAuthn (D-106)."""

import json
from datetime import timedelta

from axes.backends import AxesStandaloneBackend
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters

from leg_platform.accounts import passkey_methods, passkeys, rate_limits, second_factor
from leg_platform.accounts.forms import PortalFormMixin, ReauthenticationForm
from leg_platform.accounts.login_protection import axes_client_ip
from leg_platform.accounts.models import Passkey, User

ERROR = gettext_lazy("The passkey could not be verified. Please try again.")


def payload(request):
    if request.content_type != "application/json" or len(request.body) > 32768:
        raise ValueError("Invalid request.")
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")
    return data


def limited(request):
    return not rate_limits.consume(
        "passkey-request", rate_limits.ip_key(axes_client_ip(request)),
        ((30, timedelta(minutes=5)), (200, timedelta(hours=1))),
    )


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class OptionsView(View):
    http_method_names = ["post"]

    def post(self, request):
        if limited(request):
            return JsonResponse({"error": str(ERROR)}, status=429)
        try:
            data = payload(request)
            purpose = data.get("purpose")
            action = data.get("action", "")
            user = None
            if purpose in {"proof", "register"}:
                if not request.user.is_authenticated:
                    raise ValueError("Authentication required.")
                user = request.user
                if purpose == "register" and not passkey_methods.setup_allowed(request):
                    raise ValueError("Current proof required.")
                if purpose == "proof" and user.authentication_method != User.AuthenticationMethod.PASSKEY:
                    raise ValueError("Wrong method.")
            elif purpose != "login":
                raise ValueError("Wrong purpose.")
            if not isinstance(action, str) or (action and (not action.startswith("/") or action.startswith("//"))):
                raise ValueError("Invalid action.")
            if purpose != "proof":
                action = ""
            return JsonResponse(passkeys.begin(
                request, purpose, user=user, action=action, mode=data.get("mode"),
            ))
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": str(ERROR)}, status=400)


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class LoginView(View):
    http_method_names = ["post"]

    def post(self, request):
        if limited(request):
            return JsonResponse({"error": str(ERROR)}, status=429)
        try:
            data = payload(request)
            # Resolve the account only from a stored credential, never a posted username/user id.
            raw_id = passkeys.credential_from_response(data["credential"])
            key = Passkey.objects.filter(
                credential_digest=passkeys.credential_digest(raw_id), active=True
            ).select_related("user").first()
            credentials = {"username": key.user.email if key else ""}
            with transaction.atomic():
                if key:
                    User.objects.select_for_update().get(pk=key.user_id)
                try:
                    AxesStandaloneBackend().authenticate(request, **credentials)
                except PermissionDenied:
                    user_login_failed.send(sender=__name__, credentials=credentials, request=request)
                    return JsonResponse({"error": str(ERROR)}, status=429)
                user = passkeys.authenticate(request, data["ceremony"], data["credential"])
                if user is None:
                    user_login_failed.send(sender=__name__, credentials=credentials, request=request)
                    return JsonResponse({"error": str(ERROR)}, status=400)
                setattr(request, second_factor.STAY_WISH_ATTRIBUTE, data.get("stay") is True)
                login(request, user, backend=second_factor.LOGIN_BACKEND)
                second_factor.mark_passkey_session(request, user)
            return JsonResponse({"redirect": reverse("home")})
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": str(ERROR)}, status=400)


class CredentialNameForm(PortalFormMixin, forms.Form):
    name = forms.CharField(
        label=gettext_lazy("Passkey name"), max_length=80,
        help_text=gettext_lazy("Choose a name you recognise."),
        widget=forms.TextInput(attrs={"form": "passkey-create"}),
    )


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class SetupView(LoginRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
            second_factor.end_session_for_wrong_codes(request)
            return redirect("accounts:login")
        return response

    def get(self, request):
        if request.user.authentication_method == User.AuthenticationMethod.RECOVERY:
            if not passkey_methods.setup_allowed(request):
                logout(request)
                return redirect("accounts:login")
        return self.page(request)

    def page(self, request, form=None):
        allowed = passkey_methods.setup_allowed(request)
        return render(request, "accounts/passkey_setup.html", {
            "allowed": allowed, "form": form or (CredentialNameForm() if allowed else ReauthenticationForm(request=request)),
            "keys": request.user.passkeys.filter(active=True) | request.user.passkeys.filter(setup_binding=passkeys.binding(request)) if allowed else [],
            "adding": request.user.authentication_method == User.AuthenticationMethod.PASSKEY,
            "recovering": request.user.authentication_method == User.AuthenticationMethod.RECOVERY,
        })

    def post(self, request):
        if request.content_type == "application/json":
            if limited(request) or not passkey_methods.setup_allowed(request):
                return JsonResponse({"error": str(ERROR)}, status=400)
            try:
                data = payload(request)
                form = CredentialNameForm({"name": data.get("name")})
                if not form.is_valid() or passkeys.register(
                    request, data["ceremony"], data["credential"], form.cleaned_data["name"]
                ) is None:
                    return JsonResponse({"error": str(ERROR)}, status=400)
                return JsonResponse({"redirect": reverse("accounts:passkey_setup")})
            except (ValueError, KeyError, TypeError):
                return JsonResponse({"error": str(ERROR)}, status=400)
        if request.POST.get("activate") == "yes":
            user = passkey_methods.activate(request)
            if user is None:
                messages.error(request, ERROR)
                return self.page(request)
            request.user = user
            update_session_auth_hash(request, user)
            second_factor.mark_passkey_session(request, user)
            return redirect("accounts:profile")
        if request.user.authentication_method == User.AuthenticationMethod.RECOVERY:
            return redirect("accounts:login_method")
        form = ReauthenticationForm(request.POST, request=request)
        if form.is_valid():
            passkey_methods.allow_setup(request)
            return redirect("accounts:passkey_setup")
        return self.page(request, form)


@method_decorator(never_cache, name="dispatch")
class ManageView(LoginRequiredMixin, View):
    http_method_names = ["get", "post"]

    def get(self, request):
        if request.user.authentication_method != User.AuthenticationMethod.PASSKEY:
            return redirect("accounts:login_method")
        return render(request, "accounts/passkeys.html", {
            "keys": request.user.passkeys.filter(active=True).order_by("created_at", "pk"),
            "form": ReauthenticationForm(request=request),
        })

    def post(self, request):
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=request.user.pk)
            if locked.get_session_auth_hash() != request.user.get_session_auth_hash():
                logout(request)
                return redirect("accounts:login")
            request.user = locked
            return self.remove_locked(request)

    def remove_locked(self, request):
        form = ReauthenticationForm(request.POST, request=request)
        if not form.is_valid():
            if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
                second_factor.end_session_for_wrong_codes(request)
                return redirect("accounts:login")
            return render(request, "accounts/passkeys.html", {
                "keys": request.user.passkeys.filter(active=True), "form": form,
            })
        if not passkey_methods.remove(request.user, request.POST.get("credential")):
            form.add_error(None, _("Keep at least the required number of passkeys. Add a replacement first."))
            return render(request, "accounts/passkeys.html", {
                "keys": request.user.passkeys.filter(active=True), "form": form,
            })
        logout(request)
        return redirect("accounts:login")
