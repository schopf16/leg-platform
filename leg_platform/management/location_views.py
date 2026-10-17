"""The pages of the locations (D-078; LEG-Design-Vorgaben/verwaltung/standorte).

List, detail and the dialogs. Every view uses the access check of ``management.access``; objects are
looked up by the id in the address only after the permission is checked, and every state change
is validated on the server (never trusted from the browser). A dialog is a normal page: it works
without JavaScript and returns to the place it was opened from (``management.returns``).
"""

import json
from dataclasses import dataclass

from django.contrib import messages
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import (
    history,
    location_list,
    locations,
    metering_points,
    preferences,
    transformer_circuits,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Location, PersonAssignment, TransformerCircuit
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_forms import (
    AddressEditForm,
    CircuitChoiceField,
    CircuitForm,
    LocationCreateForm,
    MeteringPointForm,
    RemarkForm,
)
from leg_platform.management.navigation import AREA_VIEW_PERMISSIONS
from leg_platform.management.returns import return_url, with_next
from leg_platform.management.summaries import duplicate_summary

VIEW = "community.view_location"
ADD = "community.add_location"
CHANGE = "community.change_location"
DELETE = "community.delete_location"
VIEW_PERSONS = "community.view_personrecord"

COLUMNS = (
    lists.Column("address", gettext_lazy("Location"), width=380, info_template="management/locations/_tip_location.html"),
    lists.Column("circuit", gettext_lazy("Transformer circuit"), width=150, info_template="management/locations/_tip_circuit.html"),
    lists.Column("metering_points", gettext_lazy("Metering points"), width=260, info_template="management/locations/_tip_points.html"),
    lists.Column(
        "occupancy", gettext_lazy("Occupancy"), resizable=False, info_placement="left",
        info_template="management/locations/_tip_occupancy.html",
    ),
)


def count_phrase(count):
    if count == 0:
        return _("no metering points")
    return _("1 metering point") if count == 1 else _("%(count)d metering points") % {"count": count}


def occupancy_phrase(metering_point_count, persons):
    if metering_point_count == 0:
        return _("empty")
    if persons == 0:
        return _("free")
    return _("1 person") if persons == 1 else _("%(count)d persons") % {"count": persons}


def circuit_phrase(location):
    return location.transformer_circuit.bkw_number if location.transformer_circuit_id else _("no transformer circuit")


def event_label(event):
    try:
        return HistoryEvent(event).label
    except ValueError:
        return event


class ListWidthsView(ManagementAccessMixin, View):
    """Saves the column widths of a list for the account (progressive enhancement of the lists:
    ``management/js/list.js`` posts them after a column was resized). The widths are checked
    (``community.preferences``); the account is always the one of the session."""

    any_permission_required = AREA_VIEW_PERMISSIONS
    http_method_names = ["post"]

    def post(self, request, list_key):
        if list_key not in preferences.LIST_KEYS:
            raise Http404
        if len(request.body) > 4096:
            return HttpResponseBadRequest()
        try:
            widths = json.loads(request.body)["widths"]
            preferences.save_column_widths(request.user, list_key, widths)
        except (ValueError, KeyError, TypeError):
            return HttpResponseBadRequest()
        return JsonResponse({"saved": True})


def occupancy_options():
    return [
        (location_list.OCCUPANCY_ALL, _("All")),
        (location_list.OCCUPANCY_OCCUPIED, _("occupied")),
        (location_list.OCCUPANCY_FREE, _("free")),
        (location_list.OCCUPANCY_EMPTY, _("empty")),
        (location_list.OCCUPANCY_ARCHIVED, _("archived")),
    ]


@dataclass
class LocationList:
    """The list of locations as the address asks for it: the state and the queryset of every row of the view
    (search and filters applied, sorted), not only one page. The page and the printing use it."""

    state: lists.ListState
    circuits: list
    see_persons: bool
    today: object
    problem_count: int
    queryset: object


def build_list(params, user):
    today = timezone.localdate()
    see_persons = user.has_perm(VIEW_PERSONS)
    circuits = transformer_circuits.choices()
    state = lists.parse(
        params, path=reverse("management:locations"), columns=COLUMNS, default_sort=location_list.DEFAULT_SORT,
        filters={
            "circuit": ([c.bkw_number for c in circuits], ""),
            "occupancy": (location_list.OCCUPANCIES, location_list.OCCUPANCY_ALL),
        },
    )
    queryset = location_list.apply_search(
        location_list.base_queryset(today), state.query, see_persons=see_persons, today=today
    )
    queryset = location_list.apply_circuit(queryset, state.values["circuit"])
    queryset = location_list.apply_occupancy(queryset, state.values["occupancy"])
    # The button counts the entries with a problem in the current view (search and filters).
    problem_count = location_list.apply_problems(queryset.filter(is_archived=False)).count()
    if state.problems:
        queryset = location_list.apply_problems(queryset)
    queryset = location_list.apply_sort(queryset, state.sort, state.descending)
    return LocationList(state, circuits, see_persons, today, problem_count, queryset)


class LocationListView(ManagementAccessMixin, View):
    """The list of locations (``standorte-liste-desktop`` and ``-handy``)."""

    permission_required = (VIEW,)

    def get(self, request):
        found = build_list(request.GET, request.user)
        state, circuits, see_persons, today = found.state, found.circuits, found.see_persons, found.today
        problem_count = found.problem_count
        page = lists.paginate(found.queryset, state)
        rows, persons = location_list.details(page.items, today, see_persons=see_persons)
        expanded = set(preferences.get_preference(request.user, "locations").expanded_locations)

        entries = []
        for location in page.items:
            points = rows[location.pk]
            entries.append(
                {
                    "location": location,
                    "points": points,
                    "is_open": bool(points) and location.pk in expanded,
                    "can_open": bool(points),
                    "circuit": circuit_phrase(location),
                    "count_phrase": count_phrase(location.mp_count),
                    "occupancy": occupancy_phrase(location.mp_count, persons[location.pk]),
                    "persons": persons[location.pk],
                    "detail_url": reverse("management:locations_detail", args=[location.pk]),
                }
            )
        openable = [entry for entry in entries if entry["can_open"]]
        all_open = bool(openable) and all(entry["is_open"] for entry in openable)
        count_text = _("%(first)d–%(last)d of %(total)d locations") % {
            "first": page.first, "last": page.last, "total": page.total,
        }
        context = lists.list_context(
            request, "locations", COLUMNS, state, page, count_text=count_text,
            widths_url=reverse("management:list_widths", args=["locations"]),
        )
        context.update(
            page_title=_("Locations"),
            page_subtitle=_("All locations of the LEG with their metering points."),
            entries=entries,
            print_url=state.url_to(reverse("management:locations_print")),
            all_open=all_open,
            expand_ids=[entry["location"].pk for entry in openable],
            expand_url=reverse("management:locations_expanded"),
            problem_count=problem_count,
            problem_url=state.url(problems="" if state.problems else "1"),
            can_add=request.user.has_perm(ADD),
            create_url=with_next(reverse("management:locations_new"), request),
            circuit_options=[(c.bkw_number, c.bkw_number) for c in circuits],
            occupancy_options=occupancy_options(),
            see_persons=see_persons,
            current_url=request.get_full_path(),
            all_label_open=_("Collapse all"),
            all_label_closed=_("Expand all"),
        )
        return render(request, "management/locations/list.html", context)


class LocationExpandedView(ManagementAccessMixin, View):
    """Remembers which locations the account has expanded. Without JavaScript a button posts
    ``toggle`` (one id) or ``ids`` with ``open`` (all of a page); the script posts JSON with the
    same two keys. Only ids of existing locations are kept."""

    permission_required = (VIEW,)
    http_method_names = ["post"]

    def post(self, request):
        as_json = request.content_type == "application/json"
        try:
            if as_json:
                if len(request.body) > 65536:
                    return HttpResponseBadRequest()
                data = json.loads(request.body)
                ids, open_ = data["ids"], bool(data["open"])
            elif "toggle" in request.POST:
                ids, open_ = [int(request.POST["toggle"])], None
            else:
                ids, open_ = [int(i) for i in request.POST.getlist("ids")], request.POST.get("open") == "1"
            if not isinstance(ids, list) or len(ids) > 500 or any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in ids):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return HttpResponseBadRequest()
        current = set(preferences.get_preference(request.user, "locations").expanded_locations)
        if open_ is None:
            open_ = ids[0] not in current
        current = (current | set(ids)) if open_ else (current - set(ids))
        # Forget locations that no longer exist.
        current = set(Location.objects.filter(pk__in=current).values_list("pk", flat=True))
        try:
            preferences.save_expanded_locations(request.user, current)
        except ValueError:
            return HttpResponseBadRequest()
        if as_json:
            return JsonResponse({"saved": True})
        return redirect(return_url(request, reverse("management:locations")))


