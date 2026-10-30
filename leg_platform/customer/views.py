"""The pages of the customer's participation besides the wizard and the deregistration (GEBAEUDE.md; D-103): the page
Buildings, the pencils (the account's own name and description of a building, the name of a metering point), the contract
page and its download, and the saving of the bank or postal connection from the profile card.

Pages call the services of ``leg_platform.community`` and never re-implement a rule. Every id in an address is resolved
through the logged-in account: the building of another account and a metering point that is not the account's own are a
404, the same answer as for a missing one. Nothing here writes a name, an address, a number or an IBAN to a log.
"""

from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.csp import CSP
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.csp import csp_override

from leg_platform.accounts import profile_views
from leg_platform.addresses import buildings as address_buildings, neighbourhood, transformers
from leg_platform.community import applications, archived, buildings, contracts, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Direction
from leg_platform.community.persons import PersonError
from leg_platform.customer import banners, card_edit, contact, forms, limits, overview, returns, texts
from leg_platform.customer.access import CustomerAccessMixin
from leg_platform.mail import GraphEmailError
from leg_platform.customer.wizard import too_many_requests
from leg_platform.downloads import pdf_attachment, pdf_inline


class BuildingsView(CustomerAccessMixin, View):
    """The page Buildings: a card per building with the account's own metering points. ``?edit=<card id>`` shows that
    card as a form; its POST saves everything of the card (``customer.card_edit``)."""

    def _data(self, request):
        data = overview.build(request.user)
        card_edit.mark(data)
        return data

    def _render(self, request, data, status=200):
        read = banners.pending(request.user)
        response = render(request, "customer/buildings.html", {
            "overview": data, "deregistration_url": reverse("customer:deregistration_start"),
            "banners": read.banners,
        }, status=status)
        # Only after the page was built, and only for a real visit (not HEAD, not a prefetch).
        banners.mark_shown(request, request.user, read)
        return response

    def get(self, request):
        data = self._data(request)
        data.open_id = request.session.pop("buildings_open_id", "")
        edit_id = request.GET.get("edit", "")
        if edit_id:
            card = card_edit.find(data, edit_id)
            if card is None:
                return redirect("customer:buildings")
            card.editing = card_edit.EditState(card)
            card.editing.focus()
            data.editing_id = card.edit_id
        return self._render(request, data)

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        data = self._data(request)
        card = card_edit.find(data, request.GET.get("edit", ""))
        if card is None:
            messages.error(request, texts.message("not_editable"))
            return redirect("customer:buildings")
        state = card.editing = card_edit.EditState(card, request.POST)
        data.editing_id = card.edit_id
        if state.is_valid():
            result = card_edit.save(request, state)
            if result is True:
                messages.success(request, _("Saved."))
                request.session["buildings_open_id"] = card.edit_id
                return redirect("customer:buildings")
            if result is not False:
                return result
        state.focus()
        return self._render(request, data)


# The swisstopo tiles of the map: the one external host the page's policy allows, for images only (D-123). Set for this
# view alone (``csp_override``); every other page keeps ``img-src 'self'``.
NEIGHBOURHOOD_TILES = "https://wmts.geo.admin.ch"


@method_decorator(
    csp_override({**settings.SECURE_CSP, "img-src": [CSP.SELF, NEIGHBOURHOOD_TILES]}), name="dispatch",
)
class NeighbourhoodView(CustomerAccessMixin, View):
    """The page Nachbarschaft (D-123): the transformer circuits around the building of the profile address.

    The map gets public building and plant data only (outlines, PV points, counts); no other participant is read."""

    def get(self, request):
        hood, own = _neighbourhood(request.user)
        return render(request, "customer/neighbourhood.html", {
            "neighbourhood": hood,
            "legend": _legend(hood) if hood else [],
            "map_data": _map_data(hood) if hood else None,
            "profile_building": own,
            "invite_url": _invite_url(),
        })


