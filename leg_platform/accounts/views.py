"""Views of the portal accounts (docs/PROJECT.md, "User Functions").

Every view that shows account data or handles a token is marked as not cacheable. Views of
logged-in persons only ever use ``request.user``; no view takes an account identifier from the
URL or the request (docs/SECURITY.md, section 4).
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView
from django_otp import login as otp_login

from leg_platform.accounts import (
    password_reset,
    second_factor,
    second_factor_reset,
    security_log,
    totp,
)
from leg_platform.accounts.forms import (
    ChangePasswordForm,
    CodeForm,
    CompleteRegistrationForm,
    ConfirmForm,
    LoginForm,
    NewPasswordForm,
    PasswordResetRequestForm,
    ReauthenticationForm,
    RegistrationForm,
)
from leg_platform.accounts.mail_queue import submit_mail_request
from leg_platform.accounts.models import MailJob, SecurityEvent, User, VerificationToken

# The verification token's digest is kept in the session between the link and the form, so that
# the token itself is not in the URL of the page that is shown (docs/SECURITY.md, section 6;
# the approach of Django's password reset view).
REGISTRATION_SESSION_KEY = "registration_token_digest"
PASSWORD_RESET_SESSION_KEY = "password_reset_token_digest"
TWO_FACTOR_RESET_SESSION_KEY = "two_factor_reset_token_digest"
ENROLMENT_SESSION_KEY = "two_factor_enrolment_device"


def no_referrer(view):
    """Pages reached through a link with a token send no referrer (OWASP Forgot Password Cheat
    Sheet, "URL Tokens"); the header of the view wins over the project default."""

    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Referrer-Policy"] = "no-referrer"
        return response

    return wrapped


class NeverCacheMixin:
    @method_decorator(never_cache)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


class TokenPageMixin(NeverCacheMixin):
    @method_decorator(no_referrer)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)


class HomeView(View):
    """The root URL leads to the login page or, when logged in, to the profile (D-025)."""

    def get(self, request):
        return redirect("accounts:profile" if request.user.is_authenticated else "accounts:login")


class PortalLoginView(NeverCacheMixin, LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    # Django warns that redirecting logged-in persons away from the login page can allow
    # social media fingerprinting (docs/ARCHITECTURE.md, "Deliberately Open").
    redirect_authenticated_user = False

    def form_valid(self, form):
        user = form.get_user()
        if not second_factor.has_second_factor(user):
            return super().form_valid(form)
        # No login() yet: that follows the second factor (D-017).
        second_factor.begin_pending_login(self.request, user, self.get_redirect_url())
        return redirect("accounts:login_second_factor")


class SecondFactorLoginView(FormView):
    """The second step of the login: the code of the authenticator app or a recovery code."""

    template_name = "accounts/login_second_factor.html"
    form_class = CodeForm

    @method_decorator(never_cache)
    @method_decorator(sensitive_post_parameters("otp_token"))
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        self.pending = second_factor.pending_login(request)
        if self.pending is None:
            return redirect("accounts:login")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        device = second_factor.verify_second_factor(
            self.request, self.pending.user, form.cleaned_data["otp_token"]
        )
        if device is None:
            # One message for a wrong code, a used recovery code and a throttled device. A
            # lockout is answered by the middleware of the abuse protection instead.
            form.add_error("otp_token", CodeForm.invalid_code_message)
            return self.form_invalid(form)
        second_factor.complete_login(self.request, self.pending.user, device)
        target = self.pending.next_url
        if not url_has_allowed_host_and_scheme(
            target, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
        ):
            target = ""
        return redirect(target or settings.LOGIN_REDIRECT_URL)


class PortalLogoutView(NeverCacheMixin, LogoutView):
    """Logout only by POST with CSRF protection (Django's LogoutView)."""


class RegistrationView(NeverCacheMixin, FormView):
    template_name = "accounts/registration.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:registration_sent")

    def form_valid(self, form):
        submit_mail_request(self.request, MailJob.Kind.REGISTRATION, form.cleaned_data["email"])
        return super().form_valid(form)


