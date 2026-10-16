"""The pages of the metering points (D-079): the list with its quick filters, the detail page in each state,
the dialogs (steps, LEG, person, create, archive, delete) with their rights, the refusals of steps that are out
of order, the returns to the place a dialog was opened from and the flows without JavaScript. The rules
themselves are tested at service level (``community/tests/test_metering_point_rules.py``); here only what the
views add. The negative authorization of every URL for anonymous, non-staff, rights-less and second-factor-less
accounts is in ``test_access.py`` (the route walk)."""

from datetime import timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import assignments, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, LegAssignment, ManagementSettings, MeteringPoint, PersonAssignment
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    number,
    staff_user,
)

TODAY = timezone.localdate()


def d(offset=0):
    return TODAY + timedelta(days=offset)


LIST = reverse("management:metering_points")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")


@tag("mariadb")
class MeteringPointPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.none = staff_user("staff.none@example.org")
        cls.viewer = staff_user("staff.view@example.org", "view_meteringpoint")
        cls.adder = staff_user(
            "staff.add@example.org", "view_meteringpoint", "add_meteringpoint", "view_location", "view_leg"
        )
        cls.adder_bare = staff_user("staff.bare@example.org", "view_meteringpoint", "add_meteringpoint")
        cls.changer = staff_user(
            "staff.change@example.org", "view_meteringpoint", "change_meteringpoint", "view_leg",
            "view_personrecord", "view_location",
        )
        cls.no_persons = staff_user("staff.nopersons@example.org", "view_meteringpoint", "change_meteringpoint", "view_leg")
        cls.no_leg = staff_user("staff.noleg@example.org", "view_meteringpoint", "change_meteringpoint")
        cls.deleter = staff_user("staff.delete@example.org", "view_meteringpoint", "delete_meteringpoint")
        cls.full = staff_user("staff.all@example.org")
        cls.full.user_permissions.set(Permission.objects.filter(content_type__app_label="community"))

        settings_row = ManagementSettings.load()
        settings_row.country_code, settings_row.identifier = "CH", "1A2B3C4D5E6"
        settings_row.save()
        cls.leg_a, cls.leg_b, cls.leg_c = make_leg("LEG Bahnhof"), make_leg("LEG Quartier"), make_leg("LEG Seestrasse")
        cls.circuit = make_circuit("TRA21359", "Ittigen Nord")
        cls.place = make_location("Musterweg", "12", "3063", "Ittigen", transformer_circuit=cls.circuit)
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")
        cls.ruth = create_active_user("ruth.person@example.org", first_name="Ruth", last_name="Beispiel")
        cls.max = create_active_user("max.person@example.org", first_name="Max", last_name="Muster")

        cls.registered = make_metering_point(1, location=cls.place, remark="hidden note", pv_power_kwp="5.50")
        cls.no_leg_point = make_metering_point(2, location=cls.place)
        cls.notified = make_metering_point(3, location=cls.place, registered_with_bkw_on=d(-9))
        cls.standby = make_metering_point(4, location=cls.place, registered_with_bkw_on=d(-9), confirmed_on=d(-8))
        cls.active = make_metering_point(
            5, location=cls.place, registered_with_bkw_on=d(-40), confirmed_on=d(-39), start_date=d(-30)
        )
        cls.loose = make_metering_point(6)
        for point in (cls.registered, cls.notified, cls.standby):
            assign_leg(point, cls.leg_a, d(-30))
        assign_leg(cls.active, cls.leg_b, d(-30))
        assign_person(cls.active, cls.anna, d(-30))

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def detail(self, point):
        return reverse("management:metering_points_detail", args=[point.pk])

    def url(self, name, point):
        return reverse(f"management:metering_points_{name}", args=[point.pk])

    # The list -----------------------------------------------------------------------------------

    def test_the_list_shows_the_status_and_counts_the_points_in_the_quick_filters(self):
        page = self.login(self.viewer).get(LIST)
        for text in (
            "Alle (6)", "Bei der BKW anmelden (3)", "Bestätigung ausstehend (1)", "Startdatum ausstehend (1)",
            "archiviert (0)", number(5), "aktiv", "registriert", "Probleme filtern (5)",
        ):
            self.assertContains(page, text)
        # In the view "all" the last column is the status; no button of a step, no button "print".
        for text in ("Als angemeldet setzen", "Drucken", "Messpunkt anlegen"):
            self.assertNotContains(page, text)
        # The warning triangle names the problem in text.
        self.assertContains(page, "Dieser Messpunkt ist keiner LEG zugeordnet.")
        # The full number with the direction, never cut; the column widths have their handle.
        self.assertContains(page, f'{number(5)[:13]}<wbr>{number(5)[13:]}')
        self.assertContains(page, 'class="mgmt-grip"')

    def test_a_quick_filter_with_a_step_shows_the_next_step_instead_of_the_status(self):
        page = self.login(self.changer).get(LIST, {"step": "notify"})
        self.assertContains(page, f'href="{self.url("notify", self.registered)}?next=')
        self.assertContains(page, "Als angemeldet setzen")
        self.assertContains(page, "LEG fehlt")  # a metering point without a LEG has no button yet
        self.assertNotContains(page, number(3)[13:] + "</span>")  # not in this view (point 3 is notified)
        self.assertNotContains(page, "registriert</span>")  # no status column here
        self.assertContains(self.login(self.changer).get(LIST, {"step": "confirm"}), "Bestätigung erfassen")
        self.assertContains(self.login(self.changer).get(LIST, {"step": "start"}), "Startdatum erfassen")
        # Without the right to change there is no button, only the text.
        self.assertNotContains(self.login(self.viewer).get(LIST, {"step": "notify"}), "Als angemeldet setzen")

    def test_the_problem_filter_counts_the_current_view_and_shows_only_those_points(self):
        client = self.login(self.viewer)
        self.assertContains(client.get(LIST, {"step": "notify"}), "Probleme filtern (3)")
        self.assertNotContains(client.get(LIST, {"q": "000005"}), "Probleme filtern")  # point 5 has none
        page = client.get(LIST, {"problems": "1"})
        self.assertNotContains(page, number(5)[13:])
        self.assertContains(page, "Alle (5)")  # the counts follow the view

    def test_search_filters_and_sort_come_from_the_address_and_wrong_values_do_no_harm(self):
        client = self.login(self.viewer)
        self.assertNotContains(client.get(LIST, {"q": "hidden note"}), number(1)[13:])  # never the remark
        self.assertContains(client.get(LIST, {"q": "musterweg"}), "Alle (5)")
        self.assertContains(client.get(LIST, {"leg": str(self.leg_b.pk)}), "Alle (1)")
        self.assertContains(client.get(LIST, {"step": "archived"}), "archiviert (0)")
        # A name is searched only with the right to see persons.
        self.assertContains(self.login(self.changer).get(LIST, {"q": "anna muster"}), "Alle (1)")
        self.assertContains(client.get(LIST, {"q": "anna muster"}), "Alle (0)")
        for params in (
            {"sort": "x; DROP", "dir": "up", "per": "-1", "page": "zz", "step": "?", "leg": "<b>", "q": "%" * 300},
            {"sort": "status", "dir": "desc", "per": "all", "page": "99"},
            {"sort": "leg", "step": "start"},
        ):
            self.assertEqual(client.get(LIST, params).status_code, 200)

    def test_the_create_button_is_there_only_with_the_right_to_add_and_the_menu_has_the_entry(self):
        self.assertContains(self.login(self.adder).get(LIST), "Messpunkt anlegen")
        home = self.login(self.viewer).get(reverse("home")).content.decode()
        self.assertIn(LIST, home)
        self.assertNotIn(LIST, self.login(self.none).get(reverse("home")).content.decode())

    # The detail page ----------------------------------------------------------------------------

    def test_the_detail_page_of_a_point_without_leg_and_person_has_the_banner_and_the_buttons_by_right(self):
        page = self.login(self.changer).get(self.detail(self.no_leg_point)).content.decode()
        for text in (
            "Dieser Messpunkt ist keiner LEG zugeordnet.", "Dieser Messpunkt ist keiner Person zugeordnet.",
            "Als angemeldet setzen", "LEG zuweisen", "Person zuweisen", "Messpunkt archivieren",
            "Erfasst, noch nicht bei der BKW angemeldet.", "TRA21359", number(2),
        ):
            self.assertIn(text, page)
        self.assertNotIn("Messpunkt löschen", page)  # the right to delete is missing
        viewer = self.login(self.viewer).get(self.detail(self.no_leg_point)).content.decode()
        for text in ("Als angemeldet setzen", "LEG zuweisen", "Person zuweisen", "Messpunkt archivieren", "Messpunkt löschen"):
            self.assertNotIn(text, viewer)
        self.assertIn("Dieser Messpunkt ist keiner LEG zugeordnet.", viewer)
        # Without a history the one button is "delete", with the right to delete.
        self.assertIn("Messpunkt löschen", self.login(self.full).get(self.detail(self.no_leg_point)).content.decode())
        # The remarks and the figures are shown to those who see the point.
        shown = self.login(self.viewer).get(self.detail(self.registered)).content.decode()
        self.assertIn("hidden note", shown)
        self.assertIn("5.50 kWp", shown)

    def test_the_states_show_their_step_and_the_start_date(self):
        client = self.login(self.changer)
        notified = client.get(self.detail(self.notified)).content.decode()
        self.assertIn("Bestätigung erfassen", notified)
        standby = client.get(self.detail(self.standby)).content.decode()
        for text in ("Startdatum erfassen", "Anmeldung auf Standby, Startdatum fehlt.", "bestätigt"):
            self.assertIn(text, standby)
        active = client.get(self.detail(self.active)).content.decode()
        for text in ("Kündigen", "LEG wechseln", "Person wechseln", "Zuordnung beenden", "Anna Muster", "aktiv"):
            self.assertIn(text, active)
        self.assertNotIn("Dieser Messpunkt ist keiner", active)  # no problem, no banner
        self.assertNotIn("Anmeldung auf Standby", active)
        terminated = MeteringPoint.objects.get(pk=self.active.pk)
        terminated.terminated_on = d(-1)
        terminated.save()
        page = client.get(self.detail(self.active)).content.decode()
        self.assertIn("gekündigt", page)
        self.assertNotIn("Kündigen", page)

    def test_a_flying_change_shows_the_former_and_the_new_person_and_an_ended_assignment_is_gone(self):
        client = self.login(self.changer)
        assignments.assign("person", self.active, self.ruth, d(10))
        page = client.get(self.detail(self.active)).content.decode()
        for text in ("bisher", "neu", "bis ", "ab ", "Ruth Beispiel", "Zuordnung löschen"):
            self.assertIn(text, page)
        assignments.delete("person", PersonAssignment.objects.get(person=self.ruth))
        assignments.end("person", PersonAssignment.objects.get(person=self.anna), d(-1))
        page = client.get(self.detail(self.active)).content.decode()
        self.assertIn("keine Person", page)
        self.assertNotIn("Zuordnung beenden", page)
        # The ended person stays visible in the history (with the right to see persons).
        self.assertIn("Anna Muster", page)

    def test_the_person_is_shown_and_assigned_only_with_the_right_to_see_persons(self):
        page = self.login(self.no_persons).get(self.detail(self.active)).content.decode()
        self.assertNotIn("Anna Muster", page)
        for text in ("Person wechseln", "Zuordnung beenden", "Person zuweisen"):
            self.assertNotIn(text, page)
        self.assertNotIn(reverse("management:metering_points_person", args=[self.active.pk]), page)

    def test_an_archived_point_offers_only_to_restore_it(self):
        point = make_metering_point(7, location=self.place)
        client = self.login(self.changer)
        client.post(self.url("archive", point), {"next": self.detail(point)})
        page = client.get(self.detail(point)).content.decode()
        self.assertIn("Messpunkt wiederherstellen", page)
        for text in ("Als angemeldet setzen", "LEG zuweisen", "Person zuweisen", "Messpunkt archivieren"):
            self.assertNotIn(text, page)
        self.assertContains(client.get(LIST, {"step": "archived"}), number(7)[13:])
        self.assertNotContains(client.get(LIST), number(7)[13:])
        client.post(self.url("archive", point), {"archive": "0"})
        point.refresh_from_db()
        self.assertFalse(point.is_archived)
        self.assertEqual(
            [e.event for e in history.entries_for(point)], [HistoryEvent.ARCHIVED, HistoryEvent.UNARCHIVED]
        )

    # The steps ----------------------------------------------------------------------------------

    def test_the_steps_towards_the_bkw_through_their_dialogs(self):
        client = self.login(self.changer)
        point = self.no_leg_point
        notify = self.url("notify", point)
        page = client.get(notify, {"next": LIST})
        self.assertContains(page, "Als bei der BKW angemeldet setzen")
        self.assertContains(page, f'value="{TODAY.isoformat()}"')  # "Angemeldet am" is today
        self.assertContains(page, f'href="{LIST}"')
        self.assertRedirects(
            client.post(notify, {"registered_on": d(-1).isoformat(), "next": self.detail(point)}),
            self.detail(point), fetch_redirect_response=False,
        )
        point.refresh_from_db()
        self.assertEqual(point.registered_with_bkw_on, d(-1))
        self.assertEqual(history.entries_for(point).get().actor, self.changer)
        # The step cannot be done twice: the page goes back with a message, a forged post shows the refusal.
        self.assertRedirects(client.get(notify), self.detail(point), fetch_redirect_response=False)
        again = client.post(notify, {"registered_on": d(0).isoformat()})
        self.assertContains(again, "Dieser Schritt ist im jetzigen Status des Messpunkts nicht möglich.")

        confirm = self.url("confirm", point)
        self.assertContains(client.get(confirm), "Ohne Startdatum bleibt die Anmeldung auf Standby.")
        client.post(confirm, {"confirmed_on": d(0).isoformat(), "start_date": ""})
        point.refresh_from_db()
        self.assertEqual((point.confirmed_on, point.start_date), (d(0), None))
        self.assertContains(client.get(self.detail(point)), "Anmeldung auf Standby, Startdatum fehlt.")

        start = self.url("start", point)
        client.post(start, {"start_date": d(30).isoformat()})
        point.refresh_from_db()
        self.assertEqual(point.start_date, d(30))
        self.assertEqual(point.status(), "confirmed")  # until the date is reached
        self.assertContains(client.get(self.detail(point)), d(30).strftime("%d.%m.%Y"))
        self.assertEqual(client.get(start).status_code, 302)  # there is no start date to record any more
        history_page = client.get(self.detail(point)).content.decode()
        for text in ("Als angemeldet gesetzt", f"Bestätigung erfasst", "Startdatum erfasst"):
            self.assertIn(text, history_page)

    def test_the_termination_needs_the_end_of_a_month_and_an_active_point(self):
        client = self.login(self.changer)
        url = self.url("terminate", self.active)
        self.assertContains(client.get(url), "Das Datum ist ein Monatsende.")
        wrong = client.post(url, {"terminated_on": d(40).replace(day=15).isoformat()})
        self.assertContains(wrong, "Geben Sie den letzten Tag eines Monats ein.")
        self.active.refresh_from_db()
        self.assertIsNone(self.active.terminated_on)
        month_end = (d(40).replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        client.post(url, {"terminated_on": month_end.isoformat()})
        self.active.refresh_from_db()
        self.assertEqual(self.active.terminated_on, month_end)
        self.assertEqual(client.get(url).status_code, 302)
        # A point that is not active has no such dialog.
        self.assertEqual(client.get(self.url("terminate", self.registered)).status_code, 302)

    def test_a_step_needs_the_right_to_change_and_cannot_be_forged_in_the_wrong_state(self):
        for user in (self.viewer, self.adder, self.deleter):
            self.assertEqual(self.login(user).post(self.url("notify", self.registered), {"registered_on": d(0).isoformat()}).status_code, 403)
        self.registered.refresh_from_db()
        self.assertIsNone(self.registered.registered_with_bkw_on)
        client = self.login(self.changer)
        # Confirming a point that was never announced, and a date in the future, change nothing.
        client.post(self.url("confirm", self.registered), {"confirmed_on": d(0).isoformat()})
        client.post(self.url("notify", self.registered), {"registered_on": d(3).isoformat()})
        self.registered.refresh_from_db()
        self.assertEqual((self.registered.registered_with_bkw_on, self.registered.confirmed_on), (None, None))
        # An archived point is not changed.
        self.registered.is_archived = True
        self.registered.save()
        client.post(self.url("notify", self.registered), {"registered_on": d(0).isoformat()})
        self.registered.refresh_from_db()
        self.assertIsNone(self.registered.registered_with_bkw_on)

    def test_a_post_without_the_csrf_token_is_refused_and_a_hostile_way_back_is_not_followed(self):
        csrf = self.login(self.changer, enforce_csrf_checks=True)
        self.assertEqual(csrf.post(self.url("notify", self.registered), {"registered_on": d(0).isoformat()}).status_code, 403)
        self.registered.refresh_from_db()
        self.assertIsNone(self.registered.registered_with_bkw_on)
        client = self.login(self.changer)
        for hostile in HOSTILE:
            response = client.post(self.url("archive", self.loose), {"archive": "0", "next": hostile})
            self.assertRedirects(response, self.detail(self.loose), fetch_redirect_response=False)
        page = client.get(self.url("notify", self.registered), {"next": "https://evil.example/"})
        self.assertNotContains(page, 'href="https://evil.example')
        self.assertContains(page, f'href="{self.detail(self.registered)}"')

    # The person ---------------------------------------------------------------------------------

    def test_assigning_changing_ending_and_deleting_a_person(self):
        client = self.login(self.changer)
        url = self.url("person", self.no_leg_point)
        page = client.get(url)
        self.assertContains(page, 'list="person-options"')
        self.assertContains(page, "Name, Kundennummer oder E-Mail suchen")
        # A text that names nobody or several, or a date that is not valid, is refused with a message.
        self.assertContains(client.post(url, {"person": "nobody here", "valid_from": d(0).isoformat()}), "Keine Person gefunden.")
        self.assertContains(client.post(url, {"person": "muster", "valid_from": d(0).isoformat()}), "Mehrere Personen passen.")
        self.assertContains(client.post(url, {"person": "Anna Muster", "valid_from": "x"}), "mgmt-field-error")
        self.assertFalse(PersonAssignment.objects.filter(metering_point=self.no_leg_point).exists())
        response = client.post(
            url, {"person": "anna.person@example.org", "valid_from": d(0).isoformat(), "next": self.detail(self.no_leg_point)}
        )
        self.assertRedirects(response, self.detail(self.no_leg_point), fetch_redirect_response=False)
        assignment = PersonAssignment.objects.get(metering_point=self.no_leg_point)
        self.assertEqual((assignment.person, assignment.valid_from, assignment.valid_to), (self.anna, d(0), None))
        self.anna.refresh_from_db()
        self.assertTrue(self.anna.retention_obligation)  # D-016, set by an assignment that is in force

        # "Person wechseln" on the active point: the former one ends the day before.
        change = self.url("person", self.active)
        self.assertContains(client.get(change), "Hat der Messpunkt schon eine Person, endet diese am Tag vor")
        # A date that is not after the start of the current assignment is refused.
        self.assertContains(
            client.post(change, {"person": "ruth", "valid_from": d(-30).isoformat()}),
            "Das Datum muss nach dem Beginn der jetzigen Zuordnung liegen.",
        )
        client.post(change, {"person": "ruth", "valid_from": d(10).isoformat()})
        self.assertEqual(
            list(PersonAssignment.objects.filter(metering_point=self.active).values_list("valid_to", flat=True)),
            [d(9), None],
        )
        # Ending: the open (new) assignment gets its end; ending before its start is refused.
        end = self.url("person_end", self.active)
        self.assertContains(client.get(end), "Die Zuordnung zu Ruth Beispiel endet.")
        self.assertContains(client.post(end, {"valid_to": d(5).isoformat()}), "mgmt-field-error")
        client.post(end, {"valid_to": d(60).isoformat()})
        self.assertEqual(PersonAssignment.objects.get(person=self.ruth).valid_to, d(60))
        self.assertEqual(client.get(end).status_code, 302)  # nothing left to end
        # Deleting is only for an assignment that never took effect and was not ended.
        delete = self.url("person_delete", self.active)
        self.assertEqual(client.get(delete).status_code, 404)  # ended: no longer deletable
        PersonAssignment.objects.filter(person=self.ruth).update(valid_to=None)
        self.assertContains(client.get(delete), "Zuordnung löschen")
        client.post(delete, {"next": self.detail(self.active)})
        self.assertFalse(PersonAssignment.objects.filter(person=self.ruth).exists())
        self.assertIsNone(PersonAssignment.objects.get(person=self.anna, metering_point=self.active).valid_to)  # undone
        # An assignment that is in force cannot be deleted, also not with a forged post.
        client.post(delete)
        self.assertTrue(PersonAssignment.objects.filter(person=self.anna, metering_point=self.active).exists())

    def test_the_person_dialogs_need_the_right_to_see_persons_and_the_search_is_bounded(self):
        for name in ("person", "person_end", "person_delete"):
            for user in (self.no_persons, self.viewer):
                response = self.login(user).post(self.url(name, self.active), {"person": "anna", "valid_from": d(1).isoformat()})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(PersonAssignment.objects.filter(metering_point=self.active).count(), 1)
        search = reverse("management:metering_points_person_search")
        self.assertEqual(self.login(self.no_persons).get(search, {"q": "anna"}).status_code, 403)
        client = self.login(self.changer)
        results = client.get(search, {"q": "muster"}).json()["results"]
        self.assertEqual(sorted(text.split(" · ")[0] for text in results), ["Anna Muster", "Max Muster"])
        self.assertNotIn("@", "".join(results))  # the e-mail address is searched, never shown
        self.assertEqual(client.get(search, {"q": "a"}).json()["results"], [])  # at least two characters
        for index in range(15):
            create_active_user(f"bulk{index}.person@example.org", first_name="Bulk", last_name=f"Person{index}")
        self.assertEqual(len(client.get(search, {"q": "bulk"}).json()["results"]), 10)

    # The LEG ------------------------------------------------------------------------------------

    def test_assigning_a_leg_and_the_switch_with_the_move_mask(self):
        client = self.login(self.changer)
        url = self.url("leg", self.no_leg_point)
        self.assertContains(client.get(url), "LEG zuweisen")
        client.post(url, {"leg": self.leg_a.pk, "valid_from": d(0).isoformat()})
        self.assertEqual(LegAssignment.objects.get(metering_point=self.no_leg_point).leg, self.leg_a)

        feed_in = make_metering_point(8, Direction.FEED_IN, self.place)
        other_place = make_metering_point(9, Direction.CONSUMPTION, make_location("Lindenweg", "7"))
        for point in (feed_in, other_place):
            assign_person(point, self.anna, d(-30))
            assign_leg(point, self.leg_b, d(-30))
        switch = self.url("leg", self.active)
        page = client.get(switch)
        self.assertContains(page, "LEG wechseln")
        self.assertContains(page, "Messpunkt " + number(5) + " (Bezug) bei Musterweg 12 wechselt von LEG Quartier zu:")
        self.assertContains(page, "Auch zusammengehörig: 1 weiterer Messpunkt")
        self.assertContains(page, f"{number(8)} (Einspeisung, Musterweg 12, Anna Muster)")
        self.assertNotContains(page, number(9))  # another location
        self.assertNotContains(page, " checked")  # none is preselected
        self.assertNotContains(page, 'value="%d"' % self.leg_b.pk)  # the LEG it has is not offered
        # Without the right to see persons the mask names no person.
        self.assertNotContains(self.login(self.no_persons).get(switch), "Anna Muster")

        # A point that does not belong is refused and nothing moves.
        forged = client.post(switch, {"leg": self.leg_a.pk, "valid_from": d(10).isoformat(), "also": [other_place.pk]})
        self.assertContains(forged, "mgmt-field-error")
        in_leg_a = LegAssignment.objects.filter(leg=self.leg_a).count()
        self.assertEqual(in_leg_a, 4)  # the three of the setup and the one assigned above
        # Only the ticked points move: without a tick the other point keeps its LEG.
        client.post(switch, {"leg": self.leg_a.pk, "valid_from": d(10).isoformat()})
        self.assertEqual(LegAssignment.objects.filter(metering_point=self.active).count(), 2)
        self.assertEqual(LegAssignment.objects.filter(metering_point=feed_in).count(), 1)
        # The LEG that the point has (it is valid until the change) is not offered, so it is refused.
        refused = client.post(switch, {"leg": self.leg_b.pk, "valid_from": d(20).isoformat()})
        self.assertContains(refused, "mgmt-field-error")
        # With the tick both move to the new LEG.
        both = client.post(switch, {"leg": self.leg_c.pk, "valid_from": d(30).isoformat(), "also": [feed_in.pk]})
        self.assertEqual(both.status_code, 302)
        self.assertEqual(LegAssignment.objects.filter(metering_point=feed_in).count(), 2)
        self.assertEqual(LegAssignment.objects.get(metering_point=feed_in, leg=self.leg_c).valid_from, d(30))
        self.assertEqual(LegAssignment.objects.filter(metering_point=other_place).count(), 1)

    def test_the_leg_dialog_needs_the_rights_to_change_the_point_and_to_see_the_legs(self):
        url = self.url("leg", self.no_leg_point)
        for user in (self.viewer, self.adder, self.no_leg):
            self.assertEqual(self.login(user).post(url, {"leg": self.leg_a.pk, "valid_from": d(0).isoformat()}).status_code, 403)
        self.assertFalse(LegAssignment.objects.filter(metering_point=self.no_leg_point).exists())
        # The button is not offered without the right to see the LEGs.
        self.assertNotIn(url, self.login(self.no_leg).get(self.detail(self.no_leg_point)).content.decode())

    # Create, archive, delete --------------------------------------------------------------------

    def test_creating_a_metering_point_from_the_list(self):
        client = self.login(self.adder)
        url = reverse("management:metering_points_new")
        page = client.get(url, {"next": LIST})
        for text in (
            "CH1A2B3C4D5E6", "Messpunkt anlegen", "Nur die letzten Stellen. Land und Identifikator kommen aus den Einstellungen.",
            "Strasse oder Messpunkt-Nummer suchen", "Optional", "Bemerkung", "data-number-check-url",
        ):
            self.assertContains(page, text)
        data = {
            "number_digits": "7", "direction": "feed_in", "location": str(self.place), "leg": self.leg_a.pk,
            "pv_power_kwp": "9.5", "battery_kwh": "", "wallbox_kw": "11", "remark": "  Dach  ", "next": LIST,
        }
        self.assertRedirects(client.post(url, data), LIST, fetch_redirect_response=False)
        created = MeteringPoint.objects.get(number=number(7))
        self.assertEqual(
            (created.direction, created.location, str(created.pv_power_kwp), created.battery_kwh, created.remark),
            (Direction.FEED_IN, self.place, "9.50", None, "Dach"),
        )
        self.assertEqual(LegAssignment.objects.get(metering_point=created).leg, self.leg_a)
        self.assertEqual(history.entries_for(created).first().event, HistoryEvent.CREATED)
        # The number exists: the notice with the link, and nothing is saved.
        again = client.post(url, {**data, "number_digits": "1"})
        self.assertContains(again, "Diese Messpunkt-Nummer gibt es schon.")
        self.assertContains(again, "Zum bestehenden Messpunkt wechseln")
        self.assertContains(again, self.detail(self.registered))
        self.assertEqual(MeteringPoint.objects.filter(number=number(1)).count(), 1)
        for bad in (
            {"number_digits": ""}, {"number_digits": "abc"}, {"number_digits": "1" * 21}, {"direction": "x"},
            {"location": "nowhere"}, {"pv_power_kwp": "-1"}, {"leg": 99999},
        ):
            before = MeteringPoint.objects.count()
            response = client.post(url, {**data, "number_digits": "8", **bad})
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(MeteringPoint.objects.count(), before, bad)

    def test_the_location_and_the_leg_are_chosen_only_with_the_rights_to_see_them(self):
        client = self.login(self.adder_bare)
        url = reverse("management:metering_points_new")
        page = client.get(url)
        self.assertNotContains(page, 'name="location"')
        self.assertNotContains(page, 'name="leg"')
        client.post(url, {"number_digits": "10", "direction": "consumption", "location": str(self.place), "leg": self.leg_a.pk})
        created = MeteringPoint.objects.get(number=number(10))
        self.assertIsNone(created.location)  # forged fields are ignored
        self.assertFalse(LegAssignment.objects.filter(metering_point=created).exists())
        # Neither is the location offered to search for.
        search = reverse("management:metering_points_location_search")
        self.assertEqual(client.get(search, {"q": "muster"}).status_code, 403)
        self.assertEqual(self.login(self.adder).get(search, {"q": "muster"}).json()["results"], [str(self.place)])

    def test_the_number_is_checked_on_leaving_the_field(self):
        check = reverse("management:metering_points_check_number")
        client = self.login(self.adder)
        ok = client.get(check, {"digits": "77"}).json()
        self.assertEqual((ok["message"], ok["duplicate"]), ("", None))
        self.assertTrue(client.get(check, {"digits": "x"}).json()["message"])
        duplicate = client.get(check, {"digits": "2"}).json()
        self.assertEqual(duplicate["duplicate"]["number"], number(2))
        self.assertEqual(duplicate["duplicate"]["url"], self.detail(self.no_leg_point))
        self.assertEqual(self.login(self.viewer).get(check, {"digits": "2"}).status_code, 403)

    def test_deleting_is_a_question_and_only_possible_without_a_history_and_archiving_needs_the_right_to_change(self):
        fresh = make_metering_point(11, location=self.place)
        history.record(fresh, HistoryEvent.CREATED, self.full)
        question = self.login(self.deleter).get(self.url("delete", fresh))
        self.assertContains(question, "Der Messpunkt " + number(11) + " wird endgültig gelöscht.")
        self.assertEqual(self.login(self.changer).get(self.url("delete", fresh)).status_code, 403)
        self.assertEqual(self.login(self.deleter).post(self.url("archive", fresh)).status_code, 403)
        client = self.login(self.deleter)
        self.assertRedirects(client.post(self.url("delete", fresh)), LIST, fetch_redirect_response=False)
        self.assertFalse(MeteringPoint.objects.filter(pk=fresh.pk).exists())
        # With a history: no question, and a forged post is refused with a message.
        history.record(self.notified, HistoryEvent.REGISTERED_WITH_BKW, self.full)
        self.assertEqual(client.get(self.url("delete", self.notified)).status_code, 404)
        response = client.post(self.url("delete", self.notified))
        self.assertRedirects(response, self.detail(self.notified), fetch_redirect_response=False)
        self.assertTrue(MeteringPoint.objects.filter(pk=self.notified.pk).exists())
