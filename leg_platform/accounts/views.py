"""Views of the portal accounts (docs/PROJECT.md, "User Functions").

Every view that shows account data or handles a token is marked as not cacheable. Views of
logged-in persons only ever use ``request.user``; no view takes an account identifier from the
URL or the request (docs/SECURITY.md, section 4).
"""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
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
from django.utils.translation import gettext_lazy, ngettext
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView
from django_otp import login as otp_login

from leg_platform.addresses.services import check_address
from leg_platform.accounts import (
    email_change,
    password_reset,
    second_factor,
    second_factor_reset,
    security_log,
    session_timeout,
    totp,
)
from leg_platform.accounts.forms import (
    ChangePasswordForm,
    CodeForm,
    CompleteRegistrationForm,
    ConfirmForm,
    EmailChangeForm,
    LoginForm,
    NewPasswordForm,
    PasswordResetRequestForm,
    ProfileForm,
    ReauthenticationForm,
    RegistrationForm,
    SetupCodeForm,
    StartSetupForm,
)
from leg_platform.accounts.mail_queue import submit_mail_request
from leg_platform.accounts.models import (
    AccountIntegrityError,
    MailJob,
    SecurityEvent,
    User,
    VerificationToken,
)

# The verification token's digest is kept in the session between the link and the form, so that
# the token itself is not in the URL of the page that is shown (docs/SECURITY.md, section 6;
# the approach of Django's password reset view).
REGISTRATION_SESSION_KEY = "registration_token_digest"
PASSWORD_RESET_SESSION_KEY = "password_reset_token_digest"
TWO_FACTOR_RESET_SESSION_KEY = "two_factor_reset_token_digest"
EMAIL_CHANGE_SESSION_KEY = "email_change_token_digest"
ENROLMENT_SESSION_KEY = "two_factor_enrolment_device"


def no_referrer(view):
    """Pages whose URL contains a token send no referrer (OWASP Forgot Password Cheat Sheet,
    "URL Tokens"); the header of the view wins over the project default.

    Only for the views that are opened through the link of an email (``TokenPageMixin``). It must
    not be set on a page that contains a form: for a form POST the browser takes the Referrer
    Policy of the page that contains the form, and with ``no-referrer`` it sends ``Origin: null``,
    which Django's CSRF check refuses (Fetch standard, "append a request Origin header"; Django
    ``CsrfViewMiddleware._origin_verified``). The pages after the redirect have no token in
    their URL and use the project default ``same-origin`` (docs/DECISIONS.md, D-054).
    """

    def wrapped(request, *args, **kwargs):
        # Templates of this response leave out forms that post to another URL: with this policy
        # the browser would send "Origin: null" and the CSRF check would refuse them.
        request.no_referrer_page = True
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
        # The wish is written before login() and read by the session signal (D-056); it is
        # never taken from the client after that.
        wish = form.cleaned_data.get("remember_me") is True
        if not second_factor.has_second_factor(user):
            self.request.session[session_timeout.STAY_REQUESTED_KEY] = wish
            return super().form_valid(form)
        # No login() yet: that follows the second factor (D-017).
        second_factor.begin_pending_login(self.request, user, self.get_redirect_url())
        self.request.session[session_timeout.STAY_REQUESTED_KEY] = wish
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
        if totp.is_recovery_code_device(device):
            # The person has just proved to own the code, so the number of the codes that are left
            # is theirs to know. Nothing about the check changes.
            self._tell_about_the_emergency_code(self.pending.user)
        target = self.pending.next_url
        if not url_has_allowed_host_and_scheme(
            target, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
        ):
            target = ""
        return redirect(target or settings.LOGIN_REDIRECT_URL)

    def _tell_about_the_emergency_code(self, user):
        remaining = totp.remaining_recovery_codes(user)
        if remaining:
            text = ngettext(
                "You used an emergency code. %(count)d code is left. You can create new codes in your profile.",
                "You used an emergency code. %(count)d codes are left. You can create new codes in your profile.",
                remaining,
            ) % {"count": remaining}
        else:
            text = _(
                "You used an emergency code. You have no emergency codes left. Create new codes in your profile."
            )
        messages.info(self.request, text)


class PortalLogoutView(NeverCacheMixin, LogoutView):
    """Logout only by POST with CSRF protection (Django's LogoutView)."""

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        # The message is stored for the next page; the session of the person has ended.
        messages.info(request, _("You have been logged out."))
        return response


class RegistrationView(NeverCacheMixin, FormView):
    template_name = "accounts/registration.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:registration_sent")

    def form_valid(self, form):
        submit_mail_request(self.request, MailJob.Kind.REGISTRATION, form.cleaned_data["email"])
        return super().form_valid(form)


