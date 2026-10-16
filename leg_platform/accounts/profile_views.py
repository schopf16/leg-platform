"""The profile: cards that change in place (D-059; LEG-Design-Vorgaben/navigation, section 3.3).

One page, ``/profile/``, with a card for the personal details, the email addresses, the password,
two-factor authentication and the deletion of the account. A card shows its view or, when the
person asks for it, its form: the state is in the address (``?edit=email``, ``?edit=two_factor&
step=disable`` ...) and in the answers of the forms, rendered by the server, so that the page works
without a script and a reload or "back" keeps the step. Only one card is in an edit state at a time.

The forms post to the addresses of the earlier pages (they still exist, as the places where a card
is changed); a form with errors answers with the profile page in the same state, a success leads
back to the card with a message. The account is always ``request.user``.
"""

import copy

from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django_otp import login as otp_login

from leg_platform.accounts import email_change, second_factor, security_log, totp
from leg_platform.accounts import password_reset
from leg_platform.accounts.forms import (
    AccountDeleteForm,
    ChangePasswordForm,
    EmailChangeForm,
    LostPhoneForm,
    ProfileForm,
    ReauthenticationForm,
    SetupCodeForm,
    StartSetupForm,
    password_rules,
)
from leg_platform.accounts.mail_queue import enqueue_for_account, requests_refused
from leg_platform.accounts.models import (
    AccountIntegrityError,
    MailJob,
    PendingEmailChange,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.portal_settings import legal_links
from leg_platform.accounts.views import (
    ENROLMENT_SESSION_KEY,
    NeverCacheMixin,
    TwoFactorMixin,
)

CARD_ANCHORS = {
    "personal": "card-personal",
    "email": "card-email",
    "password": "card-password",
    "two_factor": "card-two-factor",
    "delete": "card-delete",
}
# The steps of the card of two-factor authentication that a person can open by address; the codes
# are only ever the answer to a form (they are stored nowhere).
TWO_FACTOR_STEPS = ("setup", "app", "codes_new", "disable", "lost_phone")


def card_url(card, step=None):
    """The address of the profile with this card in its edit state (and step)."""
    query = f"?edit={card}" + (f"&step={step}" if step else "")
    return f"{reverse('accounts:profile')}{query}#{CARD_ANCHORS[card]}"


def back_to(card):
    """Redirect to the view of a card after a success."""
    return redirect(f"{reverse('accounts:profile')}#{CARD_ANCHORS[card]}")


def two_factor_state(user, edit, step, session):
    """The state of the card of two-factor authentication that the request asks for, reduced to
    what is possible for this account."""
    active = second_factor.has_second_factor(user)
    if edit != "two_factor":
        return "setup" if second_factor.requires_second_factor(user) and not active else "view"
    if active:
        if step == "codes_new":
            return "codes_new"
        if step == "disable" and not second_factor.requires_second_factor(user):
            return "disable"
        if step == "lost_phone" and not second_factor.requires_second_factor(user):
            return "lost_phone"
        return "view"
    if step == "app" and totp.unconfirmed_totp_device(user, session.get(ENROLMENT_SESSION_KEY)):
        return "app"
    return "setup"


@sensitive_variables("codes")
def render_profile(request, *, edit=None, step=None, forms=None, codes=None, enrolled=False, status=200):
    """The profile page with one card in an edit state (or none); ``forms`` holds bound forms."""
    user = request.user
    forms = dict(forms or {})
    if edit not in CARD_ANCHORS:
        edit = None
    if codes is not None:
        edit, step = "two_factor", "codes"
    only_two_factor = (
        second_factor.requires_second_factor(user) and not second_factor.is_second_factor_complete(user)
    )
    # The messages of the views go into the card that they belong to (the tag "card-<name>"); the
    # rest, and those of a card that is not shown, stay at the top of the page.
    shown = {"two_factor"} if only_two_factor else set(CARD_ANCHORS)
    card_messages = {card: [] for card in CARD_ANCHORS}
    general_messages = []
    for message in messages.get_messages(request):
        tags = (message.extra_tags or "").split()
        card = next((tag[len("card-"):] for tag in tags if tag.startswith("card-")), None)
        if card in shown:
            card_messages[card].append(message)
        else:
            general_messages.append(message)
    context = {
        "general_messages": general_messages,
        **{f"messages_{card}": items for card, items in card_messages.items()},
        "account": user,
        "edit": edit,
        "second_factor_active": second_factor.has_second_factor(user),
        "second_factor_required": second_factor.requires_second_factor(user),
        "only_two_factor": only_two_factor,
        "pending": email_change.pending_changes(user),
        "privacy_url": legal_links(request).privacy_url,
        "password_rules": password_rules(),
    }
    if edit == "personal":
        context["personal_form"] = forms.get("personal") or ProfileForm(instance=copy.copy(user))
    elif edit == "email":
        context["email_form"] = forms.get("email") or EmailChangeForm(request=request)
    elif edit == "password":
        context["password_form"] = forms.get("password") or ChangePasswordForm(
            user, request=request
        )
    elif edit == "delete":
        context["delete_form"] = forms.get("delete") or AccountDeleteForm(request=request)
    if edit == "two_factor" or only_two_factor:
        state = "codes" if codes is not None else two_factor_state(user, edit, step, request.session)
        context["tf_state"] = state
        context["edit"] = "two_factor" if state != "view" else edit
        if state == "codes":
            context["codes"] = [totp.format_recovery_code(code) for code in codes]
            context["enrolled"] = enrolled
        elif state == "app":
            device = totp.unconfirmed_totp_device(user, request.session.get(ENROLMENT_SESSION_KEY))
            context["secret"] = totp.secret_for_display(device)
            context["code_form"] = forms.get("code") or SetupCodeForm()
        elif state == "setup":
            context["recent"] = second_factor.recently_authenticated(request)
            context["setup_form"] = forms.get("setup") or (
                StartSetupForm() if context["recent"] else ReauthenticationForm(request=request)
            )
        elif state in ("codes_new", "disable"):
            context["action_form"] = forms.get("action") or ReauthenticationForm(request=request)
        elif state == "lost_phone":
            context["action_form"] = forms.get("action") or LostPhoneForm(request=request)
    else:
        context["tf_state"] = "view"
    if context["second_factor_active"]:
        context["remaining_codes"] = totp.remaining_recovery_codes(user)
    return render(request, "accounts/profile.html", context, status=status)


class ProfileCardView(NeverCacheMixin, LoginRequiredMixin, View):
    """Base of the views of the cards: a person with a login, no caching, and the fields of the
    passwords and codes masked in error reports."""

    @method_decorator(
        sensitive_post_parameters("current_password", "new_password1", "new_password2", "otp_token")
    )
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
            # The check of a code ended this session (D-062, block 9): whatever the view answered,
            # the person is logged out and told so at the login page.
            second_factor.end_session_for_wrong_codes(request)
            return redirect("accounts:login")
        return response


class ProfileView(ProfileCardView):
    """The profile; ``?edit=<card>&step=<step>`` opens one card in its edit state."""

    def get(self, request):
        return render_profile(
            request, edit=request.GET.get("edit"), step=request.GET.get("step")
        )


class ProfileEditView(ProfileCardView):
    """Save the personal details (name, address, further details); no confirmation is needed."""

    def get(self, request):
        return render_profile(request, edit="personal")

    def post(self, request):
        form = ProfileForm(request.POST, instance=copy.copy(request.user))
        if not form.is_valid():
            return render_profile(request, edit="personal", forms={"personal": form}, status=200)
        form.save()
        messages.success(request, _("Saved."), extra_tags="card-personal")
        return back_to("personal")


class EmailChangeRequestView(ProfileCardView):
    """Request new addresses (or remove the second one) with the password and, where set up, the
    code; the response is the same for every target address (D-059)."""

    def get(self, request):
        return render_profile(request, edit="email")

    def refused(self, request):
        """The answer when the limit of the client address or of the instance is used up (D-038).
        Nothing was stored. The person is logged in, so telling it hints at no account."""
        messages.error(request, _("Too many requests, please try again later."), extra_tags="card-email")
        return render_profile(request, edit="email", status=429)

    def kinds_asked(self, request):
        """The kinds of request that the posted values ask for, judged before the password and the
        code are checked: a refusal must not use up a code of the authenticator app."""
        user = request.user
        kinds = []
        if request.POST.get("email", "").strip().lower() not in ("", user.email):
            kinds.append(MailJob.Kind.EMAIL_CHANGE_REQUEST)
        second = request.POST.get("secondary_email", "").strip().lower()
        if second and second != user.secondary_email:
            kinds.append(MailJob.Kind.SECONDARY_EMAIL_REQUEST)
        return kinds

    def post(self, request):
        if any(requests_refused(request, kind) for kind in self.kinds_asked(request)):
            return self.refused(request)
        form = EmailChangeForm(request.POST, request=request)
        if not form.is_valid():
            return render_profile(request, edit="email", forms={"email": form}, status=200)
        try:
            email_change.request_changes(
                request,
                request.user,
                login=form.login_change,
                secondary=form.secondary_change,
                remove_secondary=form.remove_secondary,
            )
        except email_change.RequestRefused:
            # A limit was reached between the check and the request: everything was rolled back.
            return self.refused(request)
        if form.login_change or form.secondary_change:
            messages.success(
                request,
                _("We sent a confirmation link to the new address. It is valid for 24 hours."),
                extra_tags="card-email",
            )
        else:
            messages.success(request, _("Saved."), extra_tags="card-email")
        return back_to("email")


class EmailChangeCancelView(ProfileCardView):
    """The button "Anfrage abbrechen": ends a pending request of the account (D-059)."""

    http_method_names = ["post"]

    def post(self, request, kind):
        if kind not in PendingEmailChange.Kind.values:
            raise Http404
        email_change.cancel(request.user, kind)
        messages.success(request, _("The request was cancelled."), extra_tags="card-email")
        return back_to("email")


class PasswordChangeView(ProfileCardView):
    """Change the password after asking for the current one: the card "Passwort" (K11).

    An account that must replace its password (D-005) keeps the page of its own, which is the only
    thing it can reach.
    """

    template_name = "accounts/password_change.html"

    def form(self, data=None):
        return ChangePasswordForm(self.request.user, data, request=self.request)

    def required(self):
        return self.request.user.password_change_required

    def get(self, request):
        if not self.required():
            return render_profile(request, edit="password")
        return render(
            request, self.template_name, {"form": self.form(), "required": True}
        )

    def post(self, request):
        # The state of the account at the start of the request: with the code as the proof the
        # password is not typed, so a change of the account in between is found by this value
        # (D-040, D-062).
        started_with = request.user.get_session_auth_hash()
        form = self.form(request.POST)
        if not form.is_valid():
            return self.invalid(form)
        changed_meanwhile = False
        with transaction.atomic():
            # The row is locked and read again, so that the new password is written to the
            # current state of the account. With the password as the proof it is checked again
            # here: a parallel change of the password in between must not be overwritten by a
            # request that gave the old password (D-040). With the code as the proof the code was
            # used up by the form and cannot be checked a second time; the session hash (derived
            # from the password) is compared with the one of the start instead.
            user = User._base_manager.select_for_update().get(pk=request.user.pk)
            if form.proof == "code":
                verified = constant_time_compare(user.get_session_auth_hash(), started_with)
                changed_meanwhile = not verified
            else:
                verified = second_factor.verify_password(
                    request, user, form.cleaned_data["current_password"]
                )
            if verified:
                form.user = user
                # An initial password that had to be replaced is replaced (D-005).
                user.password_change_required = False
                form.save()
                # A request for a new address made before the change must not be confirmed
                # afterwards (security review, final, M-A).
                user.discard_email_change_requests()
                # Links of earlier password resets must not work after the change.
                VerificationToken.objects.filter(
                    user=user, purpose=VerificationToken.Purpose.PASSWORD_RESET
                ).delete()
                password_reset.notify_password_changed(user)
                security_log.record(SecurityEvent.Event.PASSWORD_CHANGED, account=user)
        if not verified:
            if changed_meanwhile:
                form.add_error(
                    None, _("Your account was changed in the meantime. Please try again.")
                )
            else:
                form.add_error("current_password", ReauthenticationForm.password_error)
            return self.invalid(form)
        # Renews the session ID and keeps this session; Django derives the session hash from
        # the password, so every other session of the account ends (docs/SECURITY.md).
        update_session_auth_hash(request, user)
        messages.success(request, _("Your password was changed."), extra_tags="card-password")
        return redirect("accounts:profile") if self.required() else back_to("password")

    def invalid(self, form):
        if self.required():
            return render(
                self.request, self.template_name, {"form": form, "required": True}
            )
        return render_profile(self.request, edit="password", forms={"password": form})


class TwoFactorOverviewView(ProfileCardView):
    http_method_names = ["get"]

    def get(self, request):
        return render_profile(request, edit="two_factor")


class TwoFactorSetupView(ProfileCardView):
    """Start setting up the authenticator app.

    A session that authenticated at most five minutes ago starts without the password; every
    other session enters the password first (D-055). Starting is a POST: opening the page changes
    nothing. The page also is the place an account that must have a second factor is sent to.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and second_factor.has_second_factor(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def recent(self):
        return second_factor.recently_authenticated(self.request)

    def get(self, request):
        return render_profile(request, edit="two_factor", step="setup")

    def post(self, request):
        # The button of the start page and of the card sends no password. If the five minutes are
        # over by then, the password is asked for, without an error message.
        if self.recent():
            form = StartSetupForm(request.POST)
        elif "current_password" not in request.POST:
            return render_profile(request, edit="two_factor", step="setup")
        else:
            form = ReauthenticationForm(request.POST, request=request)
        if not form.is_valid():
            return render_profile(
                request, edit="two_factor", step="setup", forms={"setup": form}
            )
        try:
            device = totp.start_enrolment(request.user)
        except totp.SecondFactorError:
            return refused_change(request)
        # Only this session may finish the setup (D-040).
        request.session[ENROLMENT_SESSION_KEY] = device.pk
        return redirect("accounts:two_factor_confirm")


class TwoFactorConfirmView(ProfileCardView):
    """Step 1 of 2: the secret and a first code, which proves that the app works."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self.device = totp.unconfirmed_totp_device(
                request.user, request.session.get(ENROLMENT_SESSION_KEY)
            )
            if self.device is None:
                return redirect("accounts:two_factor_setup")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render_profile(request, edit="two_factor", step="app")

    @sensitive_variables("result", "codes")
    def post(self, request):
        form = SetupCodeForm(request.POST)
        if form.is_valid():
            result = totp.confirm_enrolment(
                request.user, self.device.pk, form.cleaned_data["otp_token"]
            )
            if result is not None:
                device, codes = result
                # Other sessions of the account ended with the confirmation; this one stays
                # (D-044).
                update_session_auth_hash(request, request.user)
                request.session.pop(ENROLMENT_SESSION_KEY, None)
                # The person just proved to own the app: this session has completed the second
                # factor. The session ID is renewed, as after every second factor.
                request.session.cycle_key()
                otp_login(request, device)
                return render_profile(request, codes=codes, enrolled=True)
            form.add_error("otp_token", form.invalid_code_message)
        return render_profile(request, edit="two_factor", step="app", forms={"code": form})


