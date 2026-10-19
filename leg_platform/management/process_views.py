"""The pages of the processes (owner design ``verwaltung/vorgaenge``; D-103): the list of the running applications and
deregistrations, the page of one application, the page of one deregistration and the notices for the staff.

Every view uses ``management.access`` (every active staff account, D-086). The rules live in ``community``
(``process_list``, ``applications.progress``, ``application_staff``, ``deregistrations``, ``application_notices``); a page
only reads and shows. Drafts and paused drafts are never shown (KONZEPT 5.6): their pages are 404. The page of an
application shows what the customer entered; it never shows the IBAN (only whether one is on file, D-087) and no
proof of a confirmation.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext
from django.views import View

from leg_platform.community import (
    application_notices,
    application_queue,
    applications,
    contracts,
    grid_operator,
    history,
    labels,
    numbers,
    own_iban,
    process_list,
)
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import (
    Application,
    ApplicationNotice,
    Deregistration,
    Direction,
)
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import event_label
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.returns import return_url, with_next

# The applications the staff can open: submitted and later (never a draft).
VISIBLE_APPLICATION_STATES = (*Application.IN_FLIGHT_STATES, Application.State.COMPLETED)

STATE_TONES = {
    Application.State.SUBMITTED: "warn",
    Application.State.IN_REVIEW: "accent",
    Application.State.OPERATOR_STARTED: "accent",
    Application.State.OPERATOR_CONFIRMED: "ring",
    Application.State.COMPLETED: "ok",
    process_list.STATE_DEREGISTRATION_SUBMITTED: "warn",
    process_list.STATE_DEREGISTRATION_CONFIRMED: "accent",
}
STEP_TONES = {applications.DONE: "ok", "next": "accent", applications.OPEN: "muted"}


def state_label(state):
    """The word of a state of a row: an application state, or a deregistration state of the list."""
    if state == process_list.STATE_DEREGISTRATION_SUBMITTED:
        return Deregistration.State.SUBMITTED.label
    if state == process_list.STATE_DEREGISTRATION_CONFIRMED:
        return Deregistration.State.CONFIRMED.label
    return labels.application_state_label(state)


def state_tone(state):
    return STATE_TONES[state]


def kind_label(kind):
    return _("Application") if kind == process_list.KIND_APPLICATION else _("Deregistration")


def person_card(person):
    """What the staff sees of the person next to a process (names, number, addresses; D-086; never an IBAN)."""
    name = f"{person.first_name} {person.last_name}".strip()
    return {
        "name": person.organisation_name or name,
        "contact": name if person.organisation_name else "",
        "number": person.customer_number_display,
        "email": person.email,
        "second_email": person.secondary_email or "",
        "url": reverse("management:persons_detail", args=[person.pk]),
    }


def history_rows(subject, person_id):
    """The history of an application or deregistration, newest first: event, time and who (field names and dates only
    are stored, D-077)."""
    rows = []
    for entry in history.entries_for(subject).order_by("-created_at", "-pk"):
        if entry.actor_id is None:
            actor = _("System")
        elif entry.actor_id == person_id:
            actor = _("Customer")
        else:
            actor = entry.actor.display_name
        rows.append({"time": entry.created_at, "label": event_label(entry.event), "actor": actor})
    return rows


# ---- The list ----


def list_columns():
    # Widths for a 1280 px screen (the content is about 1000 px wide): the kind, the count and the date of the last change
    # never shrink (their words must be whole); the others are cut with three dots and show the full text on hover.
    return (
        lists.Column("person", gettext_lazy("Person"), width=170),
        lists.Column("kind", gettext_lazy("Kind"), width=110, keep=True),
        lists.Column("building", gettext_lazy("Building"), width=190),
        lists.Column("points", gettext_lazy("Metering points"), width=112, keep=True),
        lists.Column(
            "state", gettext_lazy("Status"), width=180,
            info=_(
                "Submitted: waiting for the review. In review: the LEG checks it. Then the registration at the grid "
                "operator. In deregistration: the notice was given and the LEG confirms the date."
            ),
        ),
        lists.Column(
            "progress", gettext_lazy("Progress"), width=130,
            info=_("Applications: the five steps from the submission to the completion. Deregistrations: three steps."),
        ),
        lists.Column(
            "date", gettext_lazy("Date"), width=130,
            info=_(
                "Applications: the start date the customer chose. Deregistrations: the date from which the "
                "deregistration is effective, once the LEG has confirmed it. Open until it is known."
            ),
            info_placement="left",
        ),
        lists.Column("changed", gettext_lazy("Last change"), width=130, keep=True, info_placement="left"),
    )


def kind_options():
    return [
        (process_list.KIND_ALL, _("All")),
        (process_list.KIND_APPLICATION, _("Applications")),
        (process_list.KIND_DEREGISTRATION, _("Deregistrations")),
    ]


def state_options():
    return [
        (process_list.STATE_ALL, _("All")),
        (Application.State.SUBMITTED.value, Application.State.SUBMITTED.label),
        (Application.State.IN_REVIEW.value, Application.State.IN_REVIEW.label),
        (process_list.STATE_AT_OPERATOR, _("At the %(operator)s") % {"operator": grid_operator.name()}),
        (process_list.STATE_DEREGISTRATION_SUBMITTED, Deregistration.State.SUBMITTED.label),
        (process_list.STATE_DEREGISTRATION_CONFIRMED, Deregistration.State.CONFIRMED.label),
    ]


def build_list(params):
    """``(state, columns, rows)``: the list as the address asks for it (unknown values are the defaults)."""
    columns = list_columns()
    state = lists.parse(
        params, path=reverse("management:processes"), columns=columns, default_sort=process_list.DEFAULT_SORT,
        filters={
            "kind": (process_list.KINDS, process_list.KIND_ALL),
            "state": (process_list.STATES, process_list.STATE_ALL),
        },
    )
    found = process_list.rows(
        text=state.query, kind=state.values["kind"], state=state.values["state"], sort=state.sort,
        descending=state.descending,
    )
    return state, columns, lists.ListRows(found)


def list_entry(row):
    """What the list shows of one row."""
    if row.kind == process_list.KIND_APPLICATION:
        detail_url = reverse("management:processes_application", args=[row.pk])
        date_label = _("Start date")
    else:
        detail_url = reverse("management:processes_deregistration", args=[row.pk])
        date_label = _("Effective from")
    if row.date is not None:
        date_text = format_date(row.date)
    elif row.start_choice == Application.StartChoice.ASAP:
        date_text = _("As soon as possible")
    else:
        date_text = ""
    return {
        "row": row,
        "kind_label": kind_label(row.kind),
        "state_label": state_label(row.state),
        "state_tone": state_tone(row.state),
        "egid_text": _("EGID %(egid)s") % {"egid": row.egid} if row.egid else "",
        "more_text": _("+ %(count)d more") % {"count": row.more_buildings} if row.more_buildings else "",
        "progress_text": _("%(done)s of %(total)s steps") % {"done": row.progress_done, "total": row.progress_total},
        "date_text": date_text,
        "date_label": date_label,
        "changed_text": format_date(row.changed.astimezone(timezone.get_current_timezone())),
        "detail_url": detail_url,
    }


class ProcessListView(ManagementAccessMixin, View):
    """The list "Processes": applications and deregistrations that are running, one row each."""

    def get(self, request):
        state, columns, found = build_list(request.GET)
        page = lists.paginate(found, state)
        entries = [list_entry(row) for row in page.items]
        notice_count = application_notices.open_notices().count()
        context = lists.list_context(
            request, "processes", columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d processes") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["processes"]),
        )
        context.update(
            page_title=_("Processes"),
            page_subtitle=_("Running applications and deregistrations of the customers."),
            entries=entries,
            kind_options=kind_options(),
            state_options=state_options(),
            notice_count=notice_count,
            notices_url=reverse("management:processes_notices"),
            empty_text=_("No running processes."),
        )
        return render(request, "management/processes/list.html", context)


# ---- One application ----


def step_note(key, application):
    """The date that belongs to a step of the way after the submission (empty if not reached or not known)."""
    zone = timezone.get_current_timezone()
    if key == "submitted":
        return format_date(application.submitted_at.astimezone(zone)) if application.submitted_at else ""
    if key == "operator_registration":
        return format_date(application.operator_started_on) if application.operator_started_on else ""
    if key == "operator_confirmation":
        return format_date(application.operator_confirmed_on) if application.operator_confirmed_on else ""
    if key == "start":
        return format_date(application.completed_at.astimezone(zone)) if application.completed_at else ""
    return ""


def step_words():
    """The words of a step of the way: done, next, open (own context: the same English words mean other things in
    other places, e.g. "Done" at the recovery codes)."""
    return {
        applications.DONE: pgettext("process step", "Done"),
        "next": pgettext("process step", "Next"),
        applications.OPEN: pgettext("process step", "Open"),
    }


def way_steps(application):
    """The five steps of the way (submitted, review, registration, confirmation, completed) with their state."""
    operator = grid_operator.name()
    labels = {
        "submitted": _("Details submitted"),
        "review": _("Review by the LEG"),
        "operator_registration": _("Registration at the %(operator)s") % {"operator": operator},
        "operator_confirmation": _("Confirmation by the %(operator)s") % {"operator": operator},
        "start": _("Completed"),
    }
    words = step_words()
    steps = []
    for key, state in applications.post_submit_progress(application):
        steps.append(
            {
                "label": labels[key], "state_label": words[state], "tone": STEP_TONES[state],
                "note": step_note(key, application),
            }
        )
    return steps


def building_rows(application):
    rows = []
    for building in application.buildings.select_related("location").prefetch_related("metering_points"):
        address = building.current_address()
        points = []
        for point in building.metering_points.all():
            points.append(
                {
                    "number": numbers.format_metering_point_number(point.number),
                    "direction": Direction(point.direction).label,
                    "name": point.name,
                    "pv": point.pv_power_kwp, "battery": point.battery_kwh, "wallbox": point.wallbox_kw,
                    "url": reverse("management:metering_points_detail", args=[point.metering_point_id])
                    if point.metering_point_id else "",
                }
            )
        egid = building.egid or (building.location.egid if building.location_id else None)
        rows.append(
            {
                "address": f"{address[0]} {address[1]}".strip(),
                "town": f"{address[2]} {address[3]}".strip(),
                "egid": egid,
                "name": building.name, "description": building.description,
                "existing": building.location_id is not None,
                "url": reverse("management:locations_detail", args=[building.location_id])
                if building.location_id else "",
                "points": points,
            }
        )
    return rows


def notice_rows(queryset, request):
    """The notices as rows (EGID and numbers are staff data, deleted when resolved)."""
    rows = []
    for notice in queryset:
        rows.append(
            {
                "id": notice.pk,
                "kind": labels.notice_kind_label(notice.kind),
                "person": person_card(notice.person),
                "egid": notice.egid,
                "numbers": [numbers.format_metering_point_number(number) for number in notice.numbers],
                "created": notice.created_at,
                "resolve_url": reverse("management:processes_notice_resolve", args=[notice.pk]),
            }
        )
    return rows


class ApplicationDetailView(ManagementAccessMixin, View):
    """The page of one application: the way it goes, what the customer entered, and the next steps of the staff."""

    def get(self, request, pk):
        application = get_object_or_404(
            Application.objects.select_related("person").filter(state__in=VISIBLE_APPLICATION_STATES), pk=pk
        )
        person = application.person
        state = application.state
        progress = applications.progress(application)
        has_feed_in = progress.step(applications.BANK_CONNECTION).applicable
        acceptance = contracts.latest_acceptance(person)
        buildings = building_rows(application)
        next_url = lambda name: with_next(reverse(name, args=[application.pk]), request)  # noqa: E731
        in_flight = state in Application.IN_FLIGHT_STATES
        card = person_card(person)
        context = {
            "page_title": card["name"],
            "application": application,
            "person": card,
            "state_label": state_label(state),
            "state_tone": state_tone(state),
            "subtitle": _("Customer number %(number)s · Last change %(date)s") % {
                "number": card["number"],
                "date": format_date(application.updated_at.astimezone(timezone.get_current_timezone())),
            },
            "way": way_steps(application),
            "done": sum(1 for _key, step in applications.post_submit_progress(application) if step == applications.DONE),
            "total": len(applications.STEPS),
            "buildings": buildings,
            "has_feed_in": has_feed_in,
            "step_numbers": self.step_numbers(has_feed_in),
            "iban_on_file": own_iban.has_iban(person) if has_feed_in else None,
            "acceptance": acceptance,
            "acceptance_date": format_date(acceptance["accepted_at"].astimezone(timezone.get_current_timezone()))
            if acceptance else "",
            "acceptance_version": format_date(acceptance["version_date"]) if acceptance else "",
            "start_text": self.start_text(application),
            "operator_started": application.operator_started_on and format_date(application.operator_started_on),
            "operator_confirmed": application.operator_confirmed_on and format_date(application.operator_confirmed_on),
            "operator": grid_operator.name(),
            "empty_points": _("No metering points."),
            "can_review": state == Application.State.SUBMITTED,
            "can_return": state in Application.WITHDRAWABLE_STATES,
            "can_start_operator": state in Application.WITHDRAWABLE_STATES,
            "can_confirm": state == Application.State.OPERATOR_STARTED,
            "can_complete": state == Application.State.OPERATOR_CONFIRMED,
            "can_reject": in_flight,
            "review_url": reverse("management:processes_application_review", args=[application.pk]),
            "return_url": next_url("management:processes_application_return"),
            "operator_started_url": next_url("management:processes_application_operator_started"),
            "operator_confirmed_url": next_url("management:processes_application_operator_confirmed"),
            "complete_url": next_url("management:processes_application_complete"),
            "reject_url": next_url("management:processes_application_reject"),
            "current_url": request.get_full_path(),
            "notices": notice_rows(application_notices.open_notices().filter(person_id=person.pk), request),
            "history": history_rows(application, person.pk),
            "back_url": return_url(request, reverse("management:processes")),
        }
        return render(request, "management/processes/application.html", context)

    @staticmethod
    def step_numbers(has_feed_in):
        """The numbers of the cards: the bank connection is a step only with a feed-in, so the others count on without a
        gap (KONZEPT 5.7: only the steps that apply are shown and counted)."""
        keys = ["building", "points"] + (["bank"] if has_feed_in else []) + ["contract", "start"]
        return {key: number for number, key in enumerate(keys, start=1)}

    @staticmethod
    def start_text(application):
        if application.start_choice == Application.StartChoice.ASAP:
            return _("As soon as possible")
        if application.start_choice == Application.StartChoice.DATE and application.desired_start_date:
            return format_date(application.desired_start_date)
        return ""


# ---- One deregistration ----


class DeregistrationDetailView(ManagementAccessMixin, View):
    """The page of one deregistration: what was asked, and the step of the staff (confirm the date)."""

    def get(self, request, pk):
        deregistration = get_object_or_404(Deregistration.objects.select_related("person"), pk=pk)
        person = deregistration.person
        state = deregistration.state
        zone = timezone.get_current_timezone()
        operator = grid_operator.name()
        words = step_words()
        card = person_card(person)
        points = []
        for item in deregistration.items.select_related("metering_point__location").order_by("metering_point__number"):
            point = item.metering_point
            location = point.location
            points.append(
                {
                    "number": numbers.format_metering_point_number(point.number),
                    "direction": Direction(point.direction).label,
                    "address": f"{location.street} {location.house_number}, {location.postal_code} {location.town}"
                    if location is not None else "",
                    "egid": location.egid if location is not None else None,
                    "terminated_on": point.terminated_on and format_date(point.terminated_on),
                    "url": reverse("management:metering_points_detail", args=[point.pk]),
                }
            )
        confirmed = state in (Deregistration.State.CONFIRMED, Deregistration.State.EFFECTIVE)
        effective = state == Deregistration.State.EFFECTIVE
        dereg_label = Deregistration.State(state).label
        dereg_tone = "ok" if effective else ("accent" if confirmed else "warn")
        steps = [
            {
                "label": _("Deregistration submitted"), "tone": "ok", "state_label": words[applications.DONE],
                "note": format_date(deregistration.submitted_at.astimezone(zone)),
            },
            {
                "label": _("Date confirmed by the LEG"), "tone": "ok" if confirmed else "accent",
                "state_label": words[applications.DONE] if confirmed else words["next"],
                "note": format_date(deregistration.confirmed_date) if deregistration.confirmed_date else "",
            },
            {
                "label": _("Deregistered at the %(operator)s") % {"operator": operator},
                "tone": "ok" if effective else "muted",
                "state_label": words[applications.DONE] if effective else words[applications.OPEN],
                "note": format_date(deregistration.effective_at.astimezone(zone)) if deregistration.effective_at else "",
            },
        ]
        context = {
            "page_title": card["name"],
            "deregistration": deregistration,
            "person": card,
            "state_label": dereg_label,
            "state_tone": dereg_tone,
            "subtitle": _("Customer number %(number)s · Submitted %(date)s") % {
                "number": card["number"], "date": format_date(deregistration.submitted_at.astimezone(zone)),
            },
            "steps": steps,
            "done": sum(1 for step in steps if step["tone"] == "ok"),
            "total": process_list.DEREGISTRATION_STEPS,
            "points": points,
            "earliest": format_date(deregistration.earliest_date),
            "desired": format_date(deregistration.desired_date),
            "wants_earliest": deregistration.wants_earliest,
            "confirmed_date": format_date(deregistration.confirmed_date) if deregistration.confirmed_date else "",
            "can_confirm": state == Deregistration.State.SUBMITTED,
            "confirm_url": with_next(
                reverse("management:processes_deregistration_confirm", args=[deregistration.pk]), request
            ),
            "operator": operator,
            "history": history_rows(deregistration, person.pk),
            "back_url": return_url(request, reverse("management:processes")),
        }
        return render(request, "management/processes/deregistration.html", context)


# ---- Notices ----


class NoticesView(ManagementAccessMixin, View):
    """The open notices for the staff: a customer was refused something without being told why, or an application
    must be withdrawn at the grid operator. Oldest first; resolving a notice deletes its numbers."""

    def get(self, request):
        queryset = application_notices.open_notices()[: application_queue.MAX_ROWS]
        context = {
            "page_title": _("Notices"),
            "page_subtitle": _("Customers who were refused something without being told why."),
            "notices": notice_rows(queryset, request),
            "back_url": return_url(request, reverse("management:processes")),
            "current_url": request.get_full_path(),
        }
        return render(request, "management/processes/notices.html", context)


class NoticeResolveView(ManagementAccessMixin, View):
    """"Mark as resolved": POST only, then back to the page the button was on."""

    http_method_names = ["post"]

    def post(self, request, pk):
        notice = get_object_or_404(ApplicationNotice, pk=pk)
        try:
            application_notices.resolve(notice, request.user)
        except ApplicationError:
            raise Http404 from None
        messages.success(request, _("Saved."))
        return redirect(return_url(request, reverse("management:processes_notices")))
