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
from leg_platform.accounts.forms import PortalFormMixin, ReauthenticationForm, SignInChangeProofForm
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
    """The name of step 1 of the setup; the server validates it again when the key is registered."""

    name = forms.CharField(
        label=gettext_lazy("Name"), max_length=80,
        help_text=gettext_lazy("So you can find the passkey again later."),
        widget=forms.TextInput(attrs={
            "placeholder": gettext_lazy("My phone"), "autocomplete": "off", "autofocus": True,
        }),
    )


class RemovalProofForm(ReauthenticationForm):
    """The one proof of D-062 / D-106 for a removal. The page says in its own sentence why the proof is needed, so the
    passkey widget adds no text of its own."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "passkey_assertion" in self.fields:
            self.fields["passkey_assertion"].widget.show_lead = False


# The steps of the setup (``?step=``), the screens of the owner specification, chapters 8 and 9.
PROOF, NAME, DEVICE, CONFIRM, DONE = "proof", "name", "device", "confirm", "done"
# Display state of the session only (no authority): the validated name of step 1 and the facts of the last activation.
NAME_KEY = "passkey_setup_name"
DONE_KEY = "passkey_setup_done"


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class SetupView(LoginRequiredMixin, View):
    """Set up a passkey as one wizard (the look of the building wizard): proof, name, device, confirm in the window,
    the confirmation before the activation (a first passkey and recovery only; a further passkey is activated at once,
    D-112), done. The steps only show state: the proof grant
    (``passkey_methods.setup_allowed``), the staged keys of this session and the name decide on the server which screen is
    shown. Creating (the browser ceremony, JSON POST) and activating (``passkey_methods.activate``) are unchanged."""

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
        if request.GET.get("step") == DONE:
            return self.done(request)
        return self.page(request, step=request.GET.get("step"))

    @staticmethod
    def staged(request):
        """The keys this session created and that are not active yet: exactly what ``activate`` would switch on."""
        return request.user.passkeys.filter(active=False, setup_binding=passkeys.binding(request)).order_by("pk")

    def context(self, request):
        user = request.user
        return {
            "adding": user.authentication_method == User.AuthenticationMethod.PASSKEY,
            "recovering": user.authentication_method == User.AuthenticationMethod.RECOVERY,
            "has_code": second_factor.has_second_factor(user),
        }

    def back_url(self, request):
        """Where "Cancel" and "Back to profile" lead: always the profile, so that every exit of the wizard agrees."""
        return reverse("accounts:profile")

    @staticmethod
    def is_further(user):
        """The account already has an active passkey: a further one deletes nothing (D-112)."""
        return (
            user.authentication_method == User.AuthenticationMethod.PASSKEY
            and user.passkeys.filter(active=True).exists()
        )

    def page(self, request, form=None, step=None):
        allowed = passkey_methods.setup_allowed(request)
        context = {**self.context(request), "allowed": allowed, "back_url": self.back_url(request)}
        if not allowed:
            form = form or SignInChangeProofForm(request=request)
            if "passkey_assertion" in form.fields:
                # The heading of the screen says it; the widget adds no sentence of its own.
                form.fields["passkey_assertion"].widget.show_lead = False
            context.update(step=PROOF, form=form)
            return render(request, "accounts/passkey_setup.html", context)
        staged = list(self.staged(request))
        name = request.session.get(NAME_KEY, "")
        # Staged keys wait for their confirmation; no second one is created beside them. A step that lacks what it needs
        # (no name, no key) falls back to the one before it.
        if staged:
            step = CONFIRM
        elif step == DEVICE and name:
            step = DEVICE
        else:
            step = NAME
            request.session.pop(DONE_KEY, None)
        if step == NAME:
            form = form or CredentialNameForm(initial={"name": name})
        context.update(
            step=step, form=form, name=name, keys=staged,
            names=", ".join(key.name for key in staged),
        )
        return render(request, "accounts/passkey_setup.html", context)

    def done(self, request):
        """The result screen. It shows what the last activation of this session did; without one it leads back."""
        info = request.session.get(DONE_KEY)
        if not isinstance(info, dict) or info.get("user") != request.user.pk:
            return redirect(self.back_url(request))
        names = info.get("names")
        return render(request, "accounts/passkey_setup.html", {
            **self.context(request), "step": DONE, "back_url": reverse("accounts:profile"),
            "names": ", ".join(str(name) for name in names) if isinstance(names, list) else "",
            "password_deleted": info.get("password") is True,
            "further": info.get("further") is True,
            "recommend": request.user.passkeys.filter(active=True).count() == 1,
        })

    def cancel(self, request):
        """Ends the setup: the grant of the proof and the keys of this session that were never activated are dropped.
        Active keys and the account are not touched."""
        self.staged(request).delete()
        for key in (passkey_methods.SETUP_KEY, NAME_KEY, DONE_KEY):
            request.session.pop(key, None)
        return redirect(self.back_url(request))

    def activated(self, request, user, names, *, had_password, further):
        """What follows a successful ``passkey_methods.activate`` (the activation itself is unchanged): this session
        stays signed in, the result screen gets its facts."""
        request.user = user
        update_session_auth_hash(request, user)
        second_factor.mark_passkey_session(request, user)
        request.session.pop(NAME_KEY, None)
        request.session[DONE_KEY] = {"user": user.pk, "names": names, "password": had_password, "further": further}
        return f"{reverse('accounts:passkey_setup')}?step={DONE}"

    def post(self, request):
        if request.content_type == "application/json":
            if limited(request) or not passkey_methods.setup_allowed(request):
                return JsonResponse({"error": str(ERROR)}, status=400)
            try:
                data = payload(request)
                further = self.is_further(request.user)
                form = CredentialNameForm({"name": data.get("name")})
                if not form.is_valid() or passkeys.register(
                    request, data["ceremony"], data["credential"], form.cleaned_data["name"]
                ) is None:
                    return JsonResponse({"error": str(ERROR)}, status=400)
                if further:
                    # D-112: nothing is deleted or replaced, so the activation follows the ceremony at once (the same
                    # atomic ``activate``: other sessions end, every confirmed address is notified). If it fails the key
                    # stays staged and the confirmation screen shows it.
                    names = [key.name for key in self.staged(request)]
                    user = passkey_methods.activate(request)
                    if user is not None:
                        url = self.activated(request, user, names, had_password=False, further=True)
                        return JsonResponse({"redirect": url})
                return JsonResponse({"redirect": reverse("accounts:passkey_setup")})
            except (ValueError, KeyError, TypeError):
                return JsonResponse({"error": str(ERROR)}, status=400)
        if request.POST.get("cancel") == "yes":
            return self.cancel(request)
        if request.POST.get("activate") == "yes":
            names = [key.name for key in self.staged(request)]
            had_password = request.user.authentication_method == User.AuthenticationMethod.PASSWORD
            further = self.is_further(request.user)
            user = passkey_methods.activate(request)
            if user is None:
                messages.error(request, ERROR)
                return self.page(request)
            return redirect(self.activated(request, user, names, had_password=had_password, further=further))
        if request.GET.get("step") == NAME and passkey_methods.setup_allowed(request):
            form = CredentialNameForm(request.POST)
            if not form.is_valid():
                return self.page(request, form, step=NAME)
            request.session[NAME_KEY] = form.cleaned_data["name"]
            return redirect(f"{reverse('accounts:passkey_setup')}?step={DEVICE}")
        if request.user.authentication_method == User.AuthenticationMethod.RECOVERY:
            return redirect("accounts:login_method")
        form = SignInChangeProofForm(request.POST, request=request)
        if form.is_valid():
            passkey_methods.allow_setup(request)
            return redirect("accounts:passkey_setup")
        return self.page(request, form)


@method_decorator(never_cache, name="dispatch")
@method_decorator(sensitive_post_parameters(), name="dispatch")
class ManageView(LoginRequiredMixin, View):
    """The page "Passkeys" (owner specification, chapter 10): one card per active passkey, then the removal as a screen of
    its own (``?remove=<id>``: opening it changes nothing; the POST of its form removes, after a fresh proof)."""

    http_method_names = ["get", "post"]

    @staticmethod
    def active_keys(user):
        return list(user.passkeys.filter(active=True).order_by("created_at", "pk"))

    @staticmethod
    def requested(keys, value):
        """The key of ``?remove=`` / the posted ``credential``, only among the active keys of this account."""
        return next((key for key in keys if str(key.pk) == str(value)), None)

    def page(self, request, form=None, remove=None):
        keys = self.active_keys(request.user)
        # The last passkey is never offered for removal (the server refuses it as well, ``passkey_methods.remove``).
        if remove is not None and len(keys) < 2:
            remove = None
        return render(request, "accounts/passkeys.html", {
            "keys": keys, "only_one": len(keys) == 1, "remove": remove,
            "form": form or (RemovalProofForm(request=request) if remove else None),
        })

    def get(self, request):
        if request.user.authentication_method != User.AuthenticationMethod.PASSKEY:
            return redirect("accounts:login_method")
        remove = request.GET.get("remove")
        return self.page(request, remove=self.requested(self.active_keys(request.user), remove) if remove else None)

    def post(self, request):
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=request.user.pk)
            if locked.get_session_auth_hash() != request.user.get_session_auth_hash():
                logout(request)
                return redirect("accounts:login")
            request.user = locked
            if locked.authentication_method != User.AuthenticationMethod.PASSKEY:
                return redirect("accounts:login_method")
            return self.remove_locked(request)

    def remove_locked(self, request):
        form = RemovalProofForm(request.POST, request=request)
        remove = self.requested(self.active_keys(request.user), request.POST.get("credential"))
        if not form.is_valid():
            if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
                second_factor.end_session_for_wrong_codes(request)
                return redirect("accounts:login")
            return self.page(request, form, remove=remove)
        if not passkey_methods.remove(request.user, request.POST.get("credential")):
            form.add_error(None, _("Keep at least the required number of passkeys. Add a replacement first."))
            return self.page(request, form, remove=remove)
        logout(request)
        # Shown once on the login page (the messages survive the sign-out). It names no key: nothing about the account.
        messages.info(request, _("The passkey was removed. You were signed out."))
        return redirect("accounts:login")
