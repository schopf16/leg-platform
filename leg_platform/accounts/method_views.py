"""Shared method setup after current proof or a confirmed, restricted recovery link."""

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.hashers import make_password
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView
from django.views.decorators.debug import sensitive_post_parameters
from django_otp import login as otp_login

from leg_platform.accounts import method_changes, passkey_methods, second_factor, totp
from leg_platform.accounts.forms import NewPasswordForm, ReauthenticationForm, SetupCodeForm, password_rules
from leg_platform.accounts.mail_queue import enqueue_for_account
from leg_platform.accounts.models import MailJob, User, VerificationToken
from leg_platform.accounts.profile_views import render_profile
from leg_platform.accounts.views import NeverCacheMixin, TokenPageMixin, PasswordResetRequestView


def finish_setup(request, pending):
    if request.user.authentication_method == User.AuthenticationMethod.RECOVERY:
        result = method_changes.complete(request.user.pk, recovery=True)
        if result is None:
            return redirect("accounts:login_method")
        user, device, codes = result
        request.user = user
        request.session.pop(passkey_methods.SETUP_KEY, None)
        update_session_auth_hash(request, user)
        if device:
            request.session.cycle_key()
            otp_login(request, device)
            return render_profile(request, codes=codes, enrolled=True)
        return redirect("accounts:profile")
    enqueue_for_account(MailJob.Kind.METHOD_RETURN, request.user)
    return render(request, "accounts/method_pending.html")


@method_decorator(sensitive_post_parameters(), name="dispatch")
class PasswordMethodView(NeverCacheMixin, LoginRequiredMixin, View):
    def get(self, request):
        return self.page(request)

    def page(self, request, password_form=None, proof_form=None):
        recovery = request.user.authentication_method == User.AuthenticationMethod.RECOVERY
        return render(request, "accounts/method_password.html", {
            "form": password_form or NewPasswordForm(request.user),
            "password_rules": password_rules(),
            "proof_form": None if recovery else (proof_form or ReauthenticationForm(request=request)),
        })

    def post(self, request):
        user = request.user
        recovery = user.authentication_method == User.AuthenticationMethod.RECOVERY
        form = NewPasswordForm(user, request.POST)
        proof = None if recovery else ReauthenticationForm(request.POST, request=request)
        if not form.is_valid():
            return self.page(request, form, proof)
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=user.pk)
            if locked.get_session_auth_hash() != user.get_session_auth_hash():
                return redirect("accounts:login")
            if recovery:
                if not passkey_methods.setup_allowed(request, locked):
                    logout(request)
                    return redirect("accounts:login")
            elif not proof.is_valid():
                if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
                    second_factor.end_session_for_wrong_codes(request)
                    return redirect("accounts:login")
                return self.page(request, form, proof)
            else:
                passkey_methods.allow_setup(request)
            pending = method_changes.start(
                request, make_password(form.cleaned_data["new_password1"]),
                request.session.get("chosen_password_method"),
            )
        if pending is None:
            return redirect("accounts:login_method")
        if pending.method == "app":
            return redirect("accounts:method_app")
        return finish_setup(request, pending)


class AppMethodView(NeverCacheMixin, LoginRequiredMixin, View):
    def device(self, request):
        return totp.unconfirmed_totp_device(request.user, request.session.get("method_app_device"))

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and (
            not method_changes.session_pending(request) or not passkey_methods.setup_allowed(request)
        ):
            return redirect("accounts:login_method")
        return super().dispatch(request, *args, **kwargs)

    def page(self, request, form=None):
        device = self.device(request)
        if device is None:
            return redirect("accounts:method_password")
        return render(request, "accounts/method_app.html", {
            "form": form or SetupCodeForm(), "secret": totp.secret_for_display(device),
        })

    def get(self, request):
        return self.page(request)

    def post(self, request):
        from leg_platform.accounts.passkey_views import limited

        form = SetupCodeForm(request.POST)
        if limited(request):
            return HttpResponse(status=429)
        if form.is_valid():
            if method_changes.confirm_app(request, form.cleaned_data["otp_token"]):
                return finish_setup(request, method_changes.session_pending(request))
            form.add_error("otp_token", form.invalid_code_message)
        return self.page(request, form)


class AppMethodQrView(NeverCacheMixin, LoginRequiredMixin, View):
    def get(self, request):
        if not passkey_methods.setup_allowed(request) or not method_changes.session_pending(request):
            return HttpResponse(status=404)
        device = totp.unconfirmed_totp_device(request.user, request.session.get("method_app_device"))
        if device is None:
            return HttpResponse(status=404)
        return HttpResponse(totp.qr_code_svg(device), content_type="image/svg+xml")


class ReturnVerifyView(TokenPageMixin, View):
    def get(self, request, token):
        found = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.METHOD_RETURN)
        if found is None:
            return redirect("accounts:two_factor_reset_invalid")
        request.session.cycle_key()
        request.session["method_return_token"] = found.digest
        return redirect("accounts:method_return_confirm")


class ReturnConfirmView(NeverCacheMixin, View):
    def token(self, request):
        return VerificationToken.objects.find_valid_by_digest(
            request.session.get("method_return_token", ""), VerificationToken.Purpose.METHOD_RETURN
        )

    def get(self, request):
        if self.token(request) is None:
            return redirect("accounts:two_factor_reset_invalid")
        return render(request, "accounts/method_return_confirm.html")

    def post(self, request):
        token = self.token(request)
        result = method_changes.complete(token.user_id, token_id=token.pk) if token else None
        request.session.pop("method_return_token", None)
        if result is None:
            return redirect("accounts:two_factor_reset_invalid")
        # The link never grants a normal session. Recovery codes are shown once in this POST only.
        user, device, codes = result
        logout(request)
        if codes:
            return render(request, "accounts/method_return_done.html", {
                "codes": [totp.format_recovery_code(code) for code in codes],
            })
        messages.success(request, _("Your sign-in method was changed. Please sign in again."))
        return redirect("accounts:login")


class LoginHelpView(NeverCacheMixin, TemplateView):
    """Anonymous navigation only: no account lookup or recovery mutation."""

    http_method_names = ["get"]
    template_name = "accounts/login_help.html"


class RecoveryRequestView(PasswordResetRequestView):
    template_name = "accounts/recovery.html"
    kind = MailJob.Kind.RECOVERY_GUIDANCE
    purpose = "recovery"
    success_url = reverse_lazy("accounts:recovery_sent")
