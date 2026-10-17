"""Views of the portal accounts (docs/PROJECT.md, "User Functions").

Every view that shows account data or handles a token is marked as not cacheable. Views of
logged-in persons only ever use ``request.user``; no view takes an account identifier from the
URL or the request (docs/SECURITY.md, section 4).
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.hashers import make_password
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView

from leg_platform.accounts import (
    email_change,
    password_reset,
    rate_limits,
    resend,
    second_factor,
    second_factor_reset,
    security_log,
    session_timeout,
    tasks,
    totp,
)
from leg_platform.accounts.forms import (
    CodeForm,
    ConfirmForm,
    LoginForm,
    NewPasswordForm,
    PasswordResetRequestForm,
    RegistrationDetailsForm,
    RegistrationForm,
    RegistrationPasswordForm,
    password_rules,
)
from leg_platform.accounts.login_protection import axes_client_ip
from leg_platform.accounts.mail_queue import submit_mail_request
from leg_platform.accounts.normalization import normalize_email_address
from leg_platform.accounts.password_reset import PASSWORD_RESET_LIFETIME
from leg_platform.accounts.portal_settings import legal_links
from leg_platform.accounts.registration import REGISTRATION_LINK_LIFETIME
from leg_platform.accounts.models import (
    MailJob,
    SecurityEvent,
    User,
    VerificationToken,
)

# The verification token's digest is kept in the session between the link and the form, so that
# the token itself is not in the URL of the page that is shown (docs/SECURITY.md, section 6;
# the approach of Django's password reset view).
REGISTRATION_SESSION_KEY = "registration_token_digest"
# The password chosen on the second step, as a hash and bound to the verification link, until the
# last step creates the account (D-058). Never the password itself.
REGISTRATION_PASSWORD_SESSION_KEY = "registration_password_hash"
# The address of the privacy notice that the details page showed, bound to the link: the consent
# is stored with this address, not with the one that is current when the form is posted (B6).
REGISTRATION_PRIVACY_SESSION_KEY = "registration_privacy_url"
PASSWORD_RESET_SESSION_KEY = "password_reset_token_digest"
TWO_FACTOR_RESET_SESSION_KEY = "two_factor_reset_token_digest"
EMAIL_CHANGE_SESSION_KEY = "email_change_token_digest"
EMAIL_CHANGE_STOP_SESSION_KEY = "email_change_stop_token_digest"
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

    Every response of such a view is a redirect (D-067): no page is ever rendered under a token
    URL, so no template has to leave out its forms for this policy.
    """

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


class HomeView(NeverCacheMixin, TemplateView):
    """The start page (D-057): a greeting, the open tasks as tiles, or the empty state.

    A person without a login is sent to the login page. Everything shown belongs to
    ``request.user``; nothing is taken from the request.
    """

    template_name = "accounts/home.html"

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        user = self.request.user
        return super().get_context_data(
            account=user,
            tasks=tasks.open_tasks(user),
            address_line=user.street_address_display,
            **kwargs,
        )


class HideTaskView(NeverCacheMixin, LoginRequiredMixin, View):
    """"Hide" on a task tile: the task is gone for good for this account (D-057).

    POST only, with the CSRF check of Django. The account is always ``request.user``; the key
    must be one of the known tasks, every other key is a 404.
    """

    http_method_names = ["post"]

    def handle_no_permission(self):
        # Without a login the person goes to the login page, without "?next": a POST target is
        # no page to return to (security review of phase 2, N-1).
        return redirect(settings.LOGIN_URL)

    def post(self, request, key):
        if key not in tasks.TASKS_BY_KEY:
            raise Http404
        tasks.hide(request.user, key)
        return redirect("home")


