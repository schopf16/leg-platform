"""The registration wizard and its neighbouring pages (SCHRITT-TEMPLATE, EINSTIEG, ABBRECHEN, ANMELDUNG-ABGESCHICKT; D-103).

**The server holds the state** (``community.applications``: the draft rows, ``progress``, ``wizard_step``); there is no
browser storage. Every step is a normal form POST that works without a script; a script only helps (gates a button,
shows or hides a part of the form). The pages call the services and never re-implement a rule: what the services
refuse is shown with the texts of ``customer.texts``.

Every application is read through the logged-in account (``applications.own_application``); the id of another
account's application is a 404, the same answer as for a missing one. A step page that is not available (a later step, a
step that does not apply) redirects to the page that resumes the application. Every POST counts against the
account's request budget (``customer.limits``); the building search against a second one.
"""

import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.connection import ConnectionDoesNotExist
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy
from django.views import View

from leg_platform.addresses import plant_lookup
from leg_platform.community import applications, buildings, contracts, grid_operator, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Application, Direction, ManagementSettings
from leg_platform.community.persons import PersonError
from leg_platform.customer import forms, limits, returns, texts
from leg_platform.customer import steps as steps_module
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.customer.steps import STEP_NAMES

logger = logging.getLogger(__name__)

SUBMITTED_SESSION_KEY = "customer_submitted_application"
# The id of the application whose building was just chosen: the metering points page says so once (D-124).
BUILDING_NOTICE_SESSION_KEY = "customer_building_notice"

STEP_URLS = {
    applications.BUILDING: "customer:application_building",
    applications.METERING_POINTS: "customer:application_metering_points",
    applications.BANK_CONNECTION: "customer:application_bank_connection",
    applications.CONTRACT: "customer:application_contract",
    applications.START: "customer:application_start_date",
}
# One sentence per step on the page "Start application" (the owner's words from the mockup).
STEP_SENTENCES = {
    applications.BUILDING: _lazy("The address where the metering point is located."),
    applications.METERING_POINTS: _lazy(
        "You enter the metering point for the draw. If you also feed in electricity, you add the metering point "
        "for the feed-in."
    ),
    applications.BANK_CONNECTION: _lazy("This is how we pay you the compensation for the electricity you feed in."),
    applications.CONTRACT: _lazy("You have to agree to the articles of association."),
    applications.START: _lazy("You choose when your participation starts."),
}
# The sentence of the metering point row when a metering point is added and only the feed-in is missing (W2-06).
FEED_IN_ADD_SENTENCE = _lazy("You enter the feed-in metering point.")
NEXT_STEP_NAMES = {
    applications.BUILDING: _lazy("Enter building"),
    applications.METERING_POINTS: _lazy("Enter metering points"),
    applications.BANK_CONNECTION: _lazy("Enter bank or postal connection"),
    applications.CONTRACT: _lazy("Agree to the contract"),
    applications.START: _lazy("Choose the start date"),
}

# Number group in the prefix field: the typed part of a number starts after country and identifier.
PREFIX_LENGTH = 13


def step_url(application, key):
    return reverse(STEP_URLS[key], args=[application.pk])


def next_url(application):
    """The page of the next open step of the application (the last step when everything is complete)."""
    current = applications.progress(application)
    return step_url(application, current.next_step or applications.START)


def too_many_requests(request):
    """The refusal of a POST beyond the request budget: neutral, with the time to wait."""
    response = render(request, "customer/too_many.html", status=429)
    response["Retry-After"] = str(limits.RETRY_AFTER)
    return response


def add_service_error(form, error, field_map=None, default_field=None):
    """Put the ``ValidationError`` of a service (an address, a number, a figure) into the form."""
    field_map = field_map or {}
    if hasattr(error, "error_dict"):
        for key, errors in error.error_dict.items():
            target = field_map.get(key, key)
            form.add_error(target if target in form.fields else default_field, errors)
    else:
        form.add_error(default_field, error)


def operator_name():
    return grid_operator.name()


# ---- The start of an application ("Anmeldung starten") ----


class OverviewRow:
    """One of the five rows of "What do we still need to know?"."""

    def __init__(self, key, number, name, text, state, tag=False, change_url="", view_only=False):
        self.key, self.number, self.name, self.text = key, number, name, text
        self.state, self.tag, self.change_url, self.view_only = state, tag, change_url, view_only

    @property
    def done(self):
        return self.state in (applications.DONE, applications.KNOWN)


