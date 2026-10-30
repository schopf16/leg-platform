"""The pages of the processes (owner design ``verwaltung/vorgaenge``; D-103, D-107): the list of the applications and
deregistrations, the page of one application, the page of one deregistration and the notices for the staff.

Every view uses ``management.access`` (every active staff account, D-086). The rules live in ``community``
(``process_list``, ``applications.progress``, ``application_staff``, ``deregistrations``, ``application_notices``); a page
only reads and shows. Drafts and paused drafts are listed and have a read-only page marked "Draft / not submitted" (D-107,
which replaces KONZEPT 5.6 and the 404 of D-104); no staff step applies to them. The page of an application shows what the
customer entered; it never shows the IBAN (only whether one is on file, D-087) and no proof of a confirmation.
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
    deregistrations,
    grid_operator,
    history,
    labels,
    own_iban,
    process_list,
)
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import (
    Application,
    ApplicationNotice,
    Deregistration,
    Direction,
    MeteringPoint,
)
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import event_label
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.returns import return_url, with_next

# The applications the staff can open: the drafts too (D-107; read-only, marked as not submitted), the running ones and
# the completed ones. The dialogs and services of the staff act only on the states they name.
VISIBLE_APPLICATION_STATES = (*Application.OPEN_STATES, Application.State.COMPLETED)

STATE_TONES = {
    Application.State.DRAFT: "muted",
    Application.State.PAUSED: "muted",
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
    # The staff's words for a draft say what matters to them: nothing was submitted yet (D-107).
    if state == Application.State.DRAFT:
        return _("Draft / not submitted")
    if state == Application.State.PAUSED:
        return _("Paused / not submitted")
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
    are stored, D-077). Entries of the same event by the same actor that follow one another are one row with a count
    (the customer chooses a building three times: "Customer: building chosen × 3"), shown with the time of the newest."""
    rows = []
    last_key = None
    for entry in history.entries_for(subject).order_by("-created_at", "-pk"):
        if entry.actor_id is None:
            actor = _("System")
        elif entry.actor_id == person_id:
            actor = _("Customer")
        else:
            actor = entry.actor.display_name
        key = (entry.actor_id, entry.event)
        if rows and key == last_key:
            rows[-1]["count"] += 1
            continue
        last_key = key
        rows.append({"time": entry.created_at, "label": event_label(entry.event), "actor": actor, "count": 1})
    return rows


# ---- The list ----