class EmailSentView(NeverCacheMixin, TemplateView):
    """The same page for every address (docs/PROJECT.md, "Registration").

    One text for the registration and the password reset: it says what a person needs to know
    whatever happened, and states nothing that depends on whether the address belongs to an
    account. ``purpose`` and ``lifetime`` are set in the URL configuration.
    """

    template_name = "accounts/email_sent.html"
    purpose = None
    lifetime = None

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            purpose=self.purpose,
            lifetime_hours=int(self.lifetime.total_seconds() // 3600),
            **kwargs,
        )


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
    success_url = reverse_lazy("accounts:welcome")

    # The responses of this method come before the mixin's dispatch, so it is marked as not
    # cacheable itself. The password fields are masked in error reports.
    @method_decorator(never_cache)
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
        self._show_address_warning(form.cleaned_data)
        # The person has just proved control of the mailbox (single-use link) and chosen the
        # password, so the session logs in as after a successful login (owner decision of
        # 2026-10-14, D-055): the session ID is renewed, the session times and the time of the
        # authentication are written by the login signal, no failed attempt is recorded, and the
        # account has no privileges. The link was consumed above, so it cannot log in again.
        login(self.request, user, backend=second_factor.LOGIN_BACKEND)
        return redirect("accounts:welcome")

    def _show_address_warning(self, cleaned_data):
        result = check_address(
            street=cleaned_data.get("street", ""),
            house_number=cleaned_data.get("house_number", ""),
            postal_code=cleaned_data.get("postal_code", ""),
            locality=cleaned_data.get("town", ""),
            country=cleaned_data.get("country", "CH"),
        )
        if result["warning"]:
            messages.warning(self.request, result["warning"])


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


class EmailChangeVerifyView(TokenPageMixin, View):
    """Opening the link remembers the digest and removes the token from the address bar."""

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.EMAIL_CHANGE)
        if found is None or found.user.status != User.Status.ACTIVE:
            return render(request, "accounts/email_change_invalid.html")
        request.session[EMAIL_CHANGE_SESSION_KEY] = found.digest
        return redirect("accounts:email_change_confirm")


class EmailChangeConfirmView(NeverCacheMixin, FormView):
    template_name = "accounts/email_change_confirm.html"
    form_class = ConfirmForm

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        digest = request.session.get(EMAIL_CHANGE_SESSION_KEY)
        self.token = (
            VerificationToken.objects.find_valid_by_digest(
                digest, VerificationToken.Purpose.EMAIL_CHANGE
            )
            if digest
            else None
        )
        if self.token is None or self.token.user.status != User.Status.ACTIVE:
            return render(request, "accounts/email_change_invalid.html")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(new_email=self.token.target_email, **kwargs)

    def form_valid(self, form):
        user = email_change.confirm_change(self.token)
        self.request.session.pop(EMAIL_CHANGE_SESSION_KEY, None)
        if user is None:
            return render(self.request, "accounts/email_change_invalid.html")
        messages.success(self.request, _("Your email address was changed. Please log in again."))
        return redirect("accounts:login")


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


class WelcomeView(NeverCacheMixin, LoginRequiredMixin, TemplateView):
    """The page after the registration: one recommended next step, the extra protection.

    Starting the setup is a POST of the button to the setup page; nothing changes here.
    """

    template_name = "accounts/welcome.html"

    def get(self, request, *args, **kwargs):
        if second_factor.has_second_factor(request.user):
            return redirect("accounts:profile")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(account=self.request.user, **kwargs)


TWO_FACTOR_TIP_SESSION_KEY = "two_factor_tip_dismissed"


class ProfileView(NeverCacheMixin, LoginRequiredMixin, TemplateView):
    """The person's own profile, read only; the account is always request.user."""

    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        second_factor_active = second_factor.has_second_factor(self.request.user)
        return super().get_context_data(
            account=self.request.user,
            second_factor_active=second_factor_active,
            # The tip is shown until the person says "not now"; that is remembered in the
            # session only, nothing is stored in the database (D-054).
            show_two_factor_tip=(
                not second_factor_active
                and not self.request.session.get(TWO_FACTOR_TIP_SESSION_KEY)
            ),
            **kwargs,
        )


class TwoFactorTipDismissView(NeverCacheMixin, LoginRequiredMixin, View):
    """"Not now" on the tip about two-factor authentication: hides it for this session."""

    http_method_names = ["post"]

    def post(self, request):
        request.session[TWO_FACTOR_TIP_SESSION_KEY] = True
        return redirect("accounts:profile")


class ProfileEditView(NeverCacheMixin, LoginRequiredMixin, FormView):
    """Edit only the permitted profile fields of the logged-in account."""

    template_name = "accounts/profile_edit.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "instance": self.request.user}

    def form_valid(self, form):
        form.save()
        result = check_address(
            street=form.cleaned_data.get("street", ""),
            house_number=form.cleaned_data.get("house_number", ""),
            postal_code=form.cleaned_data.get("postal_code", ""),
            locality=form.cleaned_data.get("town", ""),
            country=form.cleaned_data.get("country", "CH"),
        )
        if result["warning"]:
            messages.warning(self.request, result["warning"])
        messages.success(self.request, _("Your profile was updated."))
        return redirect(self.get_success_url())