def _preselected_location(request, source):
    """The own building named by ``?building=<id>`` / the hidden field, or None. Any other id is a 404: the id is
    resolved through the account's current assignments (``buildings.own_building``: a former holder gets a 404 too)."""
    raw = source.get("building")
    if not raw:
        return None
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        raise Http404 from None
    try:
        location = buildings.own_building(request.user, pk)
    except ApplicationError:
        raise Http404 from None
    if location.is_archived:
        raise Http404
    return location


def _address_text(address):
    """"Street 1, 3000 Town" from the four parts of an address."""
    street, number, postal_code, town = address
    return ", ".join(part for part in (f"{street} {number}".strip(), f"{postal_code} {town}".strip()) if part)


def _feed_in_add_case(person, location):
    """True where the account has a draw at the building and only a feed-in can be added (W2-06)."""
    if location is None:
        return False
    taken = buildings.occupied_directions(location, person, timezone.localdate())
    return Direction.CONSUMPTION in taken and Direction.FEED_IN not in taken


def overview_rows(person, application=None, location=None):
    """The five rows with their state: already on file (green), finished, or open.

    Without a draft: the building is green only when the application starts from "add a metering point" (``location``),
    the bank connection when an IBAN is stored, the contract when the current version was accepted; the other two are
    never green. With a draft: the state of the stored rows (``applications.progress``). A green or finished row has
    "View" (the building and the contract) or "Change" (the bank connection, the rows of a draft)."""
    feed_in_add = False
    if application is None:
        feed_in_add = _feed_in_add_case(person, location)
        states = {
            applications.BUILDING: applications.KNOWN if location is not None else applications.OPEN,
            applications.METERING_POINTS: applications.OPEN,
            applications.BANK_CONNECTION: applications.KNOWN if own_iban.has_iban(person) else applications.OPEN,
            applications.CONTRACT: applications.KNOWN if contracts.has_accepted_current(person) else applications.OPEN,
            applications.START: applications.OPEN,
        }
        change = {
            applications.BUILDING: reverse("customer:buildings"),
            applications.BANK_CONNECTION: returns.bank_change_url(ApplicationStartView.page_url(location)),
            applications.CONTRACT: reverse("customer:contract"),
        }
        shown = list(applications.STEPS)
        address = _address_text(location.current_address()) if location is not None else ""
        optional_bank = True
    else:
        current = applications.progress(application)
        states = {step.key: step.state for step in current.steps}
        change = {
            key: step_url(application, key) for key in applications.STEPS if current.step(key).available
        }
        if states[applications.BANK_CONNECTION] == applications.KNOWN:
            change[applications.BANK_CONNECTION] = returns.bank_change_url(ApplicationStartView.page_url(None))
        # The bank row stays while it is not known whether there is a feed-in (README 2a: not shown without one).
        points_known = states[applications.METERING_POINTS] != applications.OPEN
        optional_bank = not current.step(applications.BANK_CONNECTION).applicable
        shown = [key for key in applications.STEPS if key != applications.BANK_CONNECTION or not (optional_bank and points_known)]
        first = application.buildings.select_related("location").first()
        address = _address_text(first.current_address()) if first is not None else ""
        feed_in_add = first is not None and _feed_in_add_case(person, first.location)
    rows = []
    number = 0
    for key in applications.STEPS:
        if key not in shown:
            continue
        state = states[key]
        tag = key == applications.BANK_CONNECTION and optional_bank
        # The optional bank row ("only with feed-in") has no number: the numbers and the count are the steps that apply (N-08).
        if not tag:
            number += 1
        text = STEP_SENTENCES[key]
        if key == applications.BUILDING and state != applications.OPEN and address:
            text = address
        elif key == applications.METERING_POINTS and feed_in_add:
            text = FEED_IN_ADD_SENTENCE
        rows.append(OverviewRow(
            key, None if tag else number, STEP_NAMES[key], text, state, tag=tag,
            change_url=change.get(key, "") if state != applications.OPEN else "",
            view_only=application is None and key in (applications.BUILDING, applications.CONTRACT),
        ))
    return rows