class LocationDetailView(ManagementAccessMixin, View):
    """The detail page of a location (``standort-detail-desktop``, ``-handy``, ``standort-neu``,
    ``standort-adresskorrektur``)."""

    permission_required = (VIEW,)

    def get(self, request, pk):
        location = get_object_or_404(Location.objects.select_related("transformer_circuit"), pk=pk)
        user = request.user
        today = timezone.localdate()
        see_persons = user.has_perm(VIEW_PERSONS)
        rows, persons = location_list.details([location], today, see_persons=see_persons)
        points = rows[location.pk]
        can_change = user.has_perm(CHANGE)
        deletable = locations.can_delete(location)
        assignments = []
        if see_persons:
            assignments = list(
                PersonAssignment.objects.filter(metering_point__location=location)
                .select_related("metering_point", "person")
                .order_by("-valid_from", "pk")
            )
        circuit = location.transformer_circuit
        context = {
            "page_title": f"{location.street} {location.house_number}",
            "location": location,
            "points": points,
            "subtitle": " · ".join(
                [circuit_phrase(location), count_phrase(len(points)), occupancy_phrase(len(points), persons[location.pk])]
            ),
            "circuit": circuit,
            "circuit_text": transformer_circuits.label(circuit),
            "circuit_stats": transformer_circuits.stats(circuit) if circuit else None,
            "circuit_options": transformer_circuits.choices(),
            "assignment_history": [
                {
                    "number": a.metering_point.number,
                    "person": location_list.person_name(a.person),
                    "from": a.valid_from,
                    "to": a.valid_to,
                }
                for a in assignments
            ],
            "history": [
                {"time": e.created_at, "label": event_label(e.event), "actor": e.actor.display_name if e.actor_id else ""}
                for e in history.entries_for(location).order_by("-created_at", "-pk")
            ],
            "back_url": return_url(request, reverse("management:locations")),
            "can_change": can_change,
            "can_add_point": user.has_perm("community.add_meteringpoint"),
            "can_edit_circuit": user.has_perm("community.change_transformercircuit"),
            "can_add_circuit": can_change and user.has_perm("community.add_transformercircuit"),
            "show_delete": deletable and user.has_perm(DELETE),
            "show_archive": can_change and not (deletable and user.has_perm(DELETE)),
            "suggestion_text": "",
            "see_persons": see_persons,
            "current_url": request.get_full_path(),
        }
        if can_change and location.address_correction_open and location.suggestion is not None:
            from leg_platform.community import addressing

            context["suggestion_text"] = addressing.suggestion_banner_text(location.suggestion)
        return render(request, "management/locations/detail.html", context)


