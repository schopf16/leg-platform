"""The pages of the transformer circuits (D-142): list, detail, "Create transformer circuit", "Rename" and the
suggestions of the circuit field of the location forms. Rules live in ``community.transformer_circuits`` and
``community.circuit_list``. Every staff account may use every page (D-086). A circuit is neither deleted nor merged.
"""

from dataclasses import dataclass

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import circuit_list, grid_operator, history, legs, transformer_circuits
from leg_platform.community.models import Location, ManagementSettings, TransformerCircuit
from leg_platform.management import circuit_forms, lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView, count_phrase, event_label, history_actor
from leg_platform.management.returns import created_redirect, return_url, with_next


def list_columns():
    """The list columns; the last one takes the rest because the link column is fixed."""
    operator = grid_operator.name()
    in_leg = _("Metering points of the transformer circuit that belong to a LEG, of all, for example 3 of 4.")
    return (
        lists.Column("number", gettext_lazy("Transformer circuit"), width=170, keep=True),
        lists.Column("name", gettext_lazy("Name"), width=200),
        lists.Column(
            "source", gettext_lazy("Source"), width=140,
            info=_(
                "%(operator)s: the number is a transformer station in the %(operator)s data of the address directory. "
                "Manual: entered by hand."
            ) % {"operator": operator},
        ),
        lists.Column("locations", gettext_lazy("Locations"), width=130),
        lists.Column("consumption", gettext_lazy("Consumption"), width=150, info=in_leg),
        lists.Column("feed_in", gettext_lazy("Feed-in"), width=150, info=in_leg),
        lists.Column("legs", gettext_lazy("LEGs"), info_placement="left"),
    )


@dataclass
class CircuitList:
    """Every row of the list as the address asks for it (search applied, sorted), not one page; the page and the
    printing use it."""

    state: lists.ListState
    columns: tuple
    today: object
    rows: list


def build_list(params):
    today = timezone.localdate()
    columns = list_columns()
    state = lists.parse(
        params, path=reverse("management:circuits"), columns=columns, default_sort=circuit_list.DEFAULT_SORT
    )
    queryset = circuit_list.apply_search(TransformerCircuit.objects.all(), state.query, today)
    rows = circuit_list.rows_for(queryset, transformer_circuits.directory_numbers(), today)
    rows = circuit_list.sort_rows(rows, state.sort, state.descending)
    return CircuitList(state, columns, today, rows)


def share_text(part, total):
    """"3 of 4" (of these in a LEG), or "" without metering points."""
    return _("%(part)d of %(total)d") % {"part": part, "total": total} if total else ""


def list_entry(row):
    """What the list shows of one circuit (shared by the template and the printing)."""
    return {
        "circuit": row.circuit,
        "row": row,
        "source_text": circuit_forms.source_text(row.source),
        "consumption_text": share_text(row.consumption_in_leg, row.consumption),
        "feed_in_text": share_text(row.feed_in_in_leg, row.feed_in),
        "legs": [{"name": name, "url": reverse("management:legs_detail", args=[pk])} for pk, name in row.legs],
        "detail_url": reverse("management:circuits_detail", args=[row.circuit.pk]),
    }


class CircuitListView(ManagementAccessMixin, View):
    def get(self, request):
        found = build_list(request.GET)
        state = found.state
        page = lists.paginate(lists.ListRows(found.rows), state)
        context = lists.list_context(
            request, "circuits", found.columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d transformer circuits") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["circuits"]),
        )
        context.update(
            page_title=_("Transformer circuits"),
            page_subtitle=_("The transformer circuits with their locations, metering points and LEGs."),
            entries=[list_entry(row) for row in page.items],
            print_url=state.url_to(reverse("management:circuits_print")),
            create_url=with_next(reverse("management:circuits_new"), request),
            empty_text=_("No transformer circuits found.") if state.query else _("No transformer circuits yet."),
        )
        return render(request, "management/circuits/list.html", context)


def circuit_source(circuit):
    return transformer_circuits.source(circuit.grid_number, transformer_circuits.directory_numbers())


