"""Pages of the metering points: list, detail page and their dialogs.

Every view uses ``management.access``; objects come from the id in the address and status and
assignments are re-validated on the server. A dialog is a normal page that works without JavaScript."""

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext
from django.views import View

from leg_platform.community import (
    assignments,
    grid_operator,
    history,
    leg_switch,
    location_list,
    locations,
    metering_point_list,
    metering_points,
    person_search,
    problems,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, Leg, MeteringPoint, PersonAssignment
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView, event_label
from leg_platform.management.metering_point_forms import (
    ASSIGNMENT_ERRORS,
    STATUS_ERRORS,
    AssignmentEndForm,
    ConfirmationForm,
    LegAssignForm,
    LegSwitchForm,
    MeteringPointCreateForm,
    NotifiedForm,
    PersonAssignForm,
    StartDateForm,
    TerminationForm,
)
from leg_platform.management.returns import return_url, with_next
from leg_platform.management.summaries import duplicate_summary

COLUMN_NUMBER = lists.Column(
    "number", gettext_lazy("Metering point"), width=480, keep=True,
    info=grid_operator.text(gettext_lazy(
        "Number of the smart meter at the %(operator)s, followed by the direction. Blue is consumption, green is feed-in."
    )),
)
COLUMN_LOCATION = lists.Column(
    "location", gettext_lazy("Location"), width=200, info=gettext_lazy("The address where the metering point is installed."),
)
COLUMN_LEG = lists.Column(
    "leg", gettext_lazy("LEG"), width=200,
    info=grid_operator.text(gettext_lazy(
        "The billing group the metering point belongs to. Required before the registration at the %(operator)s."
    )),
)
COLUMN_STATUS = lists.Column("status", gettext_lazy("Status"), info_placement="left")
COLUMN_NEXT = lists.Column("next_step", gettext_lazy("Next step"), sortable=False)
ALL_COLUMNS = (COLUMN_NUMBER, COLUMN_LOCATION, COLUMN_LEG, COLUMN_STATUS, COLUMN_NEXT)

STEP_ROUTES = {
    metering_points.NOTIFY: ("management:metering_points_notify", gettext_lazy("Mark as notified")),
    metering_points.CONFIRM: ("management:metering_points_confirm", gettext_lazy("Record confirmation")),
    metering_points.START: ("management:metering_points_start", gettext_lazy("Record start date")),
}


def point_title(point):
    return point.number or "–"


def direction_label(point):
    return str(Direction(point.direction).label)


def number_lead(point):
    """The values of the dialog leads."""
    return {"number": point_title(point), "direction": direction_label(point), "operator": grid_operator.name()}


@dataclass
class MeteringPointList:
    """The list as the address asks for it: every row of the view (filters applied, sorted), not one page."""

    state: lists.ListState
    legs: list
    step: str
    show_steps: bool
    columns: tuple
    counts: dict
    problem_count: int
    today: object
    queryset: object


def build_list(params):
    today = timezone.localdate()
    legs = list(Leg.objects.filter(is_archived=False).order_by("name"))
    state = lists.parse(
        params, path=reverse("management:metering_points"), columns=ALL_COLUMNS,
        default_sort=metering_point_list.DEFAULT_SORT,
        filters={
            "step": (metering_point_list.STEPS, metering_point_list.STEP_ALL),
            "leg": ([str(leg.pk) for leg in legs], ""),
        },
    )
    step = state.values["step"]
    leg_id = int(state.values["leg"]) if state.values["leg"] else None
    view = metering_point_list.apply_leg(metering_point_list.base_queryset(today), leg_id)
    view = metering_point_list.apply_search(view, state.query, today=today)
    # The counts of the quick filters and the problem button cover this view (search, LEG).
    counted = metering_point_list.apply_problems(view) if state.problems else view
    counts = metering_point_list.step_counts(counted, today)
    queryset = metering_point_list.apply_step(view, step, today)
    problem_count = metering_point_list.apply_problems(queryset).count()
    if state.problems:
        queryset = metering_point_list.apply_problems(queryset)
    queryset = metering_point_list.apply_sort(queryset, state.sort, state.descending, today)
    show_steps = step in (metering_point_list.STEP_NOTIFY, metering_point_list.STEP_CONFIRM, metering_point_list.STEP_START)
    columns = (*ALL_COLUMNS[:3], COLUMN_NEXT if show_steps else COLUMN_STATUS)
    return MeteringPointList(state, legs, step, show_steps, columns, counts, problem_count, today, queryset)


class MeteringPointListView(ManagementAccessMixin, View):
    """A table on a desktop, cards on a phone."""

    def get(self, request):
        found = build_list(request.GET)
        state, legs, step, show_steps = found.state, found.legs, found.step, found.show_steps
        columns, counts, problem_count, today = found.columns, found.counts, found.problem_count, found.today
        page = lists.paginate(found.queryset, state)

        entries = []
        for point in page.items:
            status = point.status(today)
            action = metering_points.next_step(point, point.has_leg, today)
            route = STEP_ROUTES.get(action)
            found_problems = problems.metering_point_problems(point, today)
            entries.append(
                {
                    "point": point,
                    "status": status,
                    "problem_text": " ".join(str(problems.describe(problem)) for problem in found_problems),
                    "action": action,
                    "action_label": route[1] if route else "",
                    "action_url": with_next(reverse(route[0], args=[point.pk]), request) if route else "",
                    "leg_missing": action == metering_points.LEG_MISSING,
                    "location_url": reverse("management:locations_detail", args=[point.location_id])
                    if point.location_id else "",
                    "detail_url": reverse("management:metering_points_detail", args=[point.pk]),
                }
            )
        context = lists.list_context(
            request, "metering_points", columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d metering points") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["metering_points"]),
        )
        operator = {"operator": grid_operator.name()}
        quick_filters = [
            (metering_point_list.STEP_ALL, _("All (%(count)d)") % {"count": counts["all"]}),
            (metering_point_list.STEP_NOTIFY, _("Notify the %(operator)s (%(count)d)") % {**operator, "count": counts["notify"]}),
            (metering_point_list.STEP_CONFIRM, _("Confirmation pending (%(count)d)") % {"count": counts["confirm"]}),
            (metering_point_list.STEP_START, _("Start date pending (%(count)d)") % {"count": counts["start"]}),
            (metering_point_list.STEP_ARCHIVED, _("archived (%(count)d)") % {"count": counts["archived"]}),
        ]
        context.update(
            page_title=_("Metering points"),
            page_subtitle=_("All metering points with their state. Here you see what is still to do at the %(operator)s.") % operator,
            entries=entries,
            print_url=state.url_to(reverse("management:metering_points_print")),
            show_steps=show_steps,
            quick_filters=[
                {"value": value, "label": label, "url": state.url(step=value), "active": value == step}
                for value, label in quick_filters
            ],
            problem_count=problem_count,
            problem_url=state.url(problems="" if state.problems else "1"),
            create_url=with_next(reverse("management:metering_points_new"), request),
            leg_options=[(str(leg.pk), leg.name) for leg in legs],
            current_url=request.get_full_path(),
            empty_text=_("No metering points yet. Create the first metering point."),
        )
        return render(request, "management/metering_points/list.html", context)


def assignment_rows(chain, *, single_label, name_of, url_of):
    """The rows of the card "LEG" or "Person"; with a pending change, "former" and "new" one under the other."""
    single = len(chain) == 1
    rows = []
    for index, entry in enumerate(chain):
        item = entry.assignment
        if index == 0:
            label = single_label if single else _("former")
            note = _("until %(date)s") % {"date": format_date(item.valid_to)} if item.valid_to else ""
        else:
            label = _("new")
            note = _("from %(date)s") % {"date": format_date(item.valid_from)}
        rows.append({
            "label": label, "name": name_of(item), "url": url_of(item),
            "note": note, "single": single, "valid_from": item.valid_from,
        })
    return rows


class MeteringPointDetailView(ManagementAccessMixin, View):
    """The detail page."""

    def get(self, request, pk):
        point = get_object_or_404(MeteringPoint.objects.select_related("location__transformer_circuit"), pk=pk)
        today = timezone.localdate()
        can_change = not point.is_archived
        status = point.status(today)
        found = problems.metering_point_problems(point, today)
        leg_chain = metering_points.chain(assignments.LEG, point, today)
        person_chain = metering_points.chain(assignments.PERSON, point, today)
        show_delete = metering_points.can_delete(point)
        leg_rows = assignment_rows(
            leg_chain, single_label=_("Assigned"), name_of=lambda item: item.leg.name,
            url_of=lambda item: reverse("management:legs_detail", args=[item.leg_id]),
        )
        person_rows = assignment_rows(
            person_chain, single_label=_("Occupied by"), name_of=lambda item: person_name(item.person),
            url_of=lambda item: reverse("management:persons_detail", args=[item.person_id]),
        )
        last_person = person_chain[-1].assignment if person_chain else None
        status_action = None
        if can_change:
            if status == MeteringPointStatus.REGISTERED:
                status_action = metering_points.NOTIFY
            elif status == MeteringPointStatus.NOTIFIED:
                status_action = metering_points.CONFIRM
        location = point.location
        circuit = location.transformer_circuit if location else None
        context = {
            "page_title": point_title(point),
            "point": point,
            "direction": point.direction,
            "status": status,
            "status_note": status_note(point, status, today),
            "problems": [str(problems.describe(problem)) for problem in found],
            "location": location,
            "location_url": reverse("management:locations_detail", args=[location.pk]) if location else "",
            "circuit": circuit.grid_number if circuit else "",
            "leg_rows": leg_rows,
            "person_rows": person_rows,
            "has_leg": bool(leg_chain),
            "has_person": bool(person_chain),
            "status_action": status_action,
            "status_action_label": STEP_ROUTES[status_action][1] if status_action else "",
            "status_action_url": with_next(reverse(STEP_ROUTES[status_action][0], args=[pk]), request) if status_action else "",
            "start_missing": status == MeteringPointStatus.CONFIRMED and point.start_date is None,
            "can_change": can_change,
            "can_start": can_change and status == MeteringPointStatus.CONFIRMED and point.start_date is None,
            "can_terminate": can_change and status == MeteringPointStatus.ACTIVE and point.terminated_on is None,
            "can_end_person": can_change and last_person is not None and last_person.valid_to is None,
            "can_delete_person": can_change and last_person is not None
            and assignments.is_deletable(last_person, today),
            "show_delete": show_delete,
            "show_archive": can_change and not show_delete,
            "show_restore": point.is_archived,
            "history": history_rows(point),
            "back_url": return_url(request, reverse("management:metering_points")),
            "current_url": request.get_full_path(),
        }
        return render(request, "management/metering_points/detail.html", context)


def status_note(point, status, today):
    """The line under the status: ended, or a recorded termination that lies in the future."""
    if status == MeteringPointStatus.TERMINATED:
        return _("The assignment has ended.")
    if point.terminated_on is not None and point.terminated_on > today:
        return _("Termination on %(date)s") % {"date": format_date(point.terminated_on)}
    return ""


def person_name(person):
    return location_list.person_name(person)


def history_rows(point):
    """The history, oldest first; history entries hold only ids, so LEG and person names are looked up here."""
    entries = list(history.entries_for(point))
    leg_ids = {e.detail.get("leg_id") for e in entries if e.detail.get("leg_id")}
    person_ids = {e.detail.get("person_id") for e in entries if e.detail.get("person_id")}
    legs = dict(Leg.objects.filter(pk__in=leg_ids).values_list("pk", "name")) if leg_ids else {}
    persons = {}
    if person_ids:
        persons = {p.pk: person_name(p) for p in get_user_model().objects.filter(pk__in=person_ids)}
    rows = []
    for entry in entries:
        label = str(event_label(entry.event))
        detail = entry.detail
        if entry.event == HistoryEvent.CONFIRMATION_RECORDED and detail.get("start_date"):
            label = _("%(label)s, start date %(date)s") % {"label": label, "date": format_iso(detail["start_date"])}
        name = legs.get(detail.get("leg_id")) or persons.get(detail.get("person_id"))
        if name:
            label = f"{label}, {name}"
        rows.append(
            {"time": entry.created_at, "label": label, "actor": entry.actor.display_name if entry.actor_id else ""}
        )
    return rows


def format_iso(value):
    year, month, day = value.split("-")
    return f"{day}.{month}.{year}"


def format_date(value):
    """DD.MM.YYYY."""
    return value.strftime("%d.%m.%Y")


class PointDialog(DialogView):
    """A dialog about one metering point. ``status`` is the status it needs (a GET in another status
    goes back with a message, a POST is refused by the service)."""

    status = None

    def point(self):
        if not hasattr(self, "_point"):
            self._point = get_object_or_404(MeteringPoint.objects.select_related("location"), pk=self.kwargs["pk"])
        return self._point

    @property
    def default_back(self):
        return reverse("management:metering_points_detail", args=[self.kwargs["pk"]])

    def back(self, request):
        return return_url(request, self.default_back)

    def applicable(self):
        point = self.point()
        if point.is_archived:
            return False
        return self.status is None or point.status() == self.status

    def get(self, request, **kwargs):
        if not self.applicable():
            messages.error(request, STATUS_ERRORS["wrong_status"])
            return redirect(self.back(request))
        return super().get(request, **kwargs)

    def post(self, request, **kwargs):
        if self.point().is_archived:
            messages.error(request, STATUS_ERRORS["archived"])
            return redirect(self.back(request))
        return super().post(request, **kwargs)

    def refuse(self, form, error):
        form.add_error(None, STATUS_ERRORS.get(error.code, STATUS_ERRORS["wrong_status"]))
        return None

    def done(self, request):
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class StepDialog(PointDialog):
    def run(self, point, data, actor):
        raise NotImplementedError

    def save(self, request, form):
        try:
            self.run(self.point(), form.cleaned_data, request.user)
        except metering_points.StatusError as error:
            return self.refuse(form, error)
        return self.done(request)


class NotifyView(StepDialog):
    status = MeteringPointStatus.REGISTERED
    title = grid_operator.text(gettext_lazy("Mark as notified to the %(operator)s"))
    submit_label = gettext_lazy("Mark as notified")
    form_class = NotifiedForm

    def get_lead(self):
        return _("Metering point %(number)s (%(direction)s). The registration at the %(operator)s has taken place.") % number_lead(self.point())

    def run(self, point, data, actor):
        metering_points.mark_notified(point, data["registered_on"], actor)


class ConfirmView(StepDialog):
    status = MeteringPointStatus.NOTIFIED
    title = gettext_lazy("Record confirmation")
    submit_label = gettext_lazy("Save confirmation")
    form_class = ConfirmationForm

    def get_lead(self):
        return _("Metering point %(number)s (%(direction)s). The %(operator)s has confirmed the registration.") % number_lead(self.point())

    def run(self, point, data, actor):
        metering_points.record_confirmation(point, data["confirmed_on"], data["start_date"], actor)


class StartDateView(StepDialog):
    """The start date of a confirmed registration on standby."""

    status = MeteringPointStatus.CONFIRMED
    title = gettext_lazy("Record confirmation")
    submit_label = gettext_lazy("Save confirmation")
    form_class = StartDateForm

    def applicable(self):
        return super().applicable() and self.point().start_date is None

    def get_lead(self):
        return _("Metering point %(number)s (%(direction)s). The %(operator)s has confirmed the registration.") % number_lead(self.point())

    def run(self, point, data, actor):
        metering_points.record_start_date(point, data["start_date"], actor)


class TerminateView(StepDialog):
    status = MeteringPointStatus.ACTIVE
    title = gettext_lazy("Record termination")
    submit_label = gettext_lazy("Save termination")
    form_class = TerminationForm

    def applicable(self):
        return super().applicable() and self.point().terminated_on is None

    def get_lead(self):
        return _("Metering point %(number)s (%(direction)s). Enter the date that the %(operator)s gives.") % number_lead(self.point())

    def run(self, point, data, actor):
        metering_points.terminate(point, data["terminated_on"], actor)


class AssignmentDialog(PointDialog):
    """A dialog that changes an assignment; refusals of ``assignments`` show on the dialog."""

    def assignment_error(self, form, error, field="valid_from"):
        code = error.code
        target = None if code not in ("starts_not_after_previous", "end_before_start") else field
        if code in ("not_a_person", "person_not_active") and "person" in form.fields:
            target = "person"
        form.add_error(target, ASSIGNMENT_ERRORS.get(code, _("This change is not possible.")))
        return None


class PersonView(AssignmentDialog):
    """Assign a person or, with a person, change it (the former person ends the day before "Applies from")."""

    form_class = PersonAssignForm
    template_name = "management/metering_points/dialog_person.html"
    submit_label = gettext_lazy("Save")

    def has_person(self):
        return bool(metering_points.chain(assignments.PERSON, self.point()))

    @property
    def title(self):
        return _("Change person") if self.has_person() else _("Assign person")

    def get_form(self, data=None):
        form = PersonAssignForm(data)
        if self.has_person():
            form.hint = _("If the metering point already has a person, the person ends on the day before \"Applies from\".")
        return form

    def get_lead(self):
        return _("Metering point %(number)s (%(direction)s).") % number_lead(self.point())

    def render_dialog(self, request, form, **extra):
        return super().render_dialog(
            request, form, suggest_url=reverse("management:metering_points_person_search"), **extra
        )

    def save(self, request, form):
        try:
            assignments.assign(
                assignments.PERSON, self.point(), form.cleaned_data["person"], form.cleaned_data["valid_from"], request.user
            )
        except assignments.AssignmentError as error:
            return self.assignment_error(form, error)
        return self.done(request)


class PersonEndView(AssignmentDialog):
    """The open assignment ends on a date and stays in the history."""

    title = gettext_lazy("End assignment")
    submit_label = gettext_lazy("End assignment")
    form_class = AssignmentEndForm

    def target(self):
        chain = metering_points.chain(assignments.PERSON, self.point())
        last = chain[-1].assignment if chain else None
        return last if last is not None and last.valid_to is None else None

    def applicable(self):
        return super().applicable() and self.target() is not None

    def get_lead(self):
        target = self.target()
        if target is None:
            return ""
        return _("Metering point %(number)s (%(direction)s). The assignment to %(name)s ends.") % {
            **number_lead(self.point()), "name": person_name(target.person),
        }

    def get_form(self, data=None):
        target = self.target()
        return AssignmentEndForm(data, initial={"assignment": target.pk if target is not None else ""})

    def save(self, request, form):
        target = self.target()
        if target is None or form.cleaned_data["assignment"] != str(target.pk):
            # The page showed another assignment than the open one.
            return self.refuse(form, metering_points.StatusError("wrong_status"))
        try:
            assignments.end(assignments.PERSON, target, form.cleaned_data["valid_to"], request.user)
        except assignments.AssignmentError as error:
            return self.assignment_error(form, error, "valid_to")
        return self.done(request)


class PersonDeleteView(ManagementAccessMixin, View):
    """A question first, then the deletion; only for an assignment that never took effect and was
    never ended (an entry made by mistake)."""

    def target(self, pk):
        point = get_object_or_404(MeteringPoint, pk=pk)
        if point.is_archived:
            raise Http404
        chain = metering_points.chain(assignments.PERSON, point)
        last = chain[-1].assignment if chain else None
        return point, last

    def get(self, request, pk):
        point, last = self.target(pk)
        if last is None or not assignments.is_deletable(last):
            raise Http404
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Delete assignment"),
                "dialog_lead": _("The assignment to %(name)s is deleted. This is possible only for an entry made by mistake.")
                % {"name": person_name(last.person)},
                "submit_label": _("Delete assignment"),
                "back_url": return_url(request, reverse("management:metering_points_detail", args=[pk])),
                "form": None,
                "danger": True,
                "hidden_values": [("assignment", last.pk)],
            },
        )

    def post(self, request, pk):
        point, last = self.target(pk)
        back = return_url(request, reverse("management:metering_points_detail", args=[pk]))
        if last is None or request.POST.get("assignment") != str(last.pk):
            # Nothing to delete, or the page showed another assignment.
            messages.error(request, ASSIGNMENT_ERRORS["not_deletable"])
            return redirect(back)
        try:
            assignments.delete(assignments.PERSON, last, request.user)
        except assignments.AssignmentError as error:
            messages.error(request, ASSIGNMENT_ERRORS.get(error.code, _("This change is not possible.")))
            return redirect(back)
        messages.success(request, _("Deleted."))
        return redirect(back)