class ApplicationStartView(CustomerAccessMixin, View):
    """"Start application": what is still needed (green = already on file), or the draft to continue."""

    template_name = "customer/application_start.html"

    def drafts(self, request):
        return list(Application.objects.filter(person=request.user, state__in=Application.DRAFT_STATES).order_by("-pk"))

    def get(self, request):
        location = _preselected_location(request, request.GET)
        drafts = self.drafts(request)
        context = {"location": location, "operator": operator_name()}
        if drafts:
            draft = drafts[0]
            rows = overview_rows(request.user, draft)
            current = applications.progress(draft)
            paused = draft.state == Application.State.PAUSED
            context.update(
                drafts=drafts, draft=draft, rows=rows, progress=current,
                remaining=current.total - current.done, paused=paused,
                paused_date=applications.rollback_date(draft) if paused else None,
                reason=applications.returned_reason(draft), returned=draft.returned_at is not None,
                checking=draft.returned_at is not None and current.next_step is None,
                next_url=next_url(draft),
                next_name=NEXT_STEP_NAMES[current.next_step] if current.next_step else _("Submit application"),
            )
        else:
            rows = overview_rows(request.user, None, location)
            # The count is the wizard's: the optional bank row is not a step of it (W1-01).
            counted = [row for row in rows if not row.tag]
            context.update(rows=rows, done=sum(row.done for row in counted), total=len(counted))
        return render(request, self.template_name, context)

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        location = _preselected_location(request, request.POST)
        if self.drafts(request):
            # The page offers "continue" or "start again" (which discards the draft); never a second draft.
            return redirect(self.page_url(location))
        if not request.POST.get("overview") and self.something_on_file(request.user, location):
            # Something is on file (a bank connection, the contract, the building): the overview shows what is taken
            # over (W1-05). Without anything on file the overview would only repeat the steps: straight to step 1.
            return redirect(self.page_url(location))
        try:
            application = applications.start_draft(request.user, location=location)
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            messages.error(request, texts.message(error.code))
            return redirect(self.page_url(location))
        return redirect(next_url(application))

    @staticmethod
    def something_on_file(person, location):
        """True where a step is "already on file" (the overview marks it green): a preselected building, the bank
        connection or the accepted contract."""
        return location is not None or own_iban.has_iban(person) or contracts.has_accepted_current(person)

    @staticmethod
    def page_url(location):
        url = reverse("customer:application_start")
        return f"{url}?building={location.pk}" if location is not None else url


# ---- The steps ----


class ApplicationResumeView(CustomerAccessMixin, View):
    """Leads on to the next open step of the application (a draft); a submitted one is on the page Buildings."""

    def get(self, request, pk):
        application = applications.own_application(request.user, pk)
        if application.state not in Application.DRAFT_STATES:
            return redirect("customer:buildings")
        return redirect(next_url(application))


class StepView(CustomerAccessMixin, View):
    """Base of the five step pages: the access check of the mixin, the application read through the account, the step
    guard, the request budget of a POST and the shared context of the pages."""

    step = None
    template_name = None

    def load(self, request, pk):
        """``(application, progress)`` or a redirect when the step page is not available."""
        application = applications.own_application(request.user, pk)
        if application.state not in Application.DRAFT_STATES:
            return None, None, redirect("customer:buildings")
        current = applications.progress(application)
        entry = current.step(self.step)
        if not entry.applicable or not entry.available:
            return None, None, redirect("customer:application", pk=application.pk)
        return application, current, None

    def base_context(self, request, application, current, **extra):
        applicable = [step.key for step in current.steps if step.applicable]
        return {
            "application": application,
            "step": self.step,
            "step_number": applicable.index(self.step) + 1,
            "step_total": len(applicable),
            "step_name": STEP_NAMES[self.step],
            "paused": application.state == Application.State.PAUSED,
            "paused_date": applications.rollback_date(application) if application.state == Application.State.PAUSED else None,
            "returned": application.returned_at is not None,
            "returned_reason": applications.returned_reason(application),
            "has_iban": own_iban.has_iban(request.user),
            "back_url": self.back_url(application, current),
            "confirm_cancel": request.GET.get("cancel") == "1",
            "operator": operator_name(),
            **extra,
        }

    def back_url(self, application, current):
        """The page before this step (an applicable step that can be opened), else the overview with every step (N-01)."""
        before = [step for step in current.steps if step.applicable and step.available]
        keys = [step.key for step in before]
        if self.step in keys and keys.index(self.step) > 0:
            return step_url(application, keys[keys.index(self.step) - 1])
        return reverse("customer:application_start")

    def get(self, request, pk):
        application, current, redirected = self.load(request, pk)
        if redirected is not None:
            return redirected
        return self.show(request, application, current)

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application, current, redirected = self.load(request, pk)
        if redirected is not None:
            return redirected
        return self.submit(request, application, current)

    def render_step(self, request, application, current, status=200, **extra):
        context = self.base_context(request, application, current, **extra)
        return render(request, self.template_name, context, status=status)

    def failed(self, request, error):
        """A refusal that is not tied to one field: shown on top of the page."""
        messages.error(request, texts.message(error.code))


