"""Forms of the process pages (applications, deregistrations, notices). A form only reads and checks input; the rules are
in ``leg_platform.community`` (``application_staff``, ``deregistrations``). Dialogs are laid out by ``layout`` as
``dialog_form.html`` expects (D-097)."""

from django import forms
from django.utils.translation import gettext_lazy as _

from leg_platform.community import grid_operator
from leg_platform.community.models import REJECTION_NOTE_MAX_LENGTH, RETURN_REASON_MAX_LENGTH
from leg_platform.management.form_fields import date_field
from leg_platform.management.metering_point_forms import DialogForm
from leg_platform.management.metering_point_views import format_date

SELECT = {"class": "form-select"}


class OperatorStartedForm(DialogForm):
    started_on = date_field(grid_operator.text(_("Registration at the %(operator)s started on")), initial="today")
    layout = [("single", ["started_on"])]
    # One sentence only: the lead of the dialog already says that the customer can no longer withdraw (W5-04).
    hint = _("The date is not in the future.")


class OperatorConfirmationForm(DialogForm):
    confirmed_on = date_field(grid_operator.text(_("Confirmation by the %(operator)s received on")), initial="today")
    layout = [("single", ["confirmed_on"])]
    hint = _("The date is not in the future and not before the registration.")


class CompleteForm(DialogForm):
    """Complete an application: the start date and whether the addresses of new buildings are confirmed at once.

    ``defaults`` (``application_staff.completion_defaults``) fills the start date with the customer's wish, never before
    the earliest date, and names the earliest date. ``confirm_addresses`` is an explicit tick, off by default (what the
    service does without it: new buildings stay "not yet confirmed" and editable); it is drawn by
    ``processes/dialog_complete.html`` and exists only while a building of the application is not confirmed yet."""

    start_date = date_field(_("Start date"))
    confirm_addresses = forms.BooleanField(required=False)
    layout = [("single", ["start_date"])]

    def __init__(self, *args, defaults=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.unconfirmed = defaults.unconfirmed_addresses if defaults is not None else 0
        if not self.unconfirmed:
            # Nothing to confirm: the field is not offered (a posted value is ignored).
            del self.fields["confirm_addresses"]
        if defaults is not None:
            self.fields["start_date"].initial = defaults.suggested_start_date
            earliest = format_date(defaults.earliest_start_date)
            self.fields["start_date"].help_text = (
                _("Earliest on %(date)s (three months after the registration).") % {"date": earliest}
                if defaults.registered
                else _("Earliest on %(date)s (three months from today).") % {"date": earliest}
            )

    def clean(self):
        cleaned = super().clean()
        cleaned.setdefault("confirm_addresses", False)
        return cleaned


class ReturnForm(DialogForm):
    """Return an application for correction: the reason is required and shown to the customer (500 characters)."""

    reason = forms.CharField(
        label=_("Reason for the correction"), max_length=RETURN_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 4, "maxlength": RETURN_REASON_MAX_LENGTH}),
        help_text=_("What the customer has to correct. The customer reads this text in the portal."),
    )
    layout = [("single", ["reason"])]


class RejectForm(DialogForm):
    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=[
            ("rejected", _("Not accepted by the LEG")),
            ("operator_rejected", grid_operator.text(_("Rejected by the %(operator)s"))),
        ],
        widget=forms.Select(attrs=SELECT),
    )
    note = forms.CharField(
        label=_("Note for the customer"), required=False, max_length=REJECTION_NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": REJECTION_NOTE_MAX_LENGTH}),
        help_text=_("Shown to the customer once on the next visit, not in the email."),
    )
    layout = [("single", ["reason"]), ("single", ["note"])]
    optional = ("note",)
    hint = _(
        "The application and its metering points are deleted. The account, the bank connection and stored "
        "buildings stay. The customer receives an email."
    )


class ConfirmDeregistrationForm(DialogForm):
    """Confirm the termination date of a deregistration. The field is filled with ``suggested_date`` of
    ``deregistrations.confirmation_defaults`` (the customer's wish) and names the earliest date (W5-05)."""

    confirmed_date = date_field(_("Effective on"))
    layout = [("single", ["confirmed_date"])]
    hint = _("The last day of a month, not in the past.")

    def __init__(self, *args, defaults=None, **kwargs):
        super().__init__(*args, **kwargs)
        if defaults is not None:
            self.fields["confirmed_date"].initial = defaults.suggested_date
            self.fields["confirmed_date"].help_text = _(
                "Earliest on %(date)s (four months after the notice, to the end of a month)."
            ) % {"date": format_date(defaults.earliest_date)}