class PortalLoginView(NeverCacheMixin, LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    # Django warns that redirecting logged-in persons away from the login page can allow
    # social media fingerprinting (docs/ARCHITECTURE.md, "Deliberately Open").
    redirect_authenticated_user = False

    def form_valid(self, form):
        user = form.get_user()
        # The wish of the form goes on the request object, not into the session: nothing about
        # it stays behind if the login is not completed (D-056).
        wish = form.cleaned_data.get("remember_me") is True
        if not second_factor.has_second_factor(user):
            setattr(self.request, session_timeout.STAY_WISH_ATTRIBUTE, wish)
            return super().form_valid(form)
        # No login() yet: that follows the second factor (D-017). The wish waits in the pending
        # login.
        second_factor.begin_pending_login(
            self.request, user, self.get_redirect_url(), stay=wish
        )
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
        second_factor.complete_login(self.request, self.pending.user, device, stay=self.pending.stay)
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
                "You used a recovery code. %(count)d code is left. You can create new codes in your profile.",
                "You used a recovery code. %(count)d codes are left. You can create new codes in your profile.",
                remaining,
            ) % {"count": remaining}
        else:
            text = _(
                "You used a recovery code. You have no recovery codes left. Create new codes in your profile."
            )
        messages.info(self.request, text)


class PortalLogoutView(NeverCacheMixin, LogoutView):
    """Logout only by POST with CSRF protection (Django's LogoutView)."""

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        # The message is stored for the next page; the session of the person has ended.
        messages.info(request, _("You have been logged out."))
        return response


class MailRequestView(NeverCacheMixin, FormView):
    """A form that asks for an address and has a link sent to it: registration step 1 and the
    request of a new password (D-058).

    The answer is the same for every address and for a script (security review of phase 3):

    * a request for the address that this browser entered less than a pause ago stores no new job
      and leads to the same page (the page offers "send again" after the pause);
    * a hit of the bot check (``bot_check``) leads to the same page without a job;
    * only the limit of the client address and of the instance is shown, as HTTP 429, and then
      nothing is stored, not even the session of the client.
    """

    kind = None
    purpose = None

    def form_valid(self, form):
        address = form.cleaned_data["email"]
        pending = resend.pending(self.request, self.purpose)
        within_pause = (
            not form.automated
            and pending is not None
            and pending.wait > 0
            and normalize_email_address(pending.address) == normalize_email_address(address)
        )
        if form.automated or within_pause:
            # Nothing is stored for the mail, but the answer writes the session: the requests of
            # this kind are counted per client address, and above the limit the person (or the
            # script) gets the refusal without a session (security review of phase 4, M4).
            if not rate_limits.consume(
                rate_limits.SCOPE_MAIL_SILENT,
                rate_limits.ip_key(axes_client_ip(self.request)),
                rate_limits.MAIL_SILENT_LIMITS,
            ):
                return self.refused(form)
            if within_pause:
                return super().form_valid(form)
        elif not submit_mail_request(self.request, self.kind, address):
            return self.refused(form)
        resend.remember(self.request, self.purpose, address)
        return super().form_valid(form)

    def refused(self, form):
        form.add_error(None, _("Too many requests, please try again later."))
        return self.render_to_response(self.get_context_data(form=form), status=429)


class RegistrationView(MailRequestView):
    """Step 1 of the registration: the address (D-058)."""

    template_name = "accounts/registration.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("accounts:registration_sent")
    kind = MailJob.Kind.REGISTRATION
    purpose = resend.REGISTRATION

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            lifetime_hours=int(REGISTRATION_LINK_LIFETIME.total_seconds() // 3600), **kwargs
        )


class RegistrationInvalidView(NeverCacheMixin, FormView):
    """"Link no longer valid": asks for the address again, with a form that posts to step 1.

    A page of its own, not the response of the link: the response of a link with a token has
    ``Referrer-Policy: no-referrer``, and a form on such a page would be refused by the CSRF
    check (D-054). The link redirects here, so the token is not in this URL.
    """

    template_name = "accounts/registration_invalid.html"
    form_class = RegistrationForm


