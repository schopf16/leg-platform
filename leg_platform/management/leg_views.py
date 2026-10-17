"""The LEG pages (D-080): list, detail with its metering points, and the dialogs (create, quota, master data,
archive, delete). Rules live in ``leg_platform.community`` (``legs``, ``leg_list``, ``leg_services``).

Every staff account may use every page (D-086); an archived LEG is read-only (D-085).
"""

from dataclasses import dataclass

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import assignments, grid_operator, leg_list, leg_services, legs, preferences, problems
from leg_platform.community.models import LEGAL_MINIMUM_QUOTA_PERCENT, Leg, ManagementSettings, TransformerCircuit
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.leg_forms import LegCreateForm, LegPointForm, MasterDataForm, QuotaForm
from leg_platform.management.location_views import DialogView, count_phrase
from leg_platform.management.metering_point_forms import ASSIGNMENT_ERRORS
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.returns import created_redirect, return_url, with_next

QUOTA_PROBLEMS = (legs.LegProblem.QUOTA_BELOW_LEGAL, legs.LegProblem.QUOTA_BELOW_WARNING, legs.LegProblem.QUOTA_STALE)
RATIO_PROBLEMS = (legs.LegProblem.NO_PRODUCER, legs.LegProblem.NO_CONSUMER)

TIER_TONES = {legs.DiscountTier.OPTIMISED: "ok", legs.DiscountTier.BASIC: "warn"}


def list_columns(threshold):
    """The list columns; the last one takes the rest because the link column is fixed."""
    return (
        lists.Column(
            "name", gettext_lazy("LEG"), width=280,
            info=_("Name of the LEG. A LEG is a virtual grid association."),
        ),
        lists.Column(
            "quota", gettext_lazy("Production quota"), width=200,
            info=_(
                "Value from the %(operator)s LEG portal. At least %(minimum)s are required by law. "
                "Below %(threshold)s a warning triangle is shown."
            ) % {
                "operator": grid_operator.name(), "minimum": f"{LEGAL_MINIMUM_QUOTA_PERCENT} %",
                "threshold": f"{threshold} %",
            },
        ),
        lists.Column(
            "participants", gettext_lazy("Participants"), width=150,
            info=_("Ratio of consumers to producers."),
        ),
        lists.Column(
            "tier", gettext_lazy("Discount tier"), width=170, info_template="management/legs/_tip_tier.html",
        ),
        lists.Column(
            "points", gettext_lazy("Metering points"), info_placement="left",
            info=_("Number of metering points that currently belong to the LEG."),
        ),
    )


def member_columns():
    """The columns of "Metering points of this LEG"; the action column is fixed (170 px) and has no heading."""
    return (
        lists.Column("number", gettext_lazy("Metering point"), width=400, keep=True),
        lists.Column("address", gettext_lazy("Address"), width=150),
        lists.Column("circuit", gettext_lazy("Transformer circuit"), width=100),
        lists.Column("status", gettext_lazy("Status"), width=130),
        lists.Column("occupant", gettext_lazy("Occupied by"), width=120),
    )


def problem_text(summary, kinds, threshold):
    """The texts of the problems of the given kinds as one text (the warning triangle hint)."""
    return " ".join(
        str(problems.describe(problem, threshold)) for problem in summary.problems if problem in kinds
    )


def display_options():
    return [(leg_list.DISPLAY_ALL, _("All")), (leg_list.DISPLAY_ARCHIVED, _("archived"))]


@dataclass
class LegList:
    """Every row of the list as the address asks for it (search, display, problem switch applied, sorted), not
    one page; the page and the printing use it."""

    state: lists.ListState
    columns: tuple
    threshold: int
    today: object
    problem_count: int
    rows: list


