"""Forms of the portal: registration, login, profile, confirmations. Only declared fields are accepted."""

import re

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, SetPasswordMixin
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ValidationError
from django.forms.boundfield import BoundField
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import login_methods, second_factor, totp
from leg_platform.accounts.bot_check import BotCheckMixin
from leg_platform.accounts.models import (
    EMAIL_MAX_LENGTH,
    SWISS_POSTAL_CODE_MESSAGE,
    SWITZERLAND,
    User,
    parse_swiss_postal_code,
    validate_account_email,
)
from leg_platform.accounts.normalization import normalize_email_address

# The two postal code fields of the model are replaced by one form field (D-036).
PROFILE_FIELDS = (
    "salutation",
    "first_name",
    "last_name",
    "organisation_name",
    "address_line_1",
    "street",
    "house_number",
    "post_office_box_number",
    "town",
    "country",
    "phone_number",
)
PROFILE_FIELD_ORDER = (*PROFILE_FIELDS[:8], "postal_code", *PROFILE_FIELDS[8:])


def _email_input():
    return forms.EmailInput(attrs={"inputmode": "email", "autocapitalize": "none", "spellcheck": "false"})


def _username_field():
    """The account name for the password manager only: a read-only text field, hidden from the eye.

    The page shows the address as plain text (``accounts/_account_name.html``); this field is only
    the "username" context that the browser needs to save the new password for the right account
    (Chromium, "Create Amazing Password Forms": a hidden field with the user name on forms that do
    not ask for it). It is read-only and out of the tab order, and the server never uses the posted
    value: the account comes from the link or the session (``pin_username``, ``clean_username``).
    """
    attrs = {
        "readonly": True,
        "tabindex": "-1",
        "aria-hidden": "true",
        "autocomplete": "username",
        "autocapitalize": "none",
        "spellcheck": "false",
        "class": "account-name-input",
    }
    return forms.CharField(
        label=_("Email address (username)"), required=False, widget=forms.TextInput(attrs=attrs)
    )


def _current_password_field():
    return forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
        error_messages={"required": _("Enter your current password.")},
    )


def postal_code_field():
    """The single postal code field of forms (D-036)."""
    return forms.CharField(label=_("postal code"), max_length=15, required=False)


def _minimum_password_length():
    """The minimum length of the configured validator, so the text cannot differ from the rule."""
    return next(
        (
            validator.min_length
            for validator in password_validation.get_default_password_validators()
            if isinstance(validator, password_validation.MinimumLengthValidator)
        ),
        None,
    )


def password_rules():
    """The password rules as short lines for a list (D-058)."""
    minimum = _minimum_password_length()
    rules = []
    if minimum is not None:
        rules.append(gettext("At least %(min_length)d characters.") % {"min_length": minimum})
    rules += [
        gettext("A sentence of several words is easy to remember and hard to guess."),
        gettext("All characters are allowed; upper-case letters, digits and special characters are not required."),
        gettext("No known or easily guessed passwords, no number-only passwords."),
        gettext("Not similar to your email address."),
    ]
    return rules


def password_hint():
    """The advice at the password field; replaces Django's validator help texts."""
    minimum = _minimum_password_length()
    if minimum is None:
        return ""
    return gettext(
        "Choose at least %(min_length)d characters. A sentence of several words is easy to "
        "remember and hard to guess."
    ) % {"min_length": minimum}


AUTOCOMPLETE = {
    "email": "email",
    "first_name": "given-name",
    "last_name": "family-name",
    "organisation_name": "organization",
    "street": "address-line1",
    "postal_code": "postal-code",
    "town": "address-level2",
    "country": "country",
    "phone_number": "tel",
}


class PortalBoundField(BoundField):
    """``aria-describedby`` names the lead, help text, ``described_by`` suffixes and error, in reading order."""

    @property
    def aria_describedby(self):
        if self.field.widget.attrs.get("aria-describedby"):
            return None
        ids = []
        if self.auto_id and not self.is_hidden:
            ids +=  [f"{self.auto_id}_{suffix}" for suffix in getattr(self.field, "described_first", ())]
            error_first = getattr(self.field, "error_before_hint", False)
            if error_first and self.errors:
                ids.append(f"{self.auto_id}_error")
            if self.help_text:
                ids.append(f"{self.auto_id}_helptext")
            ids += [f"{self.auto_id}_{suffix}" for suffix in getattr(self.field, "described_by", ())]
            if self.errors and not error_first:
                ids.append(f"{self.auto_id}_error")
        return " ".join(ids)


