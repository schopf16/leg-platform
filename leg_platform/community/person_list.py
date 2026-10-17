"""Queries and rules of the person list and of the metering points of one person (D-081). Pure query code.

A person is an ``accounts.User`` with a customer number (D-077), or an invited person who has not completed the
registration (D-087, ``persons.person_filter``). Account states shown in the list:

* **Active**: state ``active`` and not temporarily locked.
* **Blocked**: state ``blocked``, or an active account held by the login lockout (``accounts.login_protection``).
* **Inactive**: state ``deactivated`` (kept under the retention obligation); only with the filter "archived".
* **Invited** (D-087): state ``unverified`` of a person invited by staff; no customer number yet. Only in "All".

The metering points of a person are the non-archived ones with an assignment of this person that is valid now
or starts later (``not_ended``). The word behind the count is "registered" if at least one is still registered
(no notification to the grid operator), else "notified". The search never reads a note, the customer number at
the grid operator or the IBAN (L-6).
"""

from dataclasses import dataclass

from axes.models import AccessAttempt
from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Case, CharField, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Concat, Trim
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.login_protection import LOGIN_COOLOFF, LOGIN_FAILURE_LIMIT, axes_username
from leg_platform.community import persons
from leg_platform.community.models import LegAssignment, MeteringPoint, PersonAssignment
from leg_platform.community.status import MeteringPointStatus

STATUS_ALL = "all"
STATUS_ACTIVE = "active"
STATUS_BLOCKED = "blocked"
STATUS_ARCHIVED = "archived"
STATUSES = (STATUS_ALL, STATUS_ACTIVE, STATUS_BLOCKED, STATUS_ARCHIVED)

POINTS_ALL = "all"
POINTS_WITH = "with"
POINTS_WITHOUT = "without"
POINTS_FILTERS = (POINTS_ALL, POINTS_WITH, POINTS_WITHOUT)

SORT_KEYS = ("name", "number", "address", "points", "account")
DEFAULT_SORT = "name"

class AccountState(models.TextChoices):
    ACTIVE = "active", _("Active")
    BLOCKED = "blocked", _("Blocked")
    INACTIVE = "inactive", _("Inactive")
    INVITED = "invited", _("Invited")


ACCOUNT_RANK = {
    AccountState.ACTIVE: 0, AccountState.BLOCKED: 1, AccountState.INACTIVE: 2, AccountState.INVITED: 3,
}


def locked_account_ids(now=None, only=None):
    """The ids of the active persons held by the login lockout now.

    Axes stores only keyed digests of the login address, so the digests of the accounts are computed and
    compared (the rule of ``AxesDatabaseHandler``). Nothing is computed while no lockout exists. ``only``
    (account ids) restricts the check to the rows of a page (D-084, L-4)."""
    now = now or timezone.now()
    digests = set(
        AccessAttempt.objects.filter(attempt_time__gte=now - LOGIN_COOLOFF)
        .order_by()
        .values("username")
        .annotate(failures=Sum("failures_since_start"))
        .filter(failures__gte=LOGIN_FAILURE_LIMIT)
        .values_list("username", flat=True)
    )
    if not digests:
        return set()
    user_model = get_user_model()
    candidates = user_model.objects.filter(
        customer_number__isnull=False, status=user_model.Status.ACTIVE, normalized_email__isnull=False
    )
    if only is not None:
        candidates = candidates.filter(pk__in=list(only))
    candidates = candidates.values_list("pk", "normalized_email")
    return {pk for pk, address in candidates if axes_username(None, {"username": address}) in digests}


def account_state(person, locked_ids=()):
    """The state of the account."""
    status = person.status
    if status == type(person).Status.UNVERIFIED:
        return AccountState.INVITED
    if status == type(person).Status.DEACTIVATED:
        return AccountState.INACTIVE
    if status == type(person).Status.BLOCKED or person.pk in locked_ids:
        return AccountState.BLOCKED
    return AccountState.ACTIVE


def display_names(person):
    """``(name, contact)``: a company shows the company name and the contact person below it (D-032); any
    other person shows first and last name and no second line."""
    personal = f"{person.first_name} {person.last_name}".strip()
    if person.organisation_name:
        return person.organisation_name, personal
    return personal, ""


