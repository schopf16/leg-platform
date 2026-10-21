"""The profile page ``/profile/``: cards that change in place (D-059).

The card state is in the address (``?edit=email``, ``?edit=two_factor&step=disable``) and in the
answers of the forms, rendered by the server (works without a script). Only one card is in an edit
state at a time. The forms post to the earlier pages' addresses; the account is always ``request.user``.
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

from leg_platform.accounts import (
    email_change,
    login_methods,
    password_reset,
    second_factor,
    security_log,
    totp,
)
from leg_platform.accounts.forms import (
    AccountDeleteForm,
    ChangePasswordForm,
    EmailChangeForm,
    LoginMethodForm,
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
    "bank": "card-bank",
    "email": "card-email",
    "password": "card-password",
    "two_factor": "card-two-factor",
    "delete": "card-delete",
}
# Steps of the two-factor card that a person can open by address; codes are only ever the answer to a form.
# "choose" is the choice "Sign-in method" (D-106); it is shown only to an account that has something to choose.
TWO_FACTOR_STEPS = ("choose", "setup", "app", "codes_new", "disable", "lost_phone")
# The fields of the profile that make up the address: locked once the staff confirmed the building at that address.
ADDRESS_FIELDS = ("street", "house_number", "postal_code", "town", "country")


def card_url(card, step=None):
    """The address of the profile with this card in its edit state (and step)."""
    query = f"?edit={card}" + (f"&step={step}" if step else "")
    return f"{reverse('accounts:profile')}{query}#{CARD_ANCHORS[card]}"


def back_to(card):
    """Redirect to the view of a card after a success."""
    return redirect(f"{reverse('accounts:profile')}#{CARD_ANCHORS[card]}")


# What the profile takes from the customer's side (the community services, the customer pages). Imported when used: the
# customer pages import this module, and the profile of a staff account needs none of it.


def address_building(user):
    """The own building at the account's address (EGID, lock), or None (``community.buildings``, PROFIL.md section 1)."""
    from leg_platform.community import buildings

    return buildings.profile_address_building(user)


def personal_form(user, data=None):
    """The form of the card "Personal details"; the address fields are locked once the staff confirmed the building at
    that address (a disabled field keeps its stored value whatever the browser posts)."""
    form = ProfileForm(data, instance=copy.copy(user)) if data is not None else ProfileForm(instance=copy.copy(user))
    info = address_building(user)
    if info is not None and info.locked:
        for name in ADDRESS_FIELDS:
            form.fields[name].disabled = True
    return form


def deletion_state(user):
    """What the card "Delete account" needs: ``released`` (no running metering point without a started deregistration
    and no registration in flight, ``community.deregistrations``), ``can_start`` (a customer with metering points that
    can be deregistered now: the button leads to the deregistration) and ``retained`` (the account is deactivated, not
    deleted: a retention obligation or a metering point assignment, ``User.delete_or_deactivate``)."""
    from leg_platform.community import deregistrations
    from leg_platform.community.models import Deregistration
    from leg_platform.customer import access

    released = deregistrations.deletion_released(user)
    customer = access.is_customer(user)
    return {
        "released": released,
        "customer": customer,
        "can_start": bool(customer and not released and deregistrations.eligible_points(user)),
        "retained": bool(user.retention_obligation or user.metering_point_assignments.exists()),
        # A deregistration is open: it continues after the account is gone or deactivated (W3-05).
        "deregistration_running": bool(
            customer and released
            and Deregistration.objects.filter(
                person=user, state__in=(Deregistration.State.SUBMITTED, Deregistration.State.CONFIRMED)
            ).exists()
        ),
    }


def delete_form(request, data=None, retained=False):
    """The confirmation of the deletion; for an account that is only deactivated the box says so."""
    form = AccountDeleteForm(data, request=request) if data is not None else AccountDeleteForm(request=request)
    if retained:
        form.fields["confirm"].label = _("I know that my account will be deactivated and that this cannot be undone.")
    return form


