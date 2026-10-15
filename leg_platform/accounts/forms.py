"""Forms of the portal: registration, completing the registration, and login.

The forms accept only the fields they define, so that any other submitted value
(``is_staff``, ``is_superuser``, groups, permissions, customer number, status, retention flag)
is ignored (docs/SECURITY.md, section 3).
"""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, SetPasswordMixin
from django.core.exceptions import ValidationError
from django.forms.boundfield import BoundField
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import second_factor
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

# Fields of the profile a person enters (docs/PROJECT.md, "Profile Data and Purpose"). The two
# postal code fields of the model are replaced by one form field (D-036).
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
PROFILE_FIELD_ORDER = (
    *PROFILE_FIELDS[:8],
    "postal_code",
    *PROFILE_FIELDS[8:],
)


def postal_code_field():
    """The single postal code field of forms (D-036)."""
    return forms.CharField(label=_("postal code"), max_length=15, required=False)


def password_rules():
    """The password rules as short lines for a list (docs/SECURITY.md, section 3; D-058).

    They state the rules that the configured validators check and nothing else: the policy has
    no composition rules (no upper-case, digit or special character is required), a long
    sentence is advised. The minimum length is the one of the configured validator, so the text
    cannot differ from the rule.
    """
    minimum = next(
        (
            validator.min_length
            for validator in password_validation.get_default_password_validators()
            if isinstance(validator, password_validation.MinimumLengthValidator)
        ),
        None,
    )
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
    """The advice at the password field (docs/SECURITY.md, section 3: passphrases are advised).

    Replaces the bullet list of Django's validator help texts, which stays out of the page: the
    rules are still checked and every violation is reported at the field. The minimum length is
    the one of the configured validator, so that the text cannot differ from the rule.
    """
    minimum = next(
        (
            validator.min_length
            for validator in password_validation.get_default_password_validators()
            if isinstance(validator, password_validation.MinimumLengthValidator)
        ),
        None,
    )
    if minimum is None:
        return ""
    return gettext(
        "Choose at least %(min_length)d characters. A sentence of several words is easy to "
        "remember and hard to guess."
    ) % {"min_length": minimum}


# Values of the HTML autocomplete attribute (HTML Standard, "Autofill").
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
    """A bound field whose ``aria-describedby`` names every text that belongs to the field: the
    help text, the hint or the list of rules that the template shows next to it (the field says
    which in ``described_by``, as suffixes of its id), and the error (security review, design
    review of phase 3: the order is the one a person reads)."""

    @property
    def aria_describedby(self):
        if self.field.widget.attrs.get("aria-describedby"):
            return None
        ids = []
        if self.auto_id and not self.is_hidden:
            if self.help_text:
                ids.append(f"{self.auto_id}_helptext")
            ids += [f"{self.auto_id}_{suffix}" for suffix in getattr(self.field, "described_by", ())]
            if self.errors:
                ids.append(f"{self.auto_id}_error")
        return " ".join(ids)


class PortalFormMixin:
    """Give the widgets of a form the control classes of Tabler (docs/DESIGN.md, section 9)."""

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
    """Write the postal code field into the Swiss or the foreign postal code of the instance.

    It is written into the Swiss postal code for Switzerland and into the foreign postal code
    for every other country (Liechtenstein included); the other one is cleared (D-036).
    """
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
            # A post office box is a number; the field is text with a numeric keyboard, so that
            # the browser shows no spin buttons and does not drop leading zeros. The value is
            # still validated on the server (an integer of at most 8 digits).
            "post_office_box_number": forms.TextInput(attrs={"inputmode": "numeric"}),
        }

    field_order = PROFILE_FIELD_ORDER

    def save(self, commit=True):
        """Write only the fields of the form (security review of phase 4, M1).

        The instance was loaded at the start of the request. A save of every column would write
        back what a parallel request changed in the meantime: the end of all sessions
        (``session_version``), a new password or the second address.
        """
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
        # The person sees the phone number as they would write it, not as it is stored (E.164).
        if self.instance.phone_number and not self.is_bound:
            self.initial["phone_number"] = self.instance.phone_number_display
        # Fields that are always required. The model allows them to be empty (the administration
        # needs that for accounts that are not completed yet), so the form states the rule, with
        # the message and the HTML attribute. The names, the street and the post office box are
        # conditionally required and are checked by the model (``User.clean``).
        for name, message in (
            ("town", _("Enter a town.")),
            ("country", _("Select a country.")),
            ("postal_code", _("Enter a postal code.")),
        ):
            self.fields[name].required = True
            self.fields[name].error_messages["required"] = message
        # Swiss postal codes are numbers; the keyboard of other countries needs letters.
        self.fields["postal_code"].widget.attrs["inputmode"] = (
            "numeric" if self._country_code() == SWITZERLAND else "text"
        )
        self.fields["post_office_box_number"].described_by = ("hint",)
        # The short label of the images (the registration says "Postal code" in full).
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
    """A form that asks only for an email address (registration and password reset).

    A plain form instead of a model form: any other submitted value is ignored, and nothing is
    stored by the form; the request is handled by the mail queue (D-016, D-037).
    """

    email = forms.EmailField(
        label=_("Email address"),
        max_length=EMAIL_MAX_LENGTH,
        error_messages={"required": _("Enter your email address.")},
        widget=forms.EmailInput(
            attrs={"inputmode": "email", "autocapitalize": "none", "spellcheck": "false"}
        ),
    )
    # The hint under the field (the time the link is valid) is read with it.
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
    """Make the field "username" of a bound form the address that belongs to the page.

    The field is shown as text and is only there for the password manager; whatever the request
    carries, the form is bound to the address of the page, so that a re-rendered form never shows
    or offers another one (security review of phase 3, A4).
    """
    if form.is_bound:
        form.data = form.data.copy()
        form.data[form.add_prefix("username")] = address