def build_list(params):
    today = timezone.localdate()
    threshold = ManagementSettings.load().warning_threshold_percent
    columns = list_columns(threshold)
    state = lists.parse(
        params, path=reverse("management:legs"), columns=columns, default_sort=leg_list.DEFAULT_SORT,
        filters={"display": (leg_list.DISPLAYS, leg_list.DISPLAY_ALL)},
    )
    queryset = leg_list.apply_search(leg_list.base_queryset(state.values["display"]), state.query, today)
    rows = leg_list.rows_for(queryset, threshold, today)
    # The button counts the LEGs with a problem in the current view.
    problem_count = len(leg_list.apply_problems(rows))
    if state.problems:
        rows = leg_list.apply_problems(rows)
    rows = leg_list.sort_rows(rows, state.sort, state.descending)
    return LegList(state, columns, threshold, today, problem_count, rows)


def list_entry(row, threshold):
    """What the list shows of one LEG (shared by the template and the printing)."""
    summary = row.summary
    return {
        "leg": row.leg,
        "summary": summary,
        "quota_text": legs.format_quota(row.leg.production_quota_percent),
        "quota_problem": problem_text(summary, QUOTA_PROBLEMS, threshold),
        "ratio_problem": problem_text(summary, RATIO_PROBLEMS, threshold),
        "all_problems": problem_text(summary, legs.LegProblem.values, threshold),
        "has_tier": summary.tier != legs.DiscountTier.NONE,
        "tier_tone": TIER_TONES.get(summary.tier, ""),
        "tier_label": legs.DiscountTier(summary.tier).label,
        "detail_url": reverse("management:legs_detail", args=[row.leg.pk]),
    }


class LegListView(ManagementAccessMixin, View):
    def get(self, request):
        found = build_list(request.GET)
        state, columns, threshold, problem_count = found.state, found.columns, found.threshold, found.problem_count
        page = lists.paginate(lists.ListRows(found.rows), state)
        entries = [list_entry(row, threshold) for row in page.items]
        context = lists.list_context(
            request, "legs", columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d LEGs") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["legs"]),
        )
        context.update(
            page_title=_("LEGs"),
            page_subtitle=_("The local electricity communities."),
            entries=entries,
            print_url=state.url_to(reverse("management:legs_print")),
            problem_count=problem_count,
            problem_url=state.url(problems="" if state.problems else "1"),
            create_url=with_next(reverse("management:legs_new"), request),
            display_options=display_options(),
            empty_text=_("No LEGs yet. Create the first LEG."),
        )
        return render(request, "management/legs/list.html", context)


