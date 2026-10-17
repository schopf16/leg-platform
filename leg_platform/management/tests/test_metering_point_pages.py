"""The pages of the metering points (D-079): the list with its quick filters, the detail page in each state (also
the display of a termination, D-086), the dialogs (steps, LEG, person, create, archive, delete), the refusals of steps
that are out of order, the returns to the place a dialog was opened from and the flows without JavaScript. The rules
themselves are tested at service level (``community/tests/test_metering_point_rules.py``); here only what the views
add. Every staff account has every action (D-086); the negative authorization of every URL for anonymous, non-staff
and second-factor-less accounts is in ``test_access.py`` (the route walk)."""

from datetime import timedelta

from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import assignments, history, metering_points
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Direction,
    GridOperator,
    LegAssignment,
    ManagementSettings,
    MeteringPoint,
    PersonAssignment,
)
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


def month_end_after(days):
    """The last day of the month that contains the day ``days`` from now."""
    day = d(days)
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


LIST = reverse("management:metering_points")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")


@tag("mariadb")
class MeteringPointPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.point@example.org")

        operator = GridOperator.load()
        operator.name = "Netzbetreiber Beispiel"
        operator.save()
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
        cls.notified = make_metering_point(3, location=cls.place, registered_with_operator_on=d(-9))
        cls.standby = make_metering_point(4, location=cls.place, registered_with_operator_on=d(-9), confirmed_on=d(-8))
        cls.active = make_metering_point(
            5, location=cls.place, registered_with_operator_on=d(-40), confirmed_on=d(-39), start_date=d(-30)
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
        page = self.login(self.staff).get(LIST)
        for text in (
            "Alle (6)", "Bei Netzbetreiber Beispiel anmelden (3)", "Bestätigung ausstehend (1)",
            "Startdatum ausstehend (1)", "archiviert (0)", number(5), "aktiv", "registriert", "Probleme filtern (5)",
            "Messpunkt anlegen",
        ):
            self.assertContains(page, text)
        # In the view "all" the last column is the status; no button of a step; the button "Print" is in the head.
        self.assertNotContains(page, "Als angemeldet setzen")
        self.assertContains(page, ">Drucken</a>")
        # The grid operator is named by its name from the instance, never by a fixed name.
        self.assertNotContains(page, "BKW")
        # The warning triangle names the problem in text.
        self.assertContains(page, "Dieser Messpunkt ist keiner LEG zugeordnet.")
        # The full number with the direction, never cut; the column widths have their handle.
        self.assertContains(page, f'{number(5)[:13]}<wbr>{number(5)[13:]}')
        self.assertContains(page, 'class="mgmt-grip"')
        self.assertContains(page, 'name="leg"')  # the filter by LEG

    def test_a_quick_filter_with_a_step_shows_the_next_step_instead_of_the_status(self):
        client = self.login(self.staff)
        page = client.get(LIST, {"step": "notify"})
        self.assertContains(page, f'href="{self.url("notify", self.registered)}?next=')
        self.assertContains(page, "Als angemeldet setzen")
        self.assertContains(page, "LEG fehlt")  # a metering point without a LEG has no button yet
        self.assertNotContains(page, number(3)[13:] + "</span>")  # not in this view (point 3 is notified)
        self.assertNotContains(page, "registriert</span>")  # no status column here
        self.assertContains(client.get(LIST, {"step": "confirm"}), "Bestätigung erfassen")
        self.assertContains(client.get(LIST, {"step": "start"}), "Startdatum erfassen")

    def test_the_problem_filter_counts_the_current_view_and_shows_only_those_points(self):
        client = self.login(self.staff)
        self.assertContains(client.get(LIST, {"step": "notify"}), "Probleme filtern (3)")
        self.assertNotContains(client.get(LIST, {"q": "000005"}), "Probleme filtern")  # point 5 has none
        page = client.get(LIST, {"problems": "1"})
        self.assertNotContains(page, number(5)[13:])
        self.assertContains(page, "Alle (5)")  # the counts follow the view

    def test_search_filters_and_sort_come_from_the_address_and_wrong_values_do_no_harm(self):
        client = self.login(self.staff)
        self.assertNotContains(client.get(LIST, {"q": "hidden note"}), number(1)[13:])  # never the remark
        # The address, the LEG name and the name of the person are searched (every staff account, D-086).
        self.assertContains(client.get(LIST, {"q": "musterweg"}), "Alle (5)")
        self.assertContains(client.get(LIST, {"q": "bahnhof"}), "Alle (3)")
        self.assertContains(client.get(LIST, {"q": "anna muster"}), "Alle (1)")
        self.assertContains(client.get(LIST, {"q": number(1)[20:]}), "Alle (1)")
        self.assertContains(client.get(LIST, {"leg": str(self.leg_b.pk)}), "Alle (1)")
        self.assertContains(client.get(LIST, {"sort": "leg"}), "sort=leg")
        self.assertContains(client.get(LIST, {"step": "archived"}), "archiviert (0)")
        for params in (
            {"sort": "x; DROP", "dir": "up", "per": "-1", "page": "zz", "step": "?", "leg": "<b>", "q": "%" * 300},
            {"sort": "status", "dir": "desc", "per": "all", "page": "99"},
            {"sort": "leg", "step": "start"},
        ):
            self.assertEqual(client.get(LIST, params).status_code, 200)

    def test_the_menu_has_the_entry(self):
        self.assertIn(LIST, self.login(self.staff).get(reverse("home")).content.decode())

    # The detail page ----------------------------------------------------------------------------

    def test_the_detail_page_of_a_point_without_leg_and_person_has_the_banner_and_the_buttons(self):
        page = self.login(self.staff).get(self.detail(self.no_leg_point)).content.decode()
        for text in (
            "Dieser Messpunkt ist keiner LEG zugeordnet.", "Dieser Messpunkt ist keiner Person zugeordnet.",
            "Als angemeldet setzen", "LEG zuweisen", "Person zuweisen",
            "Messpunkt löschen",  # without a history the one button is "delete"
            "Erfasst, noch nicht bei Netzbetreiber Beispiel angemeldet.", "TRA21359", number(2),
        ):
            self.assertIn(text, page)
        self.assertNotIn("BKW", page)
        # The remarks and the figures are shown.
        shown = self.login(self.staff).get(self.detail(self.registered)).content.decode()
        self.assertIn("hidden note", shown)
        self.assertIn("5.50 kWp", shown)

    def test_the_states_show_their_step_and_the_start_date(self):
        client = self.login(self.staff)
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

    def test_the_status_card_shows_a_recorded_termination_as_a_note_until_the_day_is_reached(self):
        client = self.login(self.staff)
        # (c) A point without a termination shows neither text.
        page = client.get(self.detail(self.active)).content.decode()
        self.assertIn(">aktiv</span>", page)
        for text in ("Kündigung zum", "Die Zuordnung ist beendet.", "Gekündigt zum"):
            self.assertNotIn(text, page)
        # (a) A termination in the future: the point is still active and the note names the day.
        end = month_end_after(40)
        metering_points.terminate(self.active, end, self.staff, today=TODAY)
        page = client.get(self.detail(self.active)).content.decode()
        self.assertIn(">aktiv</span>", page)
        self.assertIn(f"Kündigung zum {end.strftime('%d.%m.%Y')}", page)
        for text in ("Die Zuordnung ist beendet.", "Gekündigt zum", ">gekündigt</span>"):
            self.assertNotIn(text, page)
        # (b) Once the day is reached (the day itself or earlier) the point is terminated and the assignment ended.
        for day in (d(-1), TODAY):
            MeteringPoint.objects.filter(pk=self.active.pk).update(terminated_on=day)
            page = client.get(self.detail(self.active)).content.decode()
            self.assertIn(">gekündigt</span>", page, day)
            self.assertIn("Die Zuordnung ist beendet.", page, day)
            for text in ("Kündigung zum", "Gekündigt zum", ">aktiv</span>", "Kündigen"):
                self.assertNotIn(text, page, day)

    def test_a_flying_change_shows_the_former_and_the_new_person_and_an_ended_assignment_is_gone(self):
        client = self.login(self.staff)
        assignments.assign("person", self.active, self.ruth, d(10))
        page = client.get(self.detail(self.active)).content.decode()
        for text in ("bisher", "neu", "bis ", "ab ", "Ruth Beispiel", "Zuordnung löschen"):
            self.assertIn(text, page)
        assignments.delete("person", PersonAssignment.objects.get(person=self.ruth))
        assignments.end("person", PersonAssignment.objects.get(person=self.anna), d(-1))
        page = client.get(self.detail(self.active)).content.decode()
        self.assertIn("keine Person", page)
        self.assertNotIn("Zuordnung beenden", page)
        # The ended person stays visible in the history.
        self.assertIn("Anna Muster", page)

    def test_an_archived_point_offers_only_to_restore_it(self):
        point = make_metering_point(7, location=self.place)
        client = self.login(self.staff)
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

    def test_the_steps_towards_the_grid_operator_through_their_dialogs(self):
        client = self.login(self.staff)
        point = self.no_leg_point
        notify = self.url("notify", point)
        page = client.get(notify, {"next": LIST})
        self.assertContains(page, "Als bei Netzbetreiber Beispiel angemeldet setzen")
        self.assertNotContains(page, "BKW")
        self.assertContains(page, f'value="{TODAY.isoformat()}"')  # "Angemeldet am" is today
        self.assertContains(page, f'href="{LIST}"')
        self.assertRedirects(
            client.post(notify, {"registered_on": d(-1).isoformat(), "next": self.detail(point)}),
            self.detail(point), fetch_redirect_response=False,
        )
        point.refresh_from_db()
        self.assertEqual(point.registered_with_operator_on, d(-1))
        self.assertEqual(history.entries_for(point).get().actor, self.staff)
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
        client = self.login(self.staff)
        url = self.url("terminate", self.active)
        self.assertContains(client.get(url), "Das Datum ist ein Monatsende.")
        wrong = client.post(url, {"terminated_on": d(40).replace(day=15).isoformat()})
        self.assertContains(wrong, "Geben Sie den letzten Tag eines Monats ein.")
        self.active.refresh_from_db()
        self.assertIsNone(self.active.terminated_on)
        month_end = month_end_after(40)
        client.post(url, {"terminated_on": month_end.isoformat()})
        self.active.refresh_from_db()
        self.assertEqual(self.active.terminated_on, month_end)
        self.assertEqual(client.get(url).status_code, 302)
        # A point that is not active has no such dialog.
        self.assertEqual(client.get(self.url("terminate", self.registered)).status_code, 302)

    def test_a_step_cannot_be_forged_in_the_wrong_state(self):
        client = self.login(self.staff)
        # Confirming a point that was never announced, and a date in the future, change nothing.
        client.post(self.url("confirm", self.registered), {"confirmed_on": d(0).isoformat()})
        client.post(self.url("notify", self.registered), {"registered_on": d(3).isoformat()})
        self.registered.refresh_from_db()
        self.assertEqual((self.registered.registered_with_operator_on, self.registered.confirmed_on), (None, None))
        # An archived point is not changed.
        self.registered.is_archived = True
        self.registered.save()
        client.post(self.url("notify", self.registered), {"registered_on": d(0).isoformat()})
        self.registered.refresh_from_db()
        self.assertIsNone(self.registered.registered_with_operator_on)

    def test_a_post_without_the_csrf_token_is_refused_and_a_hostile_way_back_is_not_followed(self):
        csrf = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(csrf.post(self.url("notify", self.registered), {"registered_on": d(0).isoformat()}).status_code, 403)
        self.registered.refresh_from_db()
        self.assertIsNone(self.registered.registered_with_operator_on)
        client = self.login(self.staff)
        for hostile in HOSTILE:
            response = client.post(self.url("archive", self.loose), {"archive": "0", "next": hostile})
            self.assertRedirects(response, self.detail(self.loose), fetch_redirect_response=False)
        page = client.get(self.url("notify", self.registered), {"next": "https://evil.example/"})
        self.assertNotContains(page, 'href="https://evil.example')
        self.assertContains(page, f'href="{self.detail(self.registered)}"')

    def test_a_hostile_way_back_is_never_followed_by_the_dialogs_that_create_change_and_assign(self):
        """The dialogs of D-095 (create, create at a location, edit, assign to a location, assign to a LEG): a ``next``
        that leaves the site is ignored on the page (way back) and after the save (redirect)."""
        client = self.login(self.staff)

        def on_this_site(location):
            self.assertTrue(location.startswith("/management/"), location)
            self.assertNotIn("evil", location)
            self.assertNotIn("//", location.split("?")[0])

        for index, hostile in enumerate(HOSTILE):
            with self.subTest(next=hostile):
                edited = make_metering_point(300 + index)
                moved = make_metering_point(400 + index)
                joined = make_metering_point(500 + index)
                dialogs = (
                    (reverse("management:metering_points_new"), {"number_digits": str(100 + index), "direction": "consumption"}),
                    (
                        reverse("management:locations_metering_point_new", args=[self.place.pk]),
                        {"number_digits": str(200 + index), "direction": "consumption"},
                    ),
                    (self.url("edit", edited), {"location": str(self.place.pk)}),
                    (
                        reverse("management:locations_metering_point_assign", args=[self.place.pk]),
                        {"metering_point": str(moved.pk)},
                    ),
                    (
                        reverse("management:legs_metering_point_assign", args=[self.leg_a.pk]),
                        {"metering_point": str(joined.pk), "valid_from": d(0).isoformat()},
                    ),
                )
                for url, data in dialogs:
                    page = client.get(url, {"next": hostile})
                    on_this_site(page.context["back_url"])
                    self.assertNotContains(page, 'href="https://evil.example')
                    response = client.post(url, {**data, "next": hostile})
                    self.assertEqual(response.status_code, 302, url)
                    on_this_site(response["Location"])
        self.assertEqual(MeteringPoint.objects.get(number=number(500)).location_id, None)
        self.assertEqual(LegAssignment.objects.get(metering_point__number=number(500)).leg, self.leg_a)
        self.assertEqual(MeteringPoint.objects.get(number=number(400)).location, self.place)

    # The person ---------------------------------------------------------------------------------

    def test_assigning_changing_ending_and_deleting_a_person(self):
        client = self.login(self.staff)
        url = self.url("person", self.no_leg_point)
        page = client.get(url)
        # The person field is a combobox of the page script (no browser list): the address of the suggestions is
        # in the field, and the way to create a missing person is under it (D-094).
        self.assertContains(page, f'data-suggest-url="{reverse("management:metering_points_person_search")}"')
        self.assertNotContains(page, "<datalist")
        self.assertContains(page, "Person fehlt? Neue Person einladen")
        self.assertContains(page, "Name, Kundennummer oder E-Mail suchen")
        self.assertContains(page, "Person zuweisen</button>")
        # A text that names nobody or several, or a date that is not valid, is refused with a message.
        nobody = client.post(url, {"person": "nobody here", "valid_from": d(0).isoformat()})
        self.assertContains(nobody, "Keine Person gefunden. Zugewiesen werden können nur aktive Personen mit Kundennummer.")
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
        # Ending: the open (new) assignment gets its end; ending before its start is refused. The dialog names the
        # assignment that it showed; another id (a page that was open for a while) ends nothing (D-084, L-6).
        end = self.url("person_end", self.active)
        ruth_id = PersonAssignment.objects.get(person=self.ruth).pk
        self.assertContains(client.get(end), "Die Zuordnung zu Ruth Beispiel endet.")
        self.assertContains(client.get(end), f'name="assignment" value="{ruth_id}"')
        self.assertContains(client.post(end, {"valid_to": d(5).isoformat(), "assignment": ruth_id}), "mgmt-field-error")
        for stale in ("", "999999", str(ruth_id + 1)):
            client.post(end, {"valid_to": d(60).isoformat(), "assignment": stale})
            self.assertIsNone(PersonAssignment.objects.get(person=self.ruth).valid_to)
        client.post(end, {"valid_to": d(60).isoformat(), "assignment": ruth_id})
        self.assertEqual(PersonAssignment.objects.get(person=self.ruth).valid_to, d(60))
        self.assertEqual(client.get(end).status_code, 302)  # nothing left to end
        # Deleting is only for an assignment that never took effect and was not ended.
        delete = self.url("person_delete", self.active)
        self.assertEqual(client.get(delete).status_code, 404)  # ended: no longer deletable
        PersonAssignment.objects.filter(person=self.ruth).update(valid_to=None)
        self.assertContains(client.get(delete), "Zuordnung löschen")
        self.assertContains(client.get(delete), f'name="assignment" value="{ruth_id}"')
        # Without the id of the assignment that the page showed (or with another one) nothing is deleted.
        for stale in ({}, {"assignment": "999999"}, {"assignment": str(ruth_id + 1)}):
            client.post(delete, {"next": self.detail(self.active), **stale})
            self.assertTrue(PersonAssignment.objects.filter(person=self.ruth).exists())
        client.post(delete, {"next": self.detail(self.active), "assignment": ruth_id})
        self.assertFalse(PersonAssignment.objects.filter(person=self.ruth).exists())
        self.assertIsNone(PersonAssignment.objects.get(person=self.anna, metering_point=self.active).valid_to)  # undone
        # An assignment that is in force cannot be deleted, also not with a forged post.
        anna_id = PersonAssignment.objects.get(person=self.anna, metering_point=self.active).pk
        client.post(delete, {"assignment": anna_id})
        self.assertTrue(PersonAssignment.objects.filter(person=self.anna, metering_point=self.active).exists())

    def test_the_person_search_is_bounded(self):
        search = reverse("management:metering_points_person_search")
        client = self.login(self.staff)
        results = client.get(search, {"q": "muster"}).json()["results"]
        self.assertEqual(sorted(text.split(" · ")[0] for text in results), ["Anna Muster", "Max Muster"])
        self.assertNotIn("@", "".join(results))  # the e-mail address is searched, never shown
        self.assertEqual(client.get(search, {"q": "a"}).json()["results"], [])  # at least two characters
        for index in range(15):
            create_active_user(f"bulk{index}.person@example.org", first_name="Bulk", last_name=f"Person{index}")
        self.assertEqual(len(client.get(search, {"q": "bulk"}).json()["results"]), 10)

    def test_the_person_search_is_limited_per_account_and_cut_to_five_words_and_a_hundred_characters(self):
        from unittest import mock

        from leg_platform.accounts import rate_limits

        search = reverse("management:metering_points_person_search")
        client = self.login(self.staff)
        with mock.patch.object(rate_limits, "PERSON_SEARCH_LIMITS", ((2, timedelta(minutes=1)),)):
            self.assertEqual(client.get(search, {"q": "muster"}).status_code, 200)
            self.assertEqual(client.get(search, {"q": "muster"}).status_code, 200)
            refused = client.get(search, {"q": "muster"})
            self.assertEqual(refused.status_code, 429)
            self.assertEqual(refused.json(), {"results": [], "limited": True})
            self.assertEqual(refused["Retry-After"], "30")
            # A query that is too short is not counted and not refused.
            self.assertEqual(client.get(search, {"q": "m"}).status_code, 200)
        # Only the first five words filter: "zzz" is the sixth word and is ignored, so Anna is still found.
        found = client.get(search, {"q": "anna muster a a a zzz"}).json()["results"]
        self.assertEqual([text.split(" · ")[0] for text in found], ["Anna Muster"])
        self.assertEqual(client.get(search, {"q": "anna muster a a zzz"}).json()["results"], [])  # the fifth word
        # The text is cut to 100 characters before it is split.
        self.assertEqual(client.get(search, {"q": "anna " + " " * 100 + "zzz"}).status_code, 200)
        self.assertEqual(len(client.get(search, {"q": "anna muster " + "a" * 500}).json()["results"]), 0)

    # The LEG ------------------------------------------------------------------------------------

    def test_assigning_a_leg_and_the_switch_with_the_move_mask(self):
        client = self.login(self.staff)
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

    # Create, archive, delete --------------------------------------------------------------------

    def test_creating_a_metering_point_from_the_list(self):
        client = self.login(self.staff)
        url = reverse("management:metering_points_new")
        page = client.get(url, {"next": LIST})
        for text in (
            "CH1A2B3C4D5E6", "Messpunkt anlegen",
            "Die letzten 20 Zeichen: Grossbuchstaben A–Z, Ziffern 0–9 und der Bindestrich (Kleinbuchstaben werden zu Grossbuchstaben). Kürzere Eingaben werden links mit Nullen aufgefüllt (23 wird zu …0023).",
            "Standort fehlt? Neuen Standort anlegen", "Noch kein Standort gewählt", "Noch keine LEG gewählt",
            "Optional", "Bemerkung", 'data-check="digits"', 'data-check="decimal"', 'name="location"', 'name="leg"',
        ):
            self.assertContains(page, text)
        for gone in ("<datalist", 'list="location-options"', "Strasse oder Messpunkt-Nummer suchen", 'maxlength="20"'):
            self.assertNotContains(page, gone)
        data = {
            "number_digits": "7", "direction": "feed_in", "location": str(self.place.pk), "leg": self.leg_a.pk,
            "pv_power_kwp": "9.5", "battery_kwh": "", "wallbox_kw": "11,25", "remark": "  Dach  ", "next": LIST,
        }
        response = client.post(url, data)
        created = MeteringPoint.objects.get(number=number(7))
        # The page of the new metering point opens; its way back is the list.
        self.assertRedirects(response, f"{self.detail(created)}?new=1&next={LIST.replace('/', '%2F')}", fetch_redirect_response=False)
        self.assertEqual(
            (created.direction, created.location, str(created.pv_power_kwp), created.battery_kwh, created.remark),
            (Direction.FEED_IN, self.place, "9.50", None, "Dach"),
        )
        self.assertEqual(str(created.wallbox_kw), "11.25")  # the decimal comma
        self.assertEqual(LegAssignment.objects.get(metering_point=created).leg, self.leg_a)
        self.assertEqual(history.entries_for(created).first().event, HistoryEvent.CREATED)
        # The number exists: the notice with the link, and nothing is saved.
        again = client.post(url, {**data, "number_digits": "1"})
        self.assertContains(again, "Diese Messpunkt-Nummer gibt es schon.")
        self.assertContains(again, "Zum bestehenden Messpunkt wechseln")
        self.assertContains(again, self.detail(self.registered))
        self.assertEqual(MeteringPoint.objects.filter(number=number(1)).count(), 1)
        for bad in (
            {"number_digits": ""}, {"number_digits": "a!c"}, {"number_digits": "1" * 21}, {"direction": "x"},
            {"location": "nowhere"}, {"location": "999999"}, {"pv_power_kwp": "-1"}, {"leg": 99999},
        ):
            before = MeteringPoint.objects.count()
            response = client.post(url, {**data, "number_digits": "8", **bad})
            self.assertEqual(response.status_code, 200, bad)
            self.assertEqual(MeteringPoint.objects.count(), before, bad)

    def test_the_number_is_normalised_to_twenty_characters_and_every_wrong_entry_has_its_own_message(self):
        # D-102: A-Z, 0-9 and the hyphen; zeros on the left; a longer entry only loses leading zeros.
        client = self.login(self.staff)
        url = reverse("management:metering_points_new")
        base = {"direction": "consumption", "location": ""}
        prefix = "CH1A2B3C4D5E6"

        def stored(suffix):
            return MeteringPoint.objects.filter(number=prefix + suffix).exists()

        for typed, suffix in (
            ("23", "23".rjust(20, "0")),  # shorter
            ("0" + "4" * 20, "4" * 20),  # 21 characters, one leading zero
            ("0" * 25, "0" * 20),  # all zeros: valid
            ("ab-12", "AB-12".rjust(20, "0")),  # small letters and a hyphen
            ("A" * 20, "A" * 20),  # exactly 20
        ):
            with self.subTest(accepted=typed[:25]):
                created = client.post(url, {**base, "number_digits": typed})
                self.assertEqual(created.status_code, 302)
                self.assertTrue(stored(suffix), typed)
        # The same value in another spelling is the same number (duplicate detection on the normalised value).
        again = client.post(url, {**base, "number_digits": "000" + "a" * 20})
        self.assertContains(again, "Diese Messpunkt-Nummer gibt es schon.")
        self.assertEqual(MeteringPoint.objects.filter(number=prefix + "A" * 20).count(), 1)
        # A page shown again holds the normalised value.
        shown = client.post(url, {**base, "number_digits": "5", "pv_power_kwp": "x"})
        self.assertContains(shown, 'value="00000000000000000005"')
        too_long = "Die Messpunktnummer ist ungültig: Sie ist zu lang."
        not_allowed = "Erlaubt sind nur die Buchstaben A–Z, die Ziffern 0–9 und der Bindestrich."
        before = MeteringPoint.objects.count()
        for typed, message in (
            ("", "Geben Sie die letzten Zeichen der Nummer ein."),
            ("12!", not_allowed), ("1 2", not_allowed), ("٣", not_allowed), ("ß", not_allowed), ("a_b", not_allowed),
            ("1" * 21, too_long + " Es sind höchstens 20 Zeichen erlaubt"),
            ("0" + "1" * 21, "(eingegeben: 22)"),  # 22 characters with one leading zero
            ("1" + "0" * 20, too_long),  # a zero that is not at the start stays
        ):
            with self.subTest(typed=typed):
                response = client.post(url, {**base, "number_digits": typed})
                self.assertContains(response, message)
        self.assertEqual(MeteringPoint.objects.count(), before)

    def test_the_live_check_uses_the_same_rule(self):
        client = self.login(self.staff)
        check = reverse("management:metering_points_check_number")
        for typed, message in (
            ("0" + "4" * 20, ""), ("ab-1", ""), ("0" * 150 + "7", ""),
            ("12!", "Erlaubt sind nur die Buchstaben"), ("1" * 21, "Die Messpunktnummer ist ungültig"),
            ("0" + "1" * 21, "Die Messpunktnummer ist ungültig"),
        ):
            with self.subTest(typed=typed[:25]):
                answer = client.get(check, {"digits": typed}).json()
                self.assertTrue(answer["message"].startswith(message) if message else answer["message"] == "", answer)
        # An entry of an existing point is the duplicate, whatever its spelling.
        existing = self.registered.number[13:]
        found = client.get(check, {"digits": "00" + existing.lower()}).json()
        self.assertEqual(found["message"], "")
        self.assertIsNotNone(found["duplicate"])

    def test_the_power_fields_take_digits_and_one_separator_only(self):
        client = self.login(self.staff)
        url = reverse("management:metering_points_new")
        base = {"direction": "consumption", "location": "", "number_digits": "31"}
        messages = {
            "invalid": "Geben Sie eine Zahl ein, z. B. 12.5 (Ziffern und ein Dezimalpunkt oder -komma).",
            "places": "Höchstens 2 Nachkommastellen.",
            "range": "Der Wert muss zwischen 0 und 999999.99 liegen.",
        }
        for typed, kind in (
            ("abc", "invalid"), ("12a", "invalid"), ("1.2.3", "invalid"), ("1,2,3", "invalid"), ("-1", "invalid"),
            ("+1", "invalid"), (".", "invalid"), (",", "invalid"), ("1e3", "invalid"), ("1 000", "invalid"),
            ("١", "invalid"), ("1.234", "places"), ("1,234", "places"), ("1000000", "range"), ("999999.991", "places"),
        ):
            with self.subTest(typed=typed):
                response = client.post(url, {**base, "pv_power_kwp": typed})
                self.assertContains(response, messages[kind])
                self.assertFalse(MeteringPoint.objects.filter(number=number(31)).exists())
        for typed, expected in (("12,5", "12.50"), ("12.5", "12.50"), ("0", "0.00"), ("999999.99", "999999.99"), (".5", "0.50"), ("7.", "7.00")):
            with self.subTest(accepted=typed):
                client.post(url, {**base, "pv_power_kwp": typed})
                point = MeteringPoint.objects.get(number=number(31))
                self.assertEqual(str(point.pv_power_kwp), expected)
                point.delete()

    def test_changing_a_metering_point_sets_the_location_and_the_plant_data(self):
        client = self.login(self.staff)
        point = self.loose
        url = self.url("edit", point)
        page = client.get(url)
        # The same form as "Create metering point": number and direction are facts, the LEG has its own dialog.
        self.assertContains(page, "Messpunkt bearbeiten")
        self.assertContains(page, point.number)
        self.assertNotContains(page, 'name="number_digits"')
        self.assertNotContains(page, 'name="direction"')
        self.assertContains(page, self.url("leg", point))
        self.assertContains(page, "Der Trafokreis ergibt sich aus dem Standort.")
        response = client.post(
            url, {"location": str(self.place.pk), "pv_power_kwp": "4,5", "remark": "Dach", "number_digits": "9999", "direction": "feed_in"}
        )
        self.assertRedirects(response, self.detail(point), fetch_redirect_response=False)
        point.refresh_from_db()
        self.assertEqual((point.location, str(point.pv_power_kwp), point.remark), (self.place, "4.50", "Dach"))
        # Number and direction are not changed by a forged post.
        self.assertNotEqual(point.number, "CH1A2B3C4D5E6" + "9999".zfill(20))
        self.assertEqual(point.direction, Direction.CONSUMPTION)
        events = [(entry.event, entry.detail) for entry in history.entries_for(point)]
        self.assertIn(
            (HistoryEvent.METERING_POINT_LOCATION_CHANGED, {"old_location_id": None, "new_location_id": self.place.pk}), events
        )
        self.assertIn((HistoryEvent.METERING_POINT_DATA_CHANGED, {"fields": ["pv_power_kwp", "remark"]}), events)
        self.assertNotIn("Dach", str(events))  # the history names fields, never values
        # The detail page offers the action where there was none.
        loose_page = client.get(self.detail(self.loose))
        self.assertContains(loose_page, self.url("edit", self.loose))

    def test_changing_a_metering_point_refuses_a_location_that_cannot_be_chosen(self):
        client = self.login(self.staff)
        archived = make_location("Altweg", "9", is_archived=True)
        url = self.url("edit", self.registered)
        before = self.registered.location
        for forged in (str(archived.pk), "999999", "abc", "-1", "9" * 40):
            with self.subTest(forged=forged[:12]):
                response = client.post(url, {"location": forged})
                self.assertEqual(response.status_code, 200)
        self.registered.refresh_from_db()
        self.assertEqual(self.registered.location, before)
        # An archived metering point is not changed.
        self.registered.is_archived = True
        self.registered.save()
        client.post(url, {"location": ""})
        self.registered.refresh_from_db()
        self.assertEqual(self.registered.location, before)
    def test_the_number_is_checked_on_leaving_the_field(self):
        check = reverse("management:metering_points_check_number")
        client = self.login(self.staff)
        ok = client.get(check, {"digits": "77"}).json()
        self.assertEqual((ok["message"], ok["duplicate"]), ("", None))
        self.assertTrue(client.get(check, {"digits": "x!"}).json()["message"])
        duplicate = client.get(check, {"digits": "2"}).json()
        self.assertEqual(duplicate["duplicate"]["number"], number(2))
        self.assertEqual(duplicate["duplicate"]["url"], self.detail(self.no_leg_point))

    def test_deleting_is_a_question_and_only_possible_without_a_history(self):
        fresh = make_metering_point(11, location=self.place)
        history.record(fresh, HistoryEvent.CREATED, self.staff)
        client = self.login(self.staff)
        question = client.get(self.url("delete", fresh))
        self.assertContains(question, "Der Messpunkt " + number(11) + " wird endgültig gelöscht.")
        self.assertRedirects(client.post(self.url("delete", fresh)), LIST, fetch_redirect_response=False)
        self.assertFalse(MeteringPoint.objects.filter(pk=fresh.pk).exists())
        # With a history: no question, and a forged post is refused with a message.
        history.record(self.notified, HistoryEvent.REGISTERED_WITH_OPERATOR, self.staff)
        self.assertEqual(client.get(self.url("delete", self.notified)).status_code, 404)
        response = client.post(self.url("delete", self.notified))
        self.assertRedirects(response, self.detail(self.notified), fetch_redirect_response=False)
        self.assertTrue(MeteringPoint.objects.filter(pk=self.notified.pk).exists())
