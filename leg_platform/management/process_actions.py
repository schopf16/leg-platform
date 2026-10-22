"""The staff's steps of an application and of a deregistration as dialogs (D-103): take into review, return for
correction, the registration at the grid operator, its confirmation, completion, roll back; confirm the date of a
deregistration; every one calls a service of ``community`` that checks the state again under a lock, so the page never
decides a rule. A dialog is a normal page (works without JavaScript) that returns to where it was opened
(``management.returns``). An unknown id is a 404; a draft has a read-only page (D-107) and no dialog applies to it.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import addressing, application_corrections, application_staff, deregistrations, grid_operator, numbers
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    Deregistration,
    ManagementSettings,
)
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView
from leg_platform.management.metering_point_forms import DialogForm
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.process_forms import (
    ApplicationBuildingForm,
    ApplicationPointForm,
    CompleteForm,
    ConfirmDeregistrationForm,
    OperatorConfirmationForm,
    OperatorStartedForm,
    RejectForm,
    ReturnForm,
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
        "reason_required": _("Enter a reason."),
        "building_unavailable": _("A building of this application is not available any more. Nothing was saved."),
        "date_before_earliest": _("The date must not be in the past."),
        "not_month_end": _("Enter the last day of a month."),
        "cannot_terminate": _("A metering point cannot be terminated to this date. Nothing was saved."),
        # The corrections of an application (D-109).
        "changed": _(
            "This entry was changed in the meantime, for example by the customer. Nothing was saved. Go back and "
            "check the page."
        ),
        "unchanged": _("Nothing was changed."),
        "building_stored": _("This building is stored already. Change its address on the page of the location."),
        "number_taken": _(
            "This number belongs to a stored metering point or to another application. Nothing was saved."
        ),
        "number_own": _("This number is used already by a metering point or an application of the same customer."),
        "number_in_application": _("This number is used already by another metering point of this application."),
        "direction_taken": _("The building has a metering point with this direction already."),
        "building_complete": _(
            "The customer has a metering point for consumption and one for feed-in at this building already."
        ),
        "direction_invalid": _("Choose consumption or feed-in."),
        "iban_required": _("A feed-in needs the bank connection of the customer. The customer enters it in the profile."),
        "draw_required": _(
            "A building needs a metering point for consumption. Change the other metering point of the building first."
        ),
        "plant_data_feed_in_only": _("Plant details apply to a feed-in only. Clear them or choose feed-in."),
        "plant_data_swap": _(
            "The other metering point of the building has plant details, which a metering point for consumption cannot "
            "have. Clear them there first."
        ),
        "prefix_not_configured": _("Enter the country and the identifier in the settings first."),
        "already_added": _("The application has this building already."),
        "already_in_progress": _("The customer has another application for this building."),
        "egid_not_offered": _("The EGID does not belong to this address."),
        "egid_invalid": _("Enter the EGID as a number."),
        "directory_unavailable": _("The address directory cannot be asked now. Try again later."),
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
    with a message; a POST is refused by the service). ``leaves_page`` is set by a step after which the page of the
    application is no longer there (a draft or a deleted application has no page): the way on is the list."""

    states = ()
    form_class = QuestionForm
    danger = False
    leaves_page = False
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
        if self.leaves_page:
            return redirect(reverse("management:processes"))
        return redirect(self.back(request))

    def save(self, request, form):
        try:
            self.run(self.application(), form.cleaned_data, request.user)
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            form.add_error(None, refusal_text(error))
            return None
        except ValidationError as error:
            # A free text with a control character (the form checked the length already).
            form.add_error(None, error)
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
    form_class = ReturnForm
    # After the step the way on is the list (walkthrough W2-02); the draft's own page is read-only (D-107).
    leaves_page = True
    success_message = gettext_lazy("The application was returned for correction.")

    def get_lead(self):
        return _(
            "The application becomes a draft again. The customer can correct it and submit it again. "
            "The customer receives an email and reads your reason in the portal."
        )

    def run(self, application, data, actor):
        application_staff.return_for_correction(application, actor, data["reason"])


class ApplicationOperatorStartedView(ProcessDialog):
    states = Application.WITHDRAWABLE_STATES
    title = grid_operator.text(gettext_lazy("Record the start at the %(operator)s"))
    submit_label = gettext_lazy("Record the start")
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
        # A request, not a statement: what the person is asked to record (walkthrough W5-04).
        return _("Record when the customer confirmed the registration at the %(operator)s.") % {
            "operator": grid_operator.name()
        }

    def run(self, application, data, actor):
        application_staff.record_operator_confirmation(application, actor, data["confirmed_on"])


