"""The pages of the persons (D-081; LEG-Design-Vorgaben/verwaltung/personen).

The list (a table on a desktop, cards on a phone), the detail page with its table of the metering points, and
the small dialogs: the note, the BKW customer number and the shares of the cooperative, plus the buttons of
the banner for the correction of the address. Every view uses the access check of ``management.access``;
objects are looked up by the id in the address only after the permission is checked, and the rules are in
``leg_platform.community`` (``person_list``, ``persons``). A dialog is a normal page: it works without
JavaScript and returns to the place it was opened from.

Rights: list and detail = ``view_personrecord``; note, BKW customer number, shares = ``change_personrecord``;
the buttons of the address banner = ``correct_person_address``. The table of the metering points of a person
and the column of their number in the list need ``view_meteringpoint`` (the data of the metering points), the
links to a location ``view_location``, to a LEG ``view_leg``, to the metering point ``view_meteringpoint``.
"Person anlegen" and "Anmeldung starten" have no specified effect and are not built (D-081, open points).
"""

from dataclasses import dataclass

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousOperation
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext
from django.views import View

from leg_platform.community import addressing, history, numbers, person_list, persons
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView, event_label
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.person_forms import BkwNumberForm, NoteForm, SharesForm
from leg_platform.management.returns import return_url, with_next

VIEW = "community.view_personrecord"
CHANGE = "community.change_personrecord"
CORRECT = "community.correct_person_address"
VIEW_POINTS = "community.view_meteringpoint"
VIEW_LOCATION = "community.view_location"
VIEW_LEG = "community.view_leg"

STATE_TONES = {
    person_list.AccountState.ACTIVE: "ok",
    person_list.AccountState.BLOCKED: "bad",
    person_list.AccountState.INACTIVE: "muted",
}


def list_columns(see_points):
    """The columns of the list (the start widths of ``liste-desktop``; the last one takes what is left, as in
    the other lists, because the link column is fixed). The column of the metering points needs the right to
    see them."""
    columns = [
        lists.Column(
            "name", gettext_lazy("Name"), width=240,
            info=_("Name of the person, or of the company for property managers and companies."),
        ),
        lists.Column(
            "number", gettext_lazy("Customer number"), width=150,
            info=_("The six-digit number under which the person is kept in the portal."),
        ),
        lists.Column(
            "address", gettext_lazy("Address"), width=240,
            info=_("Street and house number of the billing address."),
        ),
    ]
    if see_points:
        columns.append(
            lists.Column(
                "points", gettext_lazy("Metering points"), width=200,
                info=_(
                    "Number of the metering points of the person. Behind it the state: notified, if all are "
                    "notified to the BKW. Recording, if at least one is still being recorded."
                ),
            )
        )
    columns.append(
        lists.Column(
            "account", gettext_lazy("Account"), resizable=False, info_placement="left",
            info=_(
                "Whether the person can sign in to the portal. Blocked: temporarily blocked, for example after "
                "failed attempts. Inactive: the account is deactivated and is in the archive."
            ),
        )
    )
    return tuple(columns)


def points_text(count, word):
    """The count with the state behind it ("2 · Aufnahme", "12 · angemeldet", "0")."""
    if not count:
        return "0"
    label = (
        pgettext("person metering points", "recording") if word == "registered"
        else pgettext("person metering points", "notified")
    )
    return f"{count} · {label}"


def status_options():
    return [
        (person_list.STATUS_ALL, _("All")),
        (person_list.STATUS_ACTIVE, person_list.AccountState.ACTIVE.label),
        (person_list.STATUS_BLOCKED, person_list.AccountState.BLOCKED.label),
        (person_list.STATUS_ARCHIVED, _("archived")),
    ]


def points_options():
    return [
        (person_list.POINTS_ALL, _("All")),
        (person_list.POINTS_WITH, _("with metering point")),
        (person_list.POINTS_WITHOUT, _("without metering point")),
    ]


