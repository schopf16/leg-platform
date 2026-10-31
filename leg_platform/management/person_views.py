"""The pages of the persons (D-081): list, detail page with its metering point table, and the dialogs (create
person, customer data cards, note, shares, address suggestion banner buttons, resend invitation).

Every view uses ``management.access`` (every staff account, D-086); rules live in ``community.person_list`` and
``community.persons``. The customer data dialogs answer 403 for an account the actor may not change
(``persons.may_change_customer_data``); the service checks it again. Dialogs are normal pages (no JavaScript needed).
"""

from dataclasses import dataclass

from django.contrib import messages
from django.db import transaction
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext
from django.views import View

from leg_platform import render_as
from leg_platform.accounts.models import PendingEmailChange
from leg_platform.customer.access import is_customer
from leg_platform.community import addressing, grid_operator, history, person_list, persons
from leg_platform.community import iban as iban_module
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.location_views import DialogView, event_label
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.person_forms import (
    BillingAddressForm,
    ContactForm,
    DetailsForm,
    NoteForm,
    PaymentForm,
    PersonCreateForm,
    SharesForm,
)
from leg_platform.management.returns import created_redirect, origin_point_url, return_url, view_name_of, with_next

STATE_TONES = {
    person_list.AccountState.ACTIVE: "ok",
    person_list.AccountState.BLOCKED: "bad",
    person_list.AccountState.INACTIVE: "muted",
    person_list.AccountState.INVITED: "warn",
}


def list_columns():
    """The columns of the list (start widths; the last takes what is left, the link column is fixed)."""
    return (
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
        lists.Column(
            "points", gettext_lazy("Metering points"), width=200,
            info=_(
                "Number of the metering points of the person. Behind it the state: notified, if all are "
                "notified to the %(operator)s. Recording, if at least one is still being recorded."
            ) % {"operator": grid_operator.name()},
        ),
        lists.Column(
            "account", gettext_lazy("Account"), info_placement="left",
            info=_(
                "Whether the person can sign in to the portal. Blocked: temporarily blocked, for example after "
                "failed attempts. Inactive: the account is deactivated and is in the archive. Invited: the person "
                "has not completed the registration yet."
            ),
        ),
    )


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
    """The list as the address asks for it: the state and the queryset of all rows (filtered, sorted), used by the
    page and the printing. ``locked`` is the set of all locked accounts if filter or sorting needs it, else None
    and ``locked_for`` asks for the shown rows only (D-084, L-4)."""

    state: lists.ListState
    columns: tuple
    queryset: object
    locked: object

    def locked_for(self, persons):
        if self.locked is not None:
            return self.locked
        return person_list.locked_account_ids(only=[person.pk for person in persons])


def build_list(params):
    today = timezone.localdate()
    columns = list_columns()
    filters = {
        "status": (person_list.STATUSES, person_list.STATUS_ALL),
        "points": (person_list.POINTS_FILTERS, person_list.POINTS_ALL),
    }
    state = lists.parse(
        params, path=reverse("management:persons"), columns=columns, default_sort=person_list.DEFAULT_SORT,
        filters=filters,
    )
    # The lockout is looked up for all accounts only if filter or sorting needs every state.
    needs_all = state.values["status"] in (person_list.STATUS_ACTIVE, person_list.STATUS_BLOCKED) or state.sort == "account"
    locked = person_list.locked_account_ids() if needs_all else None
    queryset = person_list.base_queryset(today, locked or ())
    queryset = person_list.apply_search(queryset, state.query)
    queryset = person_list.apply_status(queryset, state.values["status"])
    queryset = person_list.apply_points(queryset, state.values["points"])
    queryset = person_list.apply_sort(queryset, state.sort, state.descending)
    return PersonList(state, columns, queryset, locked)


def list_entry(person, locked):
    """What the list shows of one person (template and printing read the same texts)."""
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

    def get(self, request):
        found = build_list(request.GET)
        state, columns = found.state, found.columns
        page = lists.paginate(found.queryset, state)
        persons_on_page = page.items
        locked = found.locked_for(persons_on_page)
        entries = [list_entry(person, locked) for person in persons_on_page]
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
            create_url=with_next(reverse("management:persons_new"), request),
            status_options=status_options(),
            points_options=points_options(),
            empty_text=_("No persons found."),
        )
        return render(request, "management/persons/list.html", context)