def list_columns():
    # Widths for a 1280 px screen: the content card is about 967 px wide (the menu takes the rest), so the sum of the start
    # widths, with the link column of 56 px, is at most that and the table fits without scrolling (walkthrough W5-03). The
    # kind, the count and the date of the last change never shrink (their words must be whole); the others are cut with
    # three dots and show the full text on hover. The status, the progress and the date have the least room their header
    # (title, sort arrow and info button) needs.
    return (
        lists.Column("person", gettext_lazy("Person"), width=116),
        lists.Column("kind", gettext_lazy("Kind"), width=100, keep=True),
        lists.Column("building", gettext_lazy("Building"), width=116),
        lists.Column("points", gettext_lazy("Metering points"), width=108, keep=True),
        lists.Column(
            "state", gettext_lazy("Status"), width=116,
            info=_(
                "Draft / not submitted: the customer has started the application and not sent it yet; you can read "
                "it, not change it. Submitted: waiting for the review. In review: the LEG checks it. Then the "
                "registration at the grid operator. In deregistration: the notice was given and the LEG confirms "
                "the date."
            ),
        ),
        lists.Column(
            "progress", gettext_lazy("Progress"), width=124,
            info=_(
                "Applications: the five steps from the submission to the completion; a draft shows the steps the "
                "customer has completed in the form. Deregistrations: three steps."
            ),
        ),
        lists.Column(
            "date", gettext_lazy("Date"), width=104,
            info=_(
                "Applications: the start date the customer chose. Deregistrations: the date from which the "
                "deregistration is effective, once the LEG has confirmed it. Open until it is known."
            ),
            info_placement="left",
        ),
        lists.Column("changed", gettext_lazy("Last change"), width=126, keep=True, info_placement="left"),
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
        (process_list.STATE_DRAFT, _("Draft / not submitted")),
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
    listed = lists.ListRows(found)
    listed.truncated = found.truncated
    return state, columns, listed


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
            page_subtitle=_("Applications, also drafts that are not submitted yet, and deregistrations of the customers."),
            entries=entries,
            kind_options=kind_options(),
            state_options=state_options(),
            notice_count=notice_count,
            notices_url=reverse("management:processes_notices"),
            empty_text=_("No running processes."),
            truncated_text=_(
                "More than %(max)d applications or deregistrations match. Only the most recently changed ones are "
                "listed, submitted applications before drafts. Narrow the list with the search or the filters."
            ) % {"max": process_list.MAX_ROWS} if found.truncated else "",
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
        # The words of the customer's page (customer/steps.py), so both sides name the way the same (W1-01, W5-04).
        "operator_registration": _("Registration at %(operator)s") % {"operator": operator},
        "operator_confirmation": _("Customer confirms the request of the %(operator)s") % {"operator": operator},
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


def draft_steps(application):
    """The steps of the form of a draft (building, metering points, bank connection if a feed-in is entered, contract,
    start date) with their state: what the customer has completed so far (D-107). The staff only reads it."""
    names = {
        applications.BUILDING: _("Building"),
        applications.METERING_POINTS: _("Metering points"),
        applications.BANK_CONNECTION: _("Bank connection"),
        applications.CONTRACT: _("Contract"),
        applications.START: _("Start date"),
    }
    current = applications.progress(application)
    words = step_words()
    steps = []
    for step in current.steps:
        if not step.applicable:
            continue
        state = applications.DONE if step.state != applications.OPEN else (
            "next" if step.key == current.next_step else applications.OPEN
        )
        steps.append({"label": names[step.key], "state_label": words[state], "tone": STEP_TONES[state], "note": ""})
    return steps, current.done, current.total


def building_rows(application, request=None):
    """The buildings of an application with their metering points for the page. With a ``request`` and an application
    that the staff may correct (D-109) every row has the address of its edit dialog (a new building; a stored building has
    its address on the page of the location) and every point the address of its own, both with the current page as
    ``next``."""
    correctable = request is not None and application.state in Application.CORRECTABLE_STATES
    rows = []
    for building in application.buildings.select_related("location").prefetch_related("metering_points"):
        address = building.current_address()
        points = []
        for point in building.metering_points.all():
            points.append(
                {
                    "number": point.number,
                    "direction": Direction(point.direction).label,
                    "direction_value": point.direction,
                    "name": point.name,
                    "pv": point.pv_power_kwp, "battery": point.battery_kwh, "wallbox": point.wallbox_kw,
                    # Plant details are asked for a feed-in; a figure on another point is shown as well.
                    "has_plant": point.direction == Direction.FEED_IN or any(
                        value is not None for value in (point.pv_power_kwp, point.battery_kwh, point.wallbox_kw)
                    ),
                    "url": reverse("management:metering_points_detail", args=[point.metering_point_id])
                    if point.metering_point_id else "",
                    "edit_url": with_next(
                        reverse("management:processes_application_point", args=[application.pk, point.pk]), request
                    ) if correctable else "",
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
                "edit_url": with_next(
                    reverse("management:processes_application_building", args=[application.pk, building.pk]), request
                ) if correctable and building.location_id is None else "",
                "points": points,
            }
        )
    return rows


def notice_rows(queryset, request):
    """The notices as rows (EGID and numbers are staff data, deleted when resolved). A number that is a metering point
    of this instance has a link to its page, where the staff sees whose it is (D-086; walkthrough W5-08)."""
    notices = list(queryset)
    wanted = {number for notice in notices for number in notice.numbers}
    known = dict(MeteringPoint.objects.filter(number__in=wanted).values_list("number", "pk")) if wanted else {}
    rows = []
    for notice in notices:
        rows.append(
            {
                "id": notice.pk,
                "kind": labels.notice_kind_label(notice.kind),
                "person": person_card(notice.person),
                "egid": notice.egid,
                "numbers": [
                    {
                        "number": number,
                        "url": reverse("management:metering_points_detail", args=[known[number]])
                        if number in known else "",
                    }
                    for number in notice.numbers
                ],
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
        buildings = building_rows(application, request)
        next_url = lambda name: with_next(reverse(name, args=[application.pk]), request)  # noqa: E731
        in_flight = state in Application.IN_FLIGHT_STATES
        is_draft = state in Application.DRAFT_STATES
        if is_draft:
            way, done, total = draft_steps(application)
        else:
            way = way_steps(application)
            done = sum(1 for _key, step in applications.post_submit_progress(application) if step == applications.DONE)
            total = len(applications.STEPS)
        card = person_card(person)
        context = {
            "page_title": card["name"],
            "application": application,
            "is_draft": is_draft,
            "person": card,
            "state_label": state_label(state),
            "state_tone": state_tone(state),
            "subtitle": _("Customer number %(number)s · Last change %(date)s") % {
                "number": card["number"],
                "date": format_date(application.updated_at.astimezone(timezone.get_current_timezone())),
            },
            "way": way,
            "done": done,
            "total": total,
            "buildings": buildings,
            "has_points": any(building["points"] for building in buildings),
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
            "can_correct": state in Application.CORRECTABLE_STATES,
            "can_start_operator": state in Application.WITHDRAWABLE_STATES,
            "can_confirm": state == Application.State.OPERATOR_STARTED,
            "can_complete": state == Application.State.OPERATOR_CONFIRMED,
            "can_reject": in_flight,
            "review_url": reverse("management:processes_application_review", args=[application.pk]),
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
                    "number": point.number,
                    "direction": Direction(point.direction).label,
                    "direction_value": point.direction,
                    "address": f"{location.street} {location.house_number}, {location.postal_code} {location.town}"
                    if location is not None else "",
                    "egid": location.egid if location is not None else None,
                    "terminated_on": point.terminated_on and format_date(point.terminated_on),
                    # Assigned but not active (yet): the staff checks it with the grid operator (owner decision 2026-10-29).
                    "not_active": point.status() in deregistrations.PENDING_STATUSES,
                    "url": reverse("management:metering_points_detail", args=[point.pk]),
                }
            )
        confirmed = state in (Deregistration.State.CONFIRMED, Deregistration.State.EFFECTIVE)
        effective = state == Deregistration.State.EFFECTIVE
        if state == Deregistration.State.SUBMITTED:
            next_text = _(
                "The customer gave notice. Confirm the date: it is recorded as the termination date of every metering point."
            )
        elif effective:
            next_text = _("The deregistration is effective.")
        else:
            next_text = _(
                "The date %(date)s is confirmed. The portal marks the deregistration effective by itself on that day."
            ) % {"date": format_date(deregistration.confirmed_date)}
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
            "next_text": next_text,
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