# Step 1 ----------------------------------------------------------------------------------------------------------------


def _candidate_label(candidate):
    street = buildings.candidate_value(candidate, "street")
    house = buildings.candidate_value(candidate, "house_number")
    postal = buildings.candidate_value(candidate, "postal_code")
    town = buildings.candidate_value(candidate, "locality") or buildings.candidate_value(candidate, "town")
    return f"{street} {house}, {postal} {town}".strip()


def _candidate_rows(candidates):
    return [
        {
            "label": _candidate_label(candidate),
            "egid": buildings.candidate_value(candidate, "egid"),
            "entrance": buildings.candidate_value(candidate, "entrance_id", None),
            "index": index,
        }
        for index, candidate in enumerate(candidates)
    ]


class BuildingStepView(StepView):
    """Step 1: where is the building. States: input, found, already created, several buildings, not clearly found."""

    step = applications.BUILDING
    template_name = "customer/step_building.html"

    def chosen(self, application):
        return application.buildings.select_related("location").first()

    def show(self, request, application, current, form=None, **extra):
        return self.render_step(
            request, application, current, form=form or forms.BuildingForm(initial=self.account_address(request.user)),
            chosen=self.chosen(application), **extra
        )

    @staticmethod
    def account_address(user):
        """The address of the account's profile as the start of the form (W1-06): editable, nothing is skipped. A foreign
        address (no Swiss postal code) is not offered: the building is in Switzerland."""
        if user.swiss_postal_code is None:
            return {}
        return {
            "street": user.street, "house_number": user.house_number,
            "postal_code": user.postal_code_display, "town": user.town,
        }

    def submit(self, request, application, current):
        person = request.user
        chosen = self.chosen(application)
        if request.POST.get("action") == "change":
            if chosen is not None:
                try:
                    applications.remove_building(person, application, chosen.pk)
                except ApplicationError as error:
                    if error.code in ("not_found", "not_allowed"):
                        raise Http404 from None
                    self.failed(request, error)
            return redirect("customer:application_building", pk=application.pk)
        if chosen is not None:
            return redirect("customer:application", pk=application.pk)
        return self.process(request, application, current)

    def store(self, person, application, address, egid, data):
        """Write the building through the service (the hook of the address change of a submitted application)."""
        return applications.choose_building(
            person, application, street=address[0], house_number=address[1], postal_code=address[2], town=address[3],
            egid=egid, name=data["name"],
        )

    def process(self, request, application, current):
        """The address form: validation, the suggestions, the directory choice, the service call."""
        person = request.user
        form = forms.BuildingForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, current, form=form)
        if not limits.allow_search(person):
            return too_many_requests(request)
        data = form.cleaned_data
        typed = form.typed()
        egid = data["egid"] or None
        if data["suggestion"] != "":
            # A suggestion the person confirmed: recomputed here (local directory only), never taken from the browser.
            candidates = buildings.suggest_candidates(*typed)
            try:
                candidate = candidates[int(data["suggestion"])]
            except (ValueError, IndexError):
                form.add_error(None, texts.NOT_CLEAR)
                return self.show(request, application, current, form=form, not_clear=True)
            typed = (
                buildings.candidate_value(candidate, "street"), buildings.candidate_value(candidate, "house_number"),
                buildings.candidate_value(candidate, "postal_code"),
                buildings.candidate_value(candidate, "locality") or buildings.candidate_value(candidate, "town"),
            )
            egid = buildings.candidate_value(candidate, "egid")
        elif not data["confirm_typed"] and not egid:
            suggestions = buildings.suggest_candidates(*typed)
            if suggestions:
                return self.show(
                    request, application, current, form=form, suggestions=_candidate_rows(suggestions)
                )
        try:
            choice = self.store(person, application, typed, egid, data)
        except ValidationError as error:
            add_service_error(form, error)
            return self.show(request, application, current, form=form)
        except ApplicationError as error:
            return self.refused(request, application, current, form, typed, error)
        return self.after_choice(request, application, choice)

    @staticmethod
    def after_choice(request, application, choice):
        """A building was added (found, or kept as typed for the review): straight to the metering points, which say it
        once (D-124). Any other next step shows the result on this page. The session holds only the id of the
        application, never an address or an EGID."""
        if next_url(application) == step_url(application, applications.METERING_POINTS):
            request.session[BUILDING_NOTICE_SESSION_KEY] = application.pk
            return redirect(step_url(application, applications.METERING_POINTS))
        return redirect("customer:application_building", pk=application.pk)

    def refused(self, request, application, current, form, typed, error):
        code = error.code
        if code in ("not_found", "not_allowed"):
            raise Http404 from None
        if code == "already_added":
            return redirect("customer:application_building", pk=application.pk)
        if code == "ambiguous":
            try:
                found = buildings.find_candidates(*typed, user=request.user)
            except ApplicationError:
                found = []
            wanted = set(error.details.get("egids", ()))
            options = [candidate for candidate in found if buildings.candidate_value(candidate, "egid") in wanted]
            return self.show(request, application, current, form=form, options=_candidate_rows(options))
        if code in ("building_not_found", "egid_not_offered", "egid_invalid"):
            return self.show(request, application, current, form=form, not_clear=True)
        self.failed(request, error)
        return self.show(request, application, current, form=form)