def format_iban(value):
    """The IBAN in blocks of four characters; empty stays empty."""
    return iban_module.display(value)


def point_columns():
    """The columns of the person's metering point table (the template texts, without the 30 days sentence)."""
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
                "Number of the smart meter at the %(operator)s. If it is missing, the metering point is recorded "
                "but not yet known at the %(operator)s."
            ) % {"operator": grid_operator.name()},
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
            "registration", grid_operator.text(gettext_lazy("Registration at the %(operator)s")),
            info_placement="left-up", info_template="management/persons/_tip_registration.html",
        ),
    )


def assignment_lines(assignment):
    """The lines of the column "Assigned": "from DD.MM.YYYY", plus "until DD.MM.YYYY" for an ended assignment."""
    lines = [_("from %(date)s") % {"date": format_date(assignment.valid_from)}]
    if assignment.valid_to:
        lines.append(_("until %(date)s") % {"date": format_date(assignment.valid_to)})
    return lines


def registration_text(row):
    """The registration column as ``(lines, tone)``: registered, notified on / confirmed on a date, or (once the
    termination date is reached) terminated; a future termination adds a line and the step stays until then (D-086)."""
    if row.registration == person_list.REGISTRATION_TERMINATED:
        return [MeteringPointStatus.TERMINATED.label], "muted"
    if row.registration == person_list.REGISTRATION_CONFIRMED:
        lines, tone = [_("confirmed on %(date)s") % {"date": format_date(row.registration_date)}], "ok"
    elif row.registration == person_list.REGISTRATION_NOTIFIED:
        lines, tone = [_("notified on %(date)s") % {"date": format_date(row.registration_date)}], "ok"
    else:
        lines, tone = [MeteringPointStatus.REGISTERED.label], "warn"
    if row.termination_date is not None:
        lines.append(_("Termination on %(date)s") % {"date": format_date(row.termination_date)})
    return lines, tone


class PersonObjectMixin:
    def person(self, pk=None):
        if not hasattr(self, "_person"):
            self._person = get_object_or_404(persons.person_queryset(), pk=pk or self.kwargs["pk"])
        return self._person


def history_label(entry):
    """The text of a history entry; a customer data change lists the changed field names (never values, D-087)."""
    label = event_label(entry.event)
    fields = entry.detail.get("fields") if isinstance(entry.detail, dict) else None
    if entry.event == history.HistoryEvent.PERSON_DATA_CHANGED and isinstance(fields, list):
        names = [str(FIELD_LABELS[name]) for name in fields if name in FIELD_LABELS]
        if names:
            return f"{label}: {', '.join(names)}"
    return label


FIELD_LABELS = {
    "salutation": gettext_lazy("Salutation"),
    "organisation_name": gettext_lazy("Company"),
    "first_name": gettext_lazy("First name"),
    "last_name": gettext_lazy("Last name"),
    "address_line_1": gettext_lazy("Second person"),
    "phone_number": gettext_lazy("Phone"),
    "secondary_email": gettext_lazy("Second email"),
    "street": gettext_lazy("Street"),
    "house_number": gettext_lazy("House number"),
    "post_office_box_number": gettext_lazy("Post office box"),
    "swiss_postal_code": gettext_lazy("Postal code"),
    "foreign_postal_code": gettext_lazy("Postal code"),
    "town": gettext_lazy("Town"),
    "country": gettext_lazy("Country"),
    "iban": gettext_lazy("IBAN"),
    "paper_invoice": gettext_lazy("Paper invoice"),
}