class NewPasswordForm(PortalFormMixin, SetPasswordForm):
    """The new password after a reset link (same password policy as everywhere, twice).

    The address is shown as text and is a real field marked as the user name, as on the password
    step of the registration, so that a password manager updates the saved login. The submitted
    value is never used.
    """

    username = forms.CharField(
        label=_("Email address (username)"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "readonly": True,
                "tabindex": "-1",
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        ),
    )
    field_order = ("username", "new_password1", "new_password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pin_username(self, self.user.email)
        self.initial["username"] = self.user.email
        self.fields["username"].widget.attrs["class"] = "plain-value"
        describe_password_fields(self, "new_password1", "new_password2")
        self.fields["new_password1"].help_text = ""
        self.fields["new_password1"].label = _("Password")
        self.fields["new_password2"].label = _("Repeat password")
        self.fields["new_password2"].help_text = ""

    def clean_username(self):
        return self.user.email


class RegistrationPasswordForm(SetPasswordMixin, PortalFormMixin, forms.Form):
    """Step 2 of the registration: the password (D-058).

    The address is shown as plain text but is a real field marked as the user name in the same
    form as the two password fields, so that a password manager saves both together. The
    submitted value is never used: the user name is the address that was verified.
    """

    username = forms.CharField(
        label=_("Email address (username)"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "readonly": True,
                "tabindex": "-1",
                "autocomplete": "username",
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        ),
    )
    password1, password2 = SetPasswordMixin.create_password_fields()

    field_order = ("username", "password1", "password2")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        pin_username(self, user.email)
        self.initial["username"] = user.email
        self.fields["username"].widget.attrs["class"] = "plain-value"
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
        # Only the address is known at this step; the names follow on the next page.
        self.validate_password_for_user(self.user, "password1")


class RegistrationDetailsForm(PortalFormMixin, forms.ModelForm):
    """Step 3 of the registration: name, address and the consent to the privacy notice (D-058).

    Only the fields of the design package (team lead decision K15). The country is Switzerland;
    the other fields of the profile (salutation, company, address addition, post office box,
    phone number) stay in the model and are entered in the profile later.
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

    field_order = (
        "first_name", "last_name", "street", "house_number", "postal_code", "town", "privacy",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label, message in (
            ("first_name", _("First name"), _("Enter your first name.")),
            ("last_name", _("Last name"), _("Enter your last name.")),
            ("street", _("Street"), _("Enter your street.")),
            ("town", _("Town"), _("Enter a town.")),
        ):
            self.fields[name].required = True
            self.fields[name].label = label
            self.fields[name].error_messages["required"] = message
        # Not every address has a house number, so the field may stay empty (the package marks
        # it neither as required nor as optional).
        self.fields["house_number"].label = _("House number")
        self.fields["house_number"].required = False
        self.fields["postal_code"].label = _("Postal code")
        self.fields["postal_code"].required = True
        self.fields["postal_code"].error_messages["required"] = _("Enter a postal code.")
        self.fields["postal_code"].widget.attrs.update(
            {"inputmode": "numeric", "autocomplete": "postal-code", "maxlength": "4"}
        )
        # A house number has no standard autocomplete value, so that no password manager fills it.
        self.fields["house_number"].widget.attrs["autocomplete"] = "off"
        self.fields["privacy"].widget.attrs["class"] = "entry-check-input"
        # The error summary names the box by a short word, not by the whole sentence.
        self.fields["privacy"].summary_label = _("Privacy policy")

    def _post_clean(self):
        super()._post_clean()
        # One clear message per field (design review of phase 3): the rules of the model for the
        # name and the address (company name, post office box) repeat what the fields say and
        # name things that this form does not ask for, so only the first message of a field stays.
        for errors in self._errors.values():
            del errors.data[1:]

    def clean(self):
        cleaned_data = super().clean()
        # The country of a registration is Switzerland (K15); the model default is the same.
        cleaned_data["country"] = SWITZERLAND
        clean_postal_code(self, cleaned_data)
        return cleaned_data


class LoginForm(PortalFormMixin, AuthenticationForm):
    """Login with the email address; every failure shows the same message."""

    error_messages = {
        "invalid_login": _("The email address or the password is incorrect."),
        "inactive": _("The email address or the password is incorrect."),
    }
    # Off by default. The login view decides what it means: a persistent session for a user, and
    # nothing for a staff user or system administrator (D-056).
    remember_me = forms.BooleanField(required=False, label=_("Stay signed in"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        username = self.fields["username"]
        username.label = _("Email address")
        username.error_messages["required"] = _("Enter your email address.")
        self.fields["password"].error_messages["required"] = _("Enter your password.")
        # An email keyboard on phones, no automatic capital letter and no spell checking. The
        # form stays ``novalidate``: the browser does not check the address, the server does.
        attrs = {
            **username.widget.attrs,
            "inputmode": "email",
            "autocapitalize": "none",
            "spellcheck": "false",
            "autocomplete": "username",
        }
        username.widget = forms.EmailInput(attrs=attrs)
        # A native checkbox of 20 px (login/VORGABEN.md, section 4), not Tabler's drawn one.
        self.fields["remember_me"].widget.attrs["class"] = "entry-check-input"

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(self.error_messages["inactive"], code="inactive")


class CodeForm(PortalFormMixin, forms.Form):
    """A code of the authenticator app or a recovery code.

    The field is named ``otp_token`` because the abuse protection masks that name when it
    stores the data of a failed attempt (``AXES_SENSITIVE_PARAMETERS``).

    Recovery codes are letters and digits, so this field must not be restricted to digits.
    """

    otp_token = forms.CharField(
        label=_("Code"),
        max_length=32,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
        error_messages={"required": _("Enter the code.")},
    )
    # One text for every place that asks for a code, at the login and in the profile (design
    # package, final spot check, 5): it says what to do. It does not tell whether the code was
    # wrong, expired or used, so it reveals nothing about the account.
    invalid_code_message = _(
        "The code is wrong, expired or was already used. The app shows a new code every 30 "
        "seconds: wait for the next one and try again."
    )


class SetupCodeForm(CodeForm):
    """The first code of the authenticator app when it is set up.

    Only the six digits of the app are valid here, never a recovery code, so the field asks for a
    numeric keyboard. Spaces and hyphens that a person copies along are still accepted by the
    check (``totp.normalize_code``), so the field has no ``maxlength`` of six.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["otp_token"].widget.attrs["inputmode"] = "numeric"
        self.fields["otp_token"].widget.attrs["placeholder"] = "000000"
        self.fields["otp_token"].label = _("Code from the app")


class StartSetupForm(PortalFormMixin, forms.Form):
    """Starts the setup of the authenticator app without a field: only for a session that
    authenticated a moment ago (``second_factor.recently_authenticated``, D-055)."""

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)