def points_word(point_count, has_unregistered):
    """The state word behind the count: "" without a metering point, "registered" if one is still being
    recorded, else "notified"."""
    if not point_count:
        return ""
    return "registered" if has_unregistered else "notified"


def mine(today):
    """The assignments of the outer person that count (see the module text)."""
    return PersonAssignment.objects.not_ended(today).filter(person=OuterRef("pk"), metering_point__is_archived=False)


def base_queryset(today=None, locked_ids=()):
    """The persons with the columns ``sort_name``, ``postal_text``, ``point_count``, ``has_unregistered`` and
    ``account_rank``."""
    today = today or timezone.localdate()
    user_model = get_user_model()
    assignments = mine(today)
    count = assignments.order_by().values("person").annotate(total=Count("metering_point", distinct=True)).values("total")
    unregistered = assignments.filter(
        metering_point__in=MeteringPoint.objects.with_status(MeteringPointStatus.REGISTERED, today).values("pk")
    )
    return user_model.objects.filter(persons.person_filter()).annotate(
        sort_name=Case(
            When(~Q(organisation_name=""), then=F("organisation_name")),
            default=Trim(Concat("first_name", Value(" "), "last_name")),
            output_field=CharField(),
        ),
        postal_text=Cast("swiss_postal_code", CharField()),
        point_count=Coalesce(Subquery(count, output_field=IntegerField()), 0),
        has_unregistered=Exists(unregistered),
        account_rank=Case(
            When(status=user_model.Status.UNVERIFIED, then=Value(ACCOUNT_RANK[AccountState.INVITED])),
            When(status=user_model.Status.DEACTIVATED, then=Value(ACCOUNT_RANK[AccountState.INACTIVE])),
            When(
                Q(status=user_model.Status.BLOCKED) | Q(pk__in=list(locked_ids)),
                then=Value(ACCOUNT_RANK[AccountState.BLOCKED]),
            ),
            default=Value(ACCOUNT_RANK[AccountState.ACTIVE]),
            output_field=IntegerField(),
        ),
    )


def apply_status(queryset, status):
    """"All" and the two states hide inactive accounts; "archived" shows only those."""
    user_model = get_user_model()
    if status == STATUS_ARCHIVED:
        return queryset.filter(status=user_model.Status.DEACTIVATED)
    queryset = queryset.exclude(status=user_model.Status.DEACTIVATED)
    if status == STATUS_ACTIVE:
        return queryset.filter(account_rank=ACCOUNT_RANK[AccountState.ACTIVE])
    if status == STATUS_BLOCKED:
        return queryset.filter(account_rank=ACCOUNT_RANK[AccountState.BLOCKED])
    return queryset


def apply_points(queryset, value):
    if value == POINTS_WITH:
        return queryset.filter(point_count__gt=0)
    if value == POINTS_WITHOUT:
        return queryset.filter(point_count=0)
    return queryset


def apply_search(queryset, text):
    """Every word must match the name (also a company contact), customer number, address, town or an e-mail
    address; never a note, the grid operator customer number or the IBAN."""
    for word in (text or "").split():
        condition = (
            Q(first_name__icontains=word) | Q(last_name__icontains=word) | Q(organisation_name__icontains=word)
            | Q(street__icontains=word) | Q(house_number__icontains=word) | Q(town__icontains=word)
            | Q(postal_text__icontains=word) | Q(foreign_postal_code__icontains=word)
            | Q(email__icontains=word) | Q(secondary_email__icontains=word)
        )
        if word.isascii() and word.isdigit():
            condition |= Q(customer_number__number__contains=word)
        queryset = queryset.filter(condition)
    return queryset


def apply_sort(queryset, key, descending=False):
    """Sort by a column; the name, then the customer number break ties."""
    first = {
        "number": [F("customer_number")],
        "address": [F("street"), Cast("house_number", IntegerField()), F("house_number")],
        "points": [F("point_count")],
        "account": [F("account_rank")],
        "name": [F("sort_name")],
    }.get(key, [])
    primary = [item.desc() if descending else item.asc() for item in first]
    return queryset.order_by(*primary, F("sort_name").asc(), F("customer_number").asc())


POINT_SORT_KEYS = ("location", "number", "direction", "leg", "assigned", "registration")
DEFAULT_POINT_SORT = "number"