def bank_context(request, editing, form):
    """The card "Bank or postal connection" (PROFIL.md section 2): the IBAN in full (shown only to the account itself),
    its status, the form when the card is open and the way back (``next``: a page of the customer's pages only)."""
    from leg_platform.community import own_iban
    from leg_platform.customer import forms as customer_forms
    from leg_platform.customer import returns

    iban = own_iban.own_iban_display(request.user)
    context = {
        "bank_iban": iban,
        "bank_next": returns.safe_return_path(request.POST.get("next") or request.GET.get("next") or ""),
    }
    if editing:
        context["bank_form"] = form or customer_forms.BankForm(initial={"iban": iban})
    return context


def two_factor_state(user, edit, step, session):
    """The state of the two-factor card that the request asks for, reduced to what is possible for this account."""
    active = second_factor.has_second_factor(user)
    if edit != "two_factor":
        return "setup" if second_factor.requires_second_factor(user) and not active else "view"
    if step == "choose" and login_methods.has_choice(user) and (active or not second_factor.requires_second_factor(user)):
        return "choose"
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
    # The card "Bank or postal connection" is the customer's (a staff account has none: it takes no part).
    from leg_platform.customer import access as customer_access

    bank_card = not only_two_factor and customer_access.is_customer(user)
    if edit == "bank" and not bank_card:
        edit = None
    # Messages tagged "card-<name>" go into that card; the rest, and those of a hidden card, stay on top.
    shown = {"two_factor"} if only_two_factor else set(CARD_ANCHORS)
    card_messages = {card: [] for card in CARD_ANCHORS}
    general_messages = []
    for message in messages.get_messages(request):
        tags = (message.extra_tags or "").split()
        card = next((tag[len("card-"):] for tag in tags if tag.startswith("card-")), None)
        if card in shown and (card != "bank" or bank_card):
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
        "can_choose": login_methods.has_choice(user),
        "passkeys_available": login_methods.passkeys_available(),
    }
    if not only_two_factor:
        context["address_building"] = address_building(user)
        context["delete_info"] = deletion_state(user)
    context["bank_card"] = bank_card
    if bank_card:
        context.update(bank_context(request, edit == "bank", forms.get("bank")))
    if edit == "personal":
        context["personal_form"] = forms.get("personal") or personal_form(user)
    elif edit == "email":
        context["email_form"] = forms.get("email") or EmailChangeForm(request=request)
    elif edit == "password":
        context["password_form"] = forms.get("password") or ChangePasswordForm(
            user, request=request
        )
    elif edit == "delete":
        context["delete_form"] = forms.get("delete") or delete_form(
            request, retained=context["delete_info"]["retained"]
        )
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
        elif state == "choose":
            choose_form = forms.get("choose") or LoginMethodForm(
                user=user, initial={"method": login_methods.current(user)}
            )
            context.update(
                choose_form=choose_form,
                methods=login_methods.available(user),
                selected=choose_form["method"].value(),
                current=login_methods.current(user),
                recommended=login_methods.recommended(user),
                recommended_title=login_methods.title_of(login_methods.recommended(user)),
                privileged=login_methods.is_privileged(user),
            )
        elif state == "lost_phone":
            context["action_form"] = forms.get("action") or LostPhoneForm(request=request)
    else:
        context["tf_state"] = "view"
    if context["second_factor_active"]:
        context["remaining_codes"] = totp.remaining_recovery_codes(user)
    return render(request, "accounts/profile.html", context, status=status)


class ProfileCardView(NeverCacheMixin, LoginRequiredMixin, View):
    """Base of the card views: a login, no caching, passwords and codes masked in error reports."""

    @method_decorator(
        sensitive_post_parameters("current_password", "new_password1", "new_password2", "otp_token")
    )
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
            # A wrong-code limit ended this session (D-062): log out and tell at the login page.
            second_factor.end_session_for_wrong_codes(request)
            return redirect("accounts:login")
        return response


class ProfileView(ProfileCardView):
    """The profile; ``?edit=<card>&step=<step>`` opens one card in its edit state."""

    def get(self, request):
        return render_profile(request, edit=request.GET.get("edit"), step=request.GET.get("step"))


