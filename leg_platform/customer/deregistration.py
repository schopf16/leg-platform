"""The deregistration ("Abmeldung") of the customer (ABMELDUNG.md; KONZEPT 7.1, 7.2; D-103, D-105).

One page with three cards: the metering points, the date, the confirmation with **one proof** (D-062: the code of the
second factor if one is set up, else the password; wrong entries are counted per session by the form like everywhere) and
the button "Deregister metering points (binding)" (no typed word, no extra dialog: KONZEPT 7.1 item 4). The rules are the service's
(``community.deregistrations``): this module reads, shapes the page and shows the answers.

* **Where it starts** (the query of the route, nothing in it is trusted):
  ``?building=<id>`` is the card's "Deregister metering points" (only that building's points, all chosen), no query is
  the "end participation" path of the profile (all running points of all buildings, grouped by building, all chosen),
  ``?point=<id>`` (repeatable, the way back from the IBAN) chooses points. Every id is resolved through the account: the
  id of a building or a metering point that is not the account's own is a 404. The account stays (the deletion of the
  account is its own page, ``accounts.profile_views.AccountDeleteView``).
* **Read-only and optional blocks:** points that are in a deregistration already ("Already in deregistration"), and
  registrations in progress ("Registration in progress"): a submitted one that the LEG has not passed on can be
  withdrawn here (``withdraw_applications``, not chosen at the start; the service is ``applications.withdraw``, the
  application becomes a draft again), one at the grid operator is shown only.
* **Feed-in without an IBAN** does not block (owner decision 2026-10-29: the customer can always cancel); the staff sees
  in the process whether the IBAN is on file.
* **No computed date is shown** (owner decision): the customer picks "as early as possible" or a month end; the LEG
  confirms the date.
* A GET changes nothing. Nothing here writes a name, a number or an IBAN to a log.
"""

from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters

from leg_platform.accounts import second_factor
from leg_platform.community import applications, buildings, deregistrations
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import (
    Application,
    DeregistrationItem,
    Direction,
    PersonAssignment,
)
from leg_platform.customer import forms, limits, overview, texts
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.customer.wizard import _preselected_location, operator_name, too_many_requests

def in_progress_applications(person, location=None):
    """The account's registrations in progress (submitted, in review, at the grid operator), of one building if given.
    Each is a row for the page: the title (name or address of the building), the numbers and whether the customer may
    still withdraw it (``Application.WITHDRAWABLE_STATES``)."""
    rows = Application.objects.filter(person=person, state__in=Application.IN_FLIGHT_STATES).prefetch_related(
        "buildings__metering_points"
    ).order_by("pk")
    if location is not None:
        rows = rows.filter(buildings__location=location).distinct()
    found = []
    for application in rows:
        titles, numbers = [], []
        for building in application.buildings.all():
            address = ", ".join(part for part in (
                f"{building.street} {building.house_number}".strip(), f"{building.postal_code} {building.town}".strip()
            ) if part)
            titles.append(building.name or address)
            numbers += [point.number for point in building.metering_points.all()]
        found.append({
            "pk": application.pk, "title": " · ".join(title for title in titles if title), "numbers": numbers,
            "withdrawable": application.state in Application.WITHDRAWABLE_STATES,
        })
    return found


def deregistering_points(person, location=None):
    """The account's points that are in a deregistration already (submitted or confirmed), with the date if the LEG
    confirmed it, of one building if given."""
    items = DeregistrationItem.objects.filter(
        deregistration__person=person, deregistration__state__in=deregistrations.OPEN_STATES
    ).select_related("metering_point", "deregistration").order_by("metering_point_id")
    if location is not None:
        items = items.filter(metering_point__location=location)
    found = [
        {
            "name": item.metering_point.name, "number": item.metering_point.number or "",
            "direction": Direction(item.metering_point.direction).label,
            "date": item.deregistration.confirmed_date,
        }
        for item in items
    ]
    return found


def point_choices(person, location, today):
    """The account's own metering points that can be deregistered now (the service's list: active ones and those that are
    not active yet, which the page shows with a note), of one building if given."""
    points = deregistrations.eligible_points(person, today)
    for point in points:
        point.pending = deregistrations.is_not_active(point, today)
    if location is not None:
        points = [point for point in points if point.location_id == location.pk]
    return points