class PortalFormMixin:
    """Give the widgets of a form the control classes of Tabler."""

    bound_field_class = PortalBoundField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Select):
                control = "form-select"
            elif isinstance(widget, forms.CheckboxInput):
                control = "form-check-input"
            else:
                control = "form-control"
            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {control}".strip()
            if name in AUTOCOMPLETE:
                widget.attrs.setdefault("autocomplete", AUTOCOMPLETE[name])


def clean_postal_code(form, cleaned_data):
    """Write the postal code into the Swiss (Switzerland) or foreign (every other country) field; D-036."""
    if "country" not in cleaned_data or "postal_code" not in cleaned_data:
        return
    country = cleaned_data["country"]
    value = cleaned_data["postal_code"]
    if country == SWITZERLAND:
        swiss_postal_code = parse_swiss_postal_code(value)
        if swiss_postal_code is None:
            form.add_error("postal_code", SWISS_POSTAL_CODE_MESSAGE)
            return
        form.instance.swiss_postal_code = swiss_postal_code
        form.instance.foreign_postal_code = ""
    elif country:
        if not value:
            form.add_error("postal_code", _("This field is required."))
            return
        form.instance.swiss_postal_code = None
        form.instance.foreign_postal_code = value


class ProfileForm(PortalFormMixin, forms.ModelForm):
    postal_code = postal_code_field()

    class Meta:
        model = User
        fields = PROFILE_FIELDS
        widgets = {
            # Text with a numeric keyboard: no spin buttons, leading zeros kept; validated on the server.
            "post_office_box_number": forms.TextInput(attrs={"inputmode": "numeric"}),
        }

    field_order = PROFILE_FIELD_ORDER

    def save(self, commit=True):
        """Write only the form's columns: a full save would undo a parallel change (session_version, password)."""
        if not commit:
            return super().save(commit=False)
        if self.errors:
            raise ValueError("The profile could not be saved because the data did not validate.")
        self.instance.save(
            update_fields=[*PROFILE_FIELDS, "swiss_postal_code", "foreign_postal_code"]
        )
        return self.instance

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("postal_code", self.instance.postal_code_display)
        if self.instance.phone_number and not self.is_bound:
            self.initial["phone_number"] = self.instance.phone_number_display
        # Always required here; the model allows empty (uncompleted accounts). The rest is checked by ``User.clean``.
        for name, message in (
            ("town", _("Enter a town.")),
            ("country", _("Select a country.")),
            ("postal_code", _("Enter a postal code.")),
        ):
            self.fields[name].required = True
            self.fields[name].error_messages["required"] = message
        self.fields["postal_code"].widget.attrs["inputmode"] = (
            "numeric" if self._country_code() == SWITZERLAND else "text"
        )
        self.fields["post_office_box_number"].described_by = ("hint",)
        self.fields["postal_code"].label = _("ZIP code")

    def _country_code(self):
        if self.is_bound:
            return self.data.get(self.add_prefix("country"), "")
        return str(self.initial.get("country") or "")

    def clean(self):
        cleaned_data = super().clean()
        clean_postal_code(self, cleaned_data)
        return cleaned_data


class EmailAddressForm(PortalFormMixin, forms.Form):
    """Asks only for an email address (registration and password reset); stores nothing (D-016, D-037)."""

    email = forms.EmailField(
        label=_("Email address"),
        max_length=EMAIL_MAX_LENGTH,
        error_messages={"required": _("Enter your email address.")},
        widget=_email_input(),
    )
    email.described_by = ("hint",)

    def clean_email(self):
        email = self.cleaned_data["email"]
        try:
            validate_account_email(email)
        except ValueError:
            raise ValidationError(_("Enter a valid email address."), code="invalid") from None
        return email


class RegistrationForm(BotCheckMixin, EmailAddressForm):
    """Registration asks only for the email address; the profile follows after the link."""


class PasswordResetRequestForm(BotCheckMixin, EmailAddressForm):
    pass