# Step 2 ----------------------------------------------------------------------------------------------------------------


class MeteringPointsStepView(StepView):
    """Step 2: the draw (required), optionally the feed-in with its figures."""

    step = applications.METERING_POINTS
    template_name = "customer/step_metering_points.html"

    FIGURES = ("pv_power_kwp", "battery_kwh", "wallbox_kw")

    def building(self, application):
        return application.buildings.select_related("location").first()

    def flags(self, request, building):
        taken = set()
        if building.location is not None:
            taken = buildings.occupied_directions(building.location, request.user, timezone.localdate())
        draw_needed = Direction.CONSUMPTION not in taken
        feed_in_offered = Direction.FEED_IN not in taken
        return {
            "draw_needed": draw_needed, "feed_in_offered": feed_in_offered,
            "feed_in_required": not draw_needed and feed_in_offered,
        }

    def initial(self, building):
        rows = {row.direction: row for row in building.metering_points.all()}
        values = {"feed_in": forms.YES if Direction.FEED_IN in rows else forms.NO}
        draw, feed_in = rows.get(Direction.CONSUMPTION), rows.get(Direction.FEED_IN)
        if draw is not None:
            values.update(draw_number=_typed_part(draw.number), draw_name=draw.name)
        if feed_in is not None:
            values.update(
                feed_in_number=_typed_part(feed_in.number), feed_in_name=feed_in.name,
                **{name: _figure(getattr(feed_in, name)) for name in self.FIGURES},
            )
        if not values.get("pv_power_kwp") and building.egid:
            try:
                registered = plant_lookup.pv_power_kw(building.egid)
            except (DatabaseError, ConnectionDoesNotExist, ValueError):
                # The plant register is an extra: without it the field stays empty (the log holds no EGID).
                logger.warning("The plant register could not be read for the prefill of the photovoltaic power.")
                registered = None
            if registered is not None:
                values["pv_power_kwp"] = _figure(registered.normalize())
        return values

    def show(self, request, application, current, form=None):
        building = self.building(application)
        flags = self.flags(request, building)
        settings_row = ManagementSettings.load()
        form = form or forms.MeteringPointsForm(
            initial=self.initial(building), prefix_text=settings_row.metering_point_prefix, **flags
        )
        # Said once, after the building step (D-124): read from the stored building, so the page never trusts the session.
        # Only a GET takes the flag: the redisplay of a failed POST keeps no note.
        announced = request.method == "GET" and request.session.get(BUILDING_NOTICE_SESSION_KEY) == application.pk
        if announced:
            del request.session[BUILDING_NOTICE_SESSION_KEY]
        return self.render_step(
            request, application, current, form=form, building=building, prefix=settings_row.metering_point_prefix,
            numbers_ready=bool(settings_row.identifier), building_notice=announced, **flags,
        )

    def submit(self, request, application, current):
        building = self.building(application)
        flags = self.flags(request, building)
        form = forms.MeteringPointsForm(request.POST, prefix_text=ManagementSettings.load().metering_point_prefix, **flags)
        if not ManagementSettings.load().identifier:
            messages.error(request, texts.message("prefix_not_configured"))
            return self.show(request, application, current, form=form)
        if not form.is_valid():
            return self.show(request, application, current, form=form)
        if not self.save(request, application, building, form):
            return self.show(request, application, current, form=form)
        return redirect(next_url(application))

    def save(self, request, application, building, form):
        """Write the points through the services; False (errors on the form) if one is refused."""
        data = form.cleaned_data
        existing = {row.direction: row for row in building.metering_points.all()}
        try:
            if form.draw_needed:
                self.upsert(request, application, building, existing, Direction.CONSUMPTION, "draw", data)
            if form.wants_feed_in:
                self.upsert(request, application, building, existing, Direction.FEED_IN, "feed_in", data)
            elif Direction.FEED_IN in existing and form.feed_in_offered:
                applications.remove_metering_point(request.user, application, existing[Direction.FEED_IN].pk)
        except _FieldRefusal as refusal:
            form.add_error(refusal.field, refusal.message)
            return False
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            self.failed(request, error)
            return False
        return True

    def upsert(self, request, application, building, existing, direction, prefix, data):
        person = request.user
        figures = {name: data.get(name) for name in self.FIGURES} if direction == Direction.FEED_IN else {}
        number_field = f"{prefix}_number"
        try:
            if direction in existing:
                applications.replace_metering_point(
                    person, application, existing[direction].pk, entered_number=data[number_field],
                    name=data[f"{prefix}_name"], **figures,
                )
                warnings = applications.plant_warnings(**figures)
            else:
                added = applications.add_metering_point(
                    person, application, building.pk, direction=direction, entered_number=data[number_field],
                    name=data[f"{prefix}_name"], **figures,
                )
                warnings = added.warnings
        except ValidationError as error:
            raise _FieldRefusal(number_field, _number_message(error)) from None
        except ApplicationError as error:
            if error.code in ("number_in_application", "number_own", "number_unavailable"):
                raise _FieldRefusal(number_field, texts.message(error.code)) from None
            raise
        for field, _kind in warnings:
            messages.warning(request, _("The figure %(name)s is unusually large. Please check it.") % {
                "name": forms.MeteringPointsForm.base_fields[field].label
            })