class PersonDetailView(PersonObjectMixin, ManagementAccessMixin, View):
    """The detail page (``person-detail-desktop`` and ``-handy``)."""

    def get(self, request, pk):
        person = self.person(pk)
        today = timezone.localdate()
        record = persons.record_of(person)
        name, contact = person_list.display_names(person)
        locked = person_list.locked_account_ids(only=[person.pk]) if person.is_active else set()
        account = person_list.AccountState(person_list.account_state(person, locked))
        # The banner has buttons: only for an actor who may change this account (D-087, M-1).
        suggestion = persons.address_suggestion(person) if persons.may_change_customer_data(request.user, person) else None
        share_entries = persons.share_history(person)
        context = {
            "page_title": name,
            "person": person,
            "name": name,
            "contact": contact,
            "number": person.customer_number_display,
            "account_label": account.label,
            "account_tone": STATE_TONES[account],
            "can_edit": persons.may_change_customer_data(request.user, person),
            # "View as this person" (D-131): not oneself, only a person who takes part as a customer.
            "can_view_as": person.pk != request.user.pk and is_customer(person),
            "invitation": persons.invitation_state(person),
            "second_pending": PendingEmailChange.objects.filter(
                user=person, kind=PendingEmailChange.Kind.SECONDARY, expires_at__gt=timezone.now()
            ).exists(),
            "salutation": person.get_salutation_display() if person.salutation else "",
            "phone": person.phone_number_display,
            "street": person.street_address_display,
            "postal_town": f"{person.postal_code_display} {person.town}".strip(),
            "country": str(person.country.name) if person.country else "",
            "iban": format_iban(iban_module.readable(record)) if record else "",
            "paper_invoice": bool(record and record.paper_invoice),
            "operator_number": record.operator_customer_number if record else "",
            "note": record.staff_note if record else "",
            "shares": persons.current_shares(share_entries, today),
            "share_entries": [
                {"date": entry.valid_from, "quantity": entry.quantity} for entry in share_entries
            ],
            "suggestion_text": addressing.suggestion_banner_text(suggestion) if suggestion else "",
            "suggestion_token": persons.suggestion_token(suggestion) if suggestion else "",
            "history": [
                {"time": entry.created_at, "label": history_label(entry), "actor": entry.actor.display_name if entry.actor_id else ""}
                for entry in history.entries_for(person).order_by("-created_at", "-pk")
            ],
            "back_url": return_url(request, reverse("management:persons")),
            # A back link that leads to another page than the list (the dialog the person was invited from) is neutral.
            "back_is_origin": return_url(request, reverse("management:persons")) != reverse("management:persons"),
            "current_url": request.get_full_path(),
            "show_new": request.GET.get("new") == "1",
            "origin_point_url": origin_point_url(request) if request.GET.get("new") == "1" else "",
            "metering_points_url": reverse("management:metering_points"),
        }
        context.update(self.points_context(request, person, today))
        return render(request, "management/persons/detail.html", context)

    def points_context(self, request, person, today):
        columns = point_columns()
        state = lists.parse(
            request.GET, path=reverse("management:persons_detail", args=[person.pk]), columns=columns,
            default_sort=person_list.DEFAULT_POINT_SORT,
        )
        rows = person_list.sort_point_rows(person_list.point_rows(person, today), state.sort, state.descending)
        entries = []
        for row in rows:
            lines, tone = registration_text(row)
            entries.append(
                {
                    "row": row,
                    "number": row.point.number,
                    "assigned": assignment_lines(row.assignment),
                    "registration_lines": lines,
                    "registration_tone": tone,
                    "point_url": reverse("management:metering_points_detail", args=[row.point.pk]),
                    "location_url": reverse("management:locations_detail", args=[row.location_id])
                    if row.location_id else "",
                    "leg_url": reverse("management:legs_detail", args=[row.leg_id]) if row.leg_id else "",
                }
            )
        return {
            "point_columns": lists.header_cells(columns, state, {}, "person_points"),
            "point_entries": entries,
            "empty_points_text": _("No metering points assigned."),
        }


class PersonDialog(PersonObjectMixin, DialogView):
    """A dialog about one person."""

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
    """"Notiz bearbeiten": the staff-only note; never searched."""

    title = gettext_lazy("Edit note")
    form_class = NoteForm

    def get_form(self, data=None):
        record = persons.record_of(self.person())
        return NoteForm(data, initial={"note": record.staff_note if record else ""})

    def save(self, request, form):
        persons.set_note(self.person(), form.cleaned_data["note"], request.user)
        return self.finish(request)