class LegView(AssignmentDialog):
    """Assign a LEG or, with a LEG, change it with the move mask; the LEG pages use this dialog too."""

    template_name = "management/metering_points/dialog_leg.html"
    submit_label = gettext_lazy("Save")

    def current(self):
        if not hasattr(self, "_current"):
            self._current = leg_switch.current_assignment(assignments.LEG, self.point())
        return self._current

    @property
    def title(self):
        return _("Change LEG") if self.current() else _("Assign LEG")

    @property
    def form_class(self):
        return LegSwitchForm if self.current() else LegAssignForm

    def get_form(self, data=None):
        if self.current():
            return LegSwitchForm(data, metering_point=self.point(), current_leg_id=self.current().leg_id)
        return LegAssignForm(data)

    def get_lead(self):
        point = self.point()
        current = self.current()
        if current is None:
            return _("Metering point %(number)s (%(direction)s).") % number_lead(point)
        values = {**number_lead(point), "leg": current.leg.name}
        if point.location_id:
            return _("Metering point %(number)s (%(direction)s) at %(location)s changes from %(leg)s to:") % {
                **values, "location": f"{point.location.street} {point.location.house_number}",
            }
        return _("Metering point %(number)s (%(direction)s) changes from %(leg)s to:") % values

    def render_dialog(self, request, form, **extra):
        related = []
        if isinstance(form, LegSwitchForm):
            names = person_names(form.related)
            for point in form.related:
                parts = [direction_label(point)]
                if point.location_id:
                    parts.append(f"{point.location.street} {point.location.house_number}")
                if names.get(point.pk):
                    parts.append(names[point.pk])
                related.append({"point": point, "text": f"{point.number or '–'} ({', '.join(parts)})"})
        checked = {str(pk) for pk in request.POST.getlist("also")} if request.method == "POST" else set()
        return super().render_dialog(
            request, form, related=related, checked=checked, related_count=len(related),
            related_title=ngettext(
                "Also related: %(count)d more metering point", "Also related: %(count)d more metering points", len(related)
            ) % {"count": len(related)},
            **extra,
        )

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg_switch.switch_leg(
                self.point(), data["leg"], data["valid_from"], also=list(data.get("also", ())), actor=request.user
            )
        except assignments.AssignmentError as error:
            return self.assignment_error(form, error)
        return self.done(request)