class ApplicationCompleteView(ProcessDialog):
    """"Complete application": the start date is filled from ``application_staff.completion_defaults`` (the customer's
    wish, never before the earliest date), the dialog names the wish and the earliest date, and the confirmation of the
    new addresses is an explicit tick (W5-05, W5-07)."""

    states = (Application.State.OPERATOR_CONFIRMED,)
    title = gettext_lazy("Complete application")
    submit_label = gettext_lazy("Complete application")
    form_class = CompleteForm
    template_name = "management/processes/dialog_complete.html"
    success_message = gettext_lazy("The application is completed. The metering points were created.")

    def defaults(self):
        """The defaults of the service; None if the state moved meanwhile (the service then refuses the step with its
        own message)."""
        if not hasattr(self, "_defaults"):
            try:
                self._defaults = application_staff.completion_defaults(self.application(), self.request.user)
            except ApplicationError as error:
                if error.code == "not_found":
                    raise Http404 from None
                self._defaults = None
        return self._defaults

    def get_form(self, data=None):
        return CompleteForm(data, defaults=self.defaults())

    def get_lead(self):
        lead = _("The buildings and metering points are created and assigned to the customer from the start date.")
        defaults = self.defaults()
        if defaults is not None:
            if defaults.start_choice == Application.StartChoice.ASAP:
                lead += " " + _("The customer wished the start as soon as possible.")
            elif defaults.desired_start_date is not None:
                lead += " " + _("The customer wished the start on %(date)s.") % {
                    "date": format_date(defaults.desired_start_date)
                }
        return lead

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
    leaves_page = True
    success_message = gettext_lazy("The application was rolled back.")

    def get_lead(self):
        return _("The application is rolled back and cannot be restored.")

    def run(self, application, data, actor):
        application_staff.reject(application, actor, data["reason"], data.get("note", ""))


# ---- The staff's corrections of an application in flight (D-109) ----


def add_validation_errors(form, error, names=None):
    """Show the messages of a service ``ValidationError`` at the fields they name (``names`` maps a model field to the
    field of the form); a message without a field of the form is shown for the whole form."""
    names = names or {}
    if hasattr(error, "error_dict"):
        for key, errors in error.error_dict.items():
            name = names.get(key, key)
            form.add_error(name if name in form.fields else None, errors)
    else:
        form.add_error(None, error)


class CorrectionDialog(ProcessDialog):
    """A dialog that corrects one row (a building or a metering point) of an application in flight. It applies while the
    LEG works on the application (``Application.CORRECTABLE_STATES``); the service checks the state, the rules and the
    token of the row again under the lock, so a stale page or a forged POST changes nothing that the rules refuse. The
    row is found through the application of the address (a foreign id is a 404)."""

    states = Application.CORRECTABLE_STATES
    submit_label = gettext_lazy("Save")
    success_message = gettext_lazy("Saved.")

    def row(self):
        raise NotImplementedError

    def row_refusal(self, request):
        """A response if the dialog does not apply to this row, else None."""
        return None

    def refusal(self, request, *args, **kwargs):
        """The hook of the access mixin (after the access check, before the handler): the state of the application is
        refused by ``ProcessDialog`` with its message; a row that this dialog does not apply to, here."""
        if not self.applicable():
            return None
        return self.row_refusal(request)


class ApplicationBuildingView(CorrectionDialog):
    """"Edit address" of a new building of the application: checked like the customer's entry (the address directory,
    the EGID, duplicates). A stored building has its address on the page of the location. An address that the directory
    does not know is shown with the directory's suggestion and kept as entered (without an EGID) only after a second,
    explicit save."""

    title = gettext_lazy("Edit address")
    form_class = ApplicationBuildingForm
    template_name = "management/processes/dialog_building.html"

    def row(self):
        if not hasattr(self, "_row"):
            self._row = get_object_or_404(
                ApplicationBuilding, pk=self.kwargs["building_id"], application=self.application()
            )
        return self._row

    def row_refusal(self, request):
        if self.row().location_id is not None:
            messages.error(request, error_texts()["building_stored"])
            return redirect(self.back(request))
        return None

    def get_lead(self):
        return _(
            "The address that the customer entered. It is checked against the address directory like the customer's "
            "entry. The customer sees the corrected address."
        )

    def get_form(self, data=None):
        row = self.row()
        initial = {
            "street": row.street, "house_number": row.house_number, "postal_code": row.postal_code, "town": row.town,
            "egid": row.egid or "", "expected": application_corrections.building_token(row),
        }
        return ApplicationBuildingForm(data, initial=initial)

    def save(self, request, form):
        data = form.cleaned_data
        try:
            application_corrections.correct_building(
                self.application(), request.user, self.row().pk, expected=data["expected"], egid=data["egid"] or None,
                allow_unlisted=form.allow_unlisted, **form.address(),
            )
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            if error.code == "building_not_found":
                return self.ask_unlisted(request, form)
            if error.code == "ambiguous":
                form.add_error("egid", _(
                    "Several buildings have this address. Enter the EGID of one of them: %(egids)s."
                ) % {"egids": ", ".join(str(egid) for egid in error.details["egids"])})
                return None
            form.add_error(None, refusal_text(error))
            return None
        except ValidationError as error:
            add_validation_errors(form, error)
            return None
        messages.success(request, self.success_message)
        return redirect(self.back(request))

    def ask_unlisted(self, request, form):
        """The directory does not know the address: show the directory's suggestion (or the notice) and let the next save
        keep the address as entered, without an EGID."""
        check = addressing.check_address(*form.address().values())
        detail = (
            addressing.suggestion_banner_text(check.suggestion) if check.suggestion is not None else check.not_found_notice
        )
        data = request.POST.copy()
        data["confirm_unlisted"] = "1"
        return self.render_dialog(
            request, ApplicationBuildingForm(data), submit_label=_("Save as entered"), unlisted=True,
            unlisted_detail=detail,
        )


