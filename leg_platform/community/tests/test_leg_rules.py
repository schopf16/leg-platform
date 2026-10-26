"""The rules of the LEG pages (D-080), tested without a browser: the formatting of the quota, the services (create,
change the quota and the master data, archive, delete), the list (search, display filter, problems, sort) and the
detail page (members, the state of the last column, the occupants, the sort). The pages themselves are in
``management/tests/test_leg_pages.py``."""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase, tag
from django.utils import timezone

from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.community import history, leg_list, leg_services, legs
from leg_platform.community.archived import Archived
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, HistoryEntry, Leg
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
)

TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)


class QuotaFormattingTests(SimpleTestCase):
    def test_the_quota_is_written_as_the_specification_writes_it(self):
        self.assertEqual(legs.format_quota(Decimal("37.60")), "37.6 %")
        self.assertEqual(legs.format_quota(Decimal("8.20")), "8.2 %")
        self.assertEqual(legs.format_quota(Decimal("40.00")), "40 %")
        self.assertEqual(legs.format_quota(Decimal("100.00")), "100 %")
        self.assertEqual(legs.format_quota(Decimal("0.00")), "0 %")
        self.assertIsNone(legs.format_quota(None))
        self.assertEqual(legs.quota_number(Decimal("37.60")), "37.6")
        self.assertEqual(legs.quota_number(None), "")

    def test_the_level_follows_the_legal_minimum_and_the_threshold_of_the_settings(self):
        for quota, threshold, level in (
            (None, 10, None), (Decimal("4.99"), 10, legs.QuotaLevel.LEGAL), (Decimal("5"), 10, legs.QuotaLevel.WARNING),
            (Decimal("9.99"), 10, legs.QuotaLevel.WARNING), (Decimal("10"), 10, legs.QuotaLevel.OK),
            (Decimal("7"), 6, legs.QuotaLevel.OK),
        ):
            self.assertEqual(legs.quota_level(quota, threshold), level, (quota, threshold))

    def test_the_texts_of_the_quota_line_and_of_the_problems(self):
        self.assertEqual(legs.quota_level_text(legs.QuotaLevel.OK), "Ausreichend")
        self.assertEqual(legs.quota_level_text(legs.QuotaLevel.WARNING, 12), "Unter 12 %")
        self.assertEqual(legs.quota_level_text(legs.QuotaLevel.LEGAL), "Unter 5 %")
        self.assertEqual(legs.problem_text(legs.LegProblem.QUOTA_BELOW_WARNING, 10), "Problem: Produktionsquote unter 10 %")
        self.assertEqual(
            legs.problem_text(legs.LegProblem.QUOTA_BELOW_LEGAL), "Problem: Produktionsquote unter 5 % (gesetzlich nötig)"
        )
        self.assertEqual(
            legs.problem_text(legs.LegProblem.QUOTA_STALE),
            "Problem: Messpunkte seit dem Stand hinzugefügt, Wert neu ablesen",
        )
        self.assertEqual(legs.problem_text(legs.LegProblem.NO_PRODUCER), "Problem: kein Produzent in der LEG")
        self.assertEqual(legs.problem_text(legs.LegProblem.NO_CONSUMER), "Problem: kein Konsument in der LEG")