def person_names(points):
    """``{metering point id: name of its person}`` (one query)."""
    today = timezone.localdate()
    found = {}
    for assignment in (
        PersonAssignment.objects.not_ended(today)
        .filter(metering_point__in=[p.pk for p in points])
        .select_related("person")
        .order_by("-valid_from")
    ):
        found[assignment.metering_point_id] = person_name(assignment.person)
    return found


class MeteringPointNewView(DialogView):
    """Create from the list. The number is checked on leaving the field and again on saving; an existing
    number shows a notice with a link and blocks saving."""

    title = gettext_lazy("Create metering point")
    lead = gettext_lazy("A metering point belongs to a location. Choose the location before you enter the other details.")
    template_name = "management/metering_points/dialog_create.html"
    default_back = ""

    def back(self, request):
        return return_url(request, reverse("management:metering_points"))

    def get_form(self, data=None):
        return MeteringPointCreateForm(data)

    def render_dialog(self, request, form, **extra):
        duplicate = getattr(form, "duplicate", None)
        return super().render_dialog(
            request, form, duplicate=duplicate_summary(duplicate) if duplicate is not None else None,
            check_url=reverse("management:metering_points_check_number"),
            location_url=reverse("management:metering_points_location_search"),
            **extra,
        )

    def save(self, request, form):
        try:
            metering_points.create_metering_point(
                direction=form.cleaned_data["direction"], number=form.number,
                location=form.cleaned_data.get("location"), leg=form.cleaned_data.get("leg"),
                actor=request.user, **form.metering_point_details(),
            )
        except metering_points.DuplicateNumber as duplicate:
            form.duplicate = duplicate.existing
            return None
        except (locations.Archived, assignments.AssignmentError):
            # Archived in the meantime (D-085).
            form.add_error(None, _("The location or the LEG is archived."))
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class CheckNumberView(ManagementAccessMixin, View):
    """Number check on leaving the field: ``{"message": "", "duplicate": null}``, the message, or the
    summary of the existing metering point."""

    http_method_names = ["get"]

    def get(self, request):
        digits = request.GET.get("digits", "")[:40]
        form = MeteringPointCreateForm({"number_digits": digits})
        form.full_clean()
        errors = form.errors.get("number_digits")
        if errors:
            return JsonResponse({"message": str(errors[0]), "duplicate": None})
        existing = metering_points.find_by_number(form.number)
        return JsonResponse(
            {"message": "", "duplicate": duplicate_summary(existing) if existing is not None else None,
             "title": str(_("This metering point number already exists.")),
             "link": str(_("Go to the existing metering point"))}
        )