@dataclass
class PersonList:
    """The list of persons as the address asks for it: the state and the queryset of every row of the view
    (search and filters applied, sorted), not only one page. The page and the printing use it."""

    state: lists.ListState
    columns: tuple
    see_points: bool
    queryset: object
    locked: set


def build_list(params, user):
    today = timezone.localdate()
    see_points = user.has_perm(VIEW_POINTS)
    columns = list_columns(see_points)
    filters = {"status": (person_list.STATUSES, person_list.STATUS_ALL)}
    if see_points:
        filters["points"] = (person_list.POINTS_FILTERS, person_list.POINTS_ALL)
    state = lists.parse(
        params, path=reverse("management:persons"), columns=columns, default_sort=person_list.DEFAULT_SORT,
        filters=filters,
    )
    locked = person_list.locked_account_ids()
    queryset = person_list.base_queryset(today, locked)
    queryset = person_list.apply_search(queryset, state.query)
    queryset = person_list.apply_status(queryset, state.values["status"])
    if see_points:
        queryset = person_list.apply_points(queryset, state.values["points"])
    queryset = person_list.apply_sort(queryset, state.sort, state.descending)
    return PersonList(state, columns, see_points, queryset, locked)


def list_entry(person, locked):
    """What the list shows of one person (the template and the printing read the same texts)."""
    name, contact = person_list.display_names(person)
    account = person_list.AccountState(person_list.account_state(person, locked))
    return {
        "person": person,
        "name": name,
        "contact": contact,
        "number": person.customer_number_display,
        "address": person.street_address_display,
        "points": points_text(person.point_count, person_list.points_word(person.point_count, person.has_unregistered)),
        "account_label": account.label,
        "account_tone": STATE_TONES[account],
        "detail_url": reverse("management:persons_detail", args=[person.pk]),
    }


class PersonListView(ManagementAccessMixin, View):
    """The list (``liste-desktop`` and ``liste-handy``)."""

    permission_required = (VIEW,)

    def get(self, request):
        found = build_list(request.GET, request.user)
        state, columns, see_points = found.state, found.columns, found.see_points
        page = lists.paginate(found.queryset, state)
        entries = [list_entry(person, found.locked) for person in page.items]
        context = lists.list_context(
            request, "persons", columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d persons") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["persons"]),
        )
        context.update(
            page_title=_("Persons"),
            page_subtitle=_("All customers of the LEG."),
            entries=entries,
            print_url=state.url_to(reverse("management:persons_print")),
            see_points=see_points,
            status_options=status_options(),
            points_options=points_options(),
            empty_text=_("No persons found."),
        )
        return render(request, "management/persons/list.html", context)


# The detail page ---------------------------------------------------------------------------------


def format_iban(value):
    """The IBAN in blocks of four characters ("CH93 0076 2011 6238 5295 7"); empty stays empty."""
    value = "".join((value or "").split())
    return " ".join(value[i : i + 4] for i in range(0, len(value), 4))


def point_columns():
    """The columns of the table of the metering points of a person (``person-detail-desktop``): the same
    texts as the template, without the sentence about 30 days (``personen/detail/VORGABEN.md``)."""
    return (
        lists.Column(
            "location", gettext_lazy("Location"),
            info=_(
                "The location is the address where the metering point is installed. A metering point "
                "belongs to its location for good."
            ),
        ),
        lists.Column(
            "number", gettext_lazy("Metering point"),
            info=_(
                "Number of the smart meter at the BKW. If it is missing, the metering point is recorded "
                "but not yet known at the BKW."
            ),
        ),
        lists.Column(
            "direction", gettext_lazy("Direction"), info_template="management/persons/_tip_direction.html",
        ),
        lists.Column("leg", gettext_lazy("LEG"), info=_("The billing group the metering point belongs to.")),
        lists.Column(
            "assigned", gettext_lazy("Assigned"), info_placement="left",
            info_template="management/persons/_tip_assigned.html",
        ),
        lists.Column(
            "bkw", gettext_lazy("BKW registration"), info_placement="left-up",
            info_template="management/persons/_tip_bkw.html",
        ),
    )