def _neighbourhood(person):
    """``(neighbourhood, own)``: the circuits of the building at the profile address (``own`` True). Without such a
    building: the circuits of the profile's postal code, then of the postal code where most of the LEG's buildings lie
    (``own`` False). Only the public directory and counts are read. None only where the directory shows no circuit."""
    egid = _profile_egid(person)
    hood = neighbourhood.for_building(egid) if egid is not None else None
    if hood is not None:
        return hood, True
    for postal_code in _area_postal_codes(person):
        hood = neighbourhood.for_postal_code(postal_code)
        if hood is not None:
            return hood, False
    return None, False


def _profile_egid(person):
    """The EGID of the building at the profile address, or None. It is read from the local address directory only
    (no request outside, no assignment to the account needed): the circuit is public data, and the profile address is
    the person's own. Several buildings at one address: the first by EGID."""
    if not (person.street and person.swiss_postal_code and person.town):
        return None
    search = address_buildings.find_buildings(
        person.street, person.house_number, str(person.swiss_postal_code), person.town,
    )
    return search.buildings[0].egid if search.buildings else None


def _area_postal_codes(person):
    """The postal codes to show without an own building, in order (D-123): the profile's postal code, then the postal
    codes of the service area that the staff entered in the administration (``ServicePostalCode``)."""
    codes = [str(person.swiss_postal_code)] if person.swiss_postal_code else []
    return codes + [code for code in transformers.postal_codes() if code not in codes]


# The four circuit colours of the map and the legend: the index of a colour (``.nb-swatch-0`` to ``-3`` in customer.css,
# ``COLOURS`` in ``neighbourhood-map.js``). A fifth circuit reuses the first colour.
COLOURS = 4


def _colour(index):
    return index % COLOURS


def _legend(hood):
    return [
        {
            "circuit": circuit,
            "colour": _colour(index),
            "own": circuit.name == hood.own_circuit,
        }
        for index, circuit in enumerate(hood.circuits)
    ]


def _map_data(hood):
    """The data of the map as plain lists of (longitude, latitude); read by ``customer/js/neighbourhood-map.js``."""
    return {
        "own": list(hood.own_point) if hood.own_point else None,
        "ownCircuit": hood.own_circuit,
        "circuits": [
            {
                "name": circuit.name,
                "buildings": circuit.building_count,
                "pv": circuit.pv_count,
                "colour": _colour(index),
                "outline": [list(point) for point in circuit.outline],
                "pvPoints": [list(point) for point in circuit.pv_points],
            }
            for index, circuit in enumerate(hood.circuits)
        ],
    }


def _invite_url():
    """A ``mailto:`` link with the text of an invitation; it opens the mail program of the member, so the mail leaves
    from the member's own address and the portal sends nothing (D-123)."""
    text = {
        "subject": _("Local electricity community %(name)s") % {"name": settings.INSTANCE_NAME},
        "body": _("I take part in the local electricity community. Would you like to join as well?"),
    }
    return "mailto:?" + urlencode(text, quote_via=quote)


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

    @staticmethod
    def make_form(point, data=None):
        """Name only for a draw; name and plant figures for a feed-in."""
        if point.direction == Direction.FEED_IN:
            initial = {"name": point.name, **{
                field: "" if getattr(point, field) is None else format(getattr(point, field), "f")
                for field in buildings.PLANT_FIELDS
            }}
            return forms.FeedInPointForm(data, initial=initial)
        return forms.PointNameForm(data, initial={"name": point.name})

    def get(self, request, pk):
        point = self.point(request, pk)
        return render(request, self.template_name, {"form": self.make_form(point), "point": point})

    def post(self, request, pk):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        point = self.point(request, pk)
        form = self.make_form(point, request.POST)
        if form.is_valid():
            data = form.cleaned_data
            figures = {field: data[field] for field in buildings.PLANT_FIELDS} if "pv_power_kwp" in data else {}
            try:
                buildings.set_metering_point_name(request.user, point, data["name"], **figures)
            except ValidationError as error:
                form.add_error(None, error)
            except ApplicationError as error:
                if error.code in ("not_found", "not_allowed"):
                    raise Http404 from None
                form.add_error(None, texts.message(error.code))
            else:
                for field, _kind in applications.plant_warnings(**figures):
                    messages.warning(request, _("The figure %(name)s is unusually large. Please check it.") % {
                        "name": forms.FeedInPointForm.base_fields[field].label
                    })
                messages.success(request, _("Saved."))
                return redirect("customer:buildings")
        return render(request, self.template_name, {"form": form, "point": point})