@tag("mariadb")
class LegServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = create_active_user("leg.actor@example.org")

    def events(self, leg):
        return [entry.event for entry in history.entries_for(leg)]

    def test_creating_a_leg_collapses_the_name_and_writes_the_history(self):
        leg = leg_services.create_leg("  LEG   Muster ", Decimal("37.6"), EARLIER, " internal ", actor=self.actor)
        leg.refresh_from_db()
        self.assertEqual((leg.name, leg.remark, leg.production_quota_percent, leg.quota_checked_on),
                         ("LEG Muster", "internal", Decimal("37.60"), EARLIER))
        entry = history.entries_for(leg).get()
        self.assertEqual((entry.event, entry.actor), (HistoryEvent.CREATED, self.actor))

    def test_a_quota_needs_its_date_and_without_a_quota_there_is_no_date(self):
        with self.assertRaises(ValueError):
            leg_services.create_leg("LEG A", Decimal("20"), None)
        leg = leg_services.create_leg("LEG B", None, EARLIER)
        self.assertEqual((leg.production_quota_percent, leg.quota_checked_on), (None, None))

    def test_the_name_is_unique_ignoring_case_and_white_space(self):
        leg_services.create_leg("LEG Muster")
        for name in ("leg muster", " LEG  Muster", "LEG MUSTER"):
            with self.assertRaises(leg_services.DuplicateName, msg=name):
                leg_services.create_leg(name)
        other = leg_services.create_leg("LEG Andere")
        with self.assertRaises(leg_services.DuplicateName):
            leg_services.update_master_data(other, "leg muster", "")
        # The own name is no duplicate of itself.
        self.assertFalse(leg_services.name_in_use("LEG Andere", exclude_pk=other.pk))
        self.assertTrue(leg_services.name_in_use("LEG Andere"))

    def test_changing_the_quota_saves_value_and_date_together_and_logs_old_and_new(self):
        leg = make_leg("LEG Q", production_quota_percent=Decimal("8.2"), quota_checked_on=EARLIER)
        leg_services.update_quota(leg, Decimal("37.6"), TODAY, actor=self.actor)
        leg.refresh_from_db()
        self.assertEqual((leg.production_quota_percent, leg.quota_checked_on), (Decimal("37.60"), TODAY))
        entry = history.entries_for(leg).get(event=HistoryEvent.QUOTA_CHANGED)
        self.assertEqual(entry.detail, {
            "old": "8.2", "new": "37.6", "old_checked_on": EARLIER.isoformat(), "new_checked_on": TODAY.isoformat(),
        })
        self.assertEqual(entry.actor, self.actor)

    def test_the_same_quota_makes_no_entry_and_removing_it_removes_the_date(self):
        leg = make_leg("LEG Q", production_quota_percent=Decimal("20"), quota_checked_on=EARLIER)
        leg_services.update_quota(leg, Decimal("20"), EARLIER)
        self.assertEqual(self.events(leg), [])
        leg_services.update_quota(leg, None, TODAY)
        leg.refresh_from_db()
        self.assertEqual((leg.production_quota_percent, leg.quota_checked_on), (None, None))
        with self.assertRaises(ValueError):
            leg_services.update_quota(leg, Decimal("20"), None)

    def test_the_master_data_history_names_the_fields_and_never_the_remark(self):
        leg = make_leg("LEG M")
        leg_services.update_master_data(leg, "LEG Neu", "secret remark", actor=self.actor)
        leg.refresh_from_db()
        self.assertEqual((leg.name, leg.remark), ("LEG Neu", "secret remark"))
        entry = history.entries_for(leg).get(event=HistoryEvent.MASTER_DATA_CHANGED)
        self.assertEqual(entry.detail, {"fields": ["name", "remark"]})
        self.assertNotIn("secret", str(entry.detail))
        leg_services.update_master_data(leg, "LEG Neu", "secret remark")  # nothing changed
        self.assertEqual(self.events(leg).count(HistoryEvent.MASTER_DATA_CHANGED), 1)

    def test_archive_and_restore_are_logged_and_a_second_call_changes_nothing(self):
        leg = make_leg("LEG A")
        leg_services.set_archived(leg, True, self.actor)
        leg_services.set_archived(leg, True, self.actor)
        self.assertTrue(Leg.objects.get(pk=leg.pk).is_archived)
        leg_services.set_archived(leg, False, self.actor)
        self.assertEqual(self.events(leg), [HistoryEvent.ARCHIVED, HistoryEvent.UNARCHIVED])

    def test_a_leg_is_deleted_only_without_a_history_of_assigned_metering_points(self):
        fresh = leg_services.create_leg("LEG Fresh", Decimal("12"), EARLIER, actor=self.actor)
        used = make_leg("LEG Used")
        moved = make_leg("LEG Moved")
        assign_leg(make_metering_point(1), used, valid_from=EARLIER)
        assign_leg(make_metering_point(2), moved, valid_from=EARLIER, valid_to=TODAY - timedelta(days=1))
        self.assertTrue(leg_services.can_delete(fresh))
        # In use, and one that moved on: both have a history.
        self.assertFalse(leg_services.can_delete(used))
        self.assertFalse(leg_services.can_delete(moved))
        with self.assertRaises(leg_services.NotDeletable):
            leg_services.delete(used)
        leg_services.update_quota(fresh, Decimal("13"), EARLIER, actor=self.actor)
        leg_services.delete(fresh)
        self.assertFalse(Leg.objects.filter(pk=fresh.pk).exists())
        # Its own entries go with it.
        self.assertFalse(HistoryEntry.objects.filter(subject_type=history.LEG, subject_id=fresh.pk).exists())
        self.assertTrue(Leg.objects.filter(pk=used.pk).exists())

    def test_an_archived_leg_is_not_deleted(self):
        archived_leg = make_leg("LEG Archived", is_archived=True)  # no history: deletable otherwise
        self.assertTrue(leg_services.can_delete(archived_leg))

        with self.assertRaises(Archived):
            leg_services.delete(archived_leg)

        self.assertTrue(Leg.objects.filter(pk=archived_leg.pk).exists())
        # A LEG that is not archived is still deleted.
        plain = make_leg("LEG Plain")
        leg_services.delete(plain)
        self.assertFalse(Leg.objects.filter(pk=plain.pk).exists())