class TwoFactorQrView(TwoFactorMixin, View):
    """The QR code of the secret that is being set up, as an image of its own.

    The policy of the pages allows images only from the own origin (no ``data:`` images,
    D-028), so the code is not embedded in the page.
    """

    def get(self, request):
        device = totp.unconfirmed_totp_device(
            request.user, request.session.get(ENROLMENT_SESSION_KEY)
        )
        if device is None:
            return HttpResponse(status=404)
        return HttpResponse(totp.qr_code_svg(device), content_type="image/svg+xml")


class RecoveryCodesView(ProfileCardView):
    """New recovery codes; the previous ones become invalid."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not second_factor.has_second_factor(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render_profile(request, edit="two_factor", step="codes_new")

    @sensitive_variables("codes")
    def post(self, request):
        form = ReauthenticationForm(request.POST, request=request)
        if not form.is_valid():
            return render_profile(
                request, edit="two_factor", step="codes_new", forms={"action": form}
            )
        try:
            codes = totp.renew_recovery_codes(request.user)
        except totp.SecondFactorError:
            return refused_change(request)
        # Other sessions ended with the renewal; this session stays (D-044).
        update_session_auth_hash(request, request.user)
        return render_profile(request, codes=codes, enrolled=False)


class RecoveryCodesDoneView(ProfileCardView):
    """"Fertig" after the codes: checked on the server. The codes cannot be shown again, so
    without the tick the person is told to make new ones if they did not save them."""

    http_method_names = ["post"]

    def post(self, request):
        if request.POST.get("saved") == "on":
            messages.success(request, _("The recovery codes are saved."), extra_tags="card-two_factor")
        else:
            messages.warning(
                request,
                _(
                    "Please confirm that you saved the codes. They are not shown again; if you "
                    "did not save them, create new codes."
                ),
                extra_tags="card-two_factor",
            )
        return back_to("two_factor")


class TwoFactorDisableView(ProfileCardView):
    """Remove the second factor; not possible for staff users and system administrators."""

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if user.is_authenticated and (
            second_factor.requires_second_factor(user)
            or not second_factor.has_second_factor(user)
        ):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render_profile(request, edit="two_factor", step="disable")

    def post(self, request):
        form = ReauthenticationForm(request.POST, request=request)
        if not form.is_valid():
            return render_profile(
                request, edit="two_factor", step="disable", forms={"action": form}
            )
        try:
            totp.disable(request.user)
        except totp.SecondFactorError:
            return refused_change(request)
        # Other sessions ended with the removal; this session stays (D-044).
        update_session_auth_hash(request, request.user)
        messages.success(request, _("Two-factor authentication was turned off."), extra_tags="card-two_factor")
        return back_to("two_factor")


class TwoFactorLostPhoneView(TwoFactorDisableView):
    """The way "phone lost" of turning off 2FA: the password and a recovery code (D-062).

    The same rules as turning off: not for staff users and administrators, and only with a second
    factor. The recovery code is used up by the check. Everything after the check is the same as
    in ``TwoFactorDisableView``: the second factor is removed, the other sessions end, every
    address is told, the event is logged.
    """

    def get(self, request):
        return render_profile(request, edit="two_factor", step="lost_phone")

    @sensitive_variables("form")
    def post(self, request):
        """The check of the password and the code and the removal are ONE transaction under the
        lock of the account (security review, block 9, L-1): the state is checked again before the
        code is used up, and a refusal of the removal gives the code back. A parallel post with the
        same code finds the second factor gone and uses up nothing. A wrong answer is committed
        (the count of the session, D-062), a refusal is rolled back."""
        form = LostPhoneForm(request.POST, request=request)
        try:
            with transaction.atomic():
                locked = User._base_manager.select_for_update().get(pk=request.user.pk)
                if second_factor.requires_second_factor(locked) or not second_factor.has_second_factor(locked):
                    raise totp.SecondFactorError("Not possible for this account.")
                valid = form.is_valid()
                if valid:
                    totp.disable(request.user, via_recovery_code=True)
        except totp.SecondFactorError:
            return refused_change(request)
        if not valid:
            return render_profile(
                request, edit="two_factor", step="lost_phone", forms={"action": form}
            )
        update_session_auth_hash(request, request.user)
        messages.success(request, _("Two-factor authentication was turned off."), extra_tags="card-two_factor")
        return back_to("two_factor")


class AccountDeleteView(ProfileCardView):
    """Delete the logged-in account, or keep it deactivated under the retention rule (D-016).

    The page and the message are the same for every account: the person is told that the account
    is deleted and that logging in is no longer possible (owner decision of 2026-10-14, D-055).
    The result is rendered by the answer itself, because the session ends with the deletion.
    """

    def get(self, request):
        return render_profile(request, edit="delete")

    def post(self, request):
        form = AccountDeleteForm(request.POST, request=request)
        if not form.is_valid():
            return render_profile(request, edit="delete", forms={"delete": form})
        user = request.user
        addresses = user.notification_addresses()
        try:
            with transaction.atomic():
                # delete_or_deactivate also blanks the name in the queued notifications of the
                # account (D-068); they are still sent, with the neutral greeting.
                outcome = user.delete_or_deactivate()
                event = (
                    SecurityEvent.Event.ACCOUNT_DEACTIVATED
                    if outcome == "deactivated"
                    else SecurityEvent.Event.ACCOUNT_DELETED
                )
                security_log.record(event, account=user, actor=user)
                # Every address of the account is told (D-059); the jobs hold no account key.
                enqueue_for_account(MailJob.Kind.ACCOUNT_DELETED, user, addresses=addresses)
        except AccountIntegrityError:
            messages.error(request, _("The account could not be deleted."), extra_tags="card-delete")
            return back_to("delete")
        logout(request)
        return render(request, "accounts/account_deleted.html")


def refused_change(request):
    """A change that the rules refused, e.g. after a parallel request changed the account."""
    messages.error(request, _("The change could not be made."), extra_tags="card-two_factor")
    return redirect("accounts:two_factor")
