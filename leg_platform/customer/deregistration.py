"""The deregistration ("Abmeldung") of the customer (ABMELDUNG.md; KONZEPT 7.1, 7.2; D-103, D-105).

One page with three cards: the metering points, the date, the confirmation with **one proof** (D-062: the code of the
second factor if one is set up, else the password; wrong entries are counted per session by the form like everywhere) and
the button "Give binding notice" (no typed word, no extra dialog: KONZEPT 7.1 item 4). The rules are the service's
(``community.deregistrations``): this module reads, shapes the page and shows the answers.

* **Where it starts** (the query of the route, nothing in it is trusted):
  ``?building=<id>`` is the card's "Remove metering point" (only that building's points, none chosen unless there is only
  one), no query is the page's "Start deregistration" (all points chosen), ``?for=account_deletion`` is the profile's
  "Delete account" (all running points chosen), ``?point=<id>`` (repeatable, the way back from the IBAN) chooses points.
  Every id is resolved through the account: the id of a building or a metering point that is not the account's own is a 404.
* **Feed-in needs an IBAN** (``iban_required``): the page leads to the profile card, which leads back with the choice.
* **The date is never shown** (ABMELDUNG.md section 3): "Earliest possible date" or a desired month end; the LEG confirms.
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
from leg_platform.community import buildings, deregistrations, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Direction, PersonAssignment
from leg_platform.customer import forms, limits, overview, returns, texts
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.customer.wizard import _preselected_location, operator_name, too_many_requests

DELETION = "account_deletion"


def point_choices(person, location, today):
    """The account's own metering points that can be deregistered now (the service's list), of one building if given."""
    points = deregistrations.eligible_points(person, today)
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
        return _("Choose at least one metering point.")
    if count == 1:
        return _("You are deregistering 1 metering point at the chosen date.")
    return _("You are deregistering %(count)s metering points at the chosen date.") % {"count": count}


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
    def is_deletion(request):
        return request.GET.get("for") == DELETION

    @staticmethod
    def page_url(location, deletion, selected=(), termination=None, desired=None):
        """The address of this page with a choice in it (the way back from the profile)."""
        params = []
        if location is not None:
            params.append(("building", location.pk))
        if deletion:
            params.append(("for", DELETION))
        params += [("point", pk) for pk in selected]
        if termination == forms.DATE and desired is not None:
            params += [("termination", forms.DATE), ("date", desired.isoformat())]
        url = reverse("customer:deregistration_start")
        return f"{url}?{urlencode(params)}" if params else url


class DeregistrationStartView(DeregistrationBase):
    """"Start deregistration" / "Remove metering point" / "Delete account": the page and its POST."""

    template_name = "customer/deregistration.html"

    def preselection(self, request, points, location, deletion):
        """The ids that start chosen: the points named by ``point`` (the way back), else by the button."""
        named = request.GET.getlist("point")
        if named:
            wanted = read_ids(named, assigned_ids(request.user))
            return [point.pk for point in points if point.pk in wanted]
        if location is not None and not deletion:
            return [point.pk for point in points] if len(points) == 1 else []
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
        deletion = self.is_deletion(request)
        points = point_choices(request.user, location, today)
        selected = self.preselection(request, points, location, deletion)
        form = forms.DeregistrationForm(request=request, points=points, initial=self.initial(request))
        return self.show(request, form, points, selected, location, deletion, today)

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        today = timezone.localdate()
        location = self.location(request)
        deletion = self.is_deletion(request)
        read_ids(request.POST.getlist("metering_points"), assigned_ids(request.user))
        points = point_choices(request.user, location, today)
        form = forms.DeregistrationForm(request.POST, request=request, points=points)
        if not form.is_valid():
            selected = [point.pk for point in points if str(point.pk) in request.POST.getlist("metering_points")]
            if form.iban_missing and set(form.errors) <= {"__all__"}:
                return self.to_bank(request, form, selected, location, deletion)
            return self.show(request, form, points, selected, location, deletion, today)
        data = form.cleaned_data
        chosen = [int(key) for key in data["metering_points"]]
        try:
            deregistration = deregistrations.request_deregistration(
                request.user, metering_point_ids=chosen, desired_date=data["desired_date"]
            )
        except ApplicationError as error:
            return self.refused(request, form, error, points, chosen, location, deletion, today)
        url = reverse("customer:deregistration_done", args=[deregistration.pk])
        return redirect(f"{url}?for={DELETION}" if deletion else url)

    def refused(self, request, form, error, points, chosen, location, deletion, today):
        code = error.code
        if code in ("not_found", "not_allowed"):
            raise Http404 from None
        if code == "iban_required":
            return self.to_bank(request, form, chosen, location, deletion)
        if code in ("date_before_earliest", "not_month_end"):
            form.add_error("desired_date", texts.message(code))
        else:
            form.add_error(None, texts.message(code))
        return self.show(request, form, points, chosen, location, deletion, today)

    def to_bank(self, request, form, selected, location, deletion):
        """The feed-in needs the bank connection: the profile card, then back here with the same choice (KONZEPT 7.1)."""
        data = form.cleaned_data if hasattr(form, "cleaned_data") else {}
        back = self.page_url(location, deletion, selected, data.get("termination"), data.get("desired_date"))
        messages.warning(request, texts.IBAN_NEEDED, extra_tags="card-bank")
        return redirect(returns.bank_change_url(back))

    def show(self, request, form, points, selected, location, deletion, today):
        shown = set(selected)
        rows = self.rows(request.user, points, shown, today)
        groups = self.groups(request.user, rows)
        needs_iban = any(
            point.direction == Direction.FEED_IN and point.pk in shown for point in points
        ) and not own_iban.has_iban(request.user)
        back = self.page_url(location, deletion, selected)
        context = {
            "form": form, "groups": groups, "empty": not points, "deletion": deletion,
            "heading": groups[0]["title"] if len(groups) == 1 else "",
            "summary": summary_text(len(shown)), "selected_count": len(shown),
            "needs_iban": needs_iban, "bank_url": returns.bank_change_url(back),
            "cancel_url": reverse("accounts:profile") + "#card-delete" if deletion else reverse("customer:buildings"),
            "today": today.isoformat(), "operator": operator_name(),
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
        deletion = self.is_deletion(request)
        context = {
            "deregistration": deregistration, "progress": overview._deregistration_block(deregistration),
            "points": [item.metering_point for item in items], "operator": operator_name(),
            "deletion": deletion, "released": deletion and deregistrations.deletion_released(request.user),
        }
        return render(request, "customer/deregistration_done.html", context)