@tag("mariadb")
class LegListQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.circuit_a = make_circuit("TRA21359")
        cls.circuit_b = make_circuit("TRA21361")
        cls.place_a = make_location("Musterweg", "12", transformer_circuit=cls.circuit_a)
        cls.place_a2 = make_location("Werkstrasse", "5", transformer_circuit=cls.circuit_a)
        cls.place_b = make_location("Lindenweg", "7", transformer_circuit=cls.circuit_b)
        # Musterquartier: two circuits (basic discount), 3 : 1, quota 37.6 checked after the points exist.
        cls.muster = make_leg("LEG Musterquartier", production_quota_percent=Decimal("37.6"),
                              quota_checked_on=TODAY, remark="Pilot Ittigen Nord")
        for number, direction, place in ((1, Direction.CONSUMPTION, cls.place_a), (2, Direction.FEED_IN, cls.place_a),
                                         (3, Direction.CONSUMPTION, cls.place_a2), (4, Direction.CONSUMPTION, cls.place_b)):
            assign_leg(make_metering_point(number, direction, place), cls.muster, valid_from=EARLIER)
        # Bahnhof: one circuit (price optimised), consumers only, quota 8.2: two problems.
        cls.bahnhof = make_leg("LEG Bahnhof", production_quota_percent=Decimal("8.2"), quota_checked_on=TODAY)
        for number in (5, 6):
            assign_leg(make_metering_point(number, Direction.CONSUMPTION, cls.place_b), cls.bahnhof, valid_from=EARLIER)
        cls.neuland = make_leg("LEG Neuland")  # no metering points, no quota
        cls.old = make_leg("LEG Alt", is_archived=True, production_quota_percent=Decimal("2"), quota_checked_on=EARLIER)

    def rows(self, display=leg_list.DISPLAY_ALL, text=""):
        queryset = leg_list.apply_search(leg_list.base_queryset(display), text, TODAY)
        return leg_list.rows_for(queryset, 10, TODAY)

    def names(self, rows):
        return [row.leg.name for row in rows]

    def test_the_figures_of_each_leg(self):
        by_name = {row.leg.name: row.summary for row in self.rows()}
        muster, bahnhof, neuland = by_name["LEG Musterquartier"], by_name["LEG Bahnhof"], by_name["LEG Neuland"]
        self.assertEqual((muster.ratio, muster.tier, muster.metering_points), ("3 : 1", legs.DiscountTier.BASIC, 4))
        self.assertEqual((bahnhof.ratio, bahnhof.tier), ("2 : 0", legs.DiscountTier.OPTIMISED))
        self.assertEqual(muster.problems, ())
        self.assertEqual(
            set(bahnhof.problems), {legs.LegProblem.QUOTA_BELOW_WARNING, legs.LegProblem.NO_PRODUCER}
        )
        # No metering points: "–" (no ratio), no tier, and no problem of the participants.
        self.assertEqual((neuland.ratio, neuland.tier, neuland.problems), (None, legs.DiscountTier.NONE, ()))

    def test_the_display_filter_shows_live_or_archived_legs_and_an_archived_one_has_no_problem(self):
        self.assertNotIn("LEG Alt", self.names(self.rows()))
        archived = self.rows(leg_list.DISPLAY_ARCHIVED)
        self.assertEqual(self.names(archived), ["LEG Alt"])
        self.assertEqual(archived[0].problems, ())  # quota 2 % but archived: out of sight

    def test_search_finds_the_name_or_a_transformer_circuit_and_never_the_remark(self):
        self.assertEqual(self.names(self.rows(text="bahnhof")), ["LEG Bahnhof"])
        self.assertEqual(sorted(self.names(self.rows(text="tra21361"))), ["LEG Bahnhof", "LEG Musterquartier"])
        self.assertEqual(self.names(self.rows(text="TRA21359")), ["LEG Musterquartier"])
        self.assertEqual(self.names(self.rows(text="leg tra21359")), ["LEG Musterquartier"])  # every word must match
        self.assertEqual(self.names(self.rows(text="Pilot")), [])  # the remark is never searched
        # A circuit counts only through the members of today.
        self.assertEqual(self.names(self.rows(text="TRA9999")), [])

    def test_problem_filter_keeps_the_legs_with_a_problem(self):
        self.assertEqual(self.names(leg_list.apply_problems(self.rows())), ["LEG Bahnhof"])

    def test_sorting_puts_missing_values_last_in_both_directions(self):
        rows = self.rows()
        for descending in (False, True):
            self.assertEqual(leg_list.sort_rows(rows, "quota", descending)[-1].leg.name, "LEG Neuland")
            self.assertEqual(leg_list.sort_rows(rows, "tier", descending)[-1].leg.name, "LEG Neuland")
            self.assertEqual(leg_list.sort_rows(rows, "participants", descending)[-1].leg.name, "LEG Neuland")
        self.assertEqual(self.names(leg_list.sort_rows(rows, "quota"))[:2], ["LEG Bahnhof", "LEG Musterquartier"])
        self.assertEqual(self.names(leg_list.sort_rows(rows, "quota", True))[:2], ["LEG Musterquartier", "LEG Bahnhof"])
        self.assertEqual(self.names(leg_list.sort_rows(rows, "tier"))[:2], ["LEG Bahnhof", "LEG Musterquartier"])
        self.assertEqual(self.names(leg_list.sort_rows(rows, "participants", True))[:2], ["LEG Musterquartier", "LEG Bahnhof"])
        self.assertEqual(self.names(leg_list.sort_rows(rows, "points"))[0], "LEG Neuland")
        self.assertEqual(self.names(leg_list.sort_rows(rows, "points", True))[0], "LEG Musterquartier")
        self.assertEqual(self.names(leg_list.sort_rows(rows, "name")), sorted(self.names(rows)))
        self.assertEqual(self.names(leg_list.sort_rows(rows, "name", True)), sorted(self.names(rows), reverse=True))
        self.assertEqual(self.names(leg_list.sort_rows(rows, "nonsense")), sorted(self.names(rows)))

    def test_the_stale_quota_is_day_exact(self):
        # The points were created today: a quota checked today is not stale, one checked yesterday is.
        leg_services.update_quota(self.muster, Decimal("37.6"), TODAY - timedelta(days=1))
        problems_now = {row.leg.name: row.summary.problems for row in self.rows()}
        self.assertEqual(problems_now["LEG Musterquartier"], (legs.LegProblem.QUOTA_STALE,))


