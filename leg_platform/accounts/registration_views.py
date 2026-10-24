"""Step 2 of the registration: the choice of the sign-in method and its setup (D-058, D-106).

The pages belong to the verified link in the session (``views.RegistrationStepMixin``), like step 3. The account is
unverified and cannot sign in until step 3; what these pages prepare is staged (``registration_methods``).
"""

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView

from leg_platform.accounts import login_methods, passkeys, registration_methods, totp
from leg_platform.accounts.forms import LoginMethodForm, SetupCodeForm
from leg_platform.accounts.passkey_views import ERROR, payload
from leg_platform.accounts.views import (
    REGISTRATION_PASSWORD_SESSION_KEY,
    NeverCacheMixin,
    RegistrationStepMixin,
)


class RegistrationMethodView(RegistrationStepMixin, FormView):
    """The choice "Sign-in method" before anything is set up (the same cards as the profile)."""

    template_name = "accounts/registration_method.html"
    form_class = LoginMethodForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.token.user
        if self.request.method == "GET":
            # Offer the earlier choice again, e.g. after "Back".
            kwargs["initial"] = {"method": registration_methods.chosen(self.request, self.token)}
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.token.user
        form = context["form"]
        recommended = login_methods.recommended(user)
        context.update(
            progress_step=2,
            choose_form=form,
            # "What happens?" speaks of an existing account.
            methods=login_methods.cards(user, registering=True),
            hide_legend=True,
            selected=form["method"].value(),
            current="",
            recommended=recommended,
            recommended_title=login_methods.title_of(recommended),
            privileged=False,
        )
        return context

    def form_valid(self, form):
        method = form.cleaned_data["method"]
        if registration_methods.choose(self.request, self.token, method):
            # Another method: what the earlier one held is gone.
            self.request.session.pop(REGISTRATION_PASSWORD_SESSION_KEY, None)
        if method == login_methods.PASSKEY:
            return redirect("accounts:registration_passkey")
        return redirect("accounts:registration_password")


class RegistrationAppView(RegistrationStepMixin, FormView):
    """Password and app: the secret to scan and the first code. The recovery codes follow in step 3."""

    template_name = "accounts/registration_app.html"
    form_class = SetupCodeForm

    @method_decorator(sensitive_post_parameters("otp_token"))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def misplaced(self):
        """The screen the person belongs on, or None if this one is right."""
        if registration_methods.chosen(self.request, self.token) != login_methods.PASSWORD_AND_APP:
            return redirect("accounts:registration_method")
        if self.held_password_hash() is None:
            return redirect("accounts:registration_password")
        return None

    def get(self, request, *args, **kwargs):
        if (response := self.misplaced()) is not None:
            return response
        if registration_methods.app_confirmed(self.token.user):
            # Confirmed already (back button): nothing is changed on a GET, the next step follows.
            return redirect("accounts:registration_complete")
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if (response := self.misplaced()) is not None:
            return response
        if registration_methods.app_confirmed(self.token.user):
            return redirect("accounts:registration_complete")
        if registration_methods.limited(request):
            return HttpResponse(status=429)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.token.user
        device = registration_methods.unconfirmed_device(self.request, user) or registration_methods.start_app(
            self.request, user
        )
        context.update(progress_step=2, secret=totp.secret_for_display(device) if device else "")
        return context

    def form_valid(self, form):
        if registration_methods.confirm_app(self.request, self.token.user, form.cleaned_data["otp_token"]):
            return redirect("accounts:registration_complete")
        form.add_error("otp_token", form.invalid_code_message)
        return self.form_invalid(form)


class RegistrationAppQrView(NeverCacheMixin, View):
    """The QR code of the staged device, only to the session that holds the link."""

    def get(self, request):
        token = RegistrationStepMixin.find_token(request)
        if token is None or registration_methods.chosen(request, token) != login_methods.PASSWORD_AND_APP:
            return HttpResponse(status=404)
        device = registration_methods.unconfirmed_device(request, token.user)
        if device is None:
            return HttpResponse(status=404)
        return HttpResponse(totp.qr_code_svg(device), content_type="image/svg+xml")


class RegistrationPasskeyView(RegistrationStepMixin, View):
    """The page that creates the passkey. The browser ceremony is ``passkeys.js`` (unchanged); the display around it is
    ``passkey-setup.js``. A key that was created stays inactive until step 3."""

    http_method_names = ["get"]

    def get(self, request):
        if registration_methods.chosen(request, self.token) != login_methods.PASSKEY:
            return redirect("accounts:registration_method")
        if registration_methods.passkey_staged(request, self.token.user):
            # Created already: the next step. "Back" to the choice drops the key.
            return redirect("accounts:registration_complete")
        return render(request, "accounts/registration_passkey.html", {
            "progress_step": 2,
            "account_email": self.token.user.email,
            "registration_ref": self.token.pk,
        })


class RegistrationPasskeyJson(NeverCacheMixin, View):
    """Base of the two JSON entry points of the ceremony: the link in the session, the method "passkey", a request budget.
    CSRF protection applies (the script sends the token)."""

    http_method_names = ["post"]

    @method_decorator(sensitive_post_parameters())
    def dispatch(self, request, *args, **kwargs):
        self.token = RegistrationStepMixin.find_token(request)
        if (
            self.token is None
            or registration_methods.chosen(request, self.token) != login_methods.PASSKEY
            or registration_methods.limited(request)
        ):
            return JsonResponse({"error": str(ERROR)}, status=400)
        return super().dispatch(request, *args, **kwargs)


class RegistrationPasskeyOptionsView(RegistrationPasskeyJson):
    def post(self, request):
        try:
            data = payload(request)
            if data.get("purpose") != "register":
                raise ValueError("Wrong purpose.")
            # A retry starts clean: a key of this session that was never switched on is dropped first.
            token_user = self.token.user
            token_user.passkeys.filter(active=False, setup_binding=passkeys.binding(request)).delete()
            return JsonResponse(passkeys.begin(
                request, "register", user=token_user, mode=data.get("mode"), registering=True,
            ))
        except (ValueError, KeyError, TypeError):
            return JsonResponse({"error": str(ERROR)}, status=400)


class RegistrationPasskeyCreateView(RegistrationPasskeyJson):
    def post(self, request):
        try:
            data = payload(request)
            # The name is not asked in the registration: a fixed one, the person can rename nothing here and
            # adds further passkeys in the profile.
            key = passkeys.register(
                request, data["ceremony"], data["credential"], _("My passkey"),
                user=self.token.user, registering=True,
            )
        except (ValueError, KeyError, TypeError):
            key = None
        if key is None:
            return JsonResponse({"error": str(ERROR)}, status=400)
        return JsonResponse({"redirect": reverse("accounts:registration_complete")})