class EmailSentView(NeverCacheMixin, TemplateView):
    """The same page for every address (docs/PROJECT.md, "Registration")."""

    template_name = "accounts/email_sent.html"


class RegistrationVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL.

    Mail scanners and link previews open links without any person acting; the registration is
    completed only when the form is submitted.
    """

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.REGISTRATION)
        if found is None or found.user.status != User.Status.UNVERIFIED:
            return render(request, "accounts/registration_invalid.html")
        request.session[REGISTRATION_SESSION_KEY] = found.digest
        return redirect("accounts:registration_complete")


class RegistrationCompleteView(NeverCacheMixin, FormView):
    template_name = "accounts/registration_complete.html"
    form_class = CompleteRegistrationForm
    success_url = reverse_lazy("accounts:login")

    # The responses of this method come before the mixin's dispatch, so it is marked as not
    # cacheable itself. The password fields are masked in error reports.
    @method_decorator(never_cache)
    @method_decorator(no_referrer)
    @method_decorator(sensitive_post_parameters("password1", "password2"))
    def dispatch(self, request, *args, **kwargs):
        self.token = self.find_token(request)
        if self.token is None:
            return render(request, "accounts/registration_invalid.html")
        # The form belongs to the registration it was rendered for. Opening another link in the
        # same browser replaces the token in the session; a form rendered for the earlier link
        # must then not be saved into the account of the later one.
        if request.method == "POST" and request.POST.get("registration") != str(self.token.pk):
            return render(request, "accounts/registration_invalid.html")
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def find_token(request):
        digest = request.session.get(REGISTRATION_SESSION_KEY)
        if not digest:
            return None
        found = VerificationToken.objects.find_valid_by_digest(
            digest, VerificationToken.Purpose.REGISTRATION
        )
        if found is None or found.user.status != User.Status.UNVERIFIED:
            return None
        return found

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.token.user
        return kwargs

    def get_context_data(self, **kwargs):
        # The address is shown but cannot be changed here: it is the one that was verified.
        return super().get_context_data(
            account_email=self.token.user.email, registration_ref=self.token.pk, **kwargs
        )

    def form_valid(self, form):
        with transaction.atomic():
            # The account is locked before its token, as when a pending registration is replaced,
            # so that the two cannot deadlock. The token is consumed only now, by the successful
            # submission, and the locks let only one of two concurrent submissions succeed.
            user = (
                User._base_manager.select_for_update()
                .filter(pk=self.token.user_id, status=User.Status.UNVERIFIED)
                .first()
            )
            token = (
                VerificationToken.objects.select_for_update()
                .filter(pk=self.token.pk, expires_at__gt=timezone.now())
                .first()
            )
            if user is None or token is None:
                return render(self.request, "accounts/registration_invalid.html")
            user = form.save()
            user.complete_registration(form.cleaned_data["password1"])
            token.delete()
        self.request.session.pop(REGISTRATION_SESSION_KEY, None)
        messages.success(self.request, _("Your account is active. You can now log in."))
        return redirect(self.get_success_url())


class PasswordResetRequestView(NeverCacheMixin, FormView):
    """Asks only for the address; the request is handled in the background (D-037)."""

    template_name = "accounts/password_reset.html"
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy("accounts:password_reset_sent")

    def form_valid(self, form):
        submit_mail_request(
            self.request, MailJob.Kind.PASSWORD_RESET, form.cleaned_data["email"]
        )
        return super().form_valid(form)


class PasswordResetVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL."""

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(
            token, VerificationToken.Purpose.PASSWORD_RESET
        )
        if found is None or found.user.status != User.Status.ACTIVE:
            return render(request, "accounts/password_reset_invalid.html")
        request.session[PASSWORD_RESET_SESSION_KEY] = found.digest
        return redirect("accounts:password_reset_set")