@tag("mariadb")
class LegDetailQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.circuit_a = make_circuit("TRA21359")
        cls.circuit_b = make_circuit("TRA21361")
        cls.circuit_c = make_circuit("TRA30000")
        cls.place_a = make_location("Musterweg", "12", transformer_circuit=cls.circuit_a)
        cls.place_b = make_location("Lindenweg", "7", transformer_circuit=cls.circuit_b)
        cls.place_b2 = make_location("Lindenweg", "9", transformer_circuit=cls.circuit_b)
        cls.place_c = make_location("Ferne", "1", transformer_circuit=cls.circuit_c)
        cls.place_none = make_location("Ohne", "3")
        cls.mixed = make_leg("LEG Mixed")
        cls.points = {}
        for number, place in ((1, cls.place_a), (2, cls.place_b), (3, cls.place_c), (4, cls.place_none), (5, None)):
            cls.points[number] = make_metering_point(number, Direction.CONSUMPTION, place)
            assign_leg(cls.points[number], cls.mixed, valid_from=EARLIER)
        # A LEG of its own for the circuit B (price optimised, only B), one for A that is archived, and one
        # that covers two circuits (no LEG of its own for anybody).
        cls.own_b = make_leg("LEG Eigen B")
        assign_leg(make_metering_point(10, Direction.FEED_IN, cls.place_b2), cls.own_b, valid_from=EARLIER)
        cls.own_a = make_leg("LEG Eigen A", is_archived=True)
        assign_leg(make_metering_point(11, Direction.FEED_IN, cls.place_a), cls.own_a, valid_from=EARLIER)
        cls.ann = create_active_user("ann.person@example.org", first_name="Ann", last_name="Zeta")
        cls.bob = create_active_user("bob.person@example.org", first_name="Bob", last_name="Alpha")
        assign_person(cls.points[1], cls.ann, EARLIER)
        assign_person(cls.points[2], cls.bob, EARLIER)

    def members(self, leg):
        summary = legs.summaries([leg], 10, TODAY)[leg.pk]
        return summary, leg_list.members(leg, summary, 10, TODAY)

    def by_number(self, rows):
        return {row.point.number[-2:]: row for row in rows}

    def test_a_leg_of_several_circuits_recommends_the_leg_of_its_own_or_says_there_is_none(self):
        summary, rows = self.members(self.mixed)
        self.assertEqual(summary.tier, legs.DiscountTier.BASIC)
        found = self.by_number(rows)
        # Circuit B has the LEG of its own: recommended. Circuit A's is archived, circuit C has none.
        self.assertEqual((found["02"].action, found["02"].other_leg), (leg_list.ACTION_RECOMMEND, self.own_b))
        self.assertEqual((found["01"].action, found["01"].other_leg), (leg_list.ACTION_NONE, None))
        self.assertEqual(found["03"].action, leg_list.ACTION_NONE)
        # No location or no circuit: nothing can be said.
        self.assertIsNone(found["04"].action)
        self.assertIsNone(found["05"].action)
        self.assertEqual((found["01"].circuit, found["05"].circuit), ("TRA21359", ""))

    def test_a_leg_of_one_circuit_is_the_own_leg_of_all_its_metering_points(self):
        summary, rows = self.members(self.own_b)
        self.assertEqual(summary.tier, legs.DiscountTier.OPTIMISED)
        self.assertEqual([row.action for row in rows], [leg_list.ACTION_OWN])

    def test_a_dedicated_leg_is_the_first_by_name_and_never_the_leg_itself(self):
        second = make_leg("LEG Eigen B2")
        assign_leg(make_metering_point(12, Direction.FEED_IN, self.place_b2), second, valid_from=EARLIER)
        dedicated = leg_list.dedicated_legs(self.mixed, 10, TODAY)
        self.assertEqual(dedicated[self.circuit_b.pk], self.own_b)
        self.assertNotIn(self.circuit_a.pk, dedicated)  # archived
        # The LEG that is asked about is excluded: its own circuit goes to the next one, never to itself.
        self.assertEqual(leg_list.dedicated_legs(self.own_b, 10, TODAY)[self.circuit_b.pk], second)

    def test_the_members_are_the_points_of_today_and_not_archived_ones(self):
        gone = make_metering_point(20, Direction.CONSUMPTION, self.place_a)
        assign_leg(gone, self.mixed, valid_from=EARLIER, valid_to=TODAY - timedelta(days=1))
        archived = make_metering_point(21, Direction.CONSUMPTION, self.place_a, is_archived=True)
        assign_leg(archived, self.mixed, valid_from=EARLIER)
        future = make_metering_point(22, Direction.CONSUMPTION, self.place_a)
        assign_leg(future, self.mixed, valid_from=TODAY + timedelta(days=5))
        numbers = {point.number[-2:] for point in leg_list.members_of(self.mixed, TODAY)}
        self.assertEqual(numbers, {"01", "02", "03", "04", "05"})

    def test_the_occupant_names_are_the_names_of_the_persons(self):
        points = [row.point for row in self.members(self.mixed)[1]]
        self.assertEqual(
            leg_list.occupants(points, TODAY), {self.points[1].pk: "Ann Zeta", self.points[2].pk: "Bob Alpha"}
        )

    def test_sorting_the_table_by_each_column(self):
        _summary, rows = self.members(self.mixed)
        occupants = leg_list.occupants([row.point for row in rows], TODAY)

        def order(key, descending=False):
            return [row.point.number[-2:] for row in leg_list.sort_members(rows, key, descending, occupants)]

        self.assertEqual(order("number"), ["01", "02", "03", "04", "05"])
        self.assertEqual(order("number", True), ["05", "04", "03", "02", "01"])
        # A metering point without a location comes last, in both directions.
        self.assertEqual(order("address"), ["03", "02", "01", "04", "05"])
        self.assertEqual(order("address", True), ["04", "01", "02", "03", "05"])
        self.assertEqual(order("circuit")[:3], ["01", "02", "03"])
        self.assertEqual(order("circuit", True)[:3], ["03", "02", "01"])
        self.assertEqual(order("circuit")[-2:], ["04", "05"])
        # Occupants sort by the name as it is shown; points without a person come last.
        self.assertEqual(order("occupant")[:2], ["01", "02"])
        self.assertEqual(order("occupant", True)[:2], ["02", "01"])
        self.assertEqual(set(order("occupant")[2:]), {"03", "04", "05"})
        self.assertEqual(len(order("status")), 5)
