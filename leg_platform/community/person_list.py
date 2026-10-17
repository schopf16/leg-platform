"""Queries and rules of the person list and of the metering points of one person (D-081;
``personen/VORGABEN.md``, ``personen/detail/VORGABEN.md``, ``LISTEN-STANDARD.md``).

A person is an account of ``accounts.User`` that has a customer number (D-077, point 5): the unverified
accounts (registration not completed) have none and are not persons. The account states give three words:

* **Active** (``Aktiv``): the account state ``active`` and not temporarily locked.
* **Blocked** (``Gesperrt``): the account state ``blocked``, or an active account that the lockout of the
  login protection holds for the moment (five wrong logins, 15 minutes; ``accounts.login_protection``). What
  the specification says: "vorübergehend gesperrt, zum Beispiel nach Fehlversuchen".
* **Inactive** (``Inaktiv``): the account state ``deactivated`` (the account was deleted and is kept under the
  retention obligation); these accounts are the archive and appear only with the filter "archived".

The metering points of a person are the metering points that are not archived and have an assignment of this
person that is valid now or starts later (``not_ended``: an assignment that ends in the future still counts).
The word behind the count is "Aufnahme" if at least one of them is still *registered* (no notification to the
BKW; ``status.MeteringPointStatus.REGISTERED``), else "angemeldet" (every one is at least notified). The
search reads the name, the customer number, the address, the town and the e-mail addresses, never a note, the
BKW customer number or the IBAN (``LISTEN-STANDARD.md``, section 4; L-6 of the security review of block 1).
Pure query code, no request: the page passes the values it parsed.
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

# The three words of the column "Account" (``personen/VORGABEN.md``, section 5).
class AccountState(models.TextChoices):
    ACTIVE = "active", _("Active")
    BLOCKED = "blocked", _("Blocked")
    INACTIVE = "inactive", _("Inactive")


ACCOUNT_RANK = {AccountState.ACTIVE: 0, AccountState.BLOCKED: 1, AccountState.INACTIVE: 2}


def locked_account_ids(now=None):
    """The ids of the active persons that the lockout of the login protection holds at the moment.

    Axes stores only a keyed digest of the login address (``login_protection.axes_username``), never the
    address itself, so the digests of the accounts are computed and compared. The rule is the one of Axes
    (``AxesDatabaseHandler``): the failures of the cool-off period, at the limit of the project. Nothing is
    computed while no lockout exists, which is the normal case."""
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
    ).values_list("pk", "normalized_email")
    return {pk for pk, address in candidates if axes_username(None, {"username": address}) in digests}


def account_state(person, locked_ids=()):
    """The state of the account as one of the three words."""
    status = person.status
    if status == type(person).Status.DEACTIVATED:
        return AccountState.INACTIVE
    if status == type(person).Status.BLOCKED or person.pk in locked_ids:
        return AccountState.BLOCKED
    return AccountState.ACTIVE


def display_names(person):
    """``(name, contact)``: the bold text and the grey text below it. A company shows the company name
    and, below it, the contact person (the first and the last name of the account stay "optional" with a
    company name, D-032); any other person shows first and last name and no second line."""
    personal = f"{person.first_name} {person.last_name}".strip()
    if person.organisation_name:
        return person.organisation_name, personal
    return personal, ""


def points_word(point_count, has_unregistered):
    """The state word behind the count: "" without a metering point, "registered" if one is still being
    recorded, else "notified". The page gives the words ("Aufnahme", "angemeldet")."""
    if not point_count:
        return ""
    return "registered" if has_unregistered else "notified"


def mine(today):
    """The assignments of the person of the outer query that count for the person (see the module text)."""
    return PersonAssignment.objects.not_ended(today).filter(person=OuterRef("pk"), metering_point__is_archived=False)


def base_queryset(today=None, locked_ids=()):
    """The persons with the columns of the list: ``sort_name``, ``postal_text``, ``point_count``,
    ``has_unregistered`` and ``account_rank`` (0 active, 1 blocked, 2 inactive)."""
    today = today or timezone.localdate()
    user_model = get_user_model()
    assignments = mine(today)
    count = assignments.order_by().values("person").annotate(total=Count("metering_point", distinct=True)).values("total")
    unregistered = assignments.filter(
        metering_point__in=MeteringPoint.objects.with_status(MeteringPointStatus.REGISTERED, today).values("pk")
    )
    return user_model.objects.filter(customer_number__isnull=False).annotate(
        sort_name=Case(
            When(~Q(organisation_name=""), then=F("organisation_name")),
            default=Trim(Concat("first_name", Value(" "), "last_name")),
            output_field=CharField(),
        ),
        postal_text=Cast("swiss_postal_code", CharField()),
        point_count=Coalesce(Subquery(count, output_field=IntegerField()), 0),
        has_unregistered=Exists(unregistered),
        account_rank=Case(
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
    """"All" and the two states show the accounts that are not inactive; "archived" shows only the
    inactive ones (``LISTEN-STANDARD.md``, section 4)."""
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
    """Every word must match something that the list or the detail page shows: a part of the name (also the
    contact person of a company), the customer number, the street, the house number, the postal code, the
    town or an e-mail address. Letters match without regard to case or accents (the collation of the
    columns). Never a note, the BKW customer number or the IBAN."""
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
    """Sort by a column; the name and then the customer number are the tie breakers. The name is sorted
    by its bold text (a company by the company name)."""
    first = []
    if key == "number":
        first = [F("customer_number")]
    elif key == "address":
        first = [F("street"), Cast("house_number", IntegerField()), F("house_number")]
    elif key == "points":
        first = [F("point_count")]
    elif key == "account":
        first = [F("account_rank")]
    elif key == "name":
        first = [F("sort_name")]
    primary = [item.desc() if descending else item.asc() for item in first]
    return queryset.order_by(*primary, F("sort_name").asc(), F("customer_number").asc())


# The metering points of one person ----------------------------------------------------------------

POINT_SORT_KEYS = ("location", "number", "direction", "leg", "assigned", "bkw")
DEFAULT_POINT_SORT = "number"

BKW_REGISTERED = "registered"
BKW_NOTIFIED = "notified"
BKW_CONFIRMED = "confirmed"
_BKW_RANK = {BKW_REGISTERED: 0, BKW_NOTIFIED: 1, BKW_CONFIRMED: 2}


@dataclass
class PointRow:
    """One assignment of the person with what the table shows. ``bkw`` is ``registered``, ``notified`` (the
    date is the day of the notification) or ``confirmed`` (the day on which the confirmation was recorded)."""

    assignment: object
    point: object
    location_id: int | None
    street: str
    house_number: str
    leg_id: int | None
    leg_name: str
    bkw: str
    bkw_date: object

    @property
    def address(self):
        return f"{self.street} {self.house_number}".strip()


def bkw_state(point):
    """What the column "BKW-Anmeldung" says: the words of the metering point status ("registriert",
    "angemeldet am", "bestätigt am"): the confirmation date if there is one, else the date of the
    notification, else registered. A terminated metering point shows the last step it reached."""
    if point.confirmed_on is not None:
        return BKW_CONFIRMED, point.confirmed_on
    if point.registered_with_bkw_on is not None:
        return BKW_NOTIFIED, point.registered_with_bkw_on
    return BKW_REGISTERED, None


def point_rows(person, today=None):
    """The assignments of the person to metering points that are not archived and not ended (valid now or
    later), with the LEG that is valid now (else the next one) of each metering point. Three queries."""
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
        bkw, bkw_date = bkw_state(point)
        rows.append(
            PointRow(
                assignment=assignment, point=point,
                location_id=point.location_id,
                street=location.street if location else "", house_number=location.house_number if location else "",
                leg_id=assignment.leg_id, leg_name=assignment.leg_name or "", bkw=bkw, bkw_date=bkw_date,
            )
        )
    return rows


def _house_key(text):
    """A house number sorted as a number first, then as text ("2" before "12", "12" before "12a")."""
    digits = ""
    for character in text:
        if not character.isdigit():
            break
        digits += character
    return (int(digits) if digits else 0, text.casefold())


def _point_key(row, key):
    """The sort value of a row, or None if the row has no value in this column (those come last)."""
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
    if key == "bkw":
        return (_BKW_RANK[row.bkw], row.bkw_date or "")
    raise ValueError(f"Unknown column: {key!r}")


def sort_point_rows(rows, key, descending=False):
    """Sort by a column; the number is the tie breaker. Rows without a value come last, also when
    descending."""
    if key not in POINT_SORT_KEYS:
        key = DEFAULT_POINT_SORT
    ordered = sorted(rows, key=lambda row: (row.point.number or "", row.assignment.valid_from))
    valued = [row for row in ordered if _point_key(row, key) is not None]
    missing = [row for row in ordered if _point_key(row, key) is None]
    valued.sort(key=lambda row: _point_key(row, key), reverse=descending)
    return valued + missing