class CircuitDetailView(ManagementAccessMixin, View):
    def get(self, request, pk):
        circuit = get_object_or_404(TransformerCircuit, pk=pk)
        today = timezone.localdate()
        row = circuit_list.rows_for(
            TransformerCircuit.objects.filter(pk=pk), transformer_circuits.directory_numbers(), today
        )[0]
        threshold = ManagementSettings.load().warning_threshold_percent
        found_legs = circuit_list.legs_of(circuit, today)
        summaries = legs.summaries([item.leg for item in found_legs], threshold, today)
        context = {
            "page_title": circuit.grid_number,
            "circuit": circuit,
            "row": row,
            "source_text": circuit_forms.source_text(row.source),
            "subtitle": " · ".join(
                part for part in (circuit.name, locations_phrase(row.locations), count_phrase(row.points)) if part
            ),
            "not_in_leg": row.points - row.points_in_leg,
            "locations": [
                {
                    "location": location,
                    "count_phrase": count_phrase(location.mp_count),
                    "url": reverse("management:locations_detail", args=[location.pk]),
                }
                for location in circuit_list.locations_of(circuit)
            ],
            "legs": [
                {
                    "leg": item.leg, "here": item.here, "total": item.total,
                    "tier": legs.DiscountTier(summaries[item.leg.pk].tier).label
                    if summaries[item.leg.pk].tier != legs.DiscountTier.NONE else "",
                    "url": reverse("management:legs_detail", args=[item.leg.pk]),
                }
                for item in found_legs
            ],
            "history": [
                {"time": entry.created_at, "label": event_label(entry.event), "actor": history_actor(entry)}
                for entry in history.entries_for(circuit).order_by("-created_at", "-pk")
            ],
            "back_url": return_url(request, reverse("management:circuits")),
            "current_url": request.get_full_path(),
            "show_new": request.GET.get("new") == "1",
        }
        return render(request, "management/circuits/detail.html", context)


def locations_phrase(count):
    if count == 0:
        return _("no locations")
    return _("1 location") if count == 1 else _("%(count)d locations") % {"count": count}


def number_taken(form):
    """The message "exists already" at the number, or above the fields when the number is a locked row."""
    field = None if form.fields["grid_number"].disabled else "grid_number"
    form.add_error(field, _("This transformer circuit already exists."))


def save_errors(form, error):
    """Show a ``ValidationError`` of the service at the form (a parallel change took the number after the check)."""
    if not hasattr(error, "error_dict"):
        form.add_error(None, _("The data could not be saved."))
        return
    for key, errors in error.error_dict.items():
        if key == "grid_number" and any(item.code == "unique" for item in errors):
            number_taken(form)
        else:
            form.add_error(key if key in form.fields and not form.fields[key].disabled else None, errors)


class CircuitCreateView(DialogView):
    """"Create transformer circuit": the only place where a circuit is created by hand (D-142)."""

    title = gettext_lazy("Create transformer circuit")
    submit_label = gettext_lazy("Create transformer circuit")
    form_class = circuit_forms.CircuitForm

    @property
    def default_back(self):
        return reverse("management:circuits")

    def get_lead(self):
        return _(
            "A transformer circuit of the %(operator)s grid. Afterwards it can be chosen at the locations."
        ) % {"operator": grid_operator.name()}

    def save(self, request, form):
        if form.existing is not None:
            number_taken(form)
            return None
        try:
            circuit = transformer_circuits.create(form.cleaned_data["grid_number"], form.cleaned_data["name"], request.user)
        except ValidationError as error:
            save_errors(form, error)
            return None
        except IntegrityError:
            number_taken(form)
            return None
        return created_redirect(
            request, "management:circuits", self.back(request),
            reverse("management:circuits_detail", args=[circuit.pk]), _("Saved."),
        )


class CircuitEditView(DialogView):
    """"Rename transformer circuit": the name, and the number only for a circuit entered by hand (the number of a
    station of the operator would no longer match the derivation, D-142). It reaches every location with it (the dialog
    says how many)."""

    title = gettext_lazy("Rename transformer circuit")

    def circuit(self):
        if not hasattr(self, "_circuit"):
            self._circuit = get_object_or_404(TransformerCircuit, pk=self.kwargs["pk"])
        return self._circuit

    @property
    def default_back(self):
        return reverse("management:circuits_detail", args=[self.kwargs["pk"]])

    def get_form(self, data=None):
        circuit = self.circuit()
        locked = circuit_source(circuit) != transformer_circuits.SOURCE_MANUAL
        return circuit_forms.CircuitForm(
            data, circuit=circuit, number_locked=locked, initial={"grid_number": circuit.grid_number, "name": circuit.name}
        )

    def get_lead(self):
        count = transformer_circuits.affected_locations(self.circuit())
        if count == 1:
            return _("The change reaches 1 location.")
        return _("The change reaches %(count)d locations.") % {"count": count}

    def save(self, request, form):
        if form.existing is not None:
            number_taken(form)
            return None
        try:
            transformer_circuits.update(
                self.circuit(), form.cleaned_data["grid_number"], form.cleaned_data["name"], request.user
            )
        except ValidationError as error:
            save_errors(form, error)
            return None
        except IntegrityError:
            number_taken(form)
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class CircuitSearchView(ManagementAccessMixin, View):
    """The suggestions of the circuit field of the location forms (``combobox.js``): at most ten texts, the automatic
    choice first (with the derived number of ``location`` when given). No personal data; an empty text lists from the
    start."""

    http_method_names = ["get"]

    def get(self, request):
        location = None
        raw = request.GET.get("location", "")
        if raw and len(raw) <= 12 and raw.isascii() and raw.isdigit():
            location = Location.objects.select_related("transformer_circuit").filter(pk=int(raw)).first()
        return JsonResponse({"results": circuit_forms.suggestions(request.GET.get("q", ""), location)})