class EmailSentView(NeverCacheMixin, TemplateView):
    """The same page for every address (docs/PROJECT.md, "Registration").

    One page for the registration and the password reset: it says what a person needs to know
    whatever happened, and states nothing that depends on whether the address belongs to an
    account. It shows the address that this browser entered and offers "send again" after a
    pause (``leg_platform.accounts.resend``). ``purpose`` and ``lifetime`` are set in the URL
    configuration.
    """

    template_name = "accounts/email_sent.html"
    purpose = None
    lifetime = None

    def get_context_data(self, **kwargs):
        state = resend.pending(self.request, self.purpose)
        registration = self.purpose == resend.REGISTRATION
        return super().get_context_data(
            purpose=self.purpose,
            lifetime_hours=int(self.lifetime.total_seconds() // 3600),
            address=state.address if state else "",
            wait_seconds=state.wait if state else 0,
            cooldown_seconds=resend.RESEND_COOLDOWN_SECONDS,
            resend_url=reverse("accounts:registration_resend" if registration else "accounts:password_reset_resend"),
            other_address_url=reverse("accounts:registration" if registration else "accounts:password_reset"),
            progress_step=1 if registration else 0,
            **kwargs,
        )


class ResendView(NeverCacheMixin, View):
    """"Send the email again": only after the pause, and only to the address of this browser.

    Nothing is sent before the pause is over (the server decides, not the countdown of the page);
    the limits of every request apply as well (D-038).
    """

    http_method_names = ["post"]
    purpose = None
    kind = None
    start_url = None
    sent_url = None

    def post(self, request):
        state = resend.pending(request, self.purpose)
        if state is None:
            return redirect(self.start_url)
        if state.wait > 0:
            return redirect(self.sent_url)
        if submit_mail_request(request, self.kind, state.address):
            resend.remember(request, self.purpose, state.address)
        return redirect(self.sent_url)


class RegistrationVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL.

    Mail scanners and link previews open links without any person acting; the registration is
    completed only when the last form is submitted. The link stays valid until then (D-058): a
    scanner, the "back" button or a lost session must not use it up.
    """

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.REGISTRATION)
        if found is None or found.user.status != User.Status.UNVERIFIED:
            clear_registration_session(request)
            return redirect("accounts:registration_invalid")
        # A new session ID for the person who opens the link; the data of the session stays
        # (security review of phase 3, B1).
        request.session.cycle_key()
        request.session[REGISTRATION_SESSION_KEY] = found.digest
        # A new link starts the steps again: a password held for an earlier link is dropped.
        request.session.pop(REGISTRATION_PASSWORD_SESSION_KEY, None)
        request.session.pop(REGISTRATION_PRIVACY_SESSION_KEY, None)
        return redirect("accounts:registration_password")


def clear_registration_session(request):
    """Remove everything of a registration from the session: the link, the held password hash, the
    notice shown, and the address of the request (security review of phase 3, B2)."""
    for key in (
        REGISTRATION_SESSION_KEY,
        REGISTRATION_PASSWORD_SESSION_KEY,
        REGISTRATION_PRIVACY_SESSION_KEY,
    ):
        request.session.pop(key, None)
    resend.forget(request, resend.REGISTRATION)


class RegistrationStepMixin(NeverCacheMixin):
    """Steps 2 and 3: they belong to the verified link that is in the session.

    The form belongs to the registration it was rendered for. Opening another link in the same
    browser replaces the token in the session; a form rendered for the earlier link must then not
    be saved into the account of the later one.
    """

    @method_decorator(never_cache)
    @method_decorator(sensitive_post_parameters("password1", "password2"))
    def dispatch(self, request, *args, **kwargs):
        self.token = self.find_token(request)
        if self.token is None:
            return self.invalid(request)
        if request.method == "POST" and request.POST.get("registration") != str(self.token.pk):
            return self.invalid(request)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def invalid(request):
        """A person who is logged in (the account was just created, the page was reloaded or "back"
        was used) goes to the start page; anybody else to the page "link no longer valid"."""
        if request.user.is_authenticated:
            return redirect("home")
        clear_registration_session(request)
        return redirect("accounts:registration_invalid")

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

    def get_context_data(self, **kwargs):
        # The address is shown but cannot be changed here: it is the one that was verified.
        return super().get_context_data(
            account_email=self.token.user.email, registration_ref=self.token.pk, **kwargs
        )


class RegistrationPasswordView(RegistrationStepMixin, FormView):
    """Step 2 of the registration: the password (D-058).

    The password is checked here and kept as a hash in the session, bound to the verification
    link, until the last step creates the account. The account is not activated before.
    """

    template_name = "accounts/registration_password.html"
    form_class = RegistrationPasswordForm
    success_url = reverse_lazy("accounts:registration_complete")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.token.user}

    def get_context_data(self, **kwargs):
        return super().get_context_data(progress_step=2, password_rules=password_rules(), **kwargs)

    def form_valid(self, form):
        self.request.session[REGISTRATION_PASSWORD_SESSION_KEY] = {
            "digest": self.token.digest,
            "hash": make_password(form.cleaned_data["password1"]),
        }
        return super().form_valid(form)


class RegistrationCompleteView(RegistrationStepMixin, FormView):
    """Step 3 of the registration: name, address and consent; "Create account" (D-058)."""

    template_name = "accounts/registration_complete.html"
    form_class = RegistrationDetailsForm
    success_url = reverse_lazy("home")

    def get(self, request, *args, **kwargs):
        if self.held_password_hash() is None:
            return redirect("accounts:registration_password")
        # The address of the privacy notice that this page shows is the one the consent is stored
        # with (B6): it is kept with the link until the form is posted.
        request.session[REGISTRATION_PRIVACY_SESSION_KEY] = {
            "digest": self.token.digest,
            "url": legal_links(request).privacy_url,
        }
        return super().get(request, *args, **kwargs)

    def shown_privacy_url(self):
        shown = self.request.session.get(REGISTRATION_PRIVACY_SESSION_KEY)
        if (
            isinstance(shown, dict)
            and shown.get("digest") == self.token.digest
            and isinstance(shown.get("url"), str)
        ):
            return shown["url"]
        return legal_links(self.request).privacy_url

    def held_password_hash(self):
        """The hash of the password of step 2 for this link, or None."""
        held = self.request.session.get(REGISTRATION_PASSWORD_SESSION_KEY)
        if (
            isinstance(held, dict)
            and isinstance(held.get("hash"), str)
            and held.get("digest") == self.token.digest
        ):
            return held["hash"]
        return None

    def post(self, request, *args, **kwargs):
        if self.held_password_hash() is None:
            return redirect("accounts:registration_password")
        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.token.user
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(progress_step=3, **kwargs)

    def form_valid(self, form):
        password_hash = self.held_password_hash()
        privacy_url = self.shown_privacy_url()
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
                return self.invalid(self.request)
            # The consent: the time and the address of the privacy notice that was shown.
            # Only the fields of the form are written, from the account that was read under the lock (L-2):
            # the instance of the form was loaded when the page was opened and must not write back any other
            # column (a password, a status or a mark changed in the meantime).
            form.instance.privacy_accepted_at = timezone.now()
            form.instance.privacy_accepted_url = privacy_url
            written = (
                *form._meta.fields, "swiss_postal_code", "foreign_postal_code",
                "privacy_accepted_at", "privacy_accepted_url",
            )
            for name in written:
                setattr(user, name, getattr(form.instance, name))
            user.save(update_fields=list(written))
            user.complete_registration(password_hash=password_hash)
            token.delete()
            security_log.record(SecurityEvent.Event.REGISTRATION_COMPLETED, account=user)
        clear_registration_session(self.request)
        # The person has just proved control of the mailbox and chosen the password, so the session
        # logs in as after a successful login (owner decision of 2026-10-14, D-055): the session ID
        # is renewed, the session times and the time of the authentication are written by the login
        # signal, no failed attempt is recorded, and the account has no privileges. The link was
        # consumed above, so it cannot log in again.
        login(self.request, user, backend=second_factor.LOGIN_BACKEND)
        return redirect(self.get_success_url())


class PasswordResetRequestView(MailRequestView):
    """Asks only for the address; the request is handled in the background (D-037)."""

    template_name = "accounts/password_reset.html"
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy("accounts:password_reset_sent")
    kind = MailJob.Kind.PASSWORD_RESET
    purpose = resend.PASSWORD_RESET

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            lifetime_hours=int(PASSWORD_RESET_LIFETIME.total_seconds() // 3600), **kwargs
        )


class PasswordResetInvalidView(NeverCacheMixin, FormView):
    """"Link no longer valid": asks for the address again (a page of its own, see
    ``RegistrationInvalidView``)."""

    template_name = "accounts/password_reset_invalid.html"
    form_class = PasswordResetRequestForm


class PasswordResetVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL."""

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(
            token, VerificationToken.Purpose.PASSWORD_RESET
        )
        if found is None or found.user.status != User.Status.ACTIVE:
            request.session.pop(PASSWORD_RESET_SESSION_KEY, None)
            resend.forget(request, resend.PASSWORD_RESET)
            return redirect("accounts:password_reset_invalid")
        # A new session ID for the person who opens the link; the data of the session stays
        # (security review of phase 3, B1).
        request.session.cycle_key()
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
            return redirect("accounts:password_reset_invalid")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.token.user
        return kwargs

    def get_context_data(self, **kwargs):
        return super().get_context_data(password_rules=password_rules(), **kwargs)

    def form_valid(self, form):
        user = password_reset.complete_reset(self.token, form)
        if user is None:
            return redirect("accounts:password_reset_invalid")
        password_reset.notify_password_changed(user)
        self.request.session.pop(PASSWORD_RESET_SESSION_KEY, None)
        resend.forget(self.request, resend.PASSWORD_RESET)
        messages.success(self.request, _("Your password was changed. You can now log in."))
        return redirect(self.get_success_url())


def find_confirmation_token(raw_token=None, digest=None):
    """The valid token of a confirmation link of an address, of either kind (D-059)."""
    for purpose in email_change.CONFIRMATION_PURPOSES:
        if digest is not None:
            found = VerificationToken.objects.find_valid_by_digest(digest, purpose)
        else:
            found = VerificationToken.objects.find_valid(raw_token, purpose)
        if found is not None:
            return found
    return None


class LinkNoLongerValidView(NeverCacheMixin, TemplateView):
    """The fixed page "Link no longer valid" of a confirmation flow (D-067).

    Every link of an email that is invalid, expired, used or replaced redirects here, so the
    page is the same for everyone and for every cause: it is not an oracle for tokens, and it
    reads nothing from the request, the session or the database. The page is a normal page
    (no token in its URL, project default ``same-origin``). ``template_name`` is set in the URL
    configuration.
    """

    http_method_names = ["get", "head"]


class EmailChangeVerifyView(TokenPageMixin, View):
    """Opening the link remembers the digest and removes the token from the address bar."""

    def get(self, request, token):
        found = find_confirmation_token(raw_token=token)
        if found is None or found.user.status != User.Status.ACTIVE:
            return redirect("accounts:email_change_invalid")
        # A new session ID for the person who opens the link; the data of the session stays.
        request.session.cycle_key()
        request.session[EMAIL_CHANGE_SESSION_KEY] = found.digest
        return redirect("accounts:email_change_confirm")


class EmailChangeConfirmView(NeverCacheMixin, FormView):
    template_name = "accounts/email_change_confirm.html"
    form_class = ConfirmForm

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        digest = request.session.get(EMAIL_CHANGE_SESSION_KEY)
        self.token = find_confirmation_token(digest=digest) if digest else None
        if self.token is None or self.token.user.status != User.Status.ACTIVE:
            # A state that is no longer valid is not kept (it is re-validated on every use anyway).
            request.session.pop(EMAIL_CHANGE_SESSION_KEY, None)
            return redirect("accounts:email_change_invalid")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            new_email=self.token.target_email,
            secondary=self.token.purpose == VerificationToken.Purpose.SECONDARY_EMAIL,
            **kwargs,
        )

    def form_valid(self, form):
        secondary = self.token.purpose == VerificationToken.Purpose.SECONDARY_EMAIL
        address = self.token.target_email
        user = email_change.confirm_change(self.token)
        self.request.session.pop(EMAIL_CHANGE_SESSION_KEY, None)
        if user is None:
            return redirect("accounts:email_change_invalid")
        # The result page (WL-18). A login address ends every session of the account, so the
        # person logs in; a second address changes nothing about the sessions.
        if secondary:
            return render(self.request, "accounts/email_second_confirmed.html", {"new_email": address})
        return render(
            self.request,
            "accounts/email_change_done.html",
            {"new_email": user.email},
        )