class _FieldRefusal(Exception):
    def __init__(self, field, message):
        super().__init__(field)
        self.field, self.message = field, message


def _figure(value):
    return "" if value is None else format(value, "f")


def _typed_part(number):
    """What the person types for a stored number: the part after country and identifier, without the zeros on the left
    that the rule adds again (``numbers.normalise_entered_number``)."""
    return number[PREFIX_LENGTH:].lstrip("0") or "0"


def _number_message(error):
    """The text for a refused number: the owner's sentence for "too long", the rule's own text otherwise."""
    if hasattr(error, "error_dict"):
        errors = [item for items in error.error_dict.values() for item in items]
    else:
        errors = error.error_list
    if any(item.code == "too_long" for item in errors):
        return texts.NUMBER_TOO_LONG
    return " ".join(message for item in errors for message in item.messages)


# Step 3 ----------------------------------------------------------------------------------------------------------------


class BankStepView(StepView):
    """Step 3: the bank or postal connection (IBAN), only with a feed-in."""

    step = applications.BANK_CONNECTION
    template_name = "customer/step_bank.html"

    def show(self, request, application, current, form=None):
        form = form or forms.BankForm(initial={"iban": own_iban.own_iban_display(request.user)})
        return self.render_step(request, application, current, form=form)

    def submit(self, request, application, current):
        form = forms.BankForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, current, form=form)
        try:
            applications.save_bank_connection(request.user, application, form.cleaned_data["iban"])
        except ValidationError:
            form.add_error("iban", texts.IBAN_INVALID)
            return self.show(request, application, current, form=form)
        except PersonError as error:
            if error.code in ("not_a_person", "not_active", "not_allowed"):
                raise Http404 from None
            form.add_error("iban", texts.message(error.code))
            return self.show(request, application, current, form=form)
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            if error.code == "step_not_available":
                return redirect("customer:application", pk=application.pk)
            self.failed(request, error)
            return self.show(request, application, current, form=form)
        return redirect(next_url(application))