class ReauthenticationForm(PortalFormMixin, forms.Form):
    """Asks the logged-in person for the password and, if set up, the second factor again.

    For changes that a hijacked session must not be able to make (docs/SECURITY.md, section 3).
    Both checks run under the abuse protection of the login, so that the form cannot be used to
    guess the password or a code.
    """

    current_password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
        error_messages={"required": _("Enter your current password.")},
    )
    # Removed for an account without a second factor. A recovery code is also accepted here.
    otp_token = forms.CharField(
        label=_("Code from the app"),
        max_length=32,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "placeholder": "000000"}),
        error_messages={"required": _("Enter the code.")},
    )
    password_error = _("The password is not correct. Please try again.")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.user = request.user
        if not second_factor.has_second_factor(self.user):
            del self.fields["otp_token"]

    def check_changes(self, cleaned_data):
        """Hook for the forms that ask for more than the proof: add errors here. The code is
        checked, and so used up, only when nothing else is wrong (security review, final, R2)."""

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("current_password")
        if password is None:
            return cleaned_data
        if not second_factor.verify_password(self.request, self.user, password):
            self.add_error("current_password", self.password_error)
            return cleaned_data
        self.check_changes(cleaned_data)
        # The code is checked only after the password was right and the rest is valid.
        if "otp_token" in self.fields and not self.errors:
            if second_factor.verify_second_factor(
                self.request, self.user, cleaned_data["otp_token"]
            ) is None:
                self.add_error("otp_token", CodeForm.invalid_code_message)
        return cleaned_data