def assignment_lines(assignment):
    """The text of the column "Assigned": "ab DD.MM.YYYY"; an assignment that is ended shows "bis DD.MM.YYYY"
    in a second line (the person leaves, for example with a flying change: both lines one under the other)."""
    lines = [_("from %(date)s") % {"date": format_date(assignment.valid_from)}]
    if assignment.valid_to:
        lines.append(_("until %(date)s") % {"date": format_date(assignment.valid_to)})
    return lines


def bkw_text(row):
    """"registriert", "angemeldet am DD.MM.YYYY" or "bestätigt am DD.MM.YYYY", and the tone of the colour."""
    if row.bkw == person_list.BKW_CONFIRMED:
        return _("confirmed on %(date)s") % {"date": format_date(row.bkw_date)}, "ok"
    if row.bkw == person_list.BKW_NOTIFIED:
        return _("notified on %(date)s") % {"date": format_date(row.bkw_date)}, "ok"
    return MeteringPointStatus.REGISTERED.label, "warn"


class PersonObjectMixin:
    def person(self, pk=None):
        if not hasattr(self, "_person"):
            self._person = get_object_or_404(
                get_user_model().objects.filter(customer_number__isnull=False), pk=pk or self.kwargs["pk"]
            )
        return self._person


class PersonDetailView(PersonObjectMixin, ManagementAccessMixin, View):
    """The detail page (``person-detail-desktop`` and ``-handy``)."""

    permission_required = (VIEW,)

    def get(self, request, pk):
        person = self.person(pk)
        user = request.user
        today = timezone.localdate()
        record = persons.record_of(person)
        name, contact = person_list.display_names(person)
        locked = person_list.locked_account_ids() if person.is_active else set()
        account = person_list.AccountState(person_list.account_state(person, locked))
        can_correct = user.has_perm(CORRECT)
        suggestion = persons.address_suggestion(person) if can_correct else None
        context = {
            "page_title": name,
            "person": person,
            "name": name,
            "contact": contact,
            "number": person.customer_number_display,
            "account_label": account.label,
            "account_tone": STATE_TONES[account],
            "salutation": person.get_salutation_display() if person.salutation else "",
            "phone": person.phone_number_display,
            "street": person.street_address_display,
            "postal_town": f"{person.postal_code_display} {person.town}".strip(),
            "country": str(person.country.name) if person.country else "",
            "iban": format_iban(record.iban) if record else "",
            "paper_invoice": bool(record and record.paper_invoice),
            "bkw_number": record.bkw_customer_number if record else "",
            "note": record.staff_note if record else "",
            "shares": record.shares if record else None,
            "shares_period": self.shares_period(record),
            "suggestion_text": addressing.suggestion_banner_text(suggestion) if suggestion else "",
            "suggestion_token": persons.suggestion_token(suggestion) if suggestion else "",
            "can_change": user.has_perm(CHANGE),
            "history": [
                {"time": entry.created_at, "label": event_label(entry.event), "actor": entry.actor.display_name if entry.actor_id else ""}
                for entry in history.entries_for(person).order_by("-created_at", "-pk")
            ],
            "back_url": return_url(request, reverse("management:persons")),
            "current_url": request.get_full_path(),
        }
        if user.has_perm(VIEW_POINTS):
            context.update(self.points_context(request, person, today))
        return render(request, "management/persons/detail.html", context)

    @staticmethod
    def shares_period(record):
        if record is None or record.shares is None:
            return ""
        if record.shares_to:
            return _("from %(from)s to %(to)s") % {"from": format_date(record.shares_from), "to": format_date(record.shares_to)}
        return _("from %(date)s") % {"date": format_date(record.shares_from)}

    def points_context(self, request, person, today):
        user = request.user
        columns = point_columns()
        state = lists.parse(
            request.GET, path=reverse("management:persons_detail", args=[person.pk]), columns=columns,
            default_sort=person_list.DEFAULT_POINT_SORT,
        )
        rows = person_list.sort_point_rows(person_list.point_rows(person, today), state.sort, state.descending)
        can_view_location = user.has_perm(VIEW_LOCATION)
        can_view_leg = user.has_perm(VIEW_LEG)
        entries = []
        for row in rows:
            text, tone = bkw_text(row)
            entries.append(
                {
                    "row": row,
                    "number": numbers.format_metering_point_number(row.point.number),
                    "assigned": assignment_lines(row.assignment),
                    "bkw_text": text,
                    "bkw_tone": tone,
                    "point_url": reverse("management:metering_points_detail", args=[row.point.pk]),
                    "location_url": reverse("management:locations_detail", args=[row.location_id])
                    if row.location_id and can_view_location else "",
                    "leg_url": reverse("management:legs_detail", args=[row.leg_id]) if row.leg_id and can_view_leg else "",
                }
            )
        return {
            "point_columns": lists.header_cells(columns, state, {}, "person_points"),
            "point_entries": entries,
            "show_points": True,
            "empty_points_text": _("No metering points assigned."),
        }


