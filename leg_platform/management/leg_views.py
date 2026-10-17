"""The pages of the LEGs (D-080; LEG-Design-Vorgaben/verwaltung/legs).

The list (a table on a desktop, cards on a phone), the detail page with its table of the metering points,
and the dialogs: create, edit the production quota, edit the master data, archive and delete. Every view
uses the access check of ``management.access``; objects are looked up by the id in the address only after
the permission is checked, and the rules are in ``leg_platform.community`` (``legs``, ``leg_list``,
``leg_services``). A dialog is a normal page: it works without JavaScript and returns to the place it was
opened from.

Rights: list and detail = ``view_leg``; create = ``add_leg``; edit, archive and restore = ``change_leg``;
delete = ``delete_leg``. On the detail page the table of the metering points needs ``view_meteringpoint``,
the link to a location ``view_location``, the name of the person ``view_personrecord`` and the icon that
changes the LEG ``change_meteringpoint`` (it opens the shared dialog of the metering points,
``management:metering_points_leg``, which needs ``view_leg`` too, which every page here has).
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

from leg_platform.community import leg_list, leg_services, legs, preferences, problems
from leg_platform.community.models import LEGAL_MINIMUM_QUOTA_PERCENT, Leg, ManagementSettings, TransformerCircuit
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.leg_forms import LegCreateForm, MasterDataForm, QuotaForm
from leg_platform.management.location_views import DialogView, count_phrase
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.returns import return_url, with_next

VIEW = "community.view_leg"
ADD = "community.add_leg"
CHANGE = "community.change_leg"
DELETE = "community.delete_leg"
VIEW_POINTS = "community.view_meteringpoint"
CHANGE_POINTS = "community.change_meteringpoint"
VIEW_LOCATION = "community.view_location"
VIEW_PERSONS = "community.view_personrecord"

QUOTA_PROBLEMS = (legs.LegProblem.QUOTA_BELOW_LEGAL, legs.LegProblem.QUOTA_BELOW_WARNING, legs.LegProblem.QUOTA_STALE)
RATIO_PROBLEMS = (legs.LegProblem.NO_PRODUCER, legs.LegProblem.NO_CONSUMER)

TIER_TONES = {legs.DiscountTier.OPTIMISED: "ok", legs.DiscountTier.BASIC: "warn"}


def list_columns(threshold):
    """The columns of the list (the widths of ``leg-liste-desktop``; the last one takes what is left, as in the
    other lists, because the link column is fixed)."""
    return (
        lists.Column(
            "name", gettext_lazy("LEG"), width=280,
            info=_("Name of the LEG. A LEG is a virtual grid association."),
        ),
        lists.Column(
            "quota", gettext_lazy("Production quota"), width=200,
            info=_(
                "Value from the BKW LEG portal. At least %(minimum)s are required by law. "
                "Below %(threshold)s a warning triangle is shown."
            ) % {"minimum": f"{LEGAL_MINIMUM_QUOTA_PERCENT} %", "threshold": f"{threshold} %"},
        ),
        lists.Column(
            "participants", gettext_lazy("Participants"), width=150,
            info=_("Ratio of consumers to producers."),
        ),
        lists.Column(
            "tier", gettext_lazy("Discount tier"), width=170, info_template="management/legs/_tip_tier.html",
        ),
        lists.Column(
            "points", gettext_lazy("Metering points"), resizable=False, info_placement="left",
            info=_("Number of metering points that currently belong to the LEG."),
        ),
    )


def member_columns():
    """The columns of the table "Metering points of this LEG" (the widths of the reference). The column of
    the action is fixed (170 px) and has no heading."""
    return (
        lists.Column("number", gettext_lazy("Metering point"), width=400),
        lists.Column("address", gettext_lazy("Address"), width=150),
        lists.Column("circuit", gettext_lazy("Transformer circuit"), width=100),
        lists.Column("status", gettext_lazy("Status"), width=130),
        lists.Column("occupant", gettext_lazy("Occupied by"), width=120),
    )


def problem_text(summary, kinds, threshold):
    """The texts of the problems of the given kinds as one text (the hint of the warning triangle)."""
    return " ".join(
        str(problems.describe(problem, threshold)) for problem in summary.problems if problem in kinds
    )


def display_options():
    return [(leg_list.DISPLAY_ALL, _("All")), (leg_list.DISPLAY_ARCHIVED, _("archived"))]


@dataclass
class LegList:
    """The list of LEGs as the address asks for it: the state, and every row of the view (the search, the
    display and the problem switch applied, sorted), not only one page. The page and the printing use it."""

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
    # The button counts the LEGs with a problem in the current view (the search and the display).
    problem_count = len(leg_list.apply_problems(rows))
    if state.problems:
        rows = leg_list.apply_problems(rows)
    rows = leg_list.sort_rows(rows, state.sort, state.descending)
    return LegList(state, columns, threshold, today, problem_count, rows)


def list_entry(row, threshold):
    """What the list shows of one LEG (the template and the printing read the same texts)."""
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
    """The list (``leg-liste-desktop`` and ``leg-liste-handy``)."""

    permission_required = (VIEW,)

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
            can_add=request.user.has_perm(ADD),
            create_url=with_next(reverse("management:legs_new"), request),
            display_options=display_options(),
            empty_text=_("No LEGs yet. Create the first LEG."),
        )
        return render(request, "management/legs/list.html", context)


class LegDetailView(ManagementAccessMixin, View):
    """The detail page (``leg-detail-desktop``, ``leg-detail-ein-trafokreis-desktop`` and the two phone views)."""

    permission_required = (VIEW,)

    def get(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        user = request.user
        today = timezone.localdate()
        threshold = ManagementSettings.load().warning_threshold_percent
        summary = legs.summaries([leg], threshold, today)[leg.pk]
        can_change = user.has_perm(CHANGE)
        show_delete = leg_services.can_delete(leg) and user.has_perm(DELETE)
        level = legs.quota_level(leg.production_quota_percent, threshold)
        circuits = list(
            TransformerCircuit.objects.filter(pk__in=summary.circuit_ids).order_by("bkw_number")
            .values_list("bkw_number", flat=True)
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
            "show_archive": can_change and not show_delete,
            "current_url": request.get_full_path(),
        }
        if user.has_perm(VIEW_POINTS):
            context.update(self.members_context(request, leg, summary, threshold, today))
        return render(request, "management/legs/detail.html", context)

    def members_context(self, request, leg, summary, threshold, today):
        user = request.user
        see_persons = user.has_perm(VIEW_PERSONS)
        columns = member_columns()
        state = lists.parse(
            request.GET, path=reverse("management:legs_detail", args=[leg.pk]), columns=columns,
            default_sort=leg_list.DEFAULT_MEMBER_SORT,
        )
        members = leg_list.members(leg, summary, threshold, today)
        occupants = leg_list.occupants([m.point for m in members], today, see_persons=see_persons)
        members = leg_list.sort_members(members, state.sort, state.descending, occupants)
        can_switch = user.has_perm(CHANGE_POINTS)
        can_view_location = user.has_perm(VIEW_LOCATION)
        rows = []
        for member in members:
            point = member.point
            rows.append(
                {
                    "member": member,
                    "point": point,
                    "address": f"{point.location.street} {point.location.house_number}" if point.location_id else "",
                    "location_url": reverse("management:locations_detail", args=[point.location_id])
                    if point.location_id and can_view_location else "",
                    "occupant": occupants.get(point.pk, ""),
                    "has_person": point.pk in occupants,
                    "switch_url": with_next(reverse("management:metering_points_leg", args=[point.pk]), request)
                    if can_switch else "",
                }
            )
        saved = preferences.get_preference(user, "leg_metering_points").column_widths
        return {
            "member_rows": rows,
            "member_columns": lists.header_cells(columns, state, saved, "leg_metering_points"),
            "member_widths_url": reverse("management:list_widths", args=["leg_metering_points"]),
            "show_actions": can_switch or any(row["member"].action for row in rows),
            "show_legend": any(row["member"].action for row in rows),
            "empty_members_text": _("No metering points belong to this LEG yet."),
        }


# Dialogs ---------------------------------------------------------------------------------------


class LegCreateView(DialogView):
    """"LEG anlegen" (``leg-anlegen-desktop``): after saving back to the list."""

    permission_required = (VIEW, ADD)
    title = gettext_lazy("Create LEG")
    lead = gettext_lazy("A LEG is a virtual grid association. You assign metering points to the LEG afterwards.")
    form_class = LegCreateForm
    template_name = "management/legs/dialog_quota.html"

    @property
    def default_back(self):
        return reverse("management:legs")

    def save(self, request, form):
        data = form.cleaned_data
        try:
            leg_services.create_leg(
                data["name"], data["quota"], data["checked_on"], data["remark"], actor=request.user
            )
        except leg_services.DuplicateName:
            form.add_error("name", _("This name is already in use."))
            return None
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegObjectDialog(DialogView):
    """A dialog about one LEG."""

    permission_required = (VIEW, CHANGE)
    template_name = "management/legs/dialog_quota.html"

    def leg(self):
        if not hasattr(self, "_leg"):
            self._leg = get_object_or_404(Leg, pk=self.kwargs["pk"])
        return self._leg

    @property
    def default_back(self):
        return reverse("management:legs_detail", args=[self.kwargs["pk"]])


class LegQuotaView(LegObjectDialog):
    """"Produktionsquote bearbeiten" (``leg-quote-bearbeiten-desktop``): the quota and its date together."""

    title = gettext_lazy("Edit production quota")
    lead = gettext_lazy(
        "The quota is in the BKW LEG portal. Enter the value and the date on which you checked it there."
    )

    def get_form(self, data=None):
        leg = self.leg()
        return QuotaForm(
            data,
            initial={"quota": legs.quota_number(leg.production_quota_percent), "checked_on": leg.quota_checked_on},
        )

    def save(self, request, form):
        data = form.cleaned_data
        leg_services.update_quota(self.leg(), data["quota"], data["checked_on"], actor=request.user)
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegMasterDataView(LegObjectDialog):
    """"Stammdaten bearbeiten" (``leg-stammdaten-bearbeiten-desktop``): name and remark."""

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
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class LegArchiveView(ManagementAccessMixin, View):
    """The button "LEG archivieren" (and "Restore LEG" for an archived one): POST only."""

    permission_required = (VIEW, CHANGE)
    http_method_names = ["post"]

    def post(self, request, pk):
        leg = get_object_or_404(Leg, pk=pk)
        leg_services.set_archived(leg, request.POST.get("archive") != "0", request.user)
        messages.success(request, _("Saved."))
        return redirect(return_url(request, reverse("management:legs_detail", args=[pk])))


class LegDeleteView(ManagementAccessMixin, View):
    """"LEG löschen": a question first, then the deletion; only possible without a history."""

    permission_required = (VIEW, DELETE)

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
        except leg_services.NotDeletable:
            messages.error(request, _("This LEG has a history and cannot be deleted. Archive it instead."))
            return redirect(reverse("management:legs_detail", args=[pk]))
        messages.success(request, _("Deleted."))
        return redirect(reverse("management:legs"))