# Registration steps at the grid operator (D-086); a terminated one sorts last.
REGISTRATION_REGISTERED = "registered"
REGISTRATION_NOTIFIED = "notified"
REGISTRATION_CONFIRMED = "confirmed"
REGISTRATION_TERMINATED = "terminated"
_REGISTRATION_RANK = {
    REGISTRATION_REGISTERED: 0, REGISTRATION_NOTIFIED: 1, REGISTRATION_CONFIRMED: 2, REGISTRATION_TERMINATED: 3,
}


@dataclass
class PointRow:
    """One assignment of the person as the table shows it. ``registration`` is ``registered``, ``notified``,
    ``confirmed`` or ``terminated`` (``registration_date`` is that step's day); ``termination_date`` is a
    recorded termination in the future (the step stays until then)."""

    assignment: object
    point: object
    location_id: int | None
    street: str
    house_number: str
    leg_id: int | None
    leg_name: str
    registration: str
    registration_date: object
    termination_date: object = None

    @property
    def address(self):
        return f"{self.street} {self.house_number}".strip()


def registration_state(point, today):
    """The registration step at the grid operator: terminated once the termination date is reached, else
    confirmed, else notified, else registered (with its date)."""
    if point.terminated_on is not None and point.terminated_on <= today:
        return REGISTRATION_TERMINATED, point.terminated_on
    if point.confirmed_on is not None:
        return REGISTRATION_CONFIRMED, point.confirmed_on
    if point.registered_with_operator_on is not None:
        return REGISTRATION_NOTIFIED, point.registered_with_operator_on
    return REGISTRATION_REGISTERED, None


def point_rows(person, today=None):
    """The person's assignments to non-archived, not ended metering points, with the current (else next) LEG of
    each. Three queries."""
    today = today or timezone.localdate()
    current_leg = LegAssignment.objects.not_ended(today).filter(metering_point=OuterRef("metering_point")).order_by("valid_from")
    found = (
        PersonAssignment.objects.not_ended(today)
        .filter(person=person, metering_point__is_archived=False)
        .select_related("metering_point", "metering_point__location")
        .annotate(leg_id=Subquery(current_leg.values("leg_id")[:1]), leg_name=Subquery(current_leg.values("leg__name")[:1]))
    )
    rows = []
    for assignment in found:
        point = assignment.metering_point
        location = point.location
        registration, registration_date = registration_state(point, today)
        rows.append(
            PointRow(
                assignment=assignment, point=point,
                location_id=point.location_id,
                street=location.street if location else "", house_number=location.house_number if location else "",
                leg_id=assignment.leg_id, leg_name=assignment.leg_name or "", registration=registration,
                registration_date=registration_date,
                termination_date=point.terminated_on if point.terminated_on and point.terminated_on > today else None,
            )
        )
    return rows


def _house_key(text):
    """A house number sorted as a number first, then as text ("2" before "12", "12" before "12a")."""
    digits = ""
    for character in text:
        if not (character.isascii() and character.isdigit()):
            break
        digits += character
    return (int(digits) if digits else 0, text.casefold())


def _point_key(row, key):
    """The sort value of a row, or None if it has none in this column (those come last)."""
    if key == "location":
        return (row.street.casefold(), _house_key(row.house_number)) if row.location_id else None
    if key == "number":
        return row.point.number or None
    if key == "direction":
        return row.point.direction
    if key == "leg":
        return row.leg_name.casefold() if row.leg_id else None
    if key == "assigned":
        return row.assignment.valid_from
    if key == "registration":
        return (_REGISTRATION_RANK[row.registration], row.registration_date or "")
    raise ValueError(f"Unknown column: {key!r}")


def sort_point_rows(rows, key, descending=False):
    """Sort by a column; the number breaks ties. Rows without a value come last, also when descending."""
    if key not in POINT_SORT_KEYS:
        key = DEFAULT_POINT_SORT
    ordered = sorted(rows, key=lambda row: (row.point.number or "", row.assignment.valid_from))
    valued = [row for row in ordered if _point_key(row, key) is not None]
    missing = [row for row in ordered if _point_key(row, key) is None]
    valued.sort(key=lambda row: _point_key(row, key), reverse=descending)
    return valued + missing