class DialogView(ManagementAccessMixin, View):
    """A dialog as a page: GET shows the form, POST saves and returns to where the person came from.
    A subclass sets the permissions, the title and the form and implements ``save``."""

    template_name = "management/dialog_form.html"
    form_class = None
    title = ""
    lead = ""
    submit_label = ""
    default_back = ""

    def get_form(self, data=None):
        return self.form_class(data)

    def back(self, request):
        return return_url(request, self.default_back or reverse("management:locations"))

    def get_lead(self):
        return self.lead

    def get_submit_label(self, form):
        return self.submit_label

    def render_dialog(self, request, form, **extra):
        context = {
            "page_title": self.title,
            "dialog_lead": self.get_lead(),
            "submit_label": self.get_submit_label(form),
            "back_url": self.back(request),
            "form": form,
            **extra,
        }
        return render(request, self.template_name, context)

    def get(self, request, **kwargs):
        return self.render_dialog(request, self.get_form())

    def post(self, request, **kwargs):
        form = self.get_form(request.POST)
        if form.is_valid():
            response = self.save(request, form)
            if response is not None:
                return response
        return self.render_dialog(request, form)

    def save(self, request, form):
        raise NotImplementedError


class LocationCreateView(DialogView):
    """"Standort anlegen" (``dialog-standort-anlegen``)."""

    permission_required = (ADD,)
    title = gettext_lazy("Create location")
    lead = gettext_lazy("A location is an address. You create the metering points afterwards at the location.")
    form_class = LocationCreateForm
    template_name = "management/locations/dialog_create.html"

    def get_form(self, data=None):
        initial = {}
        circuit_id = self.request.GET.get("circuit", "")
        if circuit_id.isdigit():
            circuit = TransformerCircuit.objects.filter(pk=int(circuit_id)).first()
            if circuit is not None:
                initial["transformer_circuit"] = transformer_circuits.label(circuit)
        return LocationCreateForm(data, initial=initial)

    def render_dialog(self, request, form, **extra):
        duplicate = form.duplicate if request.user.has_perm(VIEW) else None
        summary = None
        if duplicate is not None:
            summary = {
                "address": f"{duplicate.street} {duplicate.house_number}, {duplicate.postal_code} {duplicate.town}",
                "count": duplicate.metering_points.filter(is_archived=False).count(),
                "url": reverse("management:locations_detail", args=[duplicate.pk]),
            }
        can_add_circuit = request.user.has_perm("community.add_transformercircuit")
        return super().render_dialog(
            request, form, duplicate=summary, circuit_options=transformer_circuits.choices(),
            new_circuit_url=with_next(reverse("management:circuits_new"), request) if can_add_circuit else "",
            **extra,
        )

    def save(self, request, form):
        try:
            locations.create_location(
                **form.address(), address_addition=form.cleaned_data["address_addition"],
                transformer_circuit=form.cleaned_data["transformer_circuit"],
                remark=form.cleaned_data["remark"], actor=request.user,
            )
        except ValidationError:
            form.add_error(None, _("This address already exists."))
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LocationObjectDialog(DialogView):
    """A dialog about one location."""

    def location(self):
        return get_object_or_404(Location.objects.select_related("transformer_circuit"), pk=self.kwargs["pk"])

    @property
    def default_back(self):
        return reverse("management:locations_detail", args=[self.kwargs["pk"]])


