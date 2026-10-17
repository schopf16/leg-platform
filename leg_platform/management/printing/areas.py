"""What each list prints (D-082): the table like the list and the detail pages of its entries.

Every area takes the address of the list (``request.GET``) and the account. The list is parsed again on the
server with the list framework (``lists.parse``) and queried with the same function as the page
(``build_list`` of the views), so the search, the filters and the sorting are the ones of the page, whatever a
browser renders. The entries come from that queryset only: the posted ids of a selection are matched against
it and anything else is ignored.

The rights are those of the page: the account needs the right to view the area (the view checks it), and a
detail page prints only what the page shows that account: the names of persons need ``view_personrecord``, the
metering points of a LEG or a person ``view_meteringpoint``. Remarks and notes are printed where the page shows
them, nowhere else. The change history ("Verlauf") of the pages is not printed.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

from leg_platform.community import (
    assignments,
    leg_list,
    legs,
    location_list,
    metering_point_list,
    metering_points,
    numbers,
    person_list,
    persons,
    problems,
    transformer_circuits,
)
from leg_platform.community.models import Direction, ManagementSettings, PersonAssignment, TransformerCircuit
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import leg_views, location_views, metering_point_views, person_views
from leg_platform.management.printing.document import Column, DetailPage, PrintDocument, Section, Table

DASH = "–"

# The bounds of a printout (D-082): a print builds the whole document in memory.
MAX_LIST_ENTRIES = 1000
MAX_DETAIL_PAGES = 100
MAX_ROWS = 20000


def format_date(value):
    return value.strftime("%d.%m.%Y")


def or_dash(value):
    return value if value not in (None, "") else DASH


def with_problem(text, problem):
    """The cell text with the text of the problem under it (a printout has no warning triangle)."""
    text = or_dash(text)
    return f"{text}\n{problem}" if problem else text


@dataclass
class Item:
    """An entry of the list: the id, the name that the dialog shows, the cells of the table and the object."""

    pk: int
    label: str
    cells: list
    source: object


@dataclass
class Prepared:
    """A list that is ready to print: the state, the number of entries (the entries are loaded only when the
    number is within the bound), the table columns and the lines above the list."""

    area: object
    state: object
    total: int
    columns: list
    info: list
    load: object  # a function without arguments: the list of Item

    @property
    def back_url(self):
        return self.state.url()


class Area:
    key = ""
    permission = ""
    filename = ""
    widths = {}

    def title(self):
        return f"{_('Management')} · {self.name()}"

    def name(self):
        raise NotImplementedError

    def column(self, column):
        return Column(str(column.label), self.widths.get(column.key, 1))

    def info(self, state, columns, filters, today):
        """The lines above the list: the date, the search, the filters that are set and the sorting."""
        lines = [(_("Date"), format_date(today))]
        if state.query:
            lines.append((_("Search"), state.query))
        lines.extend(filters)
        if state.problems:
            lines.append((_("Filter"), _("Problems only")))
        direction = _("descending") if state.descending else _("ascending")
        label = next((str(column.label) for column in columns if column.key == state.sort), state.sort)
        lines.append((_("Sorting"), f"{label}, {direction}"))
        return lines

    @staticmethod
    def option_line(label, state, name, options):
        """``[(label, text)]`` when the filter is set to something else than its default, else ``[]``."""
        value = state.values[name]
        if value == state.defaults.get(name, ""):
            return []
        text = dict(options).get(value, value)
        return [(label, str(text))]

    def prepare(self, user, params):
        raise NotImplementedError

    def details(self, user, items, today):
        raise NotImplementedError

    def document(self, user, prepared, items, chosen, today):
        """The document: the list with all ``items`` and the detail pages of the ``chosen`` ones."""
        table = Table(prepared.columns, [item.cells for item in items])
        pages = self.details(user, chosen, today) if chosen else []
        return PrintDocument(self.title(), prepared.info, table, pages)


# The LEGs ------------------------------------------------------------------------------------------------


class LegArea(Area):
    key = "legs"
    permission = leg_views.VIEW
    filename = "legs.pdf"
    widths = {"name": 3.2, "quota": 2.6, "participants": 1.9, "tier": 2.2, "points": 1.3}

    def name(self):
        return _("LEGs")

    def prepare(self, user, params):
        found = leg_views.build_list(params)
        today, threshold = found.today, found.threshold

        def load():
            items = []
            for row in found.rows:
                entry = leg_views.list_entry(row, threshold)
                cells = [
                    row.leg.name,
                    with_problem(entry["quota_text"], entry["quota_problem"]),
                    with_problem(row.summary.ratio if row.summary.metering_points else "", entry["ratio_problem"]),
                    str(entry["tier_label"]) if entry["has_tier"] else DASH,
                    str(row.summary.metering_points),
                ]
                items.append(Item(row.leg.pk, row.leg.name, cells, row))
            return items

        filters = self.option_line(_("Display"), found.state, "display", leg_views.display_options())
        return Prepared(
            self, found.state, len(found.rows), [self.column(column) for column in found.columns],
            self.info(found.state, found.columns, filters, today), load,
        )

    def details(self, user, items, today):
        threshold = ManagementSettings.load().warning_threshold_percent
        see_points = user.has_perm(leg_views.VIEW_POINTS)
        see_persons = user.has_perm(leg_views.VIEW_PERSONS)
        pages = []
        for item in items:
            leg, summary = item.source.leg, item.source.summary
            level = legs.quota_level(leg.production_quota_percent, threshold)
            circuits = list(
                TransformerCircuit.objects.filter(pk__in=summary.circuit_ids).order_by("bkw_number")
                .values_list("bkw_number", flat=True)
            )
            quota = Section(
                _("Production quota"),
                [
                    (_("Share"), or_dash(legs.format_quota(leg.production_quota_percent))),
                    (_("Checked on"), format_date(leg.quota_checked_on) if leg.quota_checked_on else DASH),
                ],
                note=legs.quota_level_text(level, threshold) if level else "",
            )
            master = Section(
                _("Master data"), [(_("Name"), leg.name), (_("Remark"), leg.remark or _("None"))]
            )
            composition = Section(
                _("Composition"),
                [
                    (_("Consumers : producers"), or_dash(summary.ratio if summary.metering_points else "")),
                    (
                        _("Discount tier"),
                        str(legs.DiscountTier(summary.tier).label) if summary.tier != legs.DiscountTier.NONE else DASH,
                    ),
                    (_("Transformer circuits"), ", ".join(circuits) or DASH),
                ],
            )
            sections = [quota, master, composition]
            if see_points:
                sections.append(self.members_section(leg, today, see_persons))
            title = leg.name + (f" ({_('archived')})" if leg.is_archived else "")
            pages.append(
                DetailPage(
                    title, location_views.count_phrase(summary.metering_points),
                    [str(problems.describe(problem, threshold)) for problem in summary.problems], sections,
                )
            )
        return pages

    @staticmethod
    def members_section(leg, today, see_persons):
        points = leg_list.members_of(leg, today)
        occupants = leg_list.occupants(points, today, see_persons=see_persons)
        rows = []
        for point in points:
            location = point.location
            circuit = location.transformer_circuit if location else None
            if point.pk in occupants:
                occupant = occupants[point.pk] if see_persons and occupants[point.pk] else DASH
            else:
                occupant = _("no person")
            rows.append(
                [
                    f"{point.number or DASH}\n{Direction(point.direction).label}",
                    f"{location.street} {location.house_number}" if location else DASH,
                    circuit.bkw_number if circuit else DASH,
                    str(MeteringPointStatus(point.status(today)).label),
                    occupant,
                ]
            )
        columns = [
            Column(_("Metering point"), 4.3), Column(_("Address"), 2.0), Column(_("Transformer circuit"), 1.6),
            Column(_("Status"), 1.5), Column(_("Occupied by"), 2.0),
        ]
        return Section(
            _("Metering points of this LEG"), table=Table(columns, rows, _("No metering points belong to this LEG yet."))
        )


# The persons -----------------------------------------------------------------------------------------------


class PersonArea(Area):
    key = "persons"
    permission = person_views.VIEW
    filename = "persons.pdf"
    widths = {"name": 3.2, "number": 1.5, "address": 3.0, "points": 1.9, "account": 1.4}

    def name(self):
        return _("Persons")

    def prepare(self, user, params):
        found = person_views.build_list(params, user)
        today = timezone.localdate()
        total = found.queryset.count()

        def load():
            items = []
            for person in found.queryset[:MAX_LIST_ENTRIES]:
                entry = person_views.list_entry(person, found.locked)
                name = f"{entry['name']}\n{entry['contact']}" if entry["contact"] else entry["name"]
                cells = [name, entry["number"], or_dash(entry["address"])]
                if found.see_points:
                    cells.append(entry["points"])
                cells.append(str(entry["account_label"]))
                items.append(Item(person.pk, entry["name"], cells, person))
            return items

        filters = self.option_line(_("Status"), found.state, "status", person_views.status_options())
        if found.see_points:
            filters += self.option_line(
                _("Metering points"), found.state, "points", person_views.points_options()
            )
        return Prepared(
            self, found.state, total, [self.column(column) for column in found.columns],
            self.info(found.state, found.columns, filters, today), load,
        )

    def details(self, user, items, today):
        see_points = user.has_perm(person_views.VIEW_POINTS)
        locked = person_list.locked_account_ids()
        pages = []
        for item in items:
            person = item.source
            record = persons.record_of(person)
            name, _contact = person_list.display_names(person)
            account = person_list.AccountState(person_list.account_state(person, locked if person.is_active else set()))
            number = person.customer_number_display
            details = Section(
                _("Details"),
                [
                    (_("Salutation"), or_dash(person.get_salutation_display() if person.salutation else "")),
                    (_("Company"), or_dash(person.organisation_name)),
                    (_("First name"), or_dash(person.first_name)),
                    (_("Last name"), or_dash(person.last_name)),
                    (_("Second person"), or_dash(person.address_line_1)),
                ],
            )
            contact = Section(
                _("Contact"),
                [
                    (_("Email"), person.email),
                    (_("Second email"), or_dash(person.secondary_email)),
                    (_("Phone"), or_dash(person.phone_number_display)),
                ],
            )
            address = Section(
                _("Billing address"),
                [
                    (_("Street"), or_dash(person.street_address_display)),
                    (_("Postal code, town"), or_dash(f"{person.postal_code_display} {person.town}".strip())),
                    (_("Country"), or_dash(str(person.country.name) if person.country else "")),
                ],
            )
            payment = Section(
                _("Payment and customer numbers"),
                [
                    (_("IBAN"), or_dash(person_views.format_iban(record.iban) if record else "")),
                    (_("Paper invoice"), _("Yes") if record and record.paper_invoice else _("No")),
                    (_("Customer number"), number),
                    (_("BKW customer number"), or_dash(record.bkw_customer_number if record else "")),
                ],
            )
            if record and record.shares:
                cooperative = Section(
                    _("Cooperative"),
                    [
                        (_("Shares"), str(record.shares)),
                        (_("Period"), person_views.PersonDetailView.shares_period(record)),
                    ],
                )
            else:
                cooperative = Section(_("Cooperative"), note=_("No shares recorded."))
            note = Section(_("Note"), [(_("Note"), or_dash(record.staff_note if record else ""))])
            sections = [details, contact, address, payment, cooperative, note]
            if see_points:
                sections.append(self.points_section(person, today))
            pages.append(
                DetailPage(name, f"{_('Customer number')} {number} · {account.label}", [], sections)
            )
        return pages

    @staticmethod
    def points_section(person, today):
        rows = person_list.sort_point_rows(person_list.point_rows(person, today), person_list.DEFAULT_POINT_SORT)
        table_rows = []
        for row in rows:
            text, _tone = person_views.bkw_text(row)
            table_rows.append(
                [
                    or_dash(row.address),
                    or_dash(numbers.format_metering_point_number(row.point.number)),
                    str(Direction(row.point.direction).label),
                    or_dash(row.leg_name if row.leg_id else ""),
                    "\n".join(person_views.assignment_lines(row.assignment)),
                    str(text),
                ]
            )
        columns = [
            Column(_("Location"), 1.8), Column(_("Metering point"), 3.2), Column(_("Direction"), 1.3),
            Column(_("LEG"), 1.6), Column(_("Assigned"), 1.5), Column(_("BKW registration"), 1.8),
        ]
        return Section(
            _("Metering points and assignments"), table=Table(columns, table_rows, _("No metering points assigned."))
        )


# The locations -------------------------------------------------------------------------------------------


class LocationArea(Area):
    key = "locations"
    permission = location_views.VIEW
    filename = "locations.pdf"
    widths = {"address": 3.6, "circuit": 1.8, "metering_points": 2.4, "occupancy": 1.6}

    def name(self):
        return _("Locations")

    def prepare(self, user, params):
        found = location_views.build_list(params, user)
        total = found.queryset.count()

        def load():
            locations = list(found.queryset[:MAX_LIST_ENTRIES])
            _rows, occupants = location_list.details(locations, found.today, see_persons=found.see_persons)
            items = []
            for location in locations:
                address = f"{location.street} {location.house_number}"
                if location.needs_confirmation:
                    address += f" ({_('new')})"
                problem = [str(problems.describe(problem)) for problem in problems.location_problems(location)]
                cells = [
                    with_problem(address, " ".join(problem)),
                    location_views.circuit_phrase(location),
                    location_views.count_phrase(location.mp_count),
                    location_views.occupancy_phrase(location.mp_count, occupants[location.pk]),
                ]
                items.append(Item(location.pk, f"{location.street} {location.house_number}", cells, location))
            return items

        filters = self.option_line(
            _("Transformer circuit"), found.state, "circuit", [(c.bkw_number, c.bkw_number) for c in found.circuits]
        )
        filters += self.option_line(_("Occupancy"), found.state, "occupancy", location_views.occupancy_options())
        return Prepared(
            self, found.state, total, [self.column(column) for column in location_views.COLUMNS],
            self.info(found.state, location_views.COLUMNS, filters, found.today), load,
        )

    def details(self, user, items, today):
        see_persons = user.has_perm(location_views.VIEW_PERSONS)
        locations = [item.source for item in items]
        rows, occupants = location_list.details(locations, today, see_persons=see_persons)
        pages = []
        for location in locations:
            points = rows[location.pk]
            circuit = location.transformer_circuit
            stats = transformer_circuits.stats(circuit) if circuit else None
            address = Section(
                _("Address"),
                [
                    (_("Street"), f"{location.street} {location.house_number}"),
                    (_("Postal code, town"), f"{location.postal_code} {location.town}"),
                    (_("Address addition"), or_dash(location.address_addition)),
                ],
            )
            grid_facts = [(_("Transformer circuit"), or_dash(transformer_circuits.label(circuit)))]
            grid_facts.append(
                (_("Locations in the transformer circuit"), str(stats["locations"]) if stats else DASH)
            )
            grid_facts.append(
                (
                    _("Metering points in the transformer circuit"),
                    f"{stats['consumption']} {Direction.CONSUMPTION.label} · {stats['feed_in']} {Direction.FEED_IN.label}"
                    if stats else DASH,
                )
            )
            grid = Section(_("Grid"), grid_facts)
            remark = Section(_("Remark"), [(_("Remark"), or_dash(location.remark))])
            point_rows = []
            for row in points:
                if row.person_id:
                    occupant = or_dash(row.person_name) if see_persons else DASH
                else:
                    occupant = _("no person")
                point_rows.append(
                    [
                        f"{row.metering_point.number or DASH}\n{Direction(row.metering_point.direction).label}",
                        row.leg_name if row.leg_id else _("no LEG"),
                        str(MeteringPointStatus(row.status).label),
                        occupant,
                    ]
                )
            point_columns = [
                Column(_("Metering point"), 4.3), Column(_("LEG"), 2.0), Column(_("Status"), 1.5),
                Column(_("Occupied by"), 2.2),
            ]
            sections = [
                address, grid, remark,
                Section(
                    _("Metering points"),
                    table=Table(point_columns, point_rows, _("No metering points at this location.")),
                    note=_("The assignment to a person is changed at the person or at the metering point."),
                ),
            ]
            if see_persons:
                sections.append(self.assignments_section(location))
            subtitle = " · ".join(
                [
                    location_views.circuit_phrase(location), location_views.count_phrase(len(points)),
                    location_views.occupancy_phrase(len(points), occupants[location.pk]),
                ]
            )
            title = f"{location.street} {location.house_number}"
            if location.is_archived:
                title += f" ({_('archived')})"
            pages.append(
                DetailPage(
                    title, subtitle,
                    [str(problems.describe(problem)) for problem in problems.location_problems(location)],
                    sections,
                )
            )
        return pages

    @staticmethod
    def assignments_section(location):
        found = (
            PersonAssignment.objects.filter(metering_point__location=location)
            .select_related("metering_point", "person")
            .order_by("-valid_from", "pk")
        )
        rows = [
            [
                assignment.metering_point.number or DASH,
                location_list.person_name(assignment.person),
                format_date(assignment.valid_from),
                format_date(assignment.valid_to) if assignment.valid_to else DASH,
            ]
            for assignment in found
        ]
        columns = [Column(_("Metering point"), 3.4), Column(_("Person"), 2.6), Column(_("From"), 1.2), Column(_("To"), 1.2)]
        return Section(_("History of the assignments"), table=Table(columns, rows, _("No assignments yet.")))


# The metering points -------------------------------------------------------------------------------------


def step_options():
    """The quick filters without their counts."""
    return [
        (metering_point_list.STEP_ALL, _("All")),
        (metering_point_list.STEP_NOTIFY, _("Notify the BKW")),
        (metering_point_list.STEP_CONFIRM, _("Confirmation pending")),
        (metering_point_list.STEP_START, _("Start date pending")),
        (metering_point_list.STEP_ARCHIVED, _("archived")),
    ]


class MeteringPointArea(Area):
    key = "metering_points"
    permission = metering_point_views.VIEW
    filename = "metering-points.pdf"
    widths = {"number": 5.2, "location": 2.2, "leg": 2.4, "status": 1.6, "next_step": 1.8}

    def name(self):
        return _("Metering points")

    def prepare(self, user, params):
        found = metering_point_views.build_list(params, user)
        total = found.queryset.count()

        def load():
            items = []
            for point in found.queryset[:MAX_LIST_ENTRIES]:
                status = point.status(found.today)
                found_problems = problems.metering_point_problems(point, found.today)
                if found.show_steps:
                    action = metering_points.next_step(point, point.has_leg, found.today)
                    route = metering_point_views.STEP_ROUTES.get(action)
                    if route:
                        last = str(route[1])
                    elif action == metering_points.LEG_MISSING:
                        last = _("LEG missing")
                    else:
                        last = DASH
                else:
                    last = str(MeteringPointStatus(status).label)
                cells = [
                    with_problem(
                        f"{point.number or DASH} · {Direction(point.direction).label}",
                        " ".join(str(problems.describe(problem)) for problem in found_problems),
                    ),
                    f"{point.location.street} {point.location.house_number}" if point.location_id else DASH,
                    or_dash(point.current_leg_name if point.current_leg_id else ""),
                    last,
                ]
                items.append(Item(point.pk, point.number or DASH, cells, point))
            return items

        state = found.state
        filters = self.option_line(_("Quick filter"), state, "step", step_options())
        leg_options = [(str(leg.pk), leg.name) for leg in found.legs]
        filters += self.option_line(_("LEG"), state, "leg", leg_options)
        return Prepared(
            self, state, total, [self.column(column) for column in found.columns],
            self.info(state, metering_point_views.ALL_COLUMNS, filters, found.today), load,
        )

    def details(self, user, items, today):
        see_persons = user.has_perm(metering_point_views.VIEW_PERSONS)
        pages = []
        for item in items:
            point = item.source
            status = point.status(today)
            location = point.location
            circuit = location.transformer_circuit if location else None
            found_problems = problems.metering_point_problems(point, today)
            facts = [
                (_("Direction"), str(Direction(point.direction).label)),
                (_("Location"), f"{location.street} {location.house_number}" if location else DASH),
                (_("Transformer circuit"), circuit.bkw_number if circuit else DASH),
            ]
            for label, value, unit in (
                (_("PV power"), point.pv_power_kwp, "kWp"),
                (_("Battery"), point.battery_kwh, "kWh"),
                (_("Wallbox"), point.wallbox_kw, "kW"),
            ):
                if value is not None:
                    facts.append((label, f"{Decimal(value):.2f} {unit}"))
            if point.remark:
                facts.append((_("Remark"), point.remark))
            status_facts = [(_("State"), str(MeteringPointStatus(status).label))]
            if status == MeteringPointStatus.CONFIRMED and point.start_date is None:
                status_facts.append((_("Start date"), _("missing")))
            elif point.start_date:
                status_facts.append((_("Start date"), format_date(point.start_date)))
            if point.terminated_on:
                status_facts.append((_("Terminated to"), format_date(point.terminated_on)))
            leg_rows = metering_point_views.assignment_rows(
                metering_points.chain(assignments.LEG, point, today),
                single_label=_("Assigned"), name_of=lambda assigned: assigned.leg.name, url_of=lambda assigned: "",
            )
            person_rows = metering_point_views.assignment_rows(
                metering_points.chain(assignments.PERSON, point, today),
                single_label=_("Occupied by"),
                name_of=lambda assigned: metering_point_views.person_name(assigned.person) if see_persons else DASH,
                url_of=lambda assigned: "",
            )
            sections = [
                Section(_("Metering point"), facts),
                Section(_("Status"), status_facts),
                Section(_("LEG"), self.assignment_facts(leg_rows, _("Assigned"), _("no LEG"))),
                Section(_("Person"), self.assignment_facts(person_rows, _("Occupied by"), _("no person"))),
            ]
            title = point.number or DASH
            if point.is_archived:
                title += f" ({_('archived')})"
            pages.append(
                DetailPage(
                    title, str(Direction(point.direction).label),
                    [str(problems.describe(problem)) for problem in found_problems], sections,
                )
            )
        return pages

    @staticmethod
    def assignment_facts(rows, missing_label, missing_text):
        if not rows:
            return [(missing_label, missing_text)]
        facts = []
        for row in rows:
            facts.append((row["label"], f"{row['name']} {row['note']}".strip()))
            if row["single"]:
                facts.append((_("Applies from"), format_date(row["valid_from"])))
        return facts


AREAS = {area.key: area for area in (PersonArea(), LocationArea(), LegArea(), MeteringPointArea())}