class LegDetailView(ManagementAccessMixin, View):
    def get(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        today = timezone.localdate()
        threshold = ManagementSettings.load().warning_threshold_percent
        summary = legs.summaries([leg], threshold, today)[leg.pk]
        # An archived LEG is read-only (D-085): only "Restore LEG", even if it could be deleted.
        can_change = not leg.is_archived
        show_delete = leg_services.can_delete(leg) and can_change
        level = legs.quota_level(leg.production_quota_percent, threshold)
        circuits = list(
            TransformerCircuit.objects.filter(pk__in=summary.circuit_ids).order_by("grid_number")
            .values_list("grid_number", flat=True)
        )
        context = {
            "page_title": leg.name,
            "leg": leg,
            "summary": summary,
            "count_text": count_phrase(summary.metering_points),
            "problems": [str(problems.describe(problem, threshold)) for problem in summary.problems],
            "quota_text": legs.format_quota(leg.production_quota_percent),
            "checked_on": format_date(leg.quota_checked_on) if leg.quota_checked_on else "",
            "quota_level": level,
            "quota_level_text": legs.quota_level_text(level, threshold) if level else "",
            "legal_minimum": f"{LEGAL_MINIMUM_QUOTA_PERCENT} %",
            "has_tier": summary.tier != legs.DiscountTier.NONE,
            "tier_tone": TIER_TONES.get(summary.tier, ""),
            "tier_label": legs.DiscountTier(summary.tier).label,
            "circuits": circuits,
            "back_url": return_url(request, reverse("management:legs")),
            "can_change": can_change,
            "show_delete": show_delete,
            "show_archive": leg.is_archived or not show_delete,
            "current_url": request.get_full_path(),
            "show_new": request.GET.get("new") == "1" and can_change,
        }
        context.update(self.members_context(request, leg, summary, threshold, today))
        return render(request, "management/legs/detail.html", context)

    def members_context(self, request, leg, summary, threshold, today):
        user = request.user
        columns = member_columns()
        state = lists.parse(
            request.GET, path=reverse("management:legs_detail", args=[leg.pk]), columns=columns,
            default_sort=leg_list.DEFAULT_MEMBER_SORT,
        )
        members = leg_list.members(leg, summary, threshold, today)
        occupants = leg_list.occupants([m.point for m in members], today)
        members = leg_list.sort_members(members, state.sort, state.descending, occupants)
        rows = []
        for member in members:
            point = member.point
            rows.append(
                {
                    "member": member,
                    "point": point,
                    "address": f"{point.location.street} {point.location.house_number}" if point.location_id else "",
                    "location_url": reverse("management:locations_detail", args=[point.location_id])
                    if point.location_id else "",
                    "occupant": occupants.get(point.pk, ""),
                    "has_person": point.pk in occupants,
                    "point_url": reverse("management:metering_points_detail", args=[point.pk]),
                    "switch_url": with_next(reverse("management:metering_points_leg", args=[point.pk]), request),
                }
            )
        saved = preferences.get_preference(user, "leg_metering_points").column_widths
        return {
            "member_rows": rows,
            "member_columns": lists.header_cells(columns, state, saved, "leg_metering_points"),
            "member_widths_url": reverse("management:list_widths", args=["leg_metering_points"]),
            "show_legend": any(row["member"].action for row in rows),
            "empty_members_text": _("No metering points belong to this LEG yet."),
        }


class LegCreateView(DialogView):
    """Create a LEG; afterwards its page opens with the next step (D-095)."""

    title = gettext_lazy("Create LEG")
    lead = gettext_lazy("A LEG is a virtual grid association. You assign metering points to the LEG afterwards.")
    submit_label = gettext_lazy("Create LEG")
    form_class = LegCreateForm
    template_name = "management/legs/dialog_quota.html"

    @property
    def default_back(self):
        return reverse("management:legs")

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg = leg_services.create_leg(
                data["name"], data["quota"], data["checked_on"], data["remark"], actor=request.user
            )
        except leg_services.DuplicateName:
            form.add_error("name", _("This name is already in use."))
            return None
        return created_redirect(
            request, "management:legs", self.back(request), reverse("management:legs_detail", args=[leg.pk]), _("Saved.")
        )


def archived_refusal(request, pk):
    """Redirect with a message if the LEG is archived (D-085, D-079), else None; for GET and direct POST alike."""
    if get_object_or_404(Leg, pk=pk).is_archived:
        messages.error(request, _("This LEG is archived."))
        return redirect(return_url(request, reverse("management:legs_detail", args=[pk])))
    return None


class LegObjectDialog(DialogView):
    """A dialog about one LEG; refused for an archived LEG."""

    template_name = "management/legs/dialog_quota.html"

    def refusal(self, request, *args, **kwargs):
        return archived_refusal(request, kwargs["pk"])

    def leg(self):
        if not hasattr(self, "_leg"):
            self._leg = get_object_or_404(Leg, pk=self.kwargs["pk"])
        return self._leg

    @property
    def default_back(self):
        return reverse("management:legs_detail", args=[self.kwargs["pk"]])


class LegQuotaView(LegObjectDialog):
    """Edit the quota and its date together."""

    title = gettext_lazy("Edit production quota")
    lead = grid_operator.text(gettext_lazy(
        "The quota is in the %(operator)s LEG portal. Enter the value and the date on which you checked it there."
    ))

    def get_form(self, data=None):
        leg = self.leg()
        return QuotaForm(
            data,
            initial={"quota": legs.quota_number(leg.production_quota_percent), "checked_on": leg.quota_checked_on},
        )

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg_services.update_quota(self.leg(), data["quota"], data["checked_on"], actor=request.user)
        except leg_services.Archived:
            messages.error(request, _("This LEG is archived."))
            return redirect(reverse("management:legs_detail", args=[self.kwargs["pk"]]))
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegMasterDataView(LegObjectDialog):
    """Edit name and remark."""

    title = gettext_lazy("Edit master data")
    lead = gettext_lazy("Name and remark of the LEG. The remark is internal only.")
    template_name = "management/dialog_form.html"

    def get_form(self, data=None):
        leg = self.leg()
        return MasterDataForm(data, leg=leg, initial={"name": leg.name, "remark": leg.remark})

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg_services.update_master_data(self.leg(), data["name"], data["remark"], actor=request.user)
        except leg_services.DuplicateName:
            form.add_error("name", _("This name is already in use."))
            return None
        except leg_services.Archived:
            messages.error(request, _("This LEG is archived."))
            return redirect(reverse("management:legs_detail", args=[self.kwargs["pk"]]))
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegPointAssignView(LegObjectDialog):
    """"Assign metering point" on the page of a LEG (D-095): a metering point without a LEG joins this LEG from a
    day. One at a time; moving a point from another LEG is "Change LEG" on the point."""

    title = gettext_lazy("Assign metering point")
    submit_label = gettext_lazy("Assign metering point")
    template_name = "management/legs/dialog_assign.html"

    def get_form(self, data=None):
        return LegPointForm(data)

    def get_lead(self):
        return _("Choose a metering point that has no LEG yet. It is assigned to the LEG “%(name)s”.") % {"name": self.leg().name}

    def render_dialog(self, request, form, **extra):
        return super().render_dialog(request, form, empty=form.empty, no_submit=form.empty, **extra)

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg_services.assign_metering_point(self.leg(), data["metering_point"], data["valid_from"], request.user)
        except assignments.AssignmentError as error:
            if error.code == "leg_archived":
                return archived_refusal(request, self.kwargs["pk"])
            if error.code == "leg_missing":
                raise Http404 from None
            form.add_error(
                "valid_from" if error.code == "starts_not_after_previous" else "metering_point",
                ASSIGNMENT_ERRORS.get(error.code, _("This change is not possible.")),
            )
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegArchiveView(ManagementAccessMixin, View):
    """Archive (or restore) button: POST only."""

    http_method_names = ["post"]

    def post(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        leg_services.set_archived(leg, request.POST.get("archive") != "0", request.user)
        messages.success(request, _("Saved."))
        return redirect(return_url(request, reverse("management:legs_detail", args=[pk])))


class LegDeleteView(ManagementAccessMixin, View):
    """Confirmation, then deletion; only without a history and never for an archived LEG (D-085, D-086)."""

    def refusal(self, request, *args, **kwargs):
        return archived_refusal(request, kwargs["pk"])

    def get(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        if not leg_services.can_delete(leg):
            raise Http404
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Delete LEG"),
                "dialog_lead": _("The LEG %(name)s is deleted for good.") % {"name": leg.name},
                "submit_label": _("Delete LEG"),
                "back_url": return_url(request, reverse("management:legs_detail", args=[pk])),
                "form": None,
                "danger": True,
            },
        )

    def post(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        try:
            leg_services.delete(leg)
        except leg_services.Archived:
            messages.error(request, _("This LEG is archived."))
            return redirect(reverse("management:legs_detail", args=[pk]))
        except leg_services.NotDeletable:
            messages.error(request, _("This LEG has a history and cannot be deleted. Archive it instead."))
            return redirect(reverse("management:legs_detail", args=[pk]))
        messages.success(request, _("Deleted."))
        return redirect(reverse("management:legs"))