class LocationAddressView(LocationObjectDialog):
    """"Adresse bearbeiten": only the address can be changed. If it becomes the address of another
    location the page warns and asks again (the merge)."""

    permission_required = (CHANGE,)
    title = gettext_lazy("Edit address")
    form_class = AddressEditForm
    template_name = "management/locations/dialog_address.html"

    def get_form(self, data=None):
        location = self.location()
        initial = {
            "street": location.street, "house_number": location.house_number,
            "address_addition": location.address_addition, "postal_code": location.postal_code, "town": location.town,
        }
        return AddressEditForm(data, initial=initial, location=location)

    def get_submit_label(self, form):
        return _("Save")

    def post(self, request, **kwargs):
        form = self.get_form(request.POST)
        if not form.is_valid():
            return self.render_dialog(request, form)
        location = self.location()
        confirmed = request.POST.get("confirm_merge") == "1"
        if form.duplicate is not None and confirmed and not request.user.has_perm(DELETE):
            # A merge deletes this location: it needs the right to delete.
            raise PermissionDenied
        try:
            saved = locations.update_address(
                location, **form.address(), address_addition=form.cleaned_data["address_addition"],
                actor=request.user, merge=confirmed,
            )
        except locations.MergeNeeded as needed:
            if not request.user.has_perm(DELETE):
                form.add_error(None, _("This address already exists."))
                return self.render_dialog(request, form)
            data = request.POST.copy()
            data["confirm_merge"] = "1"
            target = needed.target
            return self.render_dialog(
                request, self.form_class(data, location=location),
                submit_label=_("Transfer and save"),
                merge={
                    "address": f"{target.street} {target.house_number}, {target.postal_code} {target.town}",
                    "count": needed.metering_points,
                },
            )
        messages.success(request, _("Saved."))
        if saved.pk != location.pk:
            return redirect(reverse("management:locations_detail", args=[saved.pk]))
        return redirect(self.back(request))