def describe_password_fields(form, first, second):
    """Advice at the first password field and a specific message when a field is empty."""
    form.fields[first].help_text = password_hint()
    form.fields[first].described_by = ("rules",)
    form.fields[first].error_messages["required"] = _("Enter a password.")
    form.fields[second].error_messages["required"] = _("Enter the password again.")


def pin_username(form, address):
    """Bind the "username" field to the address of the page, whatever the request carries."""
    if form.is_bound:
        form.data = form.data.copy()
        form.data[form.add_prefix("username")] = address


class NewPasswordForm(PortalFormMixin, SetPasswordForm):
    """The new password after a reset link; the username field is for the password manager only."""

    username = _username_field()
    field_order = ("username", "new_password1", "new_password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pin_username(self, self.user.email)
        self.initial["username"] = self.user.email
        describe_password_fields(self, "new_password1", "new_password2")
        self.fields["new_password1"].help_text = ""
        self.fields["new_password1"].label = _("Password")
        self.fields["new_password2"].label = _("Repeat password")
        self.fields["new_password2"].help_text = ""

    def clean_username(self):
        return self.user.email


class RegistrationPasswordForm(SetPasswordMixin, PortalFormMixin, forms.Form):
    """Step 2 of the registration: the password (D-058); the username is the verified address."""

    username = _username_field()
    password1, password2 = SetPasswordMixin.create_password_fields()

    field_order = ("username", "password1", "password2")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        pin_username(self, user.email)
        self.initial["username"] = user.email
        describe_password_fields(self, "password1", "password2")
        self.fields["password1"].help_text = ""
        self.fields["password1"].label = _("Password")
        self.fields["password2"].label = _("Repeat password")
        self.fields["password2"].help_text = ""

    def clean_username(self):
        return self.user.email

    def clean(self):
        cleaned_data = super().clean()
        self.validate_passwords()
        return cleaned_data

    def _post_clean(self):
        super()._post_clean()
        self.validate_password_for_user(self.user, "password1")


class RegistrationDetailsForm(PortalFormMixin, forms.ModelForm):
    """Step 3 of the registration: name, address and consent to the privacy notice (D-058).

    A person invited by staff (D-087) confirms or corrects the staff's data; an invitation with
    another country keeps it (``country_of_instance``).
    """

    postal_code = postal_code_field()
    privacy = forms.BooleanField(
        required=True,
        label=_("I have read the privacy notice and accept it."),
        error_messages={"required": _("Please confirm that you accept the privacy notice.")},
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "street", "house_number", "town")

    field_order = ("first_name", "last_name", "street", "house_number", "postal_code", "town", "privacy")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("postal_code", self.instance.postal_code_display)
        self.country = self.country_of_instance()
        for name, label, message in (
            ("first_name", _("First name"), _("Enter your first name.")),
            ("last_name", _("Last name"), _("Enter your last name.")),
            ("street", _("Street"), _("Enter your street.")),
            ("town", _("Town"), _("Enter a town.")),
        ):
            self.fields[name].required = True
            self.fields[name].label = label
            self.fields[name].error_messages["required"] = message
        self.fields["house_number"].label = _("House number")
        self.fields["house_number"].required = False
        self.fields["postal_code"].label = _("Postal code")
        self.fields["postal_code"].required = True
        self.fields["postal_code"].error_messages["required"] = _("Enter a postal code.")
        self.fields["postal_code"].widget.attrs.update(
            {"inputmode": "numeric", "autocomplete": "postal-code", "maxlength": "4"}
        )
        if self.country != SWITZERLAND:
            self.fields["postal_code"].widget.attrs.update({"inputmode": "text", "maxlength": "15"})
        # No standard autocomplete value for a house number: no password manager fills it.
        self.fields["house_number"].widget.attrs["autocomplete"] = "off"
        self.fields["privacy"].widget.attrs["class"] = "entry-check-input"
        self.fields["privacy"].summary_label = _("Privacy policy")

    def country_of_instance(self):
        """Switzerland, except for an account that staff created with another country (D-087)."""
        if self.instance.invited_at is not None and self.instance.country:
            return self.instance.country.code
        return SWITZERLAND

    def _post_clean(self):
        super()._post_clean()
        # One message per field: keep only the first (the model's rules repeat what the fields say).
        for errors in self._errors.values():
            del errors.data[1:]

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data["country"] = self.country
        clean_postal_code(self, cleaned_data)
        return cleaned_data


class LoginForm(PortalFormMixin, AuthenticationForm):
    """Login with the email address; every failure shows the same message."""

    error_messages = {
        "invalid_login": _("The email address or the password is incorrect."),
        "inactive": _("The email address or the password is incorrect."),
    }
    # What "remember me" means is decided by the login view (D-056).
    remember_me = forms.BooleanField(required=False, label=_("Stay signed in"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        username = self.fields["username"]
        username.label = _("Email address")
        username.error_messages["required"] = _("Enter your email address.")
        self.fields["password"].error_messages["required"] = _("Enter your password.")
        # ``novalidate`` stays: the server checks the address, not the browser.
        attrs = {
            **username.widget.attrs,
            "inputmode": "email",
            "autocapitalize": "none",
            "spellcheck": "false",
            "autocomplete": "username",
        }
        username.widget = forms.EmailInput(attrs=attrs)
        self.fields["remember_me"].widget.attrs["class"] = "entry-check-input"

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(self.error_messages["inactive"], code="inactive")


class CodeForm(PortalFormMixin, forms.Form):
    """A code of the authenticator app or a recovery code.

    The field is named ``otp_token`` because the abuse protection masks that name
    (``AXES_SENSITIVE_PARAMETERS``). Recovery codes contain letters, so no digit restriction.
    """

    otp_token = forms.CharField(
        label=_("Code"),
        max_length=32,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
        error_messages={"required": _("Enter the code.")},
    )
    # One text everywhere a code is asked for; it does not say whether the code was wrong, expired or used.
    invalid_code_message = _(
        "The code is wrong, expired or was already used. The app shows a new code every 30 "
        "seconds: wait for the next one and try again."
    )


class SetupCodeForm(CodeForm):
    """The first code of the authenticator app at setup; only six digits, never a recovery code.

    A plain text field (no maxlength announced); spaces and hyphens are accepted by ``totp.normalize_code``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["otp_token"].widget.attrs.pop("maxlength", None)
        self.fields["otp_token"].widget.attrs["placeholder"] = "000000"
        self.fields["otp_token"].label = _("Code from the app")


class SignInChangeCodeForm(SetupCodeForm):
    """The first number code at the setup inside "Change sign-in" (never a recovery code); the labels of the flow."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["otp_token"]
        field.label = _("Number code from your phone")
        field.widget.attrs.update({"placeholder": "000 000", "inputmode": "numeric"})


class StartSetupForm(PortalFormMixin, forms.Form):
    """Starts the setup of the authenticator app; no field, needs a recent authentication (D-055)."""

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)


class LoginMethodForm(PortalFormMixin, forms.Form):
    """The choice "Sign-in method" (D-106): the posted method must be one of ``login_methods.available(user)``.

    Choices are built from the server-side account-specific list. Missing or unoffered browser
    values, including password-only for a staff user or system administrator, are refused.
    """

    choose_message = _("Choose how you want to sign in.")
    method = forms.ChoiceField(
        label=_("Sign-in method"),
        widget=forms.RadioSelect,
        error_messages={"required": choose_message, "invalid_choice": choose_message},
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].choices = [(method.key, method.title) for method in login_methods.available(user)]
        # RadioSelect has no id for its label, so the error summary would link to "#": point it at the first radio
        # (the page renders the radios with ids ``id_method_<n>``, ``_login_method_choice.html``).
        self.fields["method"].summary_anchor = "id_method_0"


class ProofCodeInput(forms.TextInput):
    """A plain text field that never writes a value back into the page (a typed password must not reappear)."""

    def format_value(self, value):
        return None


class PasskeyProofInput(forms.HiddenInput):
    """The hidden field of a passkey proof. ``show_lead`` False leaves out the sentence of the widget where the page
    explains the proof in its own words."""

    template_name = "accounts/widgets/passkey_proof.html"
    show_lead = True

    def format_value(self, value):
        return None

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["show_lead"] = self.show_lead
        return context


def passkey_proof_field():
    return forms.CharField(
        label=_("Confirm with your passkey"), max_length=32768, required=False, widget=PasskeyProofInput()
    )


# An app code after ``totp.normalize_code``; anything else is refused before the abuse protection counts it (D-062).
CODE_SHAPE = re.compile(r"^[0-9]{6}$")
# A recovery code of the "phone lost" way: eight base32 letters and digits.
RECOVERY_CODE_SHAPE = re.compile(r"^[a-z2-7]{8}$")


class OneProofMixin:
    """One proof per confirmation: the app code if a second factor is set up, else the password (D-062).

    A recovery code is no proof here (only at login and in ``LostPhoneForm``). The unused field
    is removed (``proof`` is "code" or "password"); the code is checked last, and only if it has
    the shape of an app code.
    """

    proof = "password"
    code_label = _("2FA authentication code")
    code_required_message = _("Enter the 2FA authentication code.")
    code_shape_message = _("That is not a 2FA authentication code. It has 6 digits.")
    code_wrong_message = _(
        "The 2FA authentication code is wrong or has expired. Wait for the next code in your app "
        "and try again."
    )
    code_lead = _("Enter the 6-digit code from your authenticator app. Your password is not needed here.")

    def use_one_proof(self, user, *, autofocus=True):
        if user.authentication_method == User.AuthenticationMethod.PASSKEY:
            self.proof = "passkey"
            del self.fields["otp_token"]
            del self.fields["current_password"]
            self.fields["passkey_assertion"] = passkey_proof_field()
            return
        self.fields.pop("passkey_assertion", None)
        field = self.fields["otp_token"]
        if not second_factor.has_second_factor(user):
            self.proof = "password"
            del self.fields["otp_token"]
            self.fields["current_password"].error_before_hint = True
            return
        self.proof = "code"
        del self.fields["current_password"]
        field.widget = ProofCodeInput(
            attrs={"autocomplete": "one-time-code", "placeholder": "000000", "class": "form-control"}
        )
        if autofocus:
            field.widget.attrs["autofocus"] = True
        field.label = self.code_label
        field.error_messages["required"] = self.code_required_message
        field.lead = self.code_lead
        field.described_first = ("lead",)
        field.error_before_hint = True

    def proof_changed(self):
        """Whether the second factor was set up or removed since the form was built (then fail closed)."""
        if self.user.authentication_method == User.AuthenticationMethod.PASSKEY:
            return self.proof != "passkey"
        return self.proof == "passkey" or second_factor.has_second_factor(self.user) != (self.proof == "code")

    changed_message = _("Your account was changed in the meantime. Please try again.")

    def clean_otp_token(self):
        value = self.cleaned_data["otp_token"]
        if not CODE_SHAPE.match(totp.normalize_code(value)):
            raise ValidationError(self.code_shape_message, code="code_shape")
        return value

    def check_code(self, cleaned_data):
        """Check the app code, only when nothing else is wrong; a recovery code is never accepted or used up.

        When the budget is used up, ``verify_second_factor`` marks the request as locked out and the
        lockout page answers. A wrong code is counted per session; the fifth ends it (D-062).
        """
        if self.proof == "passkey":
            if not self.errors and not second_factor.verify_passkey(
                self.request, self.user, cleaned_data.get("passkey_assertion", "")
            ):
                self.add_error("passkey_assertion", _("The passkey could not be verified. Please try again."))
            return
        if self.proof != "code" or self.errors or "otp_token" not in cleaned_data:
            return
        if second_factor.verify_second_factor(
            self.request, self.user, cleaned_data["otp_token"], recovery=False, per_session=True
        ) is None:
            self.add_error("otp_token", self.code_wrong_message)


class ReauthenticationForm(OneProofMixin, PortalFormMixin, forms.Form):
    """Asks the logged-in person for one proof: the 2FA code if set up, else the password (D-062).

    The check runs under the abuse protection of the login.
    """

    current_password = _current_password_field()
    passkey_assertion = passkey_proof_field()
    # Instead of the password where a second factor is set up; only an app code counts, a recovery code is refused.
    otp_token = forms.CharField(
        label=_("Code from the app"),
        # Generous: a password pasted by habit gets "that is not a code", not a length message.
        max_length=256,
        widget=forms.TextInput(),
    )
    password_error = _("The password is not correct. Please try again.")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user = request.user
        self.use_one_proof(self.user)

    def check_changes(self, cleaned_data):
        """Hook for subclasses: add errors here. The code is checked (used up) only when nothing else is wrong."""

    def clean(self):
        cleaned_data = super().clean()
        if self.proof_changed():
            self.add_error(None, self.changed_message)
            return cleaned_data
        if self.proof == "password":
            password = cleaned_data.get("current_password")
            if password is None:
                return cleaned_data
            if not second_factor.verify_password(self.request, self.user, password):
                self.add_error("current_password", self.password_error)
                return cleaned_data
            self.check_changes(cleaned_data)
            return cleaned_data
        self.check_changes(cleaned_data)
        self.check_code(cleaned_data)
        return cleaned_data


class SignInChangeProofForm(ReauthenticationForm):
    """Step 1 of "Change sign-in": the one proof of D-062 (the number code if set up, else the password), nothing else.

    Same checks, budgets and counters as every confirmation; a recovery code is refused. Only the labels are the ones of
    the flow. The explanation stands above the field, so the field carries no lead of its own.
    """

    code_label = _("Number code from your phone")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.proof == "code":
            field = self.fields["otp_token"]
            field.widget.attrs.update({"placeholder": "000 000", "inputmode": "numeric"})
            field.lead = None
            field.described_first = ()
        elif self.proof == "password":
            self.fields["current_password"].label = _("Password")
            # The error stands inline at the field and the focus returns to it (no summary box).
            self.fields["current_password"].widget.attrs["autofocus"] = True


class LostPhoneForm(PortalFormMixin, forms.Form):
    """The way "phone lost" of "turn off 2FA": the password **and** a recovery code (D-062).

    Order: format and required fields, then the password (under the abuse protection), then the
    code (used up only if everything before was right). A wrong password and a wrong code give the
    same answer.
    """

    current_password = _current_password_field()
    otp_token = forms.CharField(
        label=_("Recovery code"),
        max_length=256,
        widget=ProofCodeInput(attrs={"autocomplete": "off", "placeholder": "abcd-efgh"}),
        help_text=_("One of your recovery codes, for example abcd-efgh. Each code works only once."),
        error_messages={"required": _("Enter a recovery code.")},
    )
    shape_message = _("That is not a recovery code. It has 8 letters and digits, for example abcd-efgh.")
    wrong_message = _("The password or the recovery code is wrong. Check both and try again.")
    changed_message = OneProofMixin.changed_message

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user = request.user
        self.fields["otp_token"].error_before_hint = True
        self.fields["otp_token"].widget.attrs.pop("maxlength", None)

    def clean_otp_token(self):
        value = self.cleaned_data["otp_token"]
        if not RECOVERY_CODE_SHAPE.match(totp.normalize_code(value)):
            raise ValidationError(self.shape_message, code="code_shape")
        return value

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data
        if not second_factor.has_second_factor(self.user):
            self.add_error(None, self.changed_message)
            return cleaned_data
        # A right password must not reset the session's count, or the recovery code could be guessed without limit.
        if not second_factor.verify_password(
            self.request, self.user, cleaned_data["current_password"], reset_on_success=False
        ):
            self.add_error(None, self.wrong_message)
            return cleaned_data
        if second_factor.verify_second_factor(
            self.request, self.user, cleaned_data["otp_token"], app=False, per_session=True
        ) is None:
            self.add_error(None, self.wrong_message)
        return cleaned_data


class EmailChangeForm(ReauthenticationForm):
    """The profile card for the login address and the second address, with the proof (D-059).

    Either or both may change; the second may be emptied. A new address is only requested and
    confirmed with a link. The form reveals nothing about whether an address can be used.
    """

    email = forms.EmailField(
        label=_("Email address for login (username)"),
        max_length=EMAIL_MAX_LENGTH,
        error_messages={"required": _("Enter the new email address.")},
        widget=_email_input(),
    )
    secondary_email = forms.EmailField(
        label=_("Second email address"),
        required=False,
        max_length=EMAIL_MAX_LENGTH,
        help_text=_(
            "The second address receives information. You do not log in with it. Leave the field "
            "empty if you do not want one."
        ),
        widget=_email_input(),
    )
    field_order = ("email", "secondary_email", "current_password", "otp_token")

    link_note = _(
        "You confirm new addresses with a link. The confirmation is valid for 24 hours. "
        "Otherwise the current address stays valid."
    )

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, request=request, **kwargs)
        self.initial.setdefault("email", request.user.email)
        self.initial.setdefault("secondary_email", request.user.secondary_email)
        # The proof is the third field: no autofocus; the note stands below it.
        proof_field = self.fields[
            {"code": "otp_token", "passkey": "passkey_assertion", "password": "current_password"}[self.proof]
        ]
        proof_field.widget.attrs.pop("autofocus", None)
        proof_field.closing_note = self.link_note
        proof_field.described_by = (*getattr(proof_field, "described_by", ()), "note")
        # Filled by ``clean``.
        self.login_change = None
        self.secondary_change = None
        self.remove_secondary = False

    @staticmethod
    def _normalised(value, message):
        try:
            validate_account_email(value)
        except ValueError:
            raise ValidationError(message, code="invalid") from None
        return normalize_email_address(value)

    def clean_email(self):
        return self._normalised(self.cleaned_data["email"], _("Enter a valid email address."))

    def clean_secondary_email(self):
        value = self.cleaned_data.get("secondary_email")
        if not value:
            return ""
        return self._normalised(value, _("Enter a valid email address."))

    def check_changes(self, cleaned_data):
        if self.errors:
            return
        user = self.user
        login = cleaned_data["email"]
        secondary = cleaned_data.get("secondary_email", "")
        if login != user.email:
            self.login_change = login
        if secondary != user.secondary_email:
            if secondary:
                self.secondary_change = secondary
            else:
                self.remove_secondary = True
        # The second address is never the login address (current or new).
        if secondary and secondary == (self.login_change or user.email):
            self.add_error("secondary_email", _("The second address must differ from the address for login."))
        elif not (self.login_change or self.secondary_change or self.remove_secondary):
            self.add_error(None, ValidationError(_("Nothing was changed."), code="unchanged"))


class AccountDeleteForm(ReauthenticationForm):
    """The confirmation of the deletion: the proof (D-062) and the box that says the deletion cannot be undone (owner, 2026-10-24)."""

    confirm = forms.BooleanField(
        label=_("I know that my account will be deleted or deactivated and that this cannot be undone."),
        error_messages={
            "required": _("Please tick the box to confirm that you want to delete your account.")
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The box is never shown as ticked after an error: no confirmation by habit.
        self.fields["confirm"].widget = forms.CheckboxInput(
            attrs=self.fields["confirm"].widget.attrs, check_test=lambda value: False
        )


class ChangePasswordForm(OneProofMixin, PortalFormMixin, SetPasswordForm):
    """Change the password; asks for one proof first (code if a second factor is set up, else the current password; D-062)."""

    different_password_message = _("Choose a password that is different from the current one.")

    current_password = _current_password_field()
    # Instead of the current password where a second factor is set up.
    otp_token = forms.CharField(label=_("Code from the app"), max_length=256, widget=forms.TextInput())
    field_order = ("current_password", "otp_token", "new_password1", "new_password2")

    def __init__(self, user, *args, request, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.request = request
        self.use_one_proof(user)
        self.fields["new_password1"].widget.attrs.pop("autofocus", None)
        describe_password_fields(self, "new_password1", "new_password2")
        # The list of rules under the field says the same: no lead sentence (design walkthrough W4-02).
        self.fields["new_password1"].help_text = ""

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not second_factor.verify_password(self.request, self.user, password):
            raise ValidationError(ReauthenticationForm.password_error, code="password_incorrect")
        return password

    def clean(self):
        cleaned_data = super().clean()
        if self.proof_changed():
            self.add_error(None, self.changed_message)
            return cleaned_data
        # The code is checked (used up) only after every other field is valid.
        self.check_code(cleaned_data)
        # The new password is compared only after the proof was given, so the form cannot be used
        # to guess the password; no setter is used, so no rehash.
        proven = (
            "current_password" in cleaned_data
            if self.proof == "password"
            else "otp_token" in cleaned_data and not self.errors
        )
        new = cleaned_data.get("new_password2")
        if proven and new and "new_password1" in cleaned_data and check_password(new, self.user.password):
            self.add_error("new_password1", self.different_password_message)
        return cleaned_data


class ConfirmForm(PortalFormMixin, forms.Form):
    """A page without fields that only asks the person to confirm with a button."""