# Step 4 ----------------------------------------------------------------------------------------------------------------


class ContractStepView(StepView):
    """Step 4: read the contract (download) and agree with two ticks."""

    step = applications.CONTRACT
    template_name = "customer/step_contract.html"

    def show(self, request, application, current, form=None):
        version = contracts.current_version()
        acceptance = contracts.acceptance_of(request.user, version)
        form = form or forms.ContractForm(initial={"version": version.pk if version else None})
        return self.render_step(
            request, application, current, form=form, version=version, acceptance=acceptance,
            version_date=version.version_date if version else None,
        )

    def submit(self, request, application, current):
        form = forms.ContractForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, current)
        data = form.cleaned_data
        try:
            applications.accept_contract_step(
                request.user, application, data["version"], agrees_contract=data["agrees_contract"],
            )
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            if error.code == "already_accepted":
                return redirect(next_url(application))
            if error.code == "version_changed":
                # A replaced version: the page shows the new one with fresh boxes.
                messages.error(request, texts.message(error.code))
                return self.show(request, application, current)
            form.add_error(None, texts.message(error.code))
            return self.show(request, application, current, form=form)
        return redirect(next_url(application))


# Step 5 ----------------------------------------------------------------------------------------------------------------


class StartDateStepView(StepView):
    """Step 5: as soon as possible or a desired date; "Submit application" sets it and submits."""

    step = applications.START
    template_name = "customer/step_start_date.html"

    def show(self, request, application, current, form=None):
        initial = {}
        if application.start_choice:
            initial = {"start": application.start_choice, "desired_date": application.desired_start_date}
        earliest = applications.earliest_start_date()
        initial = initial or {"start": forms.ASAP}
        if not initial.get("desired_date"):
            initial["desired_date"] = earliest
        form = form or forms.StartForm(initial=initial)
        # The earliest day for the date picker (a hint of the browser; the service checks it again) and, up front, as the
        # help text under the field (W1-11).
        form.fields["desired_date"].widget.attrs["min"] = earliest.isoformat()
        form.fields["desired_date"].help_text = _("Earliest possible: %(date)s.") % {"date": earliest.strftime("%d.%m.%Y")}
        return self.render_step(request, application, current, form=form, earliest=earliest)

    def submit(self, request, application, current):
        form = forms.StartForm(request.POST)
        if not form.is_valid():
            return self.show(request, application, current, form=form)
        data = form.cleaned_data
        person = request.user
        try:
            applications.set_start(
                person, application, asap=data["start"] == forms.ASAP, desired_date=data["desired_date"]
            )
            applications.submit(person, application)
        except ApplicationError as error:
            return self.refused(request, application, current, form, error)
        request.session[SUBMITTED_SESSION_KEY] = application.pk
        return redirect("customer:application_submitted", pk=application.pk)

    def refused(self, request, application, current, form, error):
        code = error.code
        if code in ("not_found", "not_allowed"):
            raise Http404 from None
        if code == "start_invalid":
            earliest = error.details.get("earliest")
            if earliest is not None:
                form.add_error("desired_date", _("Choose a day on or after %(date)s.") % {
                    "date": earliest.strftime("%d.%m.%Y")
                })
            else:
                form.add_error(None, texts.message(code))
            return self.show(request, application, current, form=form)
        if code == "contract_missing":
            messages.error(request, texts.message(code))
            return redirect("customer:application_contract", pk=application.pk)
        if code == "incomplete":
            messages.error(request, _("Some details are still missing."))
            missing = error.details.get("missing") or [applications.BUILDING]
            return redirect(step_url(application, missing[0]))
        self.failed(request, error)
        return self.show(request, application, current, form=form)


# ---- Cancel, submitted, withdraw ----


