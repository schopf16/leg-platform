"""Edit mode of a building card on the page Buildings (no script needed: ``?edit=<card id>``).

One POST saves everything the card offers (building name, the address while the application is SUBMITTED, the names of
the metering points, the plant figures of a feed-in point, the start) through the EXISTING services of
``leg_platform.community`` (``applications.edit_submitted_*``, ``buildings.set_*``); this module only picks the forms and
the calls. All forms are validated before anything is saved. The offer (``mark``) is a convenience, never the check: the
services check the owner and the state again under their row lock. The services keep their own transactions, so a
refusal of a later service leaves the earlier changes saved (the page then shows the refusal).
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils.translation import gettext as _

from leg_platform.community import applications, archived, buildings
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import Direction, ManagementSettings
from leg_platform.customer import application_edit, forms, limits, texts
from leg_platform.customer.wizard import _number_message, _typed_part, add_service_error, too_many_requests

FIGURES = buildings.PLANT_FIELDS
ADDRESS_FIELDS = ("street", "house_number", "postal_code", "town")


def _editable_row(row):
    return bool(row.point_id or row.application_point_id)


def offers(card):
    """What the card lets the person change (read from the overview, checked again by the services)."""
    return {
        "address": bool(card.edit.get("address")) and card.building is not None and card.edit_application is not None,
        "name": (
            not card.edit.get("address") and card.location is not None and any(row.point_id for row in card.rows)
        ),
        "start": bool(card.edit.get("start")) and card.edit_application is not None and bool(card.start_text),
        "rows": [index for index, row in enumerate(card.rows) if _editable_row(row)],
    }


def mark(data):
    """Set ``can_edit`` on every card."""
    for card in data.cards:
        found = offers(card)
        card.can_edit = bool(found["address"] or found["name"] or found["start"] or found["rows"])


def find(data, edit_id):
    return next((card for card in data.cards if card.edit_id == edit_id and card.can_edit), None)


class EditState:
    """The forms of one card in edit mode (``forms``: all of them, in page order)."""

    def __init__(self, card, post=None):
        self.card = card
        self.offer = offers(card)
        self.forms = []
        self.address = self.name = self.start = None
        self.earliest = None
        if self.offer["address"]:
            street, house_number, postal_code, town = card.building.current_address()
            self.address = forms.BuildingForm(post, prefix="b", initial={
                "name": card.name, "street": street, "house_number": house_number, "postal_code": postal_code,
                "town": town,
            })
            self.forms.append(self.address)
        elif self.offer["name"]:
            self.name = forms.BuildingDetailsForm(post, prefix="b", initial={"name": card.name})
            del self.name.fields["description"]
            self.forms.append(self.name)
        for index in self.offer["rows"]:
            row = card.rows[index]
            initial = {"name": row.name}
            if row.direction == Direction.FEED_IN:
                initial.update(row.figures)
                row.form = forms.FeedInPointForm(post, prefix=f"r{index}", initial=initial)
            else:
                row.form = forms.PointNameForm(post, prefix=f"r{index}", initial=initial)
            self.forms.append(row.form)
        if self.offer["start"]:
            application = card.edit_application
            self.earliest = applications.earliest_start_date()
            self.start = forms.StartForm(post, prefix="s", initial={
                "start": application.start_choice or forms.ASAP,
                "desired_date": application.desired_start_date or self.earliest,
            })
            self.start.fields["desired_date"].widget.attrs["min"] = self.earliest.isoformat()
            self.forms.append(self.start)

    def is_valid(self):
        # Every form is checked (no short circuit), so the page shows all errors at once.
        return all([form.is_valid() for form in self.forms])

    @property
    def errors(self):
        return any(form.errors for form in self.forms)

    def focus(self):
        """The browser focuses the first field with an error, else the first field (``autofocus``, no script)."""
        for want_error in (True, False):
            for form in self.forms:
                for field in form:
                    if not field.is_hidden and (field.errors or not want_error):
                        field.field.widget.attrs["autofocus"] = True
                        return


def _fail(form, error):
    """A refusal of a service: the text on the form (the summary shows it)."""
    form.add_error(None, texts.message(error.code))


def _guard(error):
    if error.code in ("not_found", "not_allowed"):
        raise Http404 from None


def _save_address(request, state):
    card, form = state.card, state.address
    if not form.has_changed():
        return True
    changed = set(form.changed_data)
    typed = form.typed()
    if changed & set(ADDRESS_FIELDS) and not limits.allow_search(request.user):
        return too_many_requests(request)
    # An unchanged address keeps its EGID; a changed one is looked up again by the service.
    egid = None if changed & set(ADDRESS_FIELDS) else card.egid
    try:
        applications.edit_submitted_building(
            request.user, card.edit_application, card.building.pk, street=typed[0], house_number=typed[1],
            postal_code=typed[2], town=typed[3], egid=egid, name=form.cleaned_data["name"],
        )
    except ValidationError as error:
        add_service_error(form, error)
    except ApplicationError as error:
        _guard(error)
        if error.code in application_edit.LEAVE_CODES:
            return application_edit.leave(request, error)
        if error.code in ("ambiguous", "building_not_found", "egid_not_offered", "egid_invalid"):
            form.add_error(None, texts.NOT_CLEAR)
        else:
            _fail(form, error)
    else:
        return True
    return False


def _save_name(request, state):
    card, form = state.card, state.name
    if not form.has_changed():
        return True
    try:
        buildings.set_building_details(
            request.user, card.location, name=form.cleaned_data["name"], description=card.description
        )
    except ValidationError as error:
        form.add_error(None, error)
    except archived.Archived:
        form.add_error(None, texts.message("not_editable"))
    except ApplicationError as error:
        _guard(error)
        _fail(form, error)
    else:
        return True
    return False


def _save_row(request, state, index):
    card = state.card
    row = card.rows[index]
    form = row.form
    if not form.has_changed():
        return True
    data = form.cleaned_data
    feed_in = row.direction == Direction.FEED_IN
    figures = {name: data[name] for name in FIGURES} if feed_in else {}
    try:
        if row.point_id:
            point = buildings.own_metering_point(request.user, row.point_id)
            buildings.set_metering_point_name(request.user, point, data["name"], **figures)
        else:
            if not ManagementSettings.load().identifier:
                form.add_error(None, texts.message("prefix_not_configured"))
                return False
            applications.edit_submitted_metering_point(
                request.user, row.application, row.application_point_id, entered_number=_typed_part(row.number),
                name=data["name"], **figures,
            )
    except ValidationError as error:
        form.add_error(None, _number_message(error))
    except ApplicationError as error:
        _guard(error)
        if error.code in application_edit.LEAVE_CODES:
            return application_edit.leave(request, error)
        _fail(form, error)
    else:
        for field, _kind in applications.plant_warnings(**figures):
            messages.warning(request, _("The figure %(name)s is unusually large. Please check it.") % {
                "name": forms.FeedInPointForm.base_fields[field].label
            })
        return True
    return False


def _save_start(request, state):
    card, form = state.card, state.start
    if not form.has_changed():
        return True
    data = form.cleaned_data
    try:
        applications.edit_submitted_start(
            request.user, card.edit_application, asap=data["start"] == forms.ASAP, desired_date=data["desired_date"]
        )
    except ApplicationError as error:
        _guard(error)
        if error.code == "start_invalid":
            earliest = error.details.get("earliest")
            if earliest is not None:
                form.add_error("desired_date", _("Choose a day on or after %(date)s.") % {
                    "date": earliest.strftime("%d.%m.%Y")
                })
            else:
                form.add_error(None, texts.message(error.code))
        elif error.code in application_edit.LEAVE_CODES:
            return application_edit.leave(request, error)
        else:
            _fail(form, error)
    else:
        return True
    return False


def save(request, state):
    """Save every changed part. ``True`` = all saved; ``False`` = a refusal was put on a form (show the page again);
    a response = leave (too many requests, the editing has ended)."""
    steps = []
    if state.address is not None:
        steps.append(_save_address)
    if state.name is not None:
        steps.append(_save_name)
    steps.extend(lambda r, s, i=i: _save_row(r, s, i) for i in state.offer["rows"])
    if state.start is not None:
        steps.append(_save_start)
    result = True
    for step in steps:
        outcome = step(request, state)
        if outcome is False:
            result = False
        elif outcome is not True:
            return outcome
    return result
