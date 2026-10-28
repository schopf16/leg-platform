"""Single details of a SUBMITTED application, edited in place from the page Buildings (owner decision 2026-10-28).

As long as the LEG has not started the review (state ``submitted``) the customer changes the address of a building, a
metering point number, the start date or the IBAN on a small page of its own. The forms are the wizard's, the rules are
the services' (``applications.edit_submitted_*``: the same validation as the wizard, the row lock shared with the staff's
``start_review``); the application stays ``submitted``. The server decides: a page of an application in another state, of
another account or of a fixed building never shows a form (a refusal text and the page Buildings, or a 404), and every
POST counts against the request budget (``customer.limits``). The overview offers the links only while editing is
allowed (``customer.overview``); they are a convenience, never the check.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View

from leg_platform.community import applications, buildings, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import ApplicationMeteringPoint, Direction, ManagementSettings
from leg_platform.community.persons import PersonError
from leg_platform.customer import forms, limits, texts
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.customer.wizard import (
    BuildingStepView,
    _figure,
    _number_message,
    _typed_part,
    add_service_error,
    too_many_requests,
)

BUILDINGS = "customer:buildings"
# Refusals that end the editing: shown as a message on the page Buildings.
LEAVE_CODES = ("review_started", "wrong_state", "not_editable", "building_fixed", "step_not_available", "contract_missing")


def refusal_text(application):
    return texts.message("review_started" if application.state == application.State.IN_REVIEW else "wrong_state")


def load_application(request, pk):
    """``(application, redirect)``: the application through the account (404 for another's); a redirect to the page
    Buildings with the refusal text unless the application is still ``submitted``."""
    application = applications.own_application(request.user, pk)
    if not applications.can_edit_submitted(application):
        messages.error(request, refusal_text(application))
        return None, redirect(BUILDINGS)
    return application, None


def leave(request, error):
    """A refusal of the service that is not a field error: 404 for a foreign or missing object, else a message and the
    page Buildings."""
    if error.code in ("not_found", "not_allowed"):
        raise Http404 from None
    messages.error(request, texts.message(error.code))
    return redirect(BUILDINGS)


def saved(request):
    messages.success(request, _("Saved."))
    return redirect(BUILDINGS)


class EditView(CustomerAccessMixin, View):
    """Base of the edit pages: the application through the account, the request budget of a POST."""

    template_name = None

    def render_page(self, request, application, status=200, **context):
        return render(request, self.template_name, {"application": application, **context}, status=status)

    def post(self, request, pk, **kwargs):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application, redirected = load_application(request, pk)
        if redirected is not None:
            return redirected
        return self.submit(request, application, **kwargs)

    def get(self, request, pk, **kwargs):
        application, redirected = load_application(request, pk)
        if redirected is not None:
            return redirected
        return self.show(request, application, **kwargs)


# ---- The start date ----


class StartEditView(EditView):
    template_name = "customer/application_edit_start.html"

    def show(self, request, application, form=None, status=200):
        earliest = applications.earliest_start_date()
        if form is None:
            initial = {"start": application.start_choice or forms.ASAP, "desired_date": application.desired_start_date}
            if not initial["desired_date"]:
                initial["desired_date"] = earliest
            form = forms.StartForm(initial=initial)
        form.fields["desired_date"].widget.attrs["min"] = earliest.isoformat()
        form.fields["desired_date"].help_text = _("Earliest possible: %(date)s.") % {
            "date": earliest.strftime("%d.%m.%Y")
        }
        return self.render_page(request, application, status=status, form=form, earliest=earliest)

    def submit(self, request, application):
        form = forms.StartForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, form=form)
        data = form.cleaned_data
        try:
            applications.edit_submitted_start(
                request.user, application, asap=data["start"] == forms.ASAP, desired_date=data["desired_date"]
            )
        except ApplicationError as error:
            if error.code != "start_invalid":
                return leave(request, error)
            earliest = error.details.get("earliest")
            if earliest is not None:
                form.add_error("desired_date", _("Choose a day on or after %(date)s.") % {
                    "date": earliest.strftime("%d.%m.%Y")
                })
            else:
                form.add_error(None, texts.message(error.code))
            return self.show(request, application, form=form)
        return saved(request)


# ---- The IBAN ----


class BankEditView(EditView):
    template_name = "customer/application_edit_bank.html"

    @staticmethod
    def has_feed_in(application):
        return ApplicationMeteringPoint.objects.filter(
            building__application=application, direction=Direction.FEED_IN
        ).exists()

    def show(self, request, application, form=None):
        if not self.has_feed_in(application):
            messages.error(request, texts.message("step_not_available"))
            return redirect(BUILDINGS)
        form = form or forms.BankForm(initial={"iban": own_iban.own_iban_display(request.user)})
        return self.render_page(request, application, form=form)

    def submit(self, request, application):
        form = forms.BankForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, form=form)
        try:
            applications.edit_submitted_bank_connection(request.user, application, form.cleaned_data["iban"])
        except ValidationError:
            form.add_error("iban", texts.IBAN_INVALID)
            return self.show(request, application, form=form)
        except PersonError as error:
            if error.code in ("not_a_person", "not_active", "not_allowed"):
                raise Http404 from None
            form.add_error("iban", texts.message(error.code))
            return self.show(request, application, form=form)
        except ApplicationError as error:
            return leave(request, error)
        return saved(request)


# ---- A metering point ----


class PointEditView(EditView):
    """The number (and name; for a feed-in also the figures) of one metering point of the application, with the form of
    the wizard step restricted to this point."""

    template_name = "customer/application_edit_point.html"
    FIGURES = ("pv_power_kwp", "battery_kwh", "wallbox_kw")

    def point(self, application, point_id):
        point = ApplicationMeteringPoint.objects.filter(pk=point_id, building__application=application).first()
        if point is None:
            raise Http404
        return point

    @staticmethod
    def make_form(point, data=None, initial=None):
        feed_in = point.direction == Direction.FEED_IN
        return forms.MeteringPointsForm(
            data, initial=initial, prefix_text=ManagementSettings.load().metering_point_prefix,
            draw_needed=not feed_in, feed_in_offered=feed_in, feed_in_required=feed_in,
        )

    def show(self, request, application, point_id, form=None):
        point = self.point(application, point_id)
        if form is None:
            prefix = "feed_in" if point.direction == Direction.FEED_IN else "draw"
            initial = {f"{prefix}_number": _typed_part(point.number), f"{prefix}_name": point.name}
            if prefix == "feed_in":
                initial.update({name: _figure(getattr(point, name)) for name in self.FIGURES})
            form = self.make_form(point, initial=initial)
        settings_row = ManagementSettings.load()
        return self.render_page(
            request, application, form=form, point=point, prefix=settings_row.metering_point_prefix,
            is_feed_in=point.direction == Direction.FEED_IN, numbers_ready=bool(settings_row.identifier),
        )

    def submit(self, request, application, point_id):
        point = self.point(application, point_id)
        form = self.make_form(point, data=request.POST)
        if not ManagementSettings.load().identifier:
            messages.error(request, texts.message("prefix_not_configured"))
            return self.show(request, application, point_id, form=form)
        if not form.is_valid():
            return self.show(request, application, point_id, form=form)
        data = form.cleaned_data
        feed_in = point.direction == Direction.FEED_IN
        prefix = "feed_in" if feed_in else "draw"
        figures = {name: data.get(name) for name in self.FIGURES} if feed_in else {}
        try:
            applications.edit_submitted_metering_point(
                request.user, application, point.pk, entered_number=data[f"{prefix}_number"],
                name=data[f"{prefix}_name"], **figures,
            )
        except ValidationError as error:
            form.add_error(f"{prefix}_number", _number_message(error))
            return self.show(request, application, point_id, form=form)
        except ApplicationError as error:
            if error.code in ("number_in_application", "number_own", "number_unavailable"):
                form.add_error(f"{prefix}_number", texts.message(error.code))
                return self.show(request, application, point_id, form=form)
            return leave(request, error)
        for field, _kind in applications.plant_warnings(**figures):
            messages.warning(request, _("The figure %(name)s is unusually large. Please check it.") % {
                "name": forms.MeteringPointsForm.base_fields[field].label
            })
        return saved(request)


# ---- The address of a building ----


class AddressEditView(BuildingStepView, EditView):
    """The address of one building of the application: the form, the suggestions and the directory choice of step 1
    (``BuildingStepView.process``), saved through ``applications.edit_submitted_building``."""

    template_name = "customer/application_edit_address.html"

    def load_building(self, request, pk, building_id):
        """``(application, building, redirect)``."""
        application, redirected = load_application(request, pk)
        if redirected is not None:
            return None, None, redirected
        building = application.buildings.select_related("location").filter(pk=building_id).first()
        if building is None:
            raise Http404
        if building.preselected:
            messages.error(request, texts.message("building_fixed"))
            return None, None, redirect(BUILDINGS)
        return application, building, None

    def get(self, request, pk, building_id):
        application, building, redirected = self.load_building(request, pk, building_id)
        if redirected is not None:
            return redirected
        return self.show(request, application, building)

    def post(self, request, pk, building_id):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application, building, redirected = self.load_building(request, pk, building_id)
        if redirected is not None:
            return redirected
        self.building = building
        return self.process(request, application, building)

    def show(self, request, application, current, form=None, **extra):
        # ``current`` is the building row being edited.
        if form is None:
            street, house_number, postal_code, town = current.current_address()
            form = forms.BuildingForm(initial={
                "name": current.name, "street": street, "house_number": house_number, "postal_code": postal_code,
                "town": town,
            })
        return self.render_page(request, application, form=form, building=current, **extra)

    def store(self, person, application, address, egid, data):
        return applications.edit_submitted_building(
            person, application, self.building.pk, street=address[0], house_number=address[1],
            postal_code=address[2], town=address[3], egid=egid, name=data["name"],
        )

    def after_choice(self, request, application, choice):
        return saved(request)

    def refused(self, request, application, current, form, typed, error):
        if error.code in LEAVE_CODES:
            return leave(request, error)
        if error.code == "already_added":
            form.add_error(None, texts.message("already_added"))
            return self.show(request, application, current, form=form)
        return super().refused(request, application, current, form, typed, error)