class SuggestView(ManagementAccessMixin, View):
    """JSON list of at most 10 texts for an input of at least two characters."""

    http_method_names = ["get"]

    def texts(self, text):
        raise NotImplementedError

    def get(self, request):
        text = request.GET.get("q", "").strip()[:100]
        return JsonResponse({"results": self.texts(text) if len(text) >= 2 else []})


class LocationSearchView(SuggestView):
    def texts(self, text):
        return [str(location) for location in location_list.location_suggestions(text)]


class PersonSearchView(SuggestView):
    """Active accounts with a customer number; the text holds the name and the customer number."""

    def texts(self, text):
        return [person_search.label(person) for person in person_search.search(text)]


class PointPostView(ManagementAccessMixin, View):
    """A button of the detail page: POST only, then back to the page."""

    http_method_names = ["post"]

    def post(self, request, pk):
        point = get_object_or_404(MeteringPoint, pk=pk)
        self.act(request, point)
        return redirect(return_url(request, reverse("management:metering_points_detail", args=[pk])))

    def act(self, request, point):
        raise NotImplementedError


class ArchiveView(PointPostView):
    """Archive and restore."""

    def act(self, request, point):
        metering_points.set_archived(point, request.POST.get("archive") != "0", request.user)
        messages.success(request, _("Saved."))


class DeleteView(ManagementAccessMixin, View):
    """A question first, then the deletion; only without a history."""

    def get(self, request, pk):
        point = get_object_or_404(MeteringPoint, pk=pk)
        if not metering_points.can_delete(point):
            raise Http404
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Delete metering point"),
                "dialog_lead": _("The metering point %(number)s is deleted for good.") % {"number": point_title(point)},
                "submit_label": _("Delete metering point"),
                "back_url": return_url(request, reverse("management:metering_points_detail", args=[pk])),
                "form": None,
                "danger": True,
            },
        )

    def post(self, request, pk):
        point = get_object_or_404(MeteringPoint, pk=pk)
        try:
            metering_points.delete(point)
        except metering_points.NotDeletable:
            messages.error(request, _("This metering point has a history and cannot be deleted. Archive it instead."))
            return redirect(reverse("management:metering_points_detail", args=[pk]))
        messages.success(request, _("Deleted."))
        return redirect(reverse("management:metering_points"))