class ApplicationPointView(CorrectionDialog):
    """"Edit metering point": the number, the direction and the plant details of a metering point of the application,
    checked like the customer's entry and what the submission requires (``application_corrections``)."""

    title = gettext_lazy("Edit metering point")
    form_class = ApplicationPointForm

    FIELD_OF_CODE = {
        "number_in_application": "number_digits", "number_own": "number_digits", "number_taken": "number_digits",
        "prefix_not_configured": "number_digits", "direction_taken": "direction", "direction_invalid": "direction",
    }
    FIELD_OF_MODEL = {"number": "number_digits"}

    def row(self):
        if not hasattr(self, "_row"):
            self._row = get_object_or_404(
                ApplicationMeteringPoint, pk=self.kwargs["point_id"], building__application=self.application()
            )
        return self._row

    def get_lead(self):
        lead = _(
            "The metering point that the customer entered. The number, the direction and the plant details are checked "
            "like the customer's entry. The customer sees the corrected values."
        )
        row = self.row()
        if row.building.metering_points.exclude(pk=row.pk).exists():
            lead += " " + _(
                "If the consumption and the feed-in were entered the wrong way round, choose the direction of the other "
                "metering point: the two exchange their directions."
            )
        head = row.number[: numbers.HEAD_LENGTH]
        if head != ManagementSettings.load().metering_point_prefix:
            lead += " " + _(
                "The number starts with %(prefix)s, not with the country and the identifier of the settings. It "
                "stays so unless you change the number; a changed number starts with the ones of the settings."
            ) % {"prefix": head}
        return lead

    def get_form(self, data=None):
        row = self.row()
        initial = {
            "number_digits": row.number[numbers.HEAD_LENGTH :], "direction": row.direction,
            "pv_power_kwp": row.pv_power_kwp, "battery_kwh": row.battery_kwh, "wallbox_kw": row.wallbox_kw,
            "expected": application_corrections.point_token(row),
        }
        return ApplicationPointForm(data, initial=initial)

    def save(self, request, form):
        data = form.cleaned_data
        try:
            result = application_corrections.correct_metering_point(
                self.application(), request.user, self.row().pk, entered_number=data["number_digits"],
                direction=data["direction"], pv_power_kwp=data.get("pv_power_kwp"),
                battery_kwh=data.get("battery_kwh"), wallbox_kw=data.get("wallbox_kw"), expected=data["expected"],
            )
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            if error.code == "prefix_not_configured":
                form.identifier_missing = True
            form.add_error(self.FIELD_OF_CODE.get(error.code), refusal_text(error))
            return None
        except ValidationError as error:
            add_validation_errors(form, error, self.FIELD_OF_MODEL)
            return None
        for field, _kind in result.warnings:
            messages.warning(request, _("The figure %(name)s is unusually large. Please check it.") % {
                "name": ApplicationPointForm.base_fields[field].label
            })
        messages.success(request, self.success_message)
        return redirect(self.back(request))


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
        return ConfirmDeregistrationForm(data, defaults=self.defaults())

    def defaults(self):
        """The defaults of the service (the customer's wish, the earliest date); None if the state moved meanwhile (the
        service then refuses the step with its own message)."""
        if not hasattr(self, "_defaults"):
            try:
                self._defaults = deregistrations.confirmation_defaults(self.deregistration(), self.request.user)
            except ApplicationError as error:
                if error.code == "not_found":
                    raise Http404 from None
                self._defaults = None
        return self._defaults

    def get_lead(self):
        deregistration = self.deregistration()
        date = format_date(deregistration.desired_date)
        if deregistration.wants_earliest:
            lead = _("The customer asked for the earliest possible date (%(date)s).") % {"date": date}
        else:
            lead = _("The customer asked for %(date)s.") % {"date": date}
        lead += " " + _(
            "The date is recorded as the termination date of every metering point of the deregistration."
        )
        defaults = self.defaults()
        if defaults is not None and defaults.suggested_date != defaults.desired_date:
            lead += " " + _("The wished date has passed: the end of this month is suggested.")
        return lead

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