class ProfileEditView(ProfileCardView):
    """Save the personal details; no confirmation is needed."""

    def get(self, request):
        return render_profile(request, edit="personal")

    def post(self, request):
        form = personal_form(request.user, request.POST)
        if not form.is_valid():
            return render_profile(request, edit="personal", forms={"personal": form}, status=200)
        form.save()
        messages.success(request, _("Saved."), extra_tags="card-personal")
        return back_to("personal")


class EmailChangeRequestView(ProfileCardView):
    """Request new addresses (or remove the second one) with the proof; the same response for every target address (D-059)."""

    def get(self, request):
        return render_profile(request, edit="email")

    def refused(self, request):
        """The answer when the client or instance limit is used up (D-038); nothing was stored."""
        messages.error(request, _("Too many requests, please try again later."), extra_tags="card-email")
        return render_profile(request, edit="email", status=429)

    def kinds_asked(self, request):
        """The kinds of request the posted values ask for, judged before the proof is checked
        (a refusal must not use up an app code)."""
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
            # A limit was reached between the check and the request: rolled back.
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
    """Ends a pending request of the account (D-059)."""

    http_method_names = ["post"]

    def post(self, request, kind):
        if kind not in PendingEmailChange.Kind.values:
            raise Http404
        email_change.cancel(request.user, kind)
        messages.success(request, _("The request was cancelled."), extra_tags="card-email")
        return back_to("email")


class PasswordChangeView(ProfileCardView):
    """Change the password after the proof. An account that must replace its password (D-005)
    keeps a page of its own, the only one it can reach."""

    template_name = "accounts/password_change.html"

    def form(self, data=None):
        return ChangePasswordForm(self.request.user, data, request=self.request)

    def required(self):
        return self.request.user.password_change_required

    def get(self, request):
        if not self.required():
            return render_profile(request, edit="password")
        return render(request, self.template_name, {"form": self.form(), "required": True})

    def post(self, request):
        # With a code as the proof no password is typed: a parallel change of the account is
        # found by comparing the session hash with the one at the start (D-040, D-062).
        started_with = request.user.get_session_auth_hash()
        form = self.form(request.POST)
        if not form.is_valid():
            return self.invalid(form)
        changed_meanwhile = False
        with transaction.atomic():
            # Lock and re-read the row, so the new password is written to the current state: a
            # parallel password change must not be overwritten by a request with the old password
            # (D-040). A code is used up by the form and cannot be checked twice, hence the hash.
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
                user.password_change_required = False
                form.save()
                # Requests made before the change must not be confirmed afterwards, nor old reset links used.
                user.discard_email_change_requests()
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
        # Renews the session ID and keeps this session; every other session of the account ends.
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


class LoginMethodView(ProfileCardView):
    """The choice "Sign-in method" (D-106): GET opens it in the card, POST validates the posted method on the server
    and leads into the existing flows (setup of the app, turning it off). It adds no security logic of its own: the
    flows ask for the password where they always did. An account with only one method has no choice page."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not login_methods.has_choice(request.user):
            return redirect("accounts:two_factor")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render_profile(request, edit="two_factor", step="choose")

    def post(self, request):
        user = request.user
        form = LoginMethodForm(request.POST, user=user)
        if not form.is_valid():
            return render_profile(request, edit="two_factor", step="choose", forms={"choose": form})
        method = form.cleaned_data["method"]
        if method == login_methods.current(user):
            messages.info(request, _("Your sign-in method stays as it is."), extra_tags="card-two_factor")
            return back_to("two_factor")
        if method == login_methods.PASSWORD_AND_APP:
            # The same start as the button "Set up 2FA" had: no password within five minutes of the login, else asked.
            return TwoFactorSetupView.as_view()(request)
        if method == login_methods.PASSWORD_ONLY:
            return redirect(card_url("two_factor", "disable"))
        # A method that is offered but has no flow yet: passkeys (D-106) are not built.
        raise Http404


class TwoFactorSetupView(ProfileCardView):
    """Start setting up the authenticator app (a POST; opening the page changes nothing).

    A session authenticated at most five minutes ago starts without the password; every other
    session enters it first (D-055). Also where an account that must have a second factor is sent.
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
        # The card's button sends no password; if the five minutes are over, ask for it without an error.
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
                # Other sessions ended with the confirmation; this one stays (D-044).
                update_session_auth_hash(request, request.user)
                request.session.pop(ENROLMENT_SESSION_KEY, None)
                # This session has completed the second factor: renew the session ID.
                request.session.cycle_key()
                otp_login(request, device)
                return render_profile(request, codes=codes, enrolled=True)
            form.add_error("otp_token", form.invalid_code_message)
        return render_profile(request, edit="two_factor", step="app", forms={"code": form})


