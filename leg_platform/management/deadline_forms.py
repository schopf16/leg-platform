"""The chapter "Reminders and deadlines" of the settings page (KONZEPT 5.8, D-103): the five deadlines of the
applications, and (own form) the time of the daily address directory run. The form only reads and checks input; the rules
(whole days from 1, the number of drafts a change would delete, the history) are in ``community.deadline_settings``."""

from dataclasses import dataclass

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models import EmailText
from leg_platform.community import deadline_settings, grid_operator
from leg_platform.community.models import ManagementSettings

DAY_INPUT = {
    "class": "form-control mgmt-field-short tnum", "autocomplete": "off", "inputmode": "numeric", "maxlength": "10",
}
# The column is an unsigned 32-bit integer (4294967295: ten digits); more digits are refused before any conversion.
MAX_DIGITS = 10
TOO_LARGE = _("The number is too large.")
Key = EmailText.Key


@dataclass(frozen=True)
class Deadline:
    """One row of the chapter: the setting, what starts it, who is told, the mail texts and one sentence of effect."""

    field: str
    name: object
    trigger: object
    recipient: object
    mail_keys: tuple
    effect: object


DEADLINES = (
    Deadline(
        "reminder_after_days", _("Application not completed"),
        _("The last change of a draft by the customer"),
        _("Main and second address of the customer"), (Key.APPLICATION_REMINDER, Key.CONTRACT_REMINDER),
        _("A draft without a change for this many days is paused and the customer receives one reminder."),
    ),
    Deadline(
        "rollback_after_days", _("Draft deleted"),
        _("The reminder, if the draft has not been changed since"),
        _("Main and second address of the customer"), (Key.APPLICATION_ROLLED_BACK,),
        _(
            "This many days after the reminder, without a change, the paused draft is deleted and the customer "
            "receives an email. The bank connection and stored buildings stay."
        ),
    ),
    Deadline(
        "contract_deadline_days", _("Contract not accepted"),
        _("Everything but the contract is complete in the draft"),
        _("Main and second address of the customer"), (Key.CONTRACT_REMINDER, Key.CONTRACT_ROLLED_BACK),
        _("After this many days without the acceptance the draft is deleted and the customer receives an email."),
    ),
    Deadline(
        "operator_confirmation_days", grid_operator.text(_("Confirmation by the %(operator)s")),
        grid_operator.text(_("The registration at the %(operator)s was started")),
        _("Every active staff account"), (Key.OPERATOR_CONFIRMATION_LAPSED,),
        grid_operator.text(
            _(
                "Without a recorded confirmation after this many days the application is rolled back and the staff "
                "is told to withdraw the registration at the %(operator)s."
            )
        ),
    ),
    Deadline(
        "iban_missing_reminder_days", _("Bank connection missing"),
        _("A submitted application with a feed-in and no bank connection on file"),
        _("Main and second address of the customer"), (Key.BANK_CONNECTION_MISSING,),
        _("This many days after the submission the customer receives one reminder; then once per period."),
    ),
)


class DeadlinesForm(forms.Form):
    """The five deadlines (whole days from 1, any size: the owner sets no upper limit). ``deadline_values()`` is the
    cleaned set of numbers. (The time of the address directory run has its own form, ``RunTimeForm``: it is no
    deadline.)"""

    reminder_after_days = forms.CharField(
        label=_("Days without a change until the reminder"), required=False, widget=forms.TextInput(attrs=DAY_INPUT)
    )
    rollback_after_days = forms.CharField(
        label=_("Days after the reminder until the draft is deleted"), required=False,
        widget=forms.TextInput(attrs=DAY_INPUT),
    )
    contract_deadline_days = forms.CharField(
        label=_("Days until the draft is deleted (contract)"), required=False, widget=forms.TextInput(attrs=DAY_INPUT)
    )
    operator_confirmation_days = forms.CharField(
        label=grid_operator.text(_("Days until the confirmation by the %(operator)s is overdue")), required=False,
        widget=forms.TextInput(attrs=DAY_INPUT),
    )
    iban_missing_reminder_days = forms.CharField(
        label=_("Days until the reminder (bank connection)"), required=False, widget=forms.TextInput(attrs=DAY_INPUT)
    )

    @classmethod
    def from_settings(cls):
        """The form with the stored values."""
        return cls(initial=dict(deadline_settings.current_values()))

    def clean(self):
        cleaned = super().clean()
        raw = {}
        for name in deadline_settings.FIELDS:
            text = (cleaned.get(name) or "").strip()
            if text.isascii() and text.isdigit() and len(text) > MAX_DIGITS:
                # Not converted at all: a number of thousands of digits must not reach ``int``.
                self.add_error(name, TOO_LARGE)
            else:
                raw[name] = text
        try:
            values = deadline_settings.clean_values(raw)
        except ValidationError as error:
            values = {}
            for name, errors in error.error_dict.items():
                self.add_error(name, errors)
        # The column of the database is an unsigned integer: its own range is the only upper limit.
        for name, value in list(values.items()):
            try:
                ManagementSettings._meta.get_field(name).run_validators(value)
            except ValidationError as error:
                self.add_error(name, error)
                del values[name]
        self.cleaned_values = values
        return cleaned

    def deadline_values(self):
        """The five numbers as whole days (after ``is_valid``)."""
        return dict(self.cleaned_values)


class RunTimeForm(forms.Form):
    """The time of day of the daily address directory run (KONZEPT 3a.5): its own small block of the settings page."""

    run_time = forms.TimeField(
        label=_("Time of the daily run"), input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(attrs={"type": "time", "class": "form-control mgmt-field-short tnum"}, format="%H:%M"),
        error_messages={"required": _("Enter a time of day."), "invalid": _("Enter a time of day, for example 03:00.")},
    )

    @classmethod
    def from_settings(cls):
        """The form with the stored time."""
        return cls(initial={"run_time": deadline_settings.address_run_time()})