# The dialogs ---------------------------------------------------------------------------------------


class PersonDialog(PersonObjectMixin, DialogView):
    """A dialog about one person; needs the right to see persons and to change them."""

    permission_required = (VIEW, CHANGE)

    @property
    def default_back(self):
        return reverse("management:persons_detail", args=[self.kwargs["pk"]])

    def get(self, request, **kwargs):
        self.person()
        return super().get(request, **kwargs)

    def post(self, request, **kwargs):
        self.person()
        return super().post(request, **kwargs)

    def finish(self, request):
        messages.success(request, _("Saved."))
        return redirect(self.back(request))


class PersonNoteView(PersonDialog):
    """"Notiz bearbeiten": the note that only the staff see; it is never searched."""

    title = gettext_lazy("Edit note")
    form_class = NoteForm

    def get_form(self, data=None):
        record = persons.record_of(self.person())
        return NoteForm(data, initial={"note": record.staff_note if record else ""})

    def save(self, request, form):
        persons.set_note(self.person(), form.cleaned_data["note"], request.user)
        return self.finish(request)


class PersonBkwNumberView(PersonDialog):
    """The optional BKW customer number (the card "Zahlung und Kundennummern")."""

    title = gettext_lazy("Edit BKW customer number")
    form_class = BkwNumberForm

    def get_form(self, data=None):
        record = persons.record_of(self.person())
        return BkwNumberForm(data, initial={"bkw_customer_number": record.bkw_customer_number if record else ""})

    def save(self, request, form):
        persons.set_bkw_customer_number(self.person(), form.cleaned_data["bkw_customer_number"], request.user)
        return self.finish(request)


class PersonSharesView(PersonDialog):
    """The shares of the cooperative with their period: add, change, end (the end date) or remove."""

    title = gettext_lazy("Edit shares")
    form_class = SharesForm

    def get_form(self, data=None):
        return SharesForm(data, initial=SharesForm.initial_for(persons.record_of(self.person())))

    def save(self, request, form):
        data = form.cleaned_data
        try:
            persons.set_shares(self.person(), data["shares"], data["shares_from"], data["shares_to"], request.user)
        except persons.PersonError:
            form.add_error(None, _("The shares could not be saved."))
            return None
        return self.finish(request)


class PersonAddressSuggestionView(PersonObjectMixin, ManagementAccessMixin, View):
    """"Accept" and "Do not accept" of the banner (``personen/VORGABEN.md``, "Adresskorrektur bei Personen")."""

    permission_required = (VIEW, CORRECT)
    http_method_names = ["post"]

    def post(self, request, pk):
        person = self.person(pk)
        action = request.POST.get("action")
        if action not in ("accept", "dismiss"):
            raise SuspiciousOperation("Unknown action.")
        token = request.POST.get("token", "")
        try:
            if action == "accept":
                persons.accept_address_suggestion(person, token, request.user)
            else:
                persons.dismiss_address_suggestion(person, token, request.user)
        except persons.PersonError:
            messages.error(request, _("There is no suggestion to decide on."))
        else:
            messages.success(request, _("Saved."))
        return redirect(return_url(request, reverse("management:persons_detail", args=[pk])))