class PasswordResetSetView(NeverCacheMixin, FormView):
    template_name = "accounts/password_reset_set.html"
    form_class = NewPasswordForm
    success_url = reverse_lazy("accounts:login")

    @method_decorator(never_cache)
    @method_decorator(no_referrer)
    @method_decorator(sensitive_post_parameters("new_password1", "new_password2"))
    def dispatch(self, request, *args, **kwargs):
        digest = request.session.get(PASSWORD_RESET_SESSION_KEY)
        self.token = (
            VerificationToken.objects.find_valid_by_digest(
                digest, VerificationToken.Purpose.PASSWORD_RESET
            )
            if digest
            else None
        )
        if self.token is None or self.token.user.status != User.Status.ACTIVE:
            return render(request, "accounts/password_reset_invalid.html")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.token.user
        return kwargs

    def form_valid(self, form):
        user = password_reset.complete_reset(self.token, form)
        if user is None:
            return render(self.request, "accounts/password_reset_invalid.html")
        password_reset.notify_password_changed(user)
        self.request.session.pop(PASSWORD_RESET_SESSION_KEY, None)
        messages.success(self.request, _("Your password was changed. You can now log in."))
        return redirect(self.get_success_url())


class TwoFactorResetVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL."""

    def get(self, request, token):
        found = second_factor_reset.find_link(token)
        if found is None:
            return render(request, "accounts/two_factor_reset_invalid.html")
        request.session[TWO_FACTOR_RESET_SESSION_KEY] = found.digest
        return redirect("accounts:two_factor_reset_confirm")


class TwoFactorResetConfirmView(NeverCacheMixin, FormView):
    """The owner confirms the removal of the second factor that an administrator started (D-004)."""

    template_name = "accounts/two_factor_reset_confirm.html"
    form_class = ConfirmForm
    success_url = reverse_lazy("accounts:login")

    @method_decorator(never_cache)
    @method_decorator(no_referrer)
    def dispatch(self, request, *args, **kwargs):
        digest = request.session.get(TWO_FACTOR_RESET_SESSION_KEY)
        self.token = second_factor_reset.find_link_by_digest(digest) if digest else None
        if self.token is None:
            return render(request, "accounts/two_factor_reset_invalid.html")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        if second_factor_reset.confirm_link(self.token) is None:
            return render(self.request, "accounts/two_factor_reset_invalid.html")
        self.request.session.pop(TWO_FACTOR_RESET_SESSION_KEY, None)
        messages.success(
            self.request,
            _("Two-factor authentication was removed. You can now log in with your password."),
        )
        return redirect(self.get_success_url())


class ProfileView(NeverCacheMixin, LoginRequiredMixin, TemplateView):
    """The person's own profile, read only; the account is always request.user."""

    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            account=self.request.user,
            second_factor_active=second_factor.has_second_factor(self.request.user),
            **kwargs,
        )


class PasswordChangeView(NeverCacheMixin, LoginRequiredMixin, FormView):
    """Change the password of the logged-in account after asking for the current one."""

    template_name = "accounts/password_change.html"
    form_class = ChangePasswordForm
    success_url = reverse_lazy("accounts:profile")

    @method_decorator(
        sensitive_post_parameters("current_password", "new_password1", "new_password2")
    )
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            required=self.request.user.password_change_required, **kwargs
        )

    def form_valid(self, form):
        with transaction.atomic():
            # The row is locked and read again, so that the new password is written to the
            # current state of the account.
            user = User._base_manager.select_for_update().get(pk=self.request.user.pk)
            form.user = user
            # An initial password that had to be replaced is replaced (D-005).
            user.password_change_required = False
            form.save()
            # Links of earlier password resets must not work after the change.
            VerificationToken.objects.filter(
                user=user, purpose=VerificationToken.Purpose.PASSWORD_RESET
            ).delete()
            password_reset.notify_password_changed(user)
            security_log.record(SecurityEvent.Event.PASSWORD_CHANGED, account=user)
        # Renews the session ID and keeps this session; Django derives the session hash from
        # the password, so every other session of the account ends (docs/SECURITY.md).
        update_session_auth_hash(self.request, user)
        messages.success(self.request, _("Your password was changed."))
        return redirect(self.get_success_url())


class TwoFactorMixin(NeverCacheMixin, LoginRequiredMixin):
    """Pages of the second factor; the account is always request.user."""

    @method_decorator(sensitive_post_parameters("current_password", "otp_token"))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


