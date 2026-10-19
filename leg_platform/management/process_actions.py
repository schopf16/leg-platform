"""The staff's steps of an application and of a deregistration as dialogs (D-103): take into review, return for
correction, the registration at the grid operator, its confirmation, completion, roll back; confirm the date of a
deregistration; every one calls a service of ``community`` that checks the state again under a lock, so the page never
decides a rule. A dialog is a normal page (works without JavaScript) that returns to where it was opened
(``management.returns``). An unknown id is a 404; a draft is never shown (its pages are 404).
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import application_staff, deregistrations, grid_operator
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Application, Deregistration
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView
from leg_platform.management.metering_point_forms import DialogForm
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.process_forms import (
    CompleteForm,
    ConfirmDeregistrationForm,
    OperatorConfirmationForm,
    OperatorStartedForm,
    RejectForm,
)
from leg_platform.management.process_views import VISIBLE_APPLICATION_STATES
from leg_platform.management.returns import return_url


def error_texts():
    """The refusals of the services as texts for the dialog (the code is the stable part; the text is translated)."""
    return {
        "wrong_state": _("This step is not possible in the current state. Reload the page."),
        "not_allowed": _("You are not allowed to do this."),
        "not_found": _("This entry does not exist any more."),
        "date_in_future": _("The date must not be in the future."),
        "date_before_start": _("The date must not be before the registration."),
        "number_exists": _("A metering point number of this application was stored in the meantime. Nothing was saved."),
        "building_unavailable": _("A building of this application is not available any more. Nothing was saved."),
        "date_before_earliest": _("The date must not be in the past."),
        "not_month_end": _("Enter the last day of a month."),
        "cannot_terminate": _("A metering point cannot be terminated to this date. Nothing was saved."),
    }


def refusal_text(error):
    """The text of an ``ApplicationError`` (with the facts it carries, e.g. the earliest start date)."""
    if error.code == "start_too_early":
        return _("The start date must not be before %(date)s (three months after the registration).") % {
            "date": format_date(error.details["earliest"])
        }
    return error_texts().get(error.code, _("This step is not possible. Nothing was saved."))


class QuestionForm(DialogForm):
    """A dialog without fields: only a question and the buttons."""

    layout = []


class ProcessDialog(DialogView):
    """A dialog about one application. ``states`` are the states in which it applies (a GET in another state goes back
    with a message; a POST is refused by the service)."""

    states = ()
    form_class = QuestionForm
    danger = False
    success_message = gettext_lazy("Saved.")
    detail_name = "management:processes_application"

    def application(self):
        if not hasattr(self, "_application"):
            self._application = get_object_or_404(
                Application.objects.select_related("person").filter(state__in=VISIBLE_APPLICATION_STATES),
                pk=self.kwargs["pk"],
            )
        return self._application

    @property
    def default_back(self):
        return reverse(self.detail_name, args=[self.kwargs["pk"]])

    def applicable(self):
        return self.application().state in self.states

    def get(self, request, **kwargs):
        if not self.applicable():
            messages.error(request, error_texts()["wrong_state"])
            return redirect(self.back(request))
        return super().get(request, **kwargs)

    def post(self, request, **kwargs):
        if not self.applicable():
            messages.error(request, error_texts()["wrong_state"])
            return redirect(self.back(request))
        return super().post(request, **kwargs)

    def render_dialog(self, request, form, **extra):
        return super().render_dialog(request, form, danger=self.danger, **extra)

    def run(self, application, data, actor):
        raise NotImplementedError

    def after(self, request):
        messages.success(request, self.success_message)
        return redirect(self.back(request))

    def save(self, request, form):
        try:
            self.run(self.application(), form.cleaned_data, request.user)
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            form.add_error(None, refusal_text(error))
            return None
        return self.after(request)


class ApplicationReviewView(ManagementAccessMixin, View):
    """"Take into review": a button of the page, POST only."""

    http_method_names = ["post"]

    def post(self, request, pk):
        application = get_object_or_404(Application.objects.filter(state__in=VISIBLE_APPLICATION_STATES), pk=pk)
        try:
            application_staff.start_review(application, request.user)
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            messages.error(request, refusal_text(error))
        else:
            messages.success(request, _("The application is in review."))
        return redirect(return_url(request, reverse("management:processes_application", args=[pk])))


class ApplicationReturnView(ProcessDialog):
    states = Application.WITHDRAWABLE_STATES
    title = gettext_lazy("Return for correction")
    submit_label = gettext_lazy("Return for correction")

    def get_lead(self):
        return _(
            "The application becomes a draft again. The customer can correct it and submit it again. "
            "No email is sent: the customer sees it in the portal."
        )

    def run(self, application, data, actor):
        application_staff.return_for_correction(application, actor)


class ApplicationOperatorStartedView(ProcessDialog):
    states = Application.WITHDRAWABLE_STATES
    title = grid_operator.text(gettext_lazy("Registration at the %(operator)s started"))
    submit_label = gettext_lazy("Save")
    form_class = OperatorStartedForm

    def get_lead(self):
        return _("From now on the customer can no longer withdraw the application; only the staff can change it.")

    def run(self, application, data, actor):
        application_staff.mark_operator_started(application, actor, data["started_on"])


class ApplicationOperatorConfirmedView(ProcessDialog):
    states = (Application.State.OPERATOR_STARTED,)
    title = gettext_lazy("Record confirmation")
    submit_label = gettext_lazy("Save confirmation")
    form_class = OperatorConfirmationForm

    def get_lead(self):
        return _("The customer has confirmed the registration with the %(operator)s.") % {
            "operator": grid_operator.name()
        }

    def run(self, application, data, actor):
        application_staff.record_operator_confirmation(application, actor, data["confirmed_on"])


class ApplicationCompleteView(ProcessDialog):
    states = (Application.State.OPERATOR_CONFIRMED,)
    title = gettext_lazy("Complete application")
    submit_label = gettext_lazy("Complete application")
    form_class = CompleteForm
    success_message = gettext_lazy("The application is completed. The metering points were created.")

    def get_form(self, data=None):
        return CompleteForm(data, application=self.application())

    def get_lead(self):
        return _("The buildings and metering points are created and assigned to the customer from the start date.")

    def run(self, application, data, actor):
        application_staff.complete(
            application, actor, data["start_date"], confirm_addresses=data["confirm_addresses"]
        )


class ApplicationRejectView(ProcessDialog):
    states = Application.IN_FLIGHT_STATES
    title = gettext_lazy("Roll back application")
    submit_label = gettext_lazy("Roll back application")
    form_class = RejectForm
    danger = True
    success_message = gettext_lazy("The application was rolled back.")

    def get_lead(self):
        return _("The application is rolled back and cannot be restored.")

    def run(self, application, data, actor):
        application_staff.reject(application, actor, data["reason"])

    def after(self, request):
        messages.success(request, self.success_message)
        # The application is gone, so the page it was opened from is gone too: the way on is the list.
        return redirect(reverse("management:processes"))


# ---- A deregistration ----


class DeregistrationConfirmView(DialogView):
    """"Confirm the date": records the termination date at every metering point of the deregistration."""

    title = gettext_lazy("Confirm the date")
    submit_label = gettext_lazy("Confirm the date")
    form_class = ConfirmDeregistrationForm

    def deregistration(self):
        if not hasattr(self, "_deregistration"):
            self._deregistration = get_object_or_404(Deregistration, pk=self.kwargs["pk"])
        return self._deregistration

    @property
    def default_back(self):
        return reverse("management:processes_deregistration", args=[self.kwargs["pk"]])

    def applicable(self):
        return self.deregistration().state == Deregistration.State.SUBMITTED

    def get_form(self, data=None):
        return ConfirmDeregistrationForm(data, initial={"confirmed_date": self.deregistration().desired_date})

    def get_lead(self):
        return _(
            "The customer asked for %(date)s. The date is recorded as the termination date of every metering point "
            "of the deregistration."
        ) % {"date": format_date(self.deregistration().desired_date)}

    def get(self, request, **kwargs):
        if not self.applicable():
            messages.error(request, error_texts()["wrong_state"])
            return redirect(self.back(request))
        return super().get(request, **kwargs)

    def post(self, request, **kwargs):
        if not self.applicable():
            messages.error(request, error_texts()["wrong_state"])
            return redirect(self.back(request))
        return super().post(request, **kwargs)

    def save(self, request, form):
        try:
            deregistrations.confirm(self.deregistration(), request.user, form.cleaned_data["confirmed_date"])
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            form.add_error(None, refusal_text(error))
            return None
        messages.success(request, _("The date is confirmed."))
        return redirect(self.back(request))


__all__ = [
    "ApplicationCompleteView", "ApplicationOperatorConfirmedView", "ApplicationOperatorStartedView",
    "ApplicationRejectView", "ApplicationReturnView", "ApplicationReviewView", "DeregistrationConfirmView",
]