class TwoFactorQrView(TwoFactorMixin, View):
    """The QR code of the secret being set up, as an own image (the CSP allows no ``data:`` images, D-028)."""

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
    """"Done" after the codes, checked on the server; without the tick the person is told to make new codes."""

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
    """The way "phone lost" of turning off 2FA: password and a recovery code (D-062).

    Same rules as ``TwoFactorDisableView``; the recovery code is used up by the check.
    """

    def get(self, request):
        return render_profile(request, edit="two_factor", step="lost_phone")

    @sensitive_variables("form")
    def post(self, request):
        """Check and removal are ONE transaction under the account lock: the state is re-checked
        before the code is used up, and a refusal gives the code back. A parallel post with the
        same code uses up nothing. A wrong answer is committed (session count, D-062)."""
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
    """Delete the account, or keep it deactivated under the retention rule (D-016).

    The deletion is released only when no metering point is running without a started deregistration and no registration
    is in flight (``community.deregistrations.deletion_released``; the customer starts the deregistration from this card,
    PROFIL.md section 6). The hook is here and not in ``User.delete_or_deactivate``: the guards of the account model stay
    as they are. The release is checked before the proof is asked (nothing is used up) and once more under the lock of the
    account. An account that is only deactivated is told so before (owner decision 2026-10-19, amends D-055); the answer
    renders the result itself because the session ends.
    """

    def get(self, request):
        return render_profile(request, edit="delete")

    @staticmethod
    def blocked(request):
        messages.error(
            request,
            _(
                "You cannot delete your account yet: a registration is still running or a metering point is not "
                "active yet. Withdraw the registration on the page Buildings, or wait until the metering point is "
                "active and deregister it."
            ),
            extra_tags="card-delete",
        )
        return back_to("delete")

    def post(self, request):
        info = deletion_state(request.user)
        if not info["released"]:
            return self.blocked(request)
        form = delete_form(request, request.POST, retained=info["retained"])
        if not form.is_valid():
            return render_profile(request, edit="delete", forms={"delete": form})
        user = request.user
        addresses = user.notification_addresses()
        try:
            with transaction.atomic():
                from leg_platform.community import customer_messages, deregistrations

                User._base_manager.select_for_update().get(pk=user.pk)
                if not deregistrations.deletion_released(user):
                    raise DeletionBlocked
                # Also blanks the name in the account's queued notifications (D-068).
                outcome = user.delete_or_deactivate()
                if outcome == "deactivated":
                    # The staff's free text (one-time messages, return reasons) does not outlive the use of the portal.
                    customer_messages.purge(user)
                event = (
                    SecurityEvent.Event.ACCOUNT_DEACTIVATED
                    if outcome == "deactivated"
                    else SecurityEvent.Event.ACCOUNT_DELETED
                )
                security_log.record(event, account=user, actor=user)
                # Every address is told (D-059); the jobs hold no account key.
                enqueue_for_account(MailJob.Kind.ACCOUNT_DELETED, user, addresses=addresses)
        except DeletionBlocked:
            return self.blocked(request)
        except AccountIntegrityError:
            messages.error(request, _("The account could not be deleted."), extra_tags="card-delete")
            return back_to("delete")
        logout(request)
        return render(request, "accounts/account_deleted.html", {"deactivated": outcome == "deactivated"})


class DeletionBlocked(Exception):
    """The release of the deletion was withdrawn between the first check and the lock (a parallel request)."""


def refused_change(request):
    """A change that the rules refused, e.g. after a parallel request changed the account."""
    messages.error(request, _("The change could not be made."), extra_tags="card-two_factor")
    return redirect("accounts:two_factor")