class ContractView(CustomerAccessMixin, View):
    """"Articles of association" (Information): the current version and every version the account accepted, and the
    tick for the current version (the form posts to ``ContractAcceptView``)."""

    def get(self, request):
        version = contracts.current_version()
        return render(request, "customer/contract.html", {
            "versions": contracts.customer_versions(request.user),
            "current": version,
            "acceptance": contracts.acceptance_of(request.user, version) if version else None,
            "form": forms.ContractForm(initial={"version": version.pk if version else None}),
        })


class ContractAcceptView(CustomerAccessMixin, View):
    """Saves the tick for the current version on the Information page: the same rule as the contract step of the
    application (``contracts.accept_contract``; the version shown and the tick are checked there). A GET leads back to
    the page."""

    http_method_names = ["get", "head", "post"]

    def get(self, request):
        return redirect("customer:contract")

    def post(self, request):
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        form = forms.ContractForm(request.POST)
        if not form.is_valid():
            messages.error(request, texts.message("agreement_required"))
            return redirect("customer:contract")
        try:
            contracts.accept_contract(
                request.user, form.cleaned_data["version"], agrees_contract=form.cleaned_data["agrees_contract"],
            )
        except ApplicationError as error:
            if error.code != "already_accepted":
                messages.error(request, texts.message(error.code))
        else:
            messages.success(request, _("Saved."))
        return redirect("customer:contract")


class ContactView(CustomerAccessMixin, View):
    """"Contact the LEG" (Information, D-115): one message to the LEG; the answer by mail or by phone. A phone number
    that is sent is saved to the profile, like the card "Personal details" saves it (no confirmation). Without the
    configured address of the LEG the page is not offered (a POST is a 404)."""

    def get(self, request):
        return self.show(request)

    def post(self, request):
        if not contact.available():
            raise Http404
        if not limits.allow_wizard(request.user):
            return too_many_requests(request)
        form = forms.ContactForm(request.POST)
        if not form.is_valid():
            return self.show(request, form)
        try:
            phone = contact.send(request.user, form.cleaned_data)
        except (GraphEmailError, OSError):
            messages.error(request, texts.message("contact_failed"))
            return self.show(request, form)
        if phone and phone != str(request.user.phone_number or ""):
            request.user.phone_number = phone
            request.user.save(update_fields=["phone_number"])
        messages.success(request, _("Your message was sent to the LEG."))
        return redirect("customer:contact")

    def show(self, request, form=None):
        user = request.user
        form = form or forms.ContactForm(initial={
            "email": user.email,
            "phone": user.phone_number_display if user.phone_number else "",
        })
        return render(request, "customer/contact.html", {"form": form, "available": contact.available()})


class ContractDownloadView(CustomerAccessMixin, View):
    """The PDF as an attachment: the current version, or an earlier one that the account itself accepted. Another
    version, or none, is a 404. ``nosniff``, a sandboxing policy and no caching."""

    def get(self, request, version_id=None):
        try:
            name, data = contracts.customer_download(request.user, version_id)
        except ApplicationError:
            raise Http404 from None
        return pdf_attachment(data, name)


class ContractReadingView(CustomerAccessMixin, View):
    """The current version for reading in the frame of the contract pages (D-108): ``inline``, framing allowed for the
    own origin only, ``nosniff``, no caching. Only the current version (an earlier one has the download); none is a 404."""

    def get(self, request):
        try:
            name, data = contracts.customer_download(request.user)
        except ApplicationError:
            raise Http404 from None
        return pdf_inline(data, name)


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