class LocationRemarkView(LocationObjectDialog):
    permission_required = (CHANGE,)
    title = gettext_lazy("Edit remark")
    form_class = RemarkForm

    def get_form(self, data=None):
        return RemarkForm(data, initial={"remark": self.location().remark})

    def save(self, request, form):
        location = self.location()
        location.remark = form.cleaned_data["remark"].strip()
        location.save(update_fields=["remark", "updated_at"])
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class CircuitDialog(DialogView):
    """The dialog "Trafokreis anlegen": the BKW number and the name. If the number exists, the first
    answer is the hint "This transformer circuit exists already" with the button "Use this one"
    (the existing circuit is used; a second one is never created); the second answer (``use_existing``)
    uses it."""

    form_class = CircuitForm
    template_name = "management/locations/dialog_circuit.html"
    title = gettext_lazy("Create transformer circuit")

    def get_form(self, data=None):
        return CircuitForm(data)

    def get_submit_label(self, form):
        return _("Use this one") if form.is_bound and form.existing is not None else _("Save")

    def use(self, request, circuit):
        raise NotImplementedError

    def post(self, request, **kwargs):
        form = self.get_form(request.POST)
        if form.is_valid():
            if form.existing is None:
                circuit = transformer_circuits.create(form.cleaned_data["bkw_number"], form.cleaned_data["name"])
                return self.use(request, circuit)
            if request.POST.get("use_existing") == "1":
                return self.use(request, form.existing)
        return self.render_dialog(request, form)

    def render_dialog(self, request, form, **extra):
        existing = form.existing if form.is_bound and form.is_valid() else None
        return super().render_dialog(request, form, existing=existing, **extra)


class LocationCircuitNewView(CircuitDialog, LocationObjectDialog):
    """"Trafokreis anlegen" at a location (``dialog-trafokreis-anlegen``): the location gets the
    circuit."""

    permission_required = (CHANGE, "community.add_transformercircuit")

    def get_lead(self):
        location = self.location()
        return _("The location %(address)s gets this transformer circuit.") % {
            "address": f"{location.street} {location.house_number}"
        }

    def use(self, request, circuit):
        locations.set_circuit(self.location(), circuit, request.user)
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class CircuitCreateView(CircuitDialog):
    """"Trafokreis anlegen" from the dialog "Standort anlegen" (no location yet): the circuit is
    created and the person comes back with it chosen."""

    permission_required = ("community.add_transformercircuit",)

    def use(self, request, circuit):
        back = self.back(request)
        return redirect(f"{back}{'&' if '?' in back else '?'}circuit={circuit.pk}")


class CircuitEditView(DialogView):
    """Change the name or the number of a circuit; it reaches every location with it, and the
    dialog says how many."""

    permission_required = ("community.change_transformercircuit",)
    title = gettext_lazy("Change transformer circuit")
    form_class = CircuitForm
    template_name = "management/dialog_form.html"

    def circuit(self):
        return get_object_or_404(TransformerCircuit, pk=self.kwargs["pk"])

    def get_form(self, data=None):
        circuit = self.circuit()
        return CircuitForm(data, circuit=circuit, initial={"bkw_number": circuit.bkw_number, "name": circuit.name})

    def get_lead(self):
        count = transformer_circuits.affected_locations(self.circuit())
        if count == 1:
            return _("The change reaches 1 location.")
        return _("The change reaches %(count)d locations.") % {"count": count}

    def save(self, request, form):
        if form.existing is not None:
            form.add_error("bkw_number", _("This transformer circuit already exists."))
            return None
        transformer_circuits.update(self.circuit(), form.cleaned_data["bkw_number"], form.cleaned_data["name"])
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LocationMeteringPointNewView(LocationObjectDialog):
    """"Messpunkt anlegen" at a location (``dialog-messpunkt-anlegen``): the location is given;
    direction required, number and LEG optional, no person and no date."""

    permission_required = (VIEW, "community.add_meteringpoint")
    title = gettext_lazy("Create metering point")
    template_name = "management/locations/dialog_metering_point.html"

    def get_form(self, data=None):
        return MeteringPointForm(data, may_choose_leg=self.request.user.has_perm("community.view_leg"))

    def get_lead(self):
        location = self.location()
        circuit = f", {location.transformer_circuit.bkw_number}" if location.transformer_circuit_id else ""
        return _("At the location %(address)s%(circuit)s.") % {
            "address": f"{location.street} {location.house_number}", "circuit": circuit,
        }

    def save(self, request, form):
        try:
            metering_points.create_metering_point(
                direction=form.cleaned_data["direction"], location=self.location(), number=form.number,
                leg=form.cleaned_data.get("leg"), actor=request.user,
            )
        except metering_points.DuplicateNumber as duplicate:
            # The page shows the existing metering point (the notice of the dialog).
            form.duplicate = duplicate.existing
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))

    def render_dialog(self, request, form, **extra):
        duplicate = getattr(form, "duplicate", None)
        summary = duplicate_summary(duplicate) if duplicate is not None else None
        return super().render_dialog(request, form, duplicate=summary, **extra)


