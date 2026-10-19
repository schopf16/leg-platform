"""Forms of the process pages (applications, deregistrations, notices). A form only reads and checks input; the rules are
in ``leg_platform.community`` (``application_staff``, ``deregistrations``). Dialogs are laid out by ``layout`` as
``dialog_form.html`` expects (D-097)."""

from django import forms
from django.utils.translation import gettext_lazy as _

from leg_platform.community import application_staff, grid_operator
from leg_platform.management.form_fields import date_field
from leg_platform.management.metering_point_forms import DialogForm

SELECT = {"class": "form-select"}


class OperatorStartedForm(DialogForm):
    started_on = date_field(grid_operator.text(_("Registration at the %(operator)s started on")), initial="today")
    layout = [("single", ["started_on"])]
    hint = _("The date is not in the future. From now on the customer cannot withdraw the application any more.")


class OperatorConfirmationForm(DialogForm):
    confirmed_on = date_field(grid_operator.text(_("Confirmation by the %(operator)s received on")), initial="today")
    layout = [("single", ["confirmed_on"])]
    hint = _("The date is not in the future and not before the registration.")


class CompleteForm(DialogForm):
    """Complete an application: the start date and whether the new addresses are confirmed at once."""

    start_date = date_field(_("Start date"))
    confirm_addresses = forms.TypedChoiceField(
        label=_("New addresses"),
        choices=[("0", _("Not yet confirmed")), ("1", _("Confirmed: the addresses are checked"))],
        coerce=lambda value: value == "1", widget=forms.Select(attrs=SELECT),
    )
    layout = [("single", ["start_date"]), ("single", ["confirm_addresses"])]
    hint = _("A confirmed address is locked: the customer can no longer change it.")

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        if application is not None and application.operator_started_on is not None:
            earliest = application_staff.earliest_completion_start(application)
            self.earliest = earliest
            chosen = application.desired_start_date if application.desired_start_date else earliest
            self.fields["start_date"].initial = max(chosen, earliest)
            self.fields["start_date"].help_text = _("Not before the day three months after the registration.")


class RejectForm(DialogForm):
    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=[
            ("rejected", _("Rolled back by the LEG")),
            ("operator_rejected", grid_operator.text(_("Rejected by the %(operator)s"))),
        ],
        widget=forms.Select(attrs=SELECT),
    )
    layout = [("single", ["reason"])]
    hint = _(
        "The application and its metering points are deleted. The account, the bank connection and stored "
        "buildings stay. The customer receives an email."
    )


class ConfirmDeregistrationForm(DialogForm):
    confirmed_date = date_field(_("Effective on"))
    layout = [("single", ["confirmed_date"])]
    hint = _("The last day of a month, not in the past. Recorded as the termination date of every metering point.")