class ReauthenticationMixin(TwoFactorMixin):
    """A form view whose form asks for the password and the second factor again."""

    form_class = ReauthenticationForm

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "request": self.request}


class TwoFactorOverviewView(TwoFactorMixin, TemplateView):
    template_name = "accounts/two_factor.html"

    def get_context_data(self, **kwargs):
        user = self.request.user
        return super().get_context_data(
            active=second_factor.has_second_factor(user),
            required=second_factor.requires_second_factor(user),
            remaining_codes=totp.remaining_recovery_codes(user),
            **kwargs,
        )


class TwoFactorSetupView(ReauthenticationMixin, FormView):
    """Start setting up the authenticator app, after the password was entered again."""

    template_name = "accounts/two_factor_setup.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and second_factor.has_second_factor(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            device = totp.start_enrolment(self.request.user)
        except totp.SecondFactorError:
            return refused_change(self.request)
        # Only this session may finish the setup (D-040).
        self.request.session[ENROLMENT_SESSION_KEY] = device.pk
        return redirect("accounts:two_factor_confirm")


class TwoFactorConfirmView(TwoFactorMixin, FormView):
    """Show the secret and ask for a first code, which proves that the app works."""

    template_name = "accounts/two_factor_confirm.html"
    form_class = CodeForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self.device = totp.unconfirmed_totp_device(
                request.user, request.session.get(ENROLMENT_SESSION_KEY)
            )
            if self.device is None:
                return redirect("accounts:two_factor_setup")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(secret=totp.secret_for_display(self.device), **kwargs)

    def form_valid(self, form):
        result = totp.confirm_enrolment(
            self.request.user, self.device.pk, form.cleaned_data["otp_token"]
        )
        if result is None:
            form.add_error("otp_token", CodeForm.invalid_code_message)
            return self.form_invalid(form)
        device, codes = result
        self.request.session.pop(ENROLMENT_SESSION_KEY, None)
        # The person just proved to own the app: this session has completed the second factor.
        # The session ID is renewed, as after every second factor.
        self.request.session.cycle_key()
        otp_login(self.request, device)
        return render_recovery_codes(self.request, codes, enrolled=True)


class TwoFactorQrView(TwoFactorMixin, View):
    """The QR code of the secret that is being set up, as an image of its own.

    The policy of the pages allows images only from the own origin (no ``data:`` images,
    D-028), so the code is not embedded in the page.
    """

    def get(self, request):
        device = totp.unconfirmed_totp_device(request.user, request.session.get(ENROLMENT_SESSION_KEY))
        if device is None:
            return HttpResponse(status=404)
        return HttpResponse(totp.qr_code_svg(device), content_type="image/svg+xml")


class RecoveryCodesView(ReauthenticationMixin, FormView):
    """Create new recovery codes; the previous ones become invalid."""

    template_name = "accounts/recovery_codes_new.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not second_factor.has_second_factor(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            codes = totp.renew_recovery_codes(self.request.user)
        except totp.SecondFactorError:
            return refused_change(self.request)
        return render_recovery_codes(self.request, codes, enrolled=False)


class TwoFactorDisableView(ReauthenticationMixin, FormView):
    """Remove the second factor; not possible for staff users and system administrators."""

    template_name = "accounts/two_factor_disable.html"
    success_url = reverse_lazy("accounts:two_factor")

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and (
            second_factor.requires_second_factor(user)
            or not second_factor.has_second_factor(user)
        ):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            totp.disable(self.request.user)
        except totp.SecondFactorError:
            return refused_change(self.request)
        messages.success(self.request, _("Two-factor authentication was removed."))
        return redirect(self.get_success_url())


def refused_change(request):
    """A change that the rules refused, e.g. after a parallel request changed the account."""
    messages.error(request, _("The change could not be made."))
    return redirect("accounts:two_factor")


def render_recovery_codes(request, codes, *, enrolled):
    """Show the codes once; they are stored nowhere that can be displayed again."""
    return render(
        request,
        "accounts/recovery_codes.html",
        {"codes": [totp.format_recovery_code(code) for code in codes], "enrolled": enrolled},
    )