class PersonPaymentView(PersonDialog):
    """Payment card: IBAN and paper invoice (customer data, only if the actor may change the account) and the
    grid operator customer number (D-087)."""

    title = gettext_lazy("Edit payment and customer numbers")
    form_class = PaymentForm

    def get_form(self, data=None):
        person = self.person()
        record = persons.record_of(person)
        initial = {
            "iban": format_iban(iban_module.readable(record)) if record else "",
            "paper_invoice": "1" if record and record.paper_invoice else "0",
            "operator_customer_number": record.operator_customer_number if record else "",
        }
        return PaymentForm(
            data, initial=initial, customer_data=persons.may_change_customer_data(self.request.user, person)
        )

    def save(self, request, form):
        data = form.cleaned_data
        values = {"operator_customer_number": data["operator_customer_number"]}
        if form.customer_data:
            values.update(iban=data["iban"], paper_invoice=data["paper_invoice"])
        if not run_service(form, lambda: persons.update_payment_data(self.person(), request.user, **values)):
            return None
        return self.finish(request)


def add_validation_errors(form, error):
    """Show the messages of a service ``ValidationError`` at the form fields, or at the form."""
    for name, messages_ in error.message_dict.items():
        if name in ("swiss_postal_code", "foreign_postal_code"):
            name = "postal_code"
        form.add_error(name if name in form.fields else None, messages_)


def refused(form, error):
    """The answer to a ``PersonError`` of a dialog: no person -> 404, not allowed -> 403, else shown at the form."""
    if error.code == "not_a_person":
        raise Http404
    if error.code == "not_allowed":
        raise PermissionDenied
    if error.code == "rate_limited":
        form.add_error(None, _("Too many requests, please try again later."))
    elif error.code == "email_in_use":
        form.add_error("email", _("This email address is already in use."))
    elif error.code == "email_invalid":
        form.add_error("email" if "email" in form.fields else "secondary_email", _("Enter a valid email address."))
    else:
        form.add_error(None, _("The data could not be saved."))
    return None


def run_service(form, call):
    """Run a service call; its errors are shown at the form. True if it succeeded."""
    try:
        call()
    except ValidationError as error:
        add_validation_errors(form, error)
    except persons.PersonError as error:
        refused(form, error)
    else:
        return True
    return False


class CustomerDataDialog(PersonDialog):
    """A customer data dialog: 403 before anything is shown or saved if the actor may not change the account
    (D-087, D-012); this check is the one that counts."""

    template_name = "management/persons/dialog_data.html"
    group = None

    def refusal(self, request, *args, **kwargs):
        if not persons.may_change_customer_data(request.user, get_object_or_404(persons.person_queryset(), pk=kwargs["pk"])):
            raise PermissionDenied
        return None

    def get_form(self, data=None):
        return self.form_class(data, person=self.person())

    def save(self, request, form):
        if not run_service(form, lambda: persons.update_customer_data(self.person(), self.group, form.values(), request.user)):
            return None
        return self.finish(request)


class PersonDetailsView(CustomerDataDialog):
    """The card "Angaben"."""

    title = gettext_lazy("Edit details")
    form_class = DetailsForm
    group = persons.GROUP_DETAILS


class PersonAddressView(CustomerDataDialog):
    """The card "Rechnungsadresse"."""

    title = gettext_lazy("Edit billing address")
    form_class = BillingAddressForm
    group = persons.GROUP_ADDRESS


