"""Forms of the portal: registration, completing the registration, and login.

The forms accept only the fields they define, so that any other submitted value
(``is_staff``, ``is_superuser``, groups, permissions, customer number, status, retention flag)
is ignored (docs/SECURITY.md, section 3).
"""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm, SetPasswordMixin
from django.core.exceptions import ValidationError
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import (
    EMAIL_MAX_LENGTH,
    SWISS_POSTAL_CODE_MESSAGE,
    SWITZERLAND,
    User,
    parse_swiss_postal_code,
    validate_account_email,
)

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


class PortalFormMixin:
    """Give the widgets of a form the control classes of Tabler (docs/DESIGN.md, section 9)."""

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

    field_order = PROFILE_FIELD_ORDER

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial.setdefault("postal_code", self.instance.postal_code_display)

    def clean(self):
        cleaned_data = super().clean()
        clean_postal_code(self, cleaned_data)
        return cleaned_data


class EmailAddressForm(PortalFormMixin, forms.Form):
    """A form that asks only for an email address (registration and password reset).

    A plain form instead of a model form: any other submitted value is ignored, and nothing is
    stored by the form; the request is handled by the mail queue (D-016, D-037).
    """

    email = forms.EmailField(label=_("Email address"), max_length=EMAIL_MAX_LENGTH)

    def clean_email(self):
        email = self.cleaned_data["email"]
        try:
            validate_account_email(email)
        except ValueError:
            raise ValidationError(_("Enter a valid email address."), code="invalid") from None
        return email


class RegistrationForm(EmailAddressForm):
    """Registration asks only for the email address; the profile follows after the link."""


class PasswordResetRequestForm(EmailAddressForm):
    pass


class NewPasswordForm(PortalFormMixin, SetPasswordForm):
    """The new password after a reset link (same password policy as everywhere, twice)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].help_text = mark_safe(
            str(password_validation.password_validators_help_text_html())
        )


class CompleteRegistrationForm(SetPasswordMixin, ProfileForm):
    """Checking the profile and choosing the password after the address was verified.

    The email address is shown by the view and cannot be changed here.
    """

    password1, password2 = SetPasswordMixin.create_password_fields()

    field_order = (*PROFILE_FIELD_ORDER, "password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Django builds this list of the password rules with escaped text (format_html); it is
        # the only help text that contains HTML, so it alone is marked as safe here, and the
        # template escapes every other help text.
        self.fields["password1"].help_text = mark_safe(
            str(password_validation.password_validators_help_text_html())
        )

    def clean(self):
        cleaned_data = super().clean()
        self.validate_passwords()
        return cleaned_data

    def _post_clean(self):
        super()._post_clean()
        # After the instance holds the submitted names, so that the similarity validator
        # compares the password with them.
        self.validate_password_for_user(self.instance, "password1")


class LoginForm(PortalFormMixin, AuthenticationForm):
    """Login with the email address; every failure shows the same message."""

    error_messages = {
        "invalid_login": _("The email address or the password is incorrect."),
        "inactive": _("The email address or the password is incorrect."),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("Email address")

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(self.error_messages["inactive"], code="inactive")