class EmailChangeStopVerifyView(TokenPageMixin, View):
    """The link of the notice about a requested change: it only remembers the token (D-059).

    As with every link of a mail, opening it changes nothing (link scanners open links); the
    change is stopped by the button of the next page.
    """

    def get(self, request, token):
        found = VerificationToken.objects.find_valid(
            token, VerificationToken.Purpose.EMAIL_CHANGE_STOP
        )
        # The link only has a meaning while a request is open (security review of phase 4, H1).
        if (
            found is None
            or found.user.status != User.Status.ACTIVE
            or not email_change.has_open_request(found.user)
        ):
            return redirect("accounts:email_change_stop_invalid")
        request.session.cycle_key()
        request.session[EMAIL_CHANGE_STOP_SESSION_KEY] = found.digest
        return redirect("accounts:email_change_stop_confirm")


class EmailChangeStopConfirmView(NeverCacheMixin, FormView):
    """Stops every pending change of the account, without a login (D-059)."""

    template_name = "accounts/email_change_stop_confirm.html"
    form_class = ConfirmForm

    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        digest = request.session.get(EMAIL_CHANGE_STOP_SESSION_KEY)
        self.token = (
            VerificationToken.objects.find_valid_by_digest(
                digest, VerificationToken.Purpose.EMAIL_CHANGE_STOP
            )
            if digest
            else None
        )
        if (
            self.token is None
            or self.token.user.status != User.Status.ACTIVE
            or not email_change.has_open_request(self.token.user)
        ):
            request.session.pop(EMAIL_CHANGE_STOP_SESSION_KEY, None)
            return redirect("accounts:email_change_stop_invalid")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        stopped = email_change.stop_all(self.token.user)
        self.request.session.pop(EMAIL_CHANGE_STOP_SESSION_KEY, None)
        if stopped is None:
            # The request ended between the page and the button: nothing was stopped, and no
            # session ended.
            return redirect("accounts:email_change_stop_invalid")
        return render(self.request, "accounts/email_change_stopped.html")


class TwoFactorMixin(NeverCacheMixin, LoginRequiredMixin):
    """Pages of the second factor; the account is always request.user."""

    @method_decorator(sensitive_post_parameters("current_password", "otp_token"))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


class TwoFactorResetVerifyView(TokenPageMixin, View):
    """Opening the link changes nothing; it only remembers the token and leaves the URL."""

    def get(self, request, token):
        found = second_factor_reset.find_link(token)
        if found is None:
            return redirect("accounts:two_factor_reset_invalid")
        request.session.cycle_key()
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
            request.session.pop(TWO_FACTOR_RESET_SESSION_KEY, None)
            return redirect("accounts:two_factor_reset_invalid")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        confirmed = second_factor_reset.confirm_link(self.token)
        self.request.session.pop(TWO_FACTOR_RESET_SESSION_KEY, None)
        if confirmed is None:
            return redirect("accounts:two_factor_reset_invalid")
        messages.success(
            self.request,
            _("Two-factor authentication was removed. You can now log in with your password."),
        )
        return redirect(self.get_success_url())