class EmailChangeRequestView(ReauthenticationMixin, FormView):
    """Queue an email change request with a generic response for every target address."""

    template_name = "accounts/email_change_request.html"
    form_class = EmailChangeForm
    success_url = reverse_lazy("accounts:email_change_sent")

    def form_valid(self, form):
        submit_mail_request(
            self.request,
            MailJob.Kind.EMAIL_CHANGE_REQUEST,
            form.cleaned_data["email"],
            account=self.request.user,
        )
        security_log.record(
            SecurityEvent.Event.EMAIL_CHANGE_REQUESTED, account=self.request.user
        )
        return super().form_valid(form)


class EmailChangeSentView(NeverCacheMixin, LoginRequiredMixin, TemplateView):
    template_name = "accounts/email_change_sent.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            lifetime_hours=int(email_change.EMAIL_CHANGE_LIFETIME.total_seconds() // 3600),
            **kwargs,
        )


class AccountDeleteForm(ReauthenticationForm):
    confirm = forms.BooleanField(
        label=gettext_lazy("I understand that my account will be deleted."),
        error_messages={
            "required": gettext_lazy(
                "Please tick the box to confirm that you want to delete your account."
            )
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The confirmation is given again with every attempt: after an error the box is never
        # shown as ticked, so that a repeated submit is never a confirmation by habit.
        self.fields["confirm"].widget = forms.CheckboxInput(
            attrs=self.fields["confirm"].widget.attrs, check_test=lambda value: False
        )


class AccountDeleteView(ReauthenticationMixin, FormView):
    """Delete the logged-in account, or keep it deactivated under the retention rule (D-016).

    The page and the message are the same for every account: the person is told that the account
    is deleted and that logging in is no longer possible (owner decision of 2026-10-14, D-055).
    """

    template_name = "accounts/account_delete.html"
    form_class = AccountDeleteForm

    def form_valid(self, form):
        user = self.request.user
        try:
            with transaction.atomic():
                outcome = user.delete_or_deactivate()
                event = (
                    SecurityEvent.Event.ACCOUNT_DEACTIVATED
                    if outcome == "deactivated"
                    else SecurityEvent.Event.ACCOUNT_DELETED
                )
                security_log.record(event, account=user, actor=user)
        except AccountIntegrityError:
            messages.error(self.request, _("The account could not be deleted."))
            return redirect("accounts:profile")
        logout(self.request)
        messages.success(self.request, _("Your account has been deleted."))
        return redirect("accounts:login")


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
            # The row is locked and read again, so that the current password is checked and the
            # new password is written to the current state of the account. The form checked the
            # current password before the lock; a parallel change of the password in between
            # must not be overwritten by a request that gave the old password (D-040).
            user = User._base_manager.select_for_update().get(pk=self.request.user.pk)
            verified = second_factor.verify_password(
                self.request, user, form.cleaned_data["current_password"]
            )
            if verified:
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
        if not verified:
            form.add_error("current_password", ReauthenticationForm.password_error)
            return self.form_invalid(form)
        # Renews the session ID and keeps this session; Django derives the session hash from
        # the password, so every other session of the account ends (docs/SECURITY.md).
        update_session_auth_hash(self.request, user)
        messages.success(self.request, _("Your password was changed."))
        return redirect(self.get_success_url())


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
    """Start setting up the authenticator app.

    A session that authenticated at most five minutes ago starts without the password; every
    other session enters the password first (D-055). Starting is a POST: opening the page changes
    nothing.
    """

    template_name = "accounts/two_factor_setup.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and second_factor.has_second_factor(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def recent(self):
        return second_factor.recently_authenticated(self.request)

    def get_form_class(self):
        return StartSetupForm if self.recent() else ReauthenticationForm

    def post(self, request, *args, **kwargs):
        # The button of the welcome page, the profile and the overview sends no password. If the
        # five minutes are over by then, the password is asked for, without an error message.
        if not self.recent() and "current_password" not in request.POST:
            return self.render_to_response(self.get_context_data(form=self.get_form_class()(
                **{**self.get_form_kwargs(), "data": None, "files": None}
            )))
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        # An account that must have a second factor is told so, and cannot cancel the setup.
        return super().get_context_data(
            required=second_factor.requires_second_factor(self.request.user),
            recent=self.recent(),
            **kwargs,
        )

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
    form_class = SetupCodeForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self.device = totp.unconfirmed_totp_device(
                request.user, request.session.get(ENROLMENT_SESSION_KEY)
            )
            if self.device is None:
                return redirect("accounts:two_factor_setup")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            secret=totp.secret_for_display(self.device),
            required=second_factor.requires_second_factor(self.request.user),
            **kwargs,
        )

    def form_valid(self, form):
        result = totp.confirm_enrolment(
            self.request.user, self.device.pk, form.cleaned_data["otp_token"]
        )
        if result is None:
            form.add_error("otp_token", form.invalid_code_message)
            return self.form_invalid(form)
        device, codes = result
        # Other sessions of the account ended with the confirmation; this session stays (D-044).
        update_session_auth_hash(self.request, self.request.user)
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
        # Other sessions ended with the renewal; this session stays (D-044).
        update_session_auth_hash(self.request, self.request.user)
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
        # Other sessions ended with the removal; this session stays (D-044).
        update_session_auth_hash(self.request, self.request.user)
        messages.success(self.request, _("Two-factor authentication was turned off."))
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
