"""Views of the portal accounts (docs/PROJECT.md, "User Functions").

Every view that shows account data or handles a token is marked as not cacheable. Views of
logged-in persons only ever use ``request.user``; no view takes an account identifier from the
URL or the request (docs/SECURITY.md, section 4).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView

from leg_platform.accounts import password_reset
from leg_platform.accounts.forms import (
    CompleteRegistrationForm,
    LoginForm,
    NewPasswordForm,
    PasswordResetRequestForm,
    RegistrationForm,
)
from leg_platform.accounts.mail_queue import submit_mail_request
from leg_platform.accounts.models import MailJob, User, VerificationToken

# The verification token's digest is kept in the session between the link and the form, so that
# the token itself is not in the URL of the page that is shown (docs/SECURITY.md, section 6;
# the approach of Django's password reset view).
REGISTRATION_SESSION_KEY = "registration_token_digest"
PASSWORD_RESET_SESSION_KEY = "password_reset_token_digest"


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


class ProfileView(NeverCacheMixin, LoginRequiredMixin, TemplateView):
    """The person's own profile, read only; the account is always request.user."""

    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(account=self.request.user, **kwargs)
