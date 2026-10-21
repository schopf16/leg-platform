"""The persons (D-081): the list queries (states, count and word of the metering points, search, filters, sort), the
metering points of one person, and the few things that the staff change (note, customer number at the grid operator, the
history of the shares, the address correction). Service level, no client: the pages are in
``management/tests/test_person_pages.py``."""

import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from axes.models import AccessAttempt
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, tag
from django.utils import timezone

from leg_platform.accounts.login_protection import LOGIN_FAILURE_LIMIT, axes_username
from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.addresses.models import Address, Directory
from leg_platform.community import history, numbers, person_list, persons
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import PersonRecord, ShareChange
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

User = get_user_model()
TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
LATER = TODAY + timedelta(days=30)


@tag("mariadb")
class PersonListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.anna = create_active_user(
            "anna.person@example.org", first_name="Anna", last_name="Muster", street="Musterweg", house_number="12",
            town="Ittigen", swiss_postal_code=3063,
        )
        cls.company = create_active_user(
            "verwaltung.person@example.org", organisation_name="Beispiel Verwaltung AG", first_name="Lea",
            last_name="Probe", street="Verwaltungsstrasse", house_number="4", town="Bern", swiss_postal_code=3011,
        )
        cls.mueller = create_active_user(
            "mueller.person@example.org", first_name="Jürg", last_name="Müller", street="Aarestrasse", house_number="22",
            town="Thun", swiss_postal_code=3600,
        )
        cls.blocked = create_active_user("blocked.person@example.org", first_name="Ruth", last_name="Gesperrt")
        cls.blocked.set_status(User.Status.BLOCKED)
        cls.locked = create_active_user("locked.person@example.org", first_name="Lena", last_name="Locked")
        cls.gone = create_active_user("gone.person@example.org", first_name="Max", last_name="Inaktiv")
        cls.gone.set_status(User.Status.DEACTIVATED)
        # An account that did not finish the registration has no customer number: no person.
        cls.unverified = User.objects.create_user("unverified.person@example.org")

        cls.place = make_location("Musterweg", "12", town="Ittigen", postal_code="3063")
        cls.leg = make_leg("LEG Musterquartier")
        # Anna: one metering point is only registered (Aufnahme), one is confirmed; an archived one, one whose
        # assignment ended and one of somebody else do not count; one that starts later does.
        cls.registered = make_metering_point(1, location=cls.place)
        cls.confirmed = make_metering_point(
            2, location=cls.place, registered_with_operator_on=EARLIER, confirmed_on=EARLIER, start_date=EARLIER
        )
        cls.future = make_metering_point(3, location=cls.place, registered_with_operator_on=EARLIER)
        archived = make_metering_point(4, location=cls.place, is_archived=True)
        ended = make_metering_point(5, location=cls.place)
        foreign = make_metering_point(6, location=cls.place, registered_with_operator_on=EARLIER)
        for point in (cls.registered, cls.confirmed):
            assign_person(point, cls.anna, EARLIER)
        assign_person(cls.future, cls.anna, LATER)
        assign_person(archived, cls.anna, EARLIER)
        assign_person(ended, cls.anna, EARLIER - timedelta(days=60), valid_to=EARLIER)
        assign_person(foreign, cls.mueller, EARLIER, valid_to=TODAY + timedelta(days=5))
        # The company: two notified points (angemeldet).
        for index in (7, 8):
            assign_person(make_metering_point(index, registered_with_operator_on=EARLIER), cls.company, EARLIER)
        assign_leg(cls.registered, cls.leg, EARLIER)
        # The staff-only data of Anna: never searched.
        PersonRecord.objects.create(
            person=cls.anna, staff_note="geheimnotiz", operator_customer_number="OPR-9999", iban="CH9300762011623852957",
        )
        # The login protection locks Lena for the moment (five wrong logins within the cool-off period).
        cls.attempt = AccessAttempt.objects.create(
            username=axes_username(None, {"username": cls.locked.normalized_email}), user_agent="", http_accept="",
            path_info="/login/", get_data="", post_data="", failures_since_start=LOGIN_FAILURE_LIMIT,
        )

    def rows(self, **filters):
        queryset = person_list.base_queryset(TODAY, person_list.locked_account_ids())
        queryset = person_list.apply_status(queryset, filters.get("status", person_list.STATUS_ALL))
        queryset = person_list.apply_points(queryset, filters.get("points", person_list.POINTS_ALL))
        queryset = person_list.apply_search(queryset, filters.get("q", ""))
        return person_list.apply_sort(queryset, filters.get("sort", "name"), filters.get("descending", False))

    def test_a_person_is_an_account_with_a_customer_number_and_has_one_of_three_states(self):
        locked = person_list.locked_account_ids()
        self.assertEqual(locked, {self.locked.pk})
        states = {p.pk: person_list.account_state(p, locked) for p in self.rows(status=person_list.STATUS_ARCHIVED)}
        self.assertEqual(states, {self.gone.pk: person_list.AccountState.INACTIVE})
        shown = {p.pk: p.account_rank for p in self.rows()}
        self.assertNotIn(self.unverified.pk, shown)
        self.assertNotIn(self.gone.pk, shown)  # the archive only with its filter
        self.assertEqual(shown[self.anna.pk], 0)
        self.assertEqual(shown[self.blocked.pk], 1)
        self.assertEqual(shown[self.locked.pk], 1)  # temporarily locked is "Gesperrt"
        self.assertEqual(person_list.account_state(self.blocked), person_list.AccountState.BLOCKED)
        self.assertEqual(person_list.AccountState.INACTIVE.label, "Inaktiv")

    def test_the_status_filter_keeps_the_archive_apart(self):
        def pks(status):
            return {p.pk for p in self.rows(status=status)}

        everybody = {self.anna.pk, self.company.pk, self.mueller.pk, self.blocked.pk, self.locked.pk}
        self.assertEqual(pks(person_list.STATUS_ALL), everybody)
        self.assertEqual(pks(person_list.STATUS_ACTIVE), {self.anna.pk, self.company.pk, self.mueller.pk})
        self.assertEqual(pks(person_list.STATUS_BLOCKED), {self.blocked.pk, self.locked.pk})
        self.assertEqual(pks(person_list.STATUS_ARCHIVED), {self.gone.pk})

    def test_the_lockout_counts_only_within_the_cool_off_period_and_at_the_limit(self):
        AccessAttempt.objects.filter(pk=self.attempt.pk).update(failures_since_start=LOGIN_FAILURE_LIMIT - 1)
        self.assertEqual(person_list.locked_account_ids(), set())
        AccessAttempt.objects.filter(pk=self.attempt.pk).update(
            failures_since_start=LOGIN_FAILURE_LIMIT, attempt_time=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(person_list.locked_account_ids(), set())
        AccessAttempt.objects.filter(pk=self.attempt.pk).update(attempt_time=timezone.now())
        self.assertEqual(person_list.locked_account_ids(), {self.locked.pk})

    def test_the_count_and_the_word_come_from_the_metering_points_that_are_not_archived_or_ended(self):
        by_pk = {p.pk: p for p in self.rows()}
        anna, company, mueller = by_pk[self.anna.pk], by_pk[self.company.pk], by_pk[self.mueller.pk]
        # registered, confirmed and the one that starts later; not the archived one and not the ended one.
        self.assertEqual(anna.point_count, 3)
        self.assertEqual(person_list.points_word(anna.point_count, anna.has_unregistered), "registered")
        self.assertEqual((company.point_count, person_list.points_word(company.point_count, company.has_unregistered)), (2, "notified"))
        # An assignment that ends in the future still counts; the person has no registered point.
        self.assertEqual((mueller.point_count, mueller.has_unregistered), (1, False))
        self.assertEqual(by_pk[self.blocked.pk].point_count, 0)
        self.assertEqual(person_list.points_word(0, False), "")
        self.assertEqual({p.pk for p in self.rows(points=person_list.POINTS_WITH)}, {self.anna.pk, self.company.pk, self.mueller.pk})
        self.assertEqual({p.pk for p in self.rows(points=person_list.POINTS_WITHOUT)}, {self.blocked.pk, self.locked.pk})

    def test_the_search_finds_what_the_list_and_the_page_show_without_case_and_accents(self):
        def found(text):
            return {p.pk for p in self.rows(q=text)}

        self.assertEqual(found("muller"), {self.mueller.pk})  # ü = u
        self.assertEqual(found("JÜRG müller"), {self.mueller.pk})
        self.assertEqual(found("anna muster"), {self.anna.pk})
        # The company by its name and by its contact person.
        self.assertEqual(found("verwaltung"), {self.company.pk})
        self.assertEqual(found("lea probe"), {self.company.pk})
        # The customer number with or without the space, the address, the postal code, the town, the e-mail.
        number = self.anna.customer_number_display
        self.assertEqual(found(number), {self.anna.pk})
        self.assertEqual(found(number.replace(" ", "")), {self.anna.pk})
        self.assertEqual(found("musterweg 12"), {self.anna.pk})
        self.assertEqual(found("3600"), {self.mueller.pk})
        self.assertEqual(found("thun"), {self.mueller.pk})
        self.assertEqual(found("anna.person@example"), {self.anna.pk})

    def test_the_search_never_reads_the_note_the_operator_customer_number_or_the_iban(self):
        for text in ("geheimnotiz", "OPR-9999", "CH9300762011623852957", "9300762011623852957"):
            with self.subTest(text=text):
                self.assertEqual(self.rows(q=text).count(), 0)

    def test_every_column_sorts_with_the_name_and_the_number_as_tie_breakers(self):
        def order(key, descending=False):
            return [p.sort_name for p in self.rows(sort=key, descending=descending)]

        names_up = ["Anna Muster", "Beispiel Verwaltung AG", "Jürg Müller", "Lena Locked", "Ruth Gesperrt"]
        # By the bold text: a company by its company name.
        self.assertEqual(order("name"), names_up)
        self.assertEqual(order("name", True), names_up[::-1])
        self.assertEqual(order("nonsense"), names_up)
        numbers_up = [p.customer_number_id for p in self.rows(sort="number")]
        self.assertEqual(numbers_up, sorted(numbers_up))
        # Aarestrasse, the two accounts of the example street (by name), Musterweg, Verwaltungsstrasse.
        self.assertEqual(order("address"), ["Jürg Müller", "Lena Locked", "Ruth Gesperrt", "Anna Muster", "Beispiel Verwaltung AG"])
        self.assertEqual(order("points", True), ["Anna Muster", "Beispiel Verwaltung AG", "Jürg Müller", "Lena Locked", "Ruth Gesperrt"])
        # Active first, then blocked (Gesperrt) and temporarily locked, which are equal.
        self.assertEqual(order("account"), ["Anna Muster", "Beispiel Verwaltung AG", "Jürg Müller", "Lena Locked", "Ruth Gesperrt"])
        self.assertEqual(order("account", True)[:2], ["Lena Locked", "Ruth Gesperrt"])

    def test_the_two_lines_of_a_name_and_the_words_of_the_state(self):
        self.assertEqual(person_list.display_names(self.company), ("Beispiel Verwaltung AG", "Lea Probe"))
        self.assertEqual(person_list.display_names(self.anna), ("Anna Muster", ""))

    def test_the_metering_points_of_a_person_with_their_leg_assignment_and_registration_state(self):
        rows = person_list.point_rows(self.anna, TODAY)
        by_number = {row.point.number: row for row in rows}
        self.assertEqual(len(rows), 3)  # not the archived point and not the ended assignment
        registered = by_number[self.registered.number]
        self.assertEqual((registered.registration, registered.registration_date), ("registered", None))
        self.assertEqual((registered.leg_name, registered.address), ("LEG Musterquartier", "Musterweg 12"))
        confirmed = by_number[self.confirmed.number]
        self.assertEqual((confirmed.registration, confirmed.registration_date), ("confirmed", EARLIER))
        self.assertEqual((confirmed.leg_id, confirmed.leg_name), (None, ""))
        self.assertEqual(by_number[self.future.number].registration, "notified")
        self.assertEqual(by_number[self.future.number].assignment.valid_from, LATER)

    def test_the_rows_sort_by_every_column_and_rows_without_a_value_come_last(self):
        rows = person_list.point_rows(self.anna, TODAY)

        def order(key, descending=False):
            return [r.point.number[-1] for r in person_list.sort_point_rows(rows, key, descending)]

        self.assertEqual(order("number"), ["1", "2", "3"])
        self.assertEqual(order("number", True), ["3", "2", "1"])
        self.assertEqual(order("registration"), ["1", "3", "2"])  # registered, notified, confirmed
        self.assertEqual(order("assigned", True)[0], "3")  # starts latest
        # Only point 1 has a LEG: it is first when ascending and, as the only one with a value, also when
        # descending; the others (no value) always follow.
        self.assertEqual(order("leg")[0], "1")
        self.assertEqual(order("leg", True)[0], "1")
        self.assertEqual(order("nonsense"), order("number"))


class PointNumberTests(SimpleTestCase):
    def test_a_number_shows_the_grid_operators_block_and_then_blocks_of_four_with_no_break_spaces(self):
        number = "CH1A2B3C4D5E6" + "0" * 19 + "1"
        self.assertEqual(
            numbers.format_metering_point_number(number),
            "CH1A2B3C4D5E6\u00a00000\u00a00000\u00a00000\u00a00000\u00a00001",
        )
        self.assertEqual(numbers.format_metering_point_number(None), "")
        self.assertEqual(numbers.format_metering_point_number("short"), "short")


class RegistrationStateTests(SimpleTestCase):
    def state(self, **dates):
        values = {"terminated_on": None, "confirmed_on": None, "registered_with_operator_on": None, **dates}
        return person_list.registration_state(SimpleNamespace(**values), TODAY)

    def test_a_termination_counts_once_its_day_is_reached_else_the_step_stays(self):
        self.assertEqual(self.state(), (person_list.REGISTRATION_REGISTERED, None))
        self.assertEqual(self.state(registered_with_operator_on=EARLIER), (person_list.REGISTRATION_NOTIFIED, EARLIER))
        self.assertEqual(
            self.state(registered_with_operator_on=EARLIER, confirmed_on=EARLIER),
            (person_list.REGISTRATION_CONFIRMED, EARLIER),
        )
        confirmed = {"registered_with_operator_on": EARLIER, "confirmed_on": EARLIER}
        self.assertEqual(self.state(**confirmed, terminated_on=LATER), (person_list.REGISTRATION_CONFIRMED, EARLIER))
        self.assertEqual(self.state(**confirmed, terminated_on=TODAY), (person_list.REGISTRATION_TERMINATED, TODAY))


@tag("mariadb")
class PersonChangeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = create_active_user("person.change@example.org", first_name="Anna", last_name="Muster")
        cls.actor = create_active_user("actor.change@example.org")
        cls.unverified = User.objects.create_user("unverified.change@example.org")

    def events(self):
        return list(history.entries_for(self.person).values_list("event", "detail"))

    def test_the_record_is_created_when_it_is_first_needed_and_the_history_names_no_value(self):
        self.assertIsNone(persons.record_of(self.person))
        # Nothing to save: no record, no history.
        persons.set_note(self.person, "  ", self.actor)
        persons.set_operator_customer_number(self.person, "", self.actor)
        self.assertIsNone(persons.record_of(self.person))
        self.assertEqual(self.events(), [])

        persons.set_note(self.person, "  Rückruf vereinbart.  ", self.actor)
        persons.set_operator_customer_number(self.person, " OPR   4711 ", self.actor)
        record = persons.record_of(self.person)
        self.assertEqual((record.staff_note, record.operator_customer_number), ("Rückruf vereinbart.", "OPR 4711"))
        # The entries name the event, the actor and the time, never the note, the number or an address (L-6).
        self.assertEqual(
            self.events(),
            [(HistoryEvent.PERSON_NOTE_CHANGED, {}), (HistoryEvent.PERSON_OPERATOR_NUMBER_CHANGED, {})],
        )
        self.assertEqual(history.entries_for(self.person).first().actor, self.actor)

    def test_a_change_that_changes_nothing_writes_no_history_and_an_empty_value_clears(self):
        persons.set_note(self.person, "Eine Notiz", self.actor)
        persons.set_note(self.person, "Eine Notiz", self.actor)
        persons.set_operator_customer_number(self.person, "OPR 1", self.actor)
        persons.set_operator_customer_number(self.person, "OPR 1", self.actor)
        self.assertEqual(len(self.events()), 2)
        persons.set_note(self.person, "", self.actor)
        persons.set_operator_customer_number(self.person, "", self.actor)
        record = persons.record_of(self.person)
        self.assertEqual((record.staff_note, record.operator_customer_number), ("", ""))
        self.assertEqual(len(self.events()), 4)

    def test_the_rules_refuse_what_does_not_fit(self):
        for call, code in (
            (lambda: persons.set_note(self.person, "x" * (persons.NOTE_MAX_LENGTH + 1)), "note_too_long"),
            (lambda: persons.set_operator_customer_number(self.person, "1" * 31), "number_invalid"),
            (lambda: persons.set_operator_customer_number(self.person, "12\x0034"), "number_invalid"),
            (lambda: persons.set_note(self.unverified, "Notiz"), "not_a_person"),
            (lambda: persons.add_share_change(self.unverified, 1, TODAY), "not_a_person"),
        ):
            with self.subTest(code=code), self.assertRaises(persons.PersonError) as raised:
                call()
            self.assertEqual(raised.exception.code, code)
        self.assertIsNone(persons.record_of(self.person))


@tag("mariadb")
class ShareHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = create_active_user("person.shares@example.org", first_name="Anna", last_name="Muster")
        cls.other = create_active_user("other.shares@example.org", first_name="Bruno", last_name="Probe")
        cls.actor = create_active_user("actor.shares@example.org")
        cls.unverified = User.objects.create_user("unverified.shares@example.org")

    def test_an_entry_is_added_with_the_record_the_history_event_and_the_actor(self):
        self.assertIsNone(persons.record_of(self.person))

        entry = persons.add_share_change(self.person, 5, date(2026, 3, 1), self.actor)

        self.assertEqual(
            (entry.record, entry.quantity, entry.valid_from, entry.actor),
            (persons.record_of(self.person), 5, date(2026, 3, 1), self.actor),
        )
        self.assertEqual(ShareChange.objects.count(), 1)
        # The history names the event only, never the number of shares (L-6); the actor is kept.
        logged = history.entries_for(self.person).get()
        self.assertEqual((logged.event, logged.detail, logged.actor), (HistoryEvent.PERSON_SHARES_CHANGED, {}, self.actor))
        # The actor is optional.
        self.assertIsNone(persons.add_share_change(self.person, 6, date(2026, 4, 1)).actor)

    def test_zero_and_the_maximum_are_valid_numbers(self):
        for quantity in (0, persons.SHARES_MAX):
            with self.subTest(quantity=quantity):
                self.assertEqual(persons.add_share_change(self.person, quantity, TODAY, self.actor).quantity, quantity)

    def test_the_rules_refuse_a_wrong_number_and_a_missing_date_and_store_nothing(self):
        for quantity, valid_from, code in (
            (-1, TODAY, "shares_invalid"),
            (persons.SHARES_MAX + 1, TODAY, "shares_invalid"),
            (True, TODAY, "shares_invalid"),
            ("3", TODAY, "shares_invalid"),
            (2.5, TODAY, "shares_invalid"),
            (None, TODAY, "shares_invalid"),
            (3, None, "date_invalid"),
        ):
            with self.subTest(quantity=quantity, valid_from=valid_from), self.assertRaises(persons.PersonError) as raised:
                persons.add_share_change(self.person, quantity, valid_from, self.actor)
            self.assertEqual(raised.exception.code, code)
        self.assertIsNone(persons.record_of(self.person))
        self.assertEqual(ShareChange.objects.count(), 0)
        self.assertEqual(history.entries_for(self.person).count(), 0)

    def test_the_history_lists_the_latest_date_first_and_only_the_entries_of_the_person(self):
        persons.add_share_change(self.person, 2, date(2026, 1, 1), self.actor)
        persons.add_share_change(self.person, 4, date(2026, 6, 1), self.actor)
        persons.add_share_change(self.person, 3, date(2026, 3, 1), self.actor)
        persons.add_share_change(self.other, 9, date(2026, 7, 1), self.actor)

        entries = persons.share_history(self.person)

        self.assertEqual([(e.valid_from, e.quantity) for e in entries],
                         [(date(2026, 6, 1), 4), (date(2026, 3, 1), 3), (date(2026, 1, 1), 2)])
        self.assertEqual(persons.share_history(self.unverified), [])

    def test_the_current_number_is_the_latest_entry_by_date_that_holds_today(self):
        self.assertIsNone(persons.current_shares(persons.share_history(self.person), TODAY))

        # Entered out of order: the date counts, not the time of the entry.
        persons.add_share_change(self.person, 8, EARLIER, self.actor)
        persons.add_share_change(self.person, 3, EARLIER - timedelta(days=10), self.actor)
        self.assertEqual(persons.current_shares(persons.share_history(self.person), TODAY), 8)
        # An entry for a later day does not hold yet; it does from its day on.
        persons.add_share_change(self.person, 0, LATER, self.actor)
        entries = persons.share_history(self.person)
        self.assertEqual(persons.current_shares(entries, TODAY), 8)
        self.assertEqual(persons.current_shares(entries, LATER), 0)
        # Before the first entry no number holds.
        self.assertIsNone(persons.current_shares(entries, EARLIER - timedelta(days=11)))

    def test_on_the_same_date_the_newest_entry_wins(self):
        persons.add_share_change(self.person, 3, EARLIER, self.actor)
        persons.add_share_change(self.person, 7, EARLIER, self.actor)

        entries = persons.share_history(self.person)

        self.assertEqual([e.quantity for e in entries], [7, 3])
        self.assertEqual(persons.current_shares(entries, TODAY), 7)


STREETS = (
    ("Musterweg", "12", "3063", "Ittigen"),
    ("Langerstrasse", "1", "3063", "Ittigen"),
    ("Zusatzstrasse", "9", "3063", "Ittigen-Nord-und-der-sehr-lange-Ortsteilname-der-nicht-passt"),
)


@tag("mariadb")
class PersonAddressCorrectionTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        generation = uuid.uuid4()
        Directory.objects.create(
            pk=1, generation=generation, data_date=TODAY, imported_at=timezone.now(), checksum="0" * 64,
            row_count=len(STREETS),
        )
        for street, number, postal_code, locality in STREETS:
            Address.objects.create(
                generation=generation, street=street, house_number=number, postal_code=postal_code, locality=locality
            )
        cls.actor = staff_user("actor.address@example.org")
        cls.person = create_active_user(
            "person.address@example.org", first_name="Anna", last_name="Muster", street="musterweg", house_number="12",
            town="Bern", swiss_postal_code=3063, phone_number="+41791234567",
        )

    def test_the_suggestion_replaces_only_the_address_and_is_logged_with_the_actor_and_the_time(self):
        suggestion = persons.address_suggestion(self.person)
        self.assertEqual(suggestion.as_tuple(), ("Musterweg", "12", "3063", "Ittigen"))
        before = User.objects.get(pk=self.person.pk)
        persons.accept_address_suggestion(self.person, persons.suggestion_token(suggestion), self.actor)
        person = User.objects.get(pk=self.person.pk)
        self.assertEqual((person.street, person.house_number, person.swiss_postal_code, person.town), ("Musterweg", "12", 3063, "Ittigen"))
        # Nothing else of the account changed: the controlled fields, the name, the phone, the sessions.
        for field in ("status", "email", "is_staff", "is_superuser", "customer_number_id", "retention_obligation",
                      "first_name", "last_name", "phone_number", "session_version", "foreign_postal_code", "country"):
            self.assertEqual(getattr(person, field), getattr(before, field), field)
        self.assertIsNone(persons.address_suggestion(person))
        entry = history.entries_for(person).get()
        self.assertEqual((entry.event, entry.actor, entry.detail), (HistoryEvent.ADDRESS_CORRECTION_ACCEPTED, self.actor, {}))
        self.assertLessEqual(timezone.now() - entry.created_at, timedelta(minutes=1))

    def test_do_not_accept_is_remembered_for_the_address_as_it_is_until_the_address_changes(self):
        token = persons.suggestion_token(persons.address_suggestion(self.person))
        persons.dismiss_address_suggestion(self.person, token, self.actor)
        self.assertIsNone(persons.address_suggestion(self.person))
        self.assertEqual(
            history.entries_for(self.person).get().event, HistoryEvent.ADDRESS_CORRECTION_DISMISSED
        )
        # The street was not changed by it; a changed spelling brings the warning back.
        self.assertEqual(User.objects.get(pk=self.person.pk).street, "musterweg")
        person = User.objects.get(pk=self.person.pk)
        person.town = "Berne"
        person.save(update_fields=["town"])
        self.assertIsNotNone(persons.address_suggestion(person))

    def test_a_decision_needs_the_suggestion_that_was_shown(self):
        token = persons.suggestion_token(persons.address_suggestion(self.person))
        for action in (persons.accept_address_suggestion, persons.dismiss_address_suggestion):
            with self.subTest(action=action.__name__), self.assertRaises(persons.PersonError) as raised:
                action(self.person, "0" * 64, self.actor)
            self.assertEqual(raised.exception.code, "no_open_suggestion")
        self.assertEqual(User.objects.get(pk=self.person.pk).street, "musterweg")
        self.assertEqual(token, persons.suggestion_token(persons.address_suggestion(self.person)))
        self.assertEqual(history.entries_for(self.person).count(), 0)

    def test_the_staff_guard_applies_to_the_decision_and_an_accepted_suggestion_is_a_security_event(self):
        """M-1: the data of a staff user or an administrator is decided on only by an administrator."""
        target = create_active_user(
            "staff.target@example.org", first_name="Max", last_name="Probe", street="musterweg", house_number="12",
            town="Bern", swiss_postal_code=3063,
        )
        target.set_privileges(is_staff=True, is_superuser=False)
        administrator = staff_user("admin.address@example.org", superuser=True)
        plain = create_active_user("plain.address@example.org")
        token = persons.suggestion_token(persons.address_suggestion(target))
        for action in (persons.accept_address_suggestion, persons.dismiss_address_suggestion):
            for actor in (self.actor, plain, target, None):
                with self.subTest(action=action.__name__, actor=getattr(actor, "email", None)):
                    with self.assertRaises(persons.PersonError) as raised:
                        action(target, token, actor)
                    self.assertEqual(raised.exception.code, "not_allowed")
        self.assertEqual(User.objects.get(pk=target.pk).street, "musterweg")
        self.assertEqual(history.entries_for(target).count(), 0)
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF).exists())
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            persons.accept_address_suggestion(target, token, administrator)
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF)
        self.assertEqual((event.account_id, event.actor_id), (target.pk, administrator.pk))
        self.assertIn("fields street, town", "\n".join(logs.output))
        self.assertNotIn("Musterweg", "\n".join(logs.output))
        # A plain customer account is decided on by every staff account, and that too is logged.
        token = persons.suggestion_token(persons.address_suggestion(self.person))
        persons.accept_address_suggestion(self.person, token, self.actor)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF).count(), 2)

    def test_there_is_no_suggestion_for_an_inactive_account_a_foreign_address_or_one_that_does_not_fit(self):
        def make(email, **values):
            return create_active_user(email, first_name="Max", last_name="Probe", **values)

        abroad = make("abroad.address@example.org", street="musterweg", house_number="12", town="Bern",
                      swiss_postal_code=None, foreign_postal_code="10115", country="DE")
        self.assertIsNone(persons.address_suggestion(abroad))
        box = make("box.address@example.org", street="", post_office_box_number=12, house_number="", town="Bern", swiss_postal_code=3063)
        self.assertIsNone(persons.address_suggestion(box))
        # The directory knows the street, but the town does not fit the column of the account (40 characters).
        long_town = make("long.address@example.org", street="zusatzstrasse", house_number="9", town="Bern", swiss_postal_code=3063)
        self.assertIsNone(persons.address_suggestion(long_town))
        gone = make("gone.address@example.org", street="musterweg", house_number="12", town="Bern", swiss_postal_code=3063)
        self.assertIsNotNone(persons.address_suggestion(gone))
        gone.set_status(User.Status.DEACTIVATED)
        self.assertIsNone(persons.address_suggestion(gone))
        with self.assertRaises(persons.PersonError):
            persons.accept_address_suggestion(gone, "0" * 64, self.actor)