class EmailChangeForm(ReauthenticationForm):
    """The card "E-Mail-Adressen" of the profile: the login address and the second address, with
    the current password and, where set up, the code (D-059).

    Either address may change, or both; the second one may be emptied to remove it. A new address
    is not stored: it is requested and confirmed with a link. The form tells nothing about
    whether an address can be used (no account enumeration).
    """

    email = forms.EmailField(
        label=_("Email address for login (username)"),
        max_length=EMAIL_MAX_LENGTH,
        error_messages={"required": _("Enter the new email address.")},
        widget=forms.EmailInput(
            attrs={"inputmode": "email", "autocapitalize": "none", "spellcheck": "false"}
        ),
    )
    secondary_email = forms.EmailField(
        label=_("Second email address"),
        required=False,
        max_length=EMAIL_MAX_LENGTH,
        help_text=_(
            "The second address receives information. You do not log in with it. Leave the field "
            "empty if you do not want one."
        ),
        widget=forms.EmailInput(
            attrs={"inputmode": "email", "autocapitalize": "none", "spellcheck": "false"}
        ),
    )
    field_order = ("email", "secondary_email", "current_password", "otp_token")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, request=request, **kwargs)
        self.initial.setdefault("email", request.user.email)
        self.initial.setdefault("secondary_email", request.user.secondary_email)
        self.fields["current_password"].help_text = _(
            "You confirm new addresses with a link. The confirmation is valid for 24 hours. "
            "Otherwise the current address stays valid."
        )
        # The login address and the new one(s) as the form found them; filled by ``clean``.
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
        # The second address is never the login address (the current one, or the new one).
        if secondary and secondary == (self.login_change or user.email):
            self.add_error(
                "secondary_email",
                _("The second address must differ from the address for login."),
            )
        elif not (self.login_change or self.secondary_change or self.remove_secondary):
            self.add_error(None, ValidationError(_("Nothing was changed."), code="unchanged"))


class AccountDeleteForm(ReauthenticationForm):
    """The confirmation of the deletion: the password (and code), and the box (D-059)."""

    confirm = forms.BooleanField(
        label=_(
            "I know that my account and my details will be deleted and that this cannot be undone."
        ),
        error_messages={
            "required": _("Please tick the box to confirm that you want to delete your account.")
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The confirmation is given again with every attempt: after an error the box is never
        # shown as ticked, so that a repeated submit is never a confirmation by habit.
        # The error summary names the box by a short word, not by the whole sentence (D11).
        self.fields["confirm"].summary_label = _("Confirmation")
        self.fields["confirm"].widget = forms.CheckboxInput(
            attrs=self.fields["confirm"].widget.attrs, check_test=lambda value: False
        )


class ChangePasswordForm(PortalFormMixin, SetPasswordForm):
    """Change the password of the logged-in account; asks for the current password first."""

    different_password_message = _("Choose a password that is different from the current one.")

    current_password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
        error_messages={"required": _("Enter your current password.")},
    )
    # Asked for where a second factor is set up, as for the other changes of the account (K12,
    # security review of phase 4); a recovery code is also accepted. Removed otherwise.
    otp_token = forms.CharField(
        label=_("Code from the app"),
        max_length=32,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "placeholder": "000000"}),
        error_messages={"required": _("Enter the code.")},
    )
    field_order = ("current_password", "otp_token", "new_password1", "new_password2")

    def __init__(self, user, *args, request, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.request = request
        if not second_factor.has_second_factor(user):
            del self.fields["otp_token"]
        self.fields["new_password1"].widget.attrs.pop("autofocus", None)
        describe_password_fields(self, "new_password1", "new_password2")

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not second_factor.verify_password(self.request, self.user, password):
            raise ValidationError(ReauthenticationForm.password_error, code="password_incorrect")
        return password

    def clean(self):
        cleaned_data = super().clean()
        # An initial password that must be replaced is not replaced by itself (D-005).
        if (
            self.user.password_change_required
            and "current_password" in cleaned_data
            and "new_password2" in cleaned_data
            and cleaned_data["new_password2"] == cleaned_data["current_password"]
        ):
            self.add_error("new_password1", self.different_password_message)
        # The code is checked, and so used up, only after the password was right and every other
        # field is valid: a recovery code must not be lost to a form that fails anyway (security
        # review, final, R2).
        if (
            "otp_token" in self.fields
            and not self.errors
            and second_factor.verify_second_factor(
                self.request, self.user, cleaned_data["otp_token"]
            )
            is None
        ):
            self.add_error("otp_token", CodeForm.invalid_code_message)
        return cleaned_data


class ConfirmForm(PortalFormMixin, forms.Form):
    """A page without fields that only asks the person to confirm with a button."""