def assigned_ids(person, today=None):
    """Every metering point that the account has a current assignment on (valid now or later, not ended): what an id in
    the request may name. A former holder owns nothing (D-103)."""
    today = today or timezone.localdate()
    return set(PersonAssignment.objects.not_ended(today).filter(person=person).values_list("metering_point_id", flat=True))


def read_ids(values, allowed):
    """The ids of a request as numbers; an id that is not the account's own is a 404 (like a missing one). Anything that is
    not a number is dropped (the form refuses it as an invalid choice)."""
    found = []
    for value in values:
        try:
            pk = int(value)
        except (TypeError, ValueError):
            continue
        if pk not in allowed:
            raise Http404
        if pk not in found:
            found.append(pk)
    return found


def summary_text(count):
    if count <= 0:
        return ""  # nothing is said until a point is chosen
    if count == 1:
        return _("You are deregistering 1 metering point.")
    return _("You are deregistering %(count)s metering points.") % {"count": count}


class DeregistrationBase(CustomerAccessMixin, View):
    """The reading of the query that both the page and its POST need, and what a proof asks of its view: the parameters
    of the proof are masked in error reports, and a session that the wrong entries ended (D-062) goes to the login page."""

    @method_decorator(sensitive_post_parameters("current_password", "otp_token"))
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        if getattr(request, second_factor.SESSION_ENDED_ATTRIBUTE, False):
            second_factor.end_session_for_wrong_codes(request)
            return redirect("accounts:login")
        return response

    def location(self, request):
        return _preselected_location(request, request.GET)

    @staticmethod
    def page_url(location, selected=(), termination=None, desired=None):
        """The address of this page with a choice in it (the way back from the profile)."""
        params = []
        if location is not None:
            params.append(("building", location.pk))
        params += [("point", pk) for pk in selected]
        if termination == forms.DATE and desired is not None:
            params += [("termination", forms.DATE), ("date", desired.isoformat())]
        url = reverse("customer:deregistration_start")
        return f"{url}?{urlencode(params)}" if params else url