class LocationPostView(ManagementAccessMixin, View):
    """A button of the detail page: POST only, then back to the page."""

    http_method_names = ["post"]

    def post(self, request, pk):
        location = get_object_or_404(Location, pk=pk)
        self.act(request, location)
        return redirect(return_url(request, reverse("management:locations_detail", args=[pk])))

    def act(self, request, location):
        raise NotImplementedError


class LocationCircuitView(LocationPostView):
    """The field with the pencil in the card "Netz": the typed or chosen circuit is assigned; an
    empty field removes it."""

    permission_required = (CHANGE,)

    def act(self, request, location):
        try:
            circuit = CircuitChoiceField().clean(request.POST.get("transformer_circuit", ""))
        except ValidationError as error:
            messages.error(request, error.messages[0])
            return
        locations.set_circuit(location, circuit, request.user)
        messages.success(request, _("Saved."))


class LocationArchiveView(LocationPostView):
    permission_required = (CHANGE,)

    def act(self, request, location):
        archive = request.POST.get("archive") != "0"
        locations.set_archived(location, archive, request.user)
        messages.success(request, _("Saved."))


class LocationConfirmView(LocationPostView):
    permission_required = (CHANGE,)

    def act(self, request, location):
        locations.confirm(location, request.user)
        messages.success(request, _("Saved."))


class LocationSuggestionView(LocationPostView):
    """"Accept" and "Do not accept" of the banner (``standorte/adresskorrektur``)."""

    permission_required = (CHANGE,)

    def act(self, request, location):
        action = request.POST.get("action")
        if action not in ("accept", "dismiss"):
            raise SuspiciousOperation("Unknown action.")
        try:
            if action == "accept":
                locations.accept_suggestion(location, request.user)
            else:
                locations.dismiss_suggestion(location, request.user)
        except locations.SuggestionError as error:
            messages.error(
                request,
                _("This address already exists.") if error.code == "address_in_use" else _("There is no suggestion to decide on."),
            )
            return
        messages.success(request, _("Saved."))


class LocationDeleteView(ManagementAccessMixin, View):
    """"Standort löschen": a question first, then the deletion; only possible without a history."""

    permission_required = (DELETE,)

    def location(self, pk):
        return get_object_or_404(Location, pk=pk)

    def render_question(self, request, pk, location):
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Delete location"),
                "dialog_lead": _("The location %(address)s is deleted for good.") % {"address": f"{location.street} {location.house_number}"},
                "submit_label": _("Delete location"),
                "back_url": return_url(request, reverse("management:locations_detail", args=[pk])),
                "form": None,
                "danger": True,
            },
        )

    def get(self, request, pk):
        location = self.location(pk)
        if not locations.can_delete(location):
            raise Http404
        return self.render_question(request, pk, location)

    def post(self, request, pk):
        location = self.location(pk)
        try:
            locations.delete(location)
        except locations.NotDeletable:
            messages.error(request, _("This location has a history and cannot be deleted. Archive it instead."))
            return redirect(reverse("management:locations_detail", args=[pk]))
        messages.success(request, _("Deleted."))
        return redirect(reverse("management:locations"))