class ApplicationCancelView(CustomerAccessMixin, View):
    """"Cancel application": the confirmation is a card on the step page (``?cancel=1``); this view does the rollback.
    A GET only leads to that card. The bank connection and the account stay (ABBRECHEN.md, section 4)."""

    http_method_names = ["get", "head", "post"]

    def get(self, request, pk):
        application = applications.own_application(request.user, pk)
        if application.state not in Application.DRAFT_STATES:
            return redirect("customer:buildings")
        return redirect(f"{next_url(application)}?cancel=1")

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application = applications.own_application(request.user, pk)
        try:
            applications.cancel(request.user, application)
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            messages.error(request, texts.message(error.code))
            return redirect("customer:buildings")
        if request.POST.get("restart") == "1":
            return redirect(ApplicationStartView.page_url(_preselected_or_none(request)))
        messages.success(request, _("Your application was cancelled."))
        return redirect("home")


def _preselected_or_none(request):
    """The own building to start again with (the hidden field of "Start again"); an invalid id only means none."""
    try:
        return _preselected_location(request, request.POST)
    except Http404:
        return None


class ApplicationSubmittedView(CustomerAccessMixin, View):
    """"Your application was submitted": shown once, right after submitting; the progress is then on the page Buildings."""

    def get(self, request, pk):
        application = applications.own_application(request.user, pk)
        if (
            request.session.get(SUBMITTED_SESSION_KEY) != application.pk
            or application.state not in Application.IN_FLIGHT_STATES
        ):
            return redirect("customer:buildings")
        del request.session[SUBMITTED_SESSION_KEY]
        order = steps_module.post_submit_steps(application, operator_name(), named=False)
        building = application.buildings.select_related("location").first()
        points = list(building.metering_points.all()) if building else []
        steps = [{"key": key, "state": state, "name": label, "word": word} for key, state, label, word in order]
        return render(request, "customer/application_submitted.html", {
            "application": application, "steps": steps, "done": sum(s["state"] == "done" for s in steps),
            "total": len(steps), "building": building, "points": points,
            "withdrawable": application.state in Application.WITHDRAWABLE_STATES,
            "changeable": application.state == Application.State.SUBMITTED,
            "days": ManagementSettings.load().operator_confirmation_days, "operator": operator_name(),
        })


class ApplicationChangeView(CustomerAccessMixin, View):
    """"Change application" (D-107): a submitted application that the LEG has not taken into review yet becomes a draft
    again; the customer changes it and submits it again. A confirmation page first; once the LEG started checking, the
    page says why the application can no longer be changed here."""

    def get(self, request, pk):
        application = applications.own_application(request.user, pk)
        if application.state != Application.State.SUBMITTED:
            messages.error(request, self.refusal_text(application))
            return redirect("customer:buildings")
        return render(request, "customer/application_change.html", {"application": application})

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application = applications.own_application(request.user, pk)
        try:
            applications.reopen_for_change(request.user, application)
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            messages.error(request, texts.message("wrong_state" if error.code == "not_editable" else error.code))
            return redirect("customer:buildings")
        messages.success(
            request, _("Your application is open for changes. Change what you need, then submit it again.")
        )
        return redirect("customer:application_start")

    @staticmethod
    def refusal_text(application):
        if application.state == Application.State.IN_REVIEW:
            return texts.message("review_started")
        return texts.message("wrong_state")


class ApplicationWithdrawView(CustomerAccessMixin, View):
    """"Withdraw application": a confirmation page, then the service (back to a draft with all its details)."""

    def get(self, request, pk):
        application = applications.own_application(request.user, pk)
        if application.state not in Application.WITHDRAWABLE_STATES:
            messages.error(request, texts.message("wrong_state"))
            return redirect("customer:buildings")
        return render(request, "customer/application_withdraw.html", {"application": application, "operator": operator_name()})

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        application = applications.own_application(request.user, pk)
        try:
            draft = applications.withdraw(request.user, application)
        except ApplicationError as error:
            if error.code in ("not_found", "not_allowed"):
                raise Http404 from None
            messages.error(request, texts.message("wrong_state" if error.code == "not_editable" else error.code))
            return redirect("customer:buildings")
        messages.success(request, _("Your application is a draft again. You can change it and submit it again."))
        return redirect(next_url(draft))