class DeregistrationStartView(DeregistrationBase):
    """"Start deregistration" / "Remove metering point" / "Delete account": the page and its POST."""

    template_name = "customer/deregistration.html"

    def preselection(self, request, points):
        """The ids that start chosen: the points named by ``point`` (the way back), else by the button."""
        named = request.GET.getlist("point")
        if named:
            wanted = read_ids(named, assigned_ids(request.user))
            return [point.pk for point in points if point.pk in wanted]
        return [point.pk for point in points]

    def initial(self, request):
        initial = {"termination": forms.EARLIEST}
        if request.GET.get("termination") == forms.DATE:
            try:
                initial["desired_date"] = date.fromisoformat(request.GET.get("date", ""))
                initial["termination"] = forms.DATE
            except ValueError:
                pass
        return initial

    def get(self, request):
        today = timezone.localdate()
        location = self.location(request)
        points = point_choices(request.user, location, today)
        selected = self.preselection(request, points)
        form = forms.DeregistrationForm(
            request=request, points=points, withdrawable=self.withdrawable(request.user, location),
            initial=self.initial(request),
        )
        return self.show(request, form, points, selected, location, today)

    @staticmethod
    def back_url(request):
        """Where "Cancel" leads: only the known internal pages a caller names with ``?from=`` (the fork page, the delete
        page); anything else, and a building tile, lead to the buildings page. Never a free address (no open redirect)."""
        known = {"end": "accounts:account_end", "delete": "accounts:account_delete"}
        name = known.get(request.GET.get("from", ""))
        return reverse(name) if name else reverse("customer:buildings")

    @staticmethod
    def withdrawable(person, location):
        """The registrations the customer may withdraw here (the form accepts only these ids)."""
        return [row for row in in_progress_applications(person, location) if row["withdrawable"]]

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        today = timezone.localdate()
        location = self.location(request)
        read_ids(request.POST.getlist("metering_points"), assigned_ids(request.user))
        points = point_choices(request.user, location, today)
        withdrawable = self.withdrawable(request.user, location)
        form = forms.DeregistrationForm(request.POST, request=request, points=points, withdrawable=withdrawable)
        if not form.is_valid():
            selected = [point.pk for point in points if str(point.pk) in request.POST.getlist("metering_points")]
            return self.show(request, form, points, selected, location, today)
        data = form.cleaned_data
        chosen = [int(key) for key in data["metering_points"]]
        withdraw_ids = [int(key) for key in data["withdraw_applications"]]
        deregistration = None
        failed = False
        if chosen:
            try:
                deregistration = deregistrations.request_deregistration(
                    request.user, metering_point_ids=chosen, desired_date=data["desired_date"]
                )
            except ApplicationError as error:
                return self.refused(request, form, error, points, chosen, location, today)
        for pk in withdraw_ids:
            try:
                # The service finds the application through the account and re-checks its state under the lock.
                applications.withdraw(request.user, Application(pk=pk))
            except ApplicationError:
                failed = True
        if failed:
            messages.warning(request, _("A registration could not be withdrawn. Please check the page Buildings."))
        elif withdraw_ids:
            messages.success(request, _("Your registration was withdrawn."))
        if deregistration is None:
            return redirect("customer:buildings")
        return redirect(reverse("customer:deregistration_done", args=[deregistration.pk]))

    def refused(self, request, form, error, points, chosen, location, today):
        code = error.code
        if code in ("not_found", "not_allowed"):
            raise Http404 from None
        if code in ("date_before_earliest", "not_month_end"):
            form.add_error("desired_date", texts.message(code))
        else:
            form.add_error(None, texts.message(code))
        return self.show(request, form, points, chosen, location, today)

    def show(self, request, form, points, selected, location, today):
        shown = set(selected)
        rows = self.rows(request.user, points, shown, today)
        groups = self.groups(request.user, rows)
        in_progress = in_progress_applications(request.user, location)
        chosen_applications = set(request.POST.getlist("withdraw_applications")) if form.is_bound else set()
        for row in in_progress:
            row["checked"] = str(row["pk"]) in chosen_applications
        already = deregistering_points(request.user, location)
        context = {
            "form": form, "groups": groups, "has_points": bool(points),
            # Nothing to do here at all: no point to deregister, no registration to withdraw.
            "empty": not points and not any(row["withdrawable"] for row in in_progress),
            # The title names a building only when the page was opened for one.
            "heading": groups[0]["title"] if location is not None and groups else "",
            "summary": summary_text(len(shown)), "selected_count": len(shown),
            "cancel_url": self.back_url(request),
            "today": today.isoformat(), "operator": operator_name(),
            "already": already, "in_progress": in_progress,
            # Nothing to choose because the points are in a deregistration already (W3-07), not because there are none.
            "already_deregistered": not points and bool(already),
        }
        return render(request, self.template_name, context)

    @staticmethod
    def rows(person, points, shown, today):
        since = {}
        for point_id, valid_from in PersonAssignment.objects.not_ended(today).filter(
            person=person, metering_point_id__in=[point.pk for point in points]
        ).values_list("metering_point_id", "valid_from"):
            since[point_id] = min(since.get(point_id, valid_from), valid_from)
        return [
            {
                "pk": point.pk, "number": point.number or "", "name": point.name,
                "direction": Direction(point.direction).label, "since": point.start_date or since.get(point.pk),
                "checked": point.pk in shown, "location": point.location,
            }
            for point in points
        ]

    @staticmethod
    def groups(person, rows):
        """The rows by building (the account's own name of the building as title): a person with one building sees
        one list without a heading of its own."""
        grouped = {}
        for row in rows:
            location = row["location"]
            key = location.pk if location is not None else 0
            if key not in grouped:
                if location is None:
                    title, line = _("Metering points without a building"), ""
                else:
                    name, _description = buildings.building_details(person, location)
                    title, line = overview._lines(location.current_address(), name)
                grouped[key] = {"title": title, "line": line, "rows": []}
            grouped[key]["rows"].append(row)
        ordered = sorted(grouped.values(), key=lambda group: group["title"].casefold())
        # The first box has the id the error summary links to (``id_metering_points_0``).
        index = 0
        for group in ordered:
            for row in group["rows"]:
                row["index"] = index
                index += 1
        return ordered


class DeregistrationDoneView(DeregistrationBase):
    """The result: what was submitted, what happens next, the confirmation by email. The deregistration is read through
    the account (another account's id is a 404). A GET only reads."""

    def get(self, request, pk):
        deregistration = deregistrations.own_deregistration(request.user, pk)
        items = list(deregistration.items.select_related("metering_point").order_by("metering_point_id"))
        context = {
            "deregistration": deregistration, "progress": overview._deregistration_block(deregistration),
            "points": [item.metering_point for item in items], "operator": operator_name(),
            "date_text": overview.date_note(deregistration),
        }
        return render(request, "customer/deregistration_done.html", context)