class PersonContactView(CustomerDataDialog):
    """The card "Kontakt": phone and second email (existing confirmation flow; the login address is not editable)."""

    title = gettext_lazy("Edit contact")
    form_class = ContactForm
    group = persons.GROUP_CONTACT

    def save(self, request, form):
        person = self.person()
        second_requested = False
        try:
            # One transaction (L-3): if either step is refused, nothing of the dialog is kept.
            with transaction.atomic():
                persons.update_customer_data(person, self.group, form.values(), request.user)
                if "secondary_email" in form.fields:
                    second_requested = persons.change_second_email(
                        request, person, form.cleaned_data["secondary_email"], request.user
                    )
        except ValidationError as error:
            add_validation_errors(form, error)
            return None
        except persons.PersonError as error:
            return refused(form, error)
        if second_requested and form.cleaned_data["secondary_email"]:
            messages.success(request, _("We sent a confirmation link to the new address. It is valid for 24 hours."))
            return redirect(self.back(request))
        return self.finish(request)


class PersonCreateView(DialogView):
    """"Person anlegen" (D-087): the person gets an email with a link to confirm the address and choose a password,
    and is listed as "Invited". Afterwards the page of the person opens with the next step (D-095); from another page
    (the dialog "Assign person") the person goes back there."""

    title = gettext_lazy("Create person")
    lead = gettext_lazy(
        "The person receives an email with a link to confirm the email address and to choose a password."
    )
    submit_label = gettext_lazy("Create person")
    form_class = PersonCreateForm
    template_name = "management/persons/dialog_data.html"

    def back(self, request):
        return return_url(request, reverse("management:persons"))

    def save(self, request, form):
        created = []
        if not run_service(
            form, lambda: created.append(persons.create_person(request.user, form.cleaned_data["email"], form.values()))
        ):
            return None
        back = self.back(request)
        # From the dialog "Assign person" the person goes to the page of the new person too (an invited person cannot be
        # assigned yet) and comes back to the dialog from there.
        list_view = "management:metering_points_person" if view_name_of(back) == "management:metering_points_person" else "management:persons"
        return created_redirect(
            request, list_view, back, reverse("management:persons_detail", args=[created[0].pk]),
            _("The person was created and the invitation was sent."),
        )


class PersonViewAsView(PersonObjectMixin, ManagementAccessMixin, View):
    """"View as this person": starts the read-only support view (D-131). POST with CSRF only. Any person the staff member
    sees here may be viewed, not oneself (the normal pages exist) and only a person who takes part as a customer (else the
    pages would be a 404). The state is in the staff member's own session (``leg_platform.render_as``); nobody is logged
    in as the person, no mail is sent, nothing of the person is written."""

    http_method_names = ["post"]

    def post(self, request, pk):
        person = self.person(pk)
        if person.pk == request.user.pk or not is_customer(person):
            raise Http404
        render_as.start(request, person)
        return redirect("home")


class PersonInvitationView(PersonObjectMixin, ManagementAccessMixin, View):
    """"Einladung erneut senden": a new link for a not yet registered person (D-087)."""

    http_method_names = ["post"]

    def post(self, request, pk):
        person = self.person(pk)
        try:
            persons.resend_invitation(person, request.user)
        except persons.PersonError as error:
            if error.code == "rate_limited":
                messages.error(request, _("Too many requests, please try again later."))
            else:
                messages.error(request, _("There is no invitation to send."))
        else:
            messages.success(request, _("The invitation was sent."))
        return redirect(return_url(request, reverse("management:persons_detail", args=[pk])))


class PersonSharesView(PersonDialog):
    """"Add change" of the shares: a number and the day it holds from (D-086)."""

    title = gettext_lazy("Add change")
    form_class = SharesForm

    def save(self, request, form):
        data = form.cleaned_data
        try:
            persons.add_share_change(self.person(), data["quantity"], data["valid_from"], request.user)
        except persons.PersonError:
            form.add_error(None, _("The shares could not be saved."))
            return None
        return self.finish(request)


class PersonAddressSuggestionView(PersonObjectMixin, ManagementAccessMixin, View):
    """"Accept" and "Do not accept" of the address correction banner."""

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
        except persons.PersonError as error:
            if error.code == "not_allowed":
                # Data of a staff user or administrator (D-012, D-087): not for this actor.
                raise PermissionDenied from None
            messages.error(request, _("There is no suggestion to decide on."))
        else:
            messages.success(request, _("Saved."))
        return redirect(return_url(request, reverse("management:persons_detail", args=[pk])))