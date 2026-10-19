"""The pages of the customer's participation besides the wizard and the deregistration (GEBAEUDE.md; D-103): the page
Buildings, the pencils (the account's own name and description of a building, the name of a metering point), the contract
page and its download, and the saving of the bank or postal connection from the profile card.

Pages call the services of ``leg_platform.community`` and never re-implement a rule. Every id in an address is resolved
through the logged-in account: the building of another account and a metering point that is not the account's own are a
404, the same answer as for a missing one. Nothing here writes a name, an address, a number or an IBAN to a log.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from leg_platform.accounts import profile_views
from leg_platform.community import archived, buildings, contracts, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.persons import PersonError
from leg_platform.customer import forms, limits, overview, returns, texts
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.customer.wizard import too_many_requests
from leg_platform.downloads import pdf_attachment


class BuildingsView(CustomerAccessMixin, View):
    """The page Buildings: a card per building with the account's own metering points."""

    def get(self, request):
        data = overview.build(request.user)
        return render(request, "customer/buildings.html", {
            "overview": data, "deregistration_url": reverse("customer:deregistration_start"),
            "can_deregister": data.can_deregister,
        })


class BuildingDetailsView(CustomerAccessMixin, View):
    """The pencil at the name and the description of a building (the account's own words, never another's)."""

    template_name = "customer/building_details.html"

    def location(self, request, pk):
        # Through the account's CURRENT assignments: a former holder gets ``not_found`` (a 404 by the access mixin).
        return buildings.own_building(request.user, pk)

    def get(self, request, pk):
        location = self.location(request, pk)
        name, description = buildings.building_details(request.user, location)
        form = forms.BuildingDetailsForm(initial={"name": name, "description": description})
        return render(request, self.template_name, {"form": form, "location": location})

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        location = self.location(request, pk)
        form = forms.BuildingDetailsForm(request.POST)
        if form.is_valid():
            try:
                buildings.set_building_details(
                    request.user, location, name=form.cleaned_data["name"], description=form.cleaned_data["description"]
                )
            except ValidationError as error:
                form.add_error(None, error)
            except archived.Archived:
                form.add_error(None, texts.message("not_editable"))
            else:
                messages.success(request, _("Saved."))
                return redirect("customer:buildings")
        return render(request, self.template_name, {"form": form, "location": location})


class MeteringPointNameView(CustomerAccessMixin, View):
    """The pencil at the name of a metering point (only one of the account's own)."""

    template_name = "customer/metering_point_name.html"

    def point(self, request, pk):
        # Through the account's CURRENT assignment: a former holder of the point gets ``not_found`` (404). An archived
        # point is read-only.
        point = buildings.own_metering_point(request.user, pk)
        if point.is_archived:
            raise Http404
        return point

    def get(self, request, pk):
        point = self.point(request, pk)
        return render(request, self.template_name, {"form": forms.PointNameForm(initial={"name": point.name}), "point": point})

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        point = self.point(request, pk)
        form = forms.PointNameForm(request.POST)
        if form.is_valid():
            try:
                buildings.set_metering_point_name(request.user, point, form.cleaned_data["name"])
            except ValidationError as error:
                form.add_error(None, error)
            except ApplicationError as error:
                if error.code in ("not_found", "not_allowed"):
                    raise Http404 from None
                form.add_error(None, texts.message(error.code))
            else:
                messages.success(request, _("Saved."))
                return redirect("customer:buildings")
        return render(request, self.template_name, {"form": form, "point": point})


class ContractView(CustomerAccessMixin, View):
    """"Articles of association" (Information): the current version and every version the account accepted."""

    def get(self, request):
        versions = contracts.customer_versions(request.user)
        return render(request, "customer/contract.html", {"versions": versions, "current": contracts.current_version()})


class ContractDownloadView(CustomerAccessMixin, View):
    """The PDF as an attachment: the current version, or an earlier one that the account itself accepted. Another
    version, or none, is a 404. ``nosniff``, a sandboxing policy and no caching."""

    def get(self, request, version_id=None):
        try:
            name, data = contracts.customer_download(request.user, version_id)
        except ApplicationError:
            raise Http404 from None
        return pdf_attachment(data, name)


class BankConnectionView(CustomerAccessMixin, View):
    """Saves the IBAN of the profile card "Bank or postal connection" (PROFIL.md section 2; KONZEPT 7.2).

    **No password and no second factor** (owner decision): the service ``community.own_iban`` validates the IBAN fully,
    limits the changes per hour and mails every address of the account on a change of an existing IBAN. The form posts
    here (the customer area has the budget per account and the body limit), the card is the profile's: an error shows
    the profile page again. ``next`` is the way back (a page of the customer's pages only, ``returns.safe_return_path``).
    A GET only leads to the card."""

    http_method_names = ["get", "head", "post"]

    def get(self, request):
        return redirect(returns.bank_change_url(request.GET.get("next", "")))

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        form = forms.BankForm(request.POST)
        if form.is_valid():
            try:
                own_iban.set_own_iban(request.user, form.cleaned_data["iban"])
            except ValidationError:
                form.add_error("iban", texts.IBAN_INVALID)
            except PersonError as error:
                if error.code in ("not_a_person", "not_active", "not_allowed"):
                    raise Http404 from None
                form.add_error("iban", texts.message(error.code))
            else:
                messages.success(request, _("Saved."), extra_tags="card-bank")
                back = returns.safe_return_path(request.POST.get("next", ""))
                return redirect(back or f"{reverse('accounts:profile')}#card-bank")
        return profile_views.render_profile(request, edit="bank", forms={"bank": form})
