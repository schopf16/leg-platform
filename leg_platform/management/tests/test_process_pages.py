"""The pages of the processes (D-103; owner design ``verwaltung/vorgaenge``): the list of the running applications and
deregistrations, the page of one application and of one deregistration, the staff's steps as dialogs, the notices and the
queues of the overview.

What is proven: drafts are listed as "not submitted" with a read-only page and no staff step (the dialogs and services
refuse them); every staff step calls the service and the state is checked again (a step twice
or out of order is refused with a message and changes nothing); a refused date shows its reason in the dialog; an unknown id
is a 404; the IBAN is never on the page; the customer is told by the "rolled back" mail and keeps the account, the bank
connection and the stored buildings; every route is closed to a customer, an anonymous visitor and a staff account without
the second factor (``test_access`` walks every route, ``test_acceptance_matrix`` asks each one).
"""

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.models import MailJob
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import (
    application_notices,
    application_staff,
    applications,
    deregistrations,
    history,
    numbers,
    own_iban,
    process_list,
)
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationNotice,
    CustomerMessage,
    Deregistration,
    Direction,
    Location,
    MeteringPoint,
    PersonAssignment,
)
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import (
    GOOD_IBAN,
    TODAY,
    configure_numbers,
    customer,
    full_number,
    publish_contract,
    ready_draft,
)

State = Application.State
STARTED = date(2026, 6, 1)
CONFIRMED = date(2026, 7, 1)
START = date(2026, 9, 1)  # three months after the registration at the grid operator


class ProcessBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        configure_numbers()
        cls.staff = staff_user("process.staff@example.org")
        cls.plain = create_active_user("process.plain@example.org")
        cls.anna = customer("anna.process@example.org", first_name="Anna", last_name="Muster")
        cls.bert = customer("bert.process@example.org", first_name="Bert", last_name="Beispiel")
        publish_contract(cls.staff)

    def setUp(self):
        self.client = self.login(self.staff)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def submitted(self, person=None, **kwargs):
        person = person or self.anna
        application, _building = ready_draft(person, **kwargs)
        applications.submit(person, application, today=TODAY)
        application.refresh_from_db()
        return application

    def at_operator(self, person=None, **kwargs):
        application = self.submitted(person, **kwargs)
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        application_staff.record_operator_confirmation(application, self.staff, CONFIRMED, today=TODAY)
        application.refresh_from_db()
        return application

    def state(self, application):
        return Application.objects.get(pk=application.pk).state


@tag("mariadb")
class ProcessListTests(ProcessBase):
    URL = reverse("management:processes")

    def test_it_lists_drafts_marked_as_not_submitted_next_to_the_submitted_applications(self):
        shown = self.submitted(self.anna, egid=90004269)
        draft, _building = ready_draft(self.bert, egid=90004270, number=7, address={"house_number": "52"})
        paused, _building = ready_draft(
            customer("paul.process@example.org", first_name="Paul", last_name="Pause"), egid=90004271, number=9,
            address={"house_number": "53"},
        )
        Application.objects.filter(pk=paused.pk).update(state=State.PAUSED)
        page = self.client.get(self.URL)
        self.assertContains(page, "Anna Muster")
        self.assertContains(page, "Ittigenstrasse 51, 3063 Ittigen")
        self.assertContains(page, "EGID 90004269")
        # D-107: the drafts are listed, clearly marked, with a link to their read-only page.
        self.assertContains(page, "Bert Beispiel")
        self.assertContains(page, "Paul Pause")
        self.assertContains(page, "EGID 90004270")
        # Once in the table and once in the cards of a narrow screen (D-097).
        self.assertContains(page, '<span class="mgmt-status is-muted">Entwurf / nicht eingereicht</span>', count=2)
        self.assertContains(page, '<span class="mgmt-status is-muted">Pausiert / nicht eingereicht</span>', count=2)
        self.assertContains(page, "1–3 von 3 Vorgängen")
        for application in (shown, draft, paused):
            self.assertContains(page, reverse("management:processes_application", args=[application.pk]))
        # The filter "draft" joins the draft and the paused draft; the others do not show them.
        only_drafts = self.client.get(self.URL, {"state": "draft"})
        self.assertContains(only_drafts, "1–2 von 2 Vorgängen")
        self.assertNotContains(only_drafts, "Anna Muster")
        submitted_only = self.client.get(self.URL, {"state": "submitted"})
        self.assertContains(submitted_only, "1–1 von 1 Vorgängen")
        self.assertNotContains(submitted_only, "Bert Beispiel")

    def test_the_row_shows_the_status_as_dot_and_word_the_progress_and_the_start(self):
        self.submitted(self.anna)
        page = self.client.get(self.URL)
        self.assertContains(page, '<span class="mgmt-status is-warn">Eingereicht</span>')
        self.assertContains(page, "1 von 5 Schritten")
        self.assertContains(page, '<progress class="mgmt-progress" value="1" max="5"')
        self.assertContains(page, "So bald wie möglich")
        self.assertContains(page, "Startdatum")
        # No warning triangle and no button "Filter problems": no problems of processes are defined.
        self.assertNotContains(page, "mgmt-warning")
        self.assertNotContains(page, "Probleme filtern")
        # The columns of the owner's list and a fixed link column; every column can be sorted.
        for label in ("Person", "Art", "Gebäude", "Messpunkte", "Status", "Fortschritt", "Datum", "Letzte Änderung"):
            self.assertContains(page, label)
        self.assertContains(page, "Zum Vorgang")

    def test_every_state_has_its_word_and_the_missing_date_is_the_grey_word_open(self):
        first = self.submitted(self.anna, egid=90004269)
        second = self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        application_staff.start_review(second, self.staff)
        self.at_operator(customer("carl.process@example.org", first_name="Carl", last_name="Probe"), egid=90004271,
                         number=9, address={"house_number": "53"})
        page = self.client.get(self.URL)
        for word in ("Eingereicht", "In Prüfung", "Von Netzbetreiber bestätigt"):
            self.assertContains(page, word)
        # No draft in this data: no row carries the draft mark (the filter option and the column help name the word).
        self.assertNotContains(page, 'is-muted">Entwurf')
        # An application whose start the customer left as "as soon as possible" has no date: the word, never a dash.
        Application.objects.filter(pk=first.pk).update(start_choice="", desired_start_date=None)
        self.assertContains(self.client.get(self.URL), '<span class="mgmt-open">offen</span>')

    def test_the_filters_the_search_and_the_sorting(self):
        anna = self.submitted(self.anna, egid=90004269)
        bert = self.submitted(self.bert, egid=90004270, number=5, address={"street": "Bernstrasse", "house_number": "52"})
        application_staff.start_review(bert, self.staff)
        for query, shown, hidden in (
            ("state=submitted", "Anna Muster", "Bert Beispiel"),
            ("state=in_review", "Bert Beispiel", "Anna Muster"),
            ("kind=deregistration", None, "Anna Muster"),
            ("q=beispiel", "Bert Beispiel", "Anna Muster"),
            ("q=90004269", "Anna Muster", "Bert Beispiel"),
            ("q=Bernstrasse", "Bert Beispiel", "Anna Muster"),
            (f"q={self.anna.customer_number_display.replace(' ', '+')}", "Anna Muster", "Bert Beispiel"),
            # An unknown value is the default: both rows.
            ("state=nonsense&kind=nonsense", "Anna Muster", None),
        ):
            with self.subTest(query=query):
                page = self.client.get(f"{self.URL}?{query}")
                if shown:
                    self.assertContains(page, shown)
                if hidden:
                    self.assertNotContains(page, hidden)
        ordered = self.client.get(f"{self.URL}?sort=person").content.decode()
        self.assertLess(ordered.index("Anna Muster"), ordered.index("Bert Beispiel"))
        descending = self.client.get(f"{self.URL}?sort=person&dir=desc").content.decode()
        self.assertLess(descending.index("Bert Beispiel"), descending.index("Anna Muster"))
        self.assertTrue(anna.pk)

    def test_the_default_order_is_the_oldest_change_first(self):
        anna = self.submitted(self.anna, egid=90004269)
        bert = self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        Application.objects.filter(pk=anna.pk).update(updated_at=timezone.now())
        Application.objects.filter(pk=bert.pk).update(updated_at=timezone.now() - timedelta(days=3))
        page = self.client.get(self.URL).content.decode()
        self.assertLess(page.index("Bert Beispiel"), page.index("Anna Muster"))

    def test_the_empty_list_says_so(self):
        self.assertContains(self.client.get(self.URL), "Keine laufenden Vorgänge.")

    def test_a_cut_of_the_list_is_said_on_the_page_and_a_complete_list_says_nothing(self):
        self.submitted(self.anna, egid=90004269)
        ready_draft(self.bert, egid=90004270, number=7, address={"house_number": "52"})
        complete = self.client.get(self.URL)
        self.assertNotContains(complete, "Grenzen Sie die Liste")
        with patch.object(process_list, "MAX_ROWS", 1):
            cut = self.client.get(self.URL)
        self.assertContains(cut, "Mehr als 1 Anmeldungen oder Abmeldungen passen.")
        self.assertContains(cut, "eingereichte Anmeldungen vor Entwürfen")
        # The submitted application stays, the draft drops out.
        self.assertContains(cut, "Anna Muster")
        self.assertNotContains(cut, "Bert Beispiel")

    def test_the_rows_equal_the_counts_of_the_overview_queues(self):
        self.submitted(self.anna, egid=90004269)
        bert = self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        application_staff.start_review(bert, self.staff)
        self.at_operator(customer("carl.process@example.org", first_name="Carl", last_name="Probe"), egid=90004271,
                         number=9, address={"house_number": "53"})
        overview = self.client.get(reverse("management:overview")).content.decode()
        for state, count in (("submitted", 1), ("in_review", 1), ("at_operator", 1)):
            with self.subTest(state=state):
                self.assertEqual(len(process_list.rows(state=state)), count)
                self.assertIn(f"/management/processes/?state={state}", overview)

    def test_a_deregistration_is_a_row_with_the_date_open_until_it_is_confirmed(self):
        today = timezone.localdate()
        place = make_location("Abmeldeweg", "4", "3000", "Bern", egid=88880001)
        point = make_metering_point(
            80, location=place, registered_with_operator_on=today - timedelta(days=40),
            confirmed_on=today - timedelta(days=39), start_date=today - timedelta(days=30),
        )
        assign_person(point, self.anna, today - timedelta(days=30))
        dereg = deregistrations.request_deregistration(self.anna, metering_point_ids=[point.pk], today=today)
        page = self.client.get(self.URL)
        self.assertContains(page, "Abmeldung")
        self.assertContains(page, "In Abmeldung")
        self.assertContains(page, "Wirksam zum")
        self.assertContains(page, '<span class="mgmt-open">offen</span>')
        self.assertContains(page, "EGID 88880001")
        self.assertContains(page, reverse("management:processes_deregistration", args=[dereg.pk]))
        self.assertContains(page, "1 von 3 Schritten")
        # The filters of the kind and of the deregistration states.
        self.assertContains(self.client.get(f"{self.URL}?kind=application"), "Keine laufenden Vorgänge.")
        self.assertContains(self.client.get(f"{self.URL}?state=deregistration_submitted"), "Anna Muster")
        self.assertContains(self.client.get(f"{self.URL}?state=deregistration_confirmed"), "Keine laufenden Vorgänge.")

    def test_a_customer_gets_a_404_and_an_anonymous_visitor_the_login(self):
        self.assertEqual(self.login(self.plain).get(self.URL).status_code, 404)
        self.assertEqual(Client().get(self.URL).status_code, 302)


@tag("mariadb")
class ApplicationPageTests(ProcessBase):
    def url(self, application, name="processes_application"):
        return reverse(f"management:{name}", args=[application.pk])

    def test_the_page_shows_what_the_customer_entered_and_never_the_iban(self):
        application = self.submitted(self.anna, feed_in=True)
        self.assertTrue(own_iban.has_iban(self.anna))
        page = self.client.get(self.url(application))
        self.assertContains(page, "Anna Muster")
        self.assertContains(page, "Ittigenstrasse 51")
        self.assertContains(page, "3063 Ittigen")
        self.assertContains(page, "EGID 90004269")
        # The number on one line: the grid operator's block, then blocks of four, no-break spaces (D-109).
        self.assertContains(page, numbers.format_metering_point_number(full_number(1)))
        self.assertContains(page, "Bezug")
        self.assertContains(page, "Einspeisung")
        self.assertContains(page, "anna.process@example.org")
        self.assertContains(page, reverse("management:persons_detail", args=[self.anna.pk]))
        # Only that a bank connection is on file; the value is on the page of the person (D-087).
        self.assertContains(page, "Hinterlegt")
        for fragment in (GOOD_IBAN, "CH93", "0076 2011", "00762011"):
            self.assertNotContains(page, fragment)
        self.assertContains(page, "Nur der Kunde stimmt dem Vertrag zu.")
        self.assertContains(page, "Zugestimmt am")

    def test_without_a_feed_in_there_is_no_card_for_the_bank_connection(self):
        page = self.client.get(self.url(self.submitted(self.anna)))
        self.assertNotContains(page, "Bankverbindung")

    def test_the_way_the_application_goes_and_its_progress(self):
        application = self.submitted(self.anna)
        page = self.client.get(self.url(application))
        for text in ("Angaben abgeschickt", "Prüfung durch die LEG", "1 von 5 Schritten erledigt", "Erledigt",
                     "Als Nächstes"):
            self.assertContains(page, text)
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        page = self.client.get(self.url(application))
        self.assertContains(page, "01.06.2026")
        self.assertContains(page, "3 von 5 Schritten erledigt")

    def test_the_steps_are_named_like_on_the_page_of_the_customer(self):
        # W1-01, W5-04: the same five steps, the same words; no "Netzbetreiber-Anmeldung".
        page = self.client.get(self.url(self.submitted(self.anna)))
        for text in ("Anmeldung bei Netzbetreiber", "Kunde bestätigt die Anfrage von Netzbetreiber"):
            self.assertContains(page, text)
        self.assertNotContains(page, "Netzbetreiber-Anmeldung")

    def test_the_buttons_say_what_they_do_and_the_back_link_stands_once(self):
        # W5-04: "Record the start at ..." is an action, not a status; no second "Back to the list" at the end.
        application = self.submitted(self.anna)
        page = self.client.get(self.url(application))
        self.assertContains(page, "Start bei Netzbetreiber erfassen")
        self.assertNotContains(page, "Anmeldung bei Netzbetreiber gestartet<")
        self.assertNotContains(page, "Zurück zur Liste")
        self.assertContains(page, "Zurück zu den Vorgängen", count=1)
        self.assertContains(page, "Anmeldung rückabwickeln")

    def test_the_side_column_stacks_the_facts_so_an_email_is_not_broken_inside_a_word(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "management" / "css" / "management.css").read_text(
            encoding="utf-8"
        )
        rule = css[css.index(".mgmt-process-side .mgmt-fact {"):]
        self.assertIn("grid-template-columns: minmax(0, 1fr)", rule[:rule.index("}")])
        self.assertContains(
            self.client.get(self.url(self.submitted(self.anna))), '<dd class="mgmt-wrap">anna.process@example.org</dd>'
        )

    def test_equal_entries_that_follow_one_another_are_one_line_with_a_count(self):
        # W5-04: "Customer: building chosen" three times in a row is one line "× 3".
        application = self.submitted(self.anna)
        for _ in range(2):
            history.record(application, HistoryEvent.APPLICATION_SUBMITTED, self.anna)
        page = self.client.get(self.url(application)).content.decode()
        label = "Kunde: Anmeldung eingereicht"
        self.assertEqual(page.count(label), 1)
        self.assertIn('<span class="mgmt-log-count tnum">× 3</span>', page)
        # A different entry in between breaks the run: two lines.
        application_staff.start_review(application, self.staff)
        history.record(application, HistoryEvent.APPLICATION_SUBMITTED, self.anna)
        page = self.client.get(self.url(application)).content.decode()
        self.assertEqual(page.count(label), 2)
        self.assertEqual(page.count("mgmt-log-count"), 1)

    def test_the_buttons_follow_the_state(self):
        application = self.submitted(self.anna)
        names = (
            "review", "return", "operator_started", "operator_confirmed", "complete", "reject",
        )

        def buttons():
            page = self.client.get(self.url(application)).content.decode()
            return {name for name in names if f"/applications/{application.pk}/{name.replace('_', '-')}/" in page}

        self.assertEqual(buttons(), {"review", "return", "operator_started", "reject"})
        application_staff.start_review(application, self.staff)
        self.assertEqual(buttons(), {"return", "operator_started", "reject"})
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        self.assertEqual(buttons(), {"operator_confirmed", "reject"})
        application_staff.record_operator_confirmation(application, self.staff, CONFIRMED, today=TODAY)
        self.assertEqual(buttons(), {"complete", "reject"})
        application_staff.complete(application, self.staff, START, today=TODAY)
        self.assertEqual(buttons(), set())
        self.assertContains(self.client.get(self.url(application)), "Die Anmeldung ist abgeschlossen.")

    def test_a_draft_has_a_read_only_page_and_no_staff_step_applies_to_it(self):
        draft, _building = ready_draft(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        page = self.client.get(self.url(draft))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Entwurf / nicht eingereicht")
        self.assertContains(page, "Der Kunde hat diese Anmeldung begonnen und nicht eingereicht.")
        self.assertContains(page, "Ittigenstrasse 52")
        # What the customer entered is readable; no button of a staff step is drawn, and the IBAN is never shown.
        for name in ("review", "return", "operator_started", "operator_confirmed", "complete", "reject"):
            self.assertNotContains(page, reverse(f"management:processes_application_{name}", args=[draft.pk]))
        self.assertNotContains(page, "Anmeldung rückabwickeln")
        self.assertNotContains(page, GOOD_IBAN)
        # The services and dialogs refuse every step and change nothing (a second line of defence, not only hidden buttons).
        for name in ("processes_application_return", "processes_application_operator_started",
                     "processes_application_operator_confirmed", "processes_application_complete",
                     "processes_application_reject"):
            with self.subTest(name=name):
                url = reverse(f"management:{name}", args=[draft.pk])
                self.assertEqual(self.client.get(url).status_code, 302)
                self.assertEqual(self.client.post(url, {"reason": "x", "note": "", "next": ""}).status_code, 302)
        self.assertEqual(self.client.get(reverse("management:processes_application_review", args=[draft.pk])).status_code, 405)
        self.assertEqual(self.client.post(reverse("management:processes_application_review", args=[draft.pk])).status_code, 302)
        self.assertEqual(self.state(draft), State.DRAFT)
        self.assertFalse(Application.objects.get(pk=draft.pk).returned_at)
        # The customer's own route to the draft is closed to the staff (404), so the staff cannot edit it there either.
        self.assertEqual(self.client.get(reverse("customer:application", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("customer:application_cancel", args=[draft.pk])).status_code, 404)
        self.assertTrue(Application.objects.filter(pk=draft.pk).exists())

    def test_a_paused_draft_page_says_paused_and_an_unknown_id_has_no_page(self):
        paused, _building = ready_draft(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        Application.objects.filter(pk=paused.pk).update(state=State.PAUSED)
        self.assertContains(self.client.get(self.url(paused)), "Pausiert / nicht eingereicht")
        self.assertEqual(self.client.get(reverse("management:processes_application", args=[999999])).status_code, 404)

    def test_an_application_the_customer_changed_meanwhile_is_not_stale_for_the_staff(self):
        """The customer takes a submitted application back to change it; the open page of the staff still offers
        "Take into review" and the click is refused: the state is read again under the row lock."""
        application = self.submitted(self.anna)
        self.assertContains(self.client.get(self.url(application)), "In Prüfung nehmen")
        applications.reopen_for_change(self.anna, application)
        response = self.client.post(
            reverse("management:processes_application_review", args=[application.pk]), follow=True
        )
        self.assertContains(response, "Dieser Schritt ist im aktuellen Zustand nicht möglich.")
        self.assertEqual(self.state(application), State.DRAFT)
        page = self.client.get(self.url(application))
        self.assertContains(page, "Entwurf / nicht eingereicht")
        self.assertNotContains(page, "In Prüfung nehmen")

    def test_the_history_names_who_did_what_without_a_value(self):
        application = self.submitted(self.anna)
        application_staff.start_review(application, self.staff)
        page = self.client.get(self.url(application))
        self.assertContains(page, "Kunde: Anmeldung eingereicht")
        self.assertContains(page, "Anmeldung in Prüfung")
        for entry in history.entries_for(application):
            self.assertNotIn("iban", str(entry.detail).lower())

    def test_the_customer_data_is_not_shown_to_an_account_that_is_no_staff(self):
        application = self.submitted(self.anna)
        response = self.login(self.plain).get(self.url(application))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "Anna", status_code=404)

    def test_the_cards_count_on_without_a_gap_when_there_is_no_bank_connection_step(self):
        from leg_platform.management.process_views import ApplicationDetailView

        self.assertEqual(ApplicationDetailView.step_numbers(False),
                         {"building": 1, "points": 2, "contract": 3, "start": 4})
        self.assertEqual(ApplicationDetailView.step_numbers(True),
                         {"building": 1, "points": 2, "bank": 3, "contract": 4, "start": 5})
        page = self.client.get(self.url(self.submitted(self.anna))).content.decode()
        self.assertIn('aria-hidden="true">4</span>Startdatum', page)
        self.assertNotIn(">5</span>", page)

    def test_what_a_customer_typed_is_escaped_on_every_page(self):
        from leg_platform.community.models import ApplicationBuilding, ApplicationMeteringPoint

        application = self.submitted(self.anna)
        evil = '<script>alert("x")</script>'
        ApplicationBuilding.objects.filter(application=application).update(name=evil, description=evil)
        ApplicationMeteringPoint.objects.filter(building__application=application).update(name=evil)
        type(self.anna).objects.filter(pk=self.anna.pk).update(first_name='<img src=x onerror=alert(1)>')
        for url in (self.url(application), reverse("management:processes"), reverse("management:overview")):
            with self.subTest(url=url):
                page = self.client.get(url).content.decode()
                self.assertNotIn("<script>alert", page)
                self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", self.client.get(self.url(application)).content.decode())


@tag("mariadb")
class ApplicationStepTests(ProcessBase):
    def url(self, application, name):
        return reverse(f"management:processes_application_{name}", args=[application.pk])

    def detail(self, application):
        return reverse("management:processes_application", args=[application.pk])

    def test_take_into_review_is_a_post_button_and_a_second_press_changes_nothing(self):
        application = self.submitted(self.anna)
        response = self.client.post(self.url(application, "review"), {"next": self.detail(application)})
        self.assertRedirects(response, self.detail(application), fetch_redirect_response=False)
        self.assertEqual(self.state(application), State.IN_REVIEW)
        self.assertEqual(self.client.get(self.url(application, "review")).status_code, 405)
        again = self.client.post(self.url(application, "review"), {"next": self.detail(application)}, follow=True)
        self.assertContains(again, "Dieser Schritt ist im aktuellen Zustand nicht möglich")
        self.assertEqual(self.state(application), State.IN_REVIEW)
        events = list(history.entries_for(application).values_list("event", flat=True))
        self.assertEqual(events.count(HistoryEvent.APPLICATION_IN_REVIEW), 1)

    def test_return_for_correction_asks_first_for_a_reason_and_makes_it_a_draft_again(self):
        application = self.submitted(self.anna)
        page = self.client.get(self.url(application, "return"))
        self.assertContains(page, "Zur Korrektur zurückgeben")
        self.assertContains(page, "Der Kunde erhält eine E-Mail und liest Ihre Begründung im Portal.")
        self.assertContains(page, "Begründung der Korrektur")
        self.assertContains(page, "<textarea")
        self.assertContains(page, 'maxlength="500"')
        self.assertEqual(self.state(application), State.SUBMITTED)
        # Without a reason nothing happens.
        empty = self.client.post(self.url(application, "return"), {"reason": "", "next": self.detail(application)})
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(self.state(application), State.SUBMITTED)
        response = self.client.post(
            self.url(application, "return"), {"reason": "Nummer prüfen", "next": self.detail(application)},
            follow=True,
        )
        # W2-02: after the step the way on is the list, with a message (the draft has a read-only page, D-107).
        self.assertRedirects(response, reverse("management:processes"))
        self.assertContains(response, "Die Anmeldung wurde zur Korrektur zurückgegeben.")
        application.refresh_from_db()
        self.assertEqual(application.state, State.DRAFT)
        self.assertIsNotNone(application.returned_at)
        self.assertEqual(application.returned_reason, "Nummer prüfen")
        # The returned draft is marked as not submitted on its read-only page.
        self.assertContains(self.client.get(self.detail(application)), "Entwurf / nicht eingereicht")
        self.assertEqual(MailJob.objects.filter(kind="application_returned").count(), 1)
        self.assertEqual(MailJob.objects.filter(kind="application_rolled_back").count(), 0)

    def test_the_reason_of_a_return_is_at_most_500_characters_and_never_a_500_error(self):
        application = self.submitted(self.anna)
        url = self.url(application, "return")
        for reason in ("x" * 501, "   ", "bad\x00text"):
            with self.subTest(reason=reason[:10]):
                response = self.client.post(url, {"reason": reason, "next": self.detail(application)})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.state(application), State.SUBMITTED)
        ok = self.client.post(url, {"reason": "y" * 500, "next": self.detail(application)})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(self.state(application), State.DRAFT)

    def test_the_registration_at_the_grid_operator_needs_a_date_that_is_not_in_the_future(self):
        application = self.submitted(self.anna)
        url = self.url(application, "operator_started")
        page = self.client.get(url)
        self.assertContains(page, "Anmeldung bei Netzbetreiber gestartet am")
        # W5-04: an action as title and button, and the sentence about the withdrawal only once.
        self.assertContains(page, "Start bei Netzbetreiber erfassen")
        self.assertContains(page, "Start erfassen")
        self.assertEqual(page.content.decode().count("nicht mehr zurückziehen"), 1)
        future = (timezone.localdate() + timedelta(days=2)).isoformat()
        response = self.client.post(url, {"started_on": future, "next": self.detail(application)})
        self.assertContains(response, "Das Datum darf nicht in der Zukunft liegen.")
        self.assertEqual(self.state(application), State.SUBMITTED)
        today = timezone.localdate().isoformat()
        response = self.client.post(url, {"started_on": today, "next": self.detail(application)})
        self.assertRedirects(response, self.detail(application), fetch_redirect_response=False)
        application.refresh_from_db()
        self.assertEqual(application.state, State.OPERATOR_STARTED)
        self.assertEqual(application.operator_started_on, timezone.localdate())
        # Out of order: a second time is refused with a message.
        again = self.client.get(url, follow=True)
        self.assertContains(again, "Dieser Schritt ist im aktuellen Zustand nicht möglich")

    def test_the_confirmation_is_not_before_the_registration_and_then_opens_the_completion(self):
        application = self.submitted(self.anna)
        application_staff.mark_operator_started(application, self.staff, STARTED, today=TODAY)
        url = self.url(application, "operator_confirmed")
        # W5-04: the lead asks the person to record something; it is no statement.
        self.assertContains(
            self.client.get(url), "Erfassen Sie, wann der Kunde die Anmeldung bei Netzbetreiber bestätigt hat."
        )
        response = self.client.post(url, {"confirmed_on": "2026-05-01", "next": self.detail(application)})
        self.assertContains(response, "Das Datum darf nicht vor der Anmeldung liegen.")
        self.assertEqual(self.state(application), State.OPERATOR_STARTED)
        response = self.client.post(url, {"confirmed_on": CONFIRMED.isoformat(), "next": self.detail(application)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.state(application), State.OPERATOR_CONFIRMED)
        self.assertContains(self.client.get(self.detail(application)), self.url(application, "complete"))

    def test_the_completion_proposes_the_earliest_start_and_refuses_an_earlier_one(self):
        application = self.at_operator()
        url = self.url(application, "complete")
        page = self.client.get(url)
        self.assertContains(page, 'value="2026-09-01"')
        self.assertContains(page, "Frühestens am 01.09.2026 (drei Monate nach der Anmeldung).")
        self.assertContains(page, "Der Kunde wünscht den Start so bald wie möglich.")
        too_early = self.client.post(url, {"start_date": "2026-08-31", "next": self.detail(application)})
        self.assertContains(too_early, "Das Startdatum darf nicht vor dem 01.09.2026 liegen")
        self.assertEqual(self.state(application), State.OPERATOR_CONFIRMED)
        self.assertEqual(MeteringPoint.objects.count(), 0)

    def test_the_completion_is_filled_with_the_wish_of_the_customer_never_before_the_earliest_date(self):
        # W5-05: a wish after the earliest date is the value of the field; one before it is lifted to the earliest date.
        later = self.at_operator()
        Application.objects.filter(pk=later.pk).update(
            start_choice=Application.StartChoice.DATE, desired_start_date=date(2026, 11, 15)
        )
        page = self.client.get(self.url(later, "complete"))
        self.assertContains(page, 'value="2026-11-15"')
        self.assertContains(page, "Der Kunde wünscht den Start am 15.11.2026.")
        self.assertContains(page, "Frühestens am 01.09.2026")
        earlier = self.at_operator(self.bert, egid=90004270, number=7, address={"house_number": "52"})
        Application.objects.filter(pk=earlier.pk).update(
            start_choice=Application.StartChoice.DATE, desired_start_date=date(2026, 8, 1)
        )
        page = self.client.get(self.url(earlier, "complete"))
        self.assertContains(page, 'value="2026-09-01"')
        self.assertContains(page, "Der Kunde wünscht den Start am 01.08.2026.")

    def test_the_confirmation_of_the_addresses_is_an_explicit_tick_that_is_off_by_default(self):
        # W5-07: no select "New addresses"; a tick "Addresses are checked and will be locked", only while a building
        # of the application is not confirmed yet; left alone, the new building stays editable.
        application = self.at_operator()
        url = self.url(application, "complete")
        page = self.client.get(url)
        html = page.content.decode()
        self.assertContains(page, "Adressen sind geprüft und werden gesperrt")
        self.assertContains(page, "1 Gebäude hat eine Adresse, die noch nicht bestätigt ist.")
        self.assertNotContains(page, "Neue Adressen")
        self.assertRegex(html, r'<input type="checkbox" name="confirm_addresses"(?![^>]*checked)[^>]*>')
        self.assertEqual(
            self.client.post(url, {"start_date": START.isoformat(), "next": self.detail(application)}).status_code, 302
        )
        self.assertTrue(Location.objects.get(egid=90004269).needs_confirmation)

    def test_without_a_building_to_confirm_the_tick_is_not_offered_and_a_posted_one_is_ignored(self):
        make_location(egid=90004269, street="Ittigenstrasse", house_number="51", postal_code="3063", town="Ittigen")
        Location.objects.filter(egid=90004269).update(needs_confirmation=False)
        application = self.at_operator()
        url = self.url(application, "complete")
        self.assertNotContains(self.client.get(url), "Adressen sind geprüft und werden gesperrt")
        self.assertEqual(
            self.client.post(
                url, {"start_date": START.isoformat(), "confirm_addresses": "1", "next": self.detail(application)}
            ).status_code, 302,
        )
        self.assertEqual(self.state(application), State.COMPLETED)

    def test_the_completion_creates_the_real_rows_through_the_service(self):
        application = self.at_operator(feed_in=True)
        url = self.url(application, "complete")
        response = self.client.post(
            url, {"start_date": START.isoformat(), "confirm_addresses": "1", "next": self.detail(application)},
            follow=True,
        )
        self.assertContains(response, "Die Anmeldung ist abgeschlossen. Die Messpunkte wurden angelegt.")
        self.assertEqual(self.state(application), State.COMPLETED)
        location = Location.objects.get(egid=90004269)
        self.assertFalse(location.needs_confirmation)
        self.assertEqual(MeteringPoint.objects.filter(location=location).count(), 2)
        self.assertEqual(PersonAssignment.objects.filter(person=self.anna, valid_from=START).count(), 2)
        # The page now links to the metering points that were created and offers no step.
        detail = self.client.get(self.detail(application))
        for point in MeteringPoint.objects.all():
            self.assertContains(detail, reverse("management:metering_points_detail", args=[point.pk]))
        self.assertNotContains(detail, "/complete/")

    def test_a_number_that_was_stored_meanwhile_is_a_message_and_nothing_is_kept(self):
        application = self.at_operator()
        make_metering_point(1, location=None)  # the number of the application, stored in the meantime
        response = self.client.post(
            self.url(application, "complete"),
            {"start_date": START.isoformat(), "next": self.detail(application)},
        )
        self.assertContains(response, "Eine Messpunkt-Nummer dieser Anmeldung wurde inzwischen gespeichert")
        self.assertEqual(self.state(application), State.OPERATOR_CONFIRMED)
        self.assertEqual(Location.objects.filter(egid=90004269).count(), 0)

    def test_roll_back_deletes_only_the_draft_rows_and_the_customer_gets_the_mail(self):
        application = self.submitted(self.anna, feed_in=True)
        url = self.url(application, "reject")
        page = self.client.get(url)
        self.assertContains(page, "Anmeldung rückabwickeln")
        # W2-03: a reason to choose, and an optional note that the customer reads once.
        self.assertContains(page, "Von der LEG nicht angenommen")
        self.assertContains(page, "Von Netzbetreiber abgelehnt")
        self.assertContains(page, "Mitteilung an den Kunden")
        self.assertContains(page, "(optional)")
        self.assertContains(page, 'maxlength="300"')
        self.assertContains(page, "Das Konto, die Bankverbindung und gespeicherte Gebäude bleiben bestehen.")
        response = self.client.post(
            url, {"reason": "rejected", "note": "Bitte neu starten.", "next": self.detail(application)}, follow=True
        )
        self.assertContains(response, "Die Anmeldung wurde rückabgewickelt.")
        self.assertRedirects(response, reverse("management:processes"))
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertTrue(own_iban.has_iban(self.anna))
        self.assertEqual(MailJob.objects.filter(kind="application_rejected").count(), 1)
        self.assertEqual(CustomerMessage.objects.get().text, "Bitte neu starten.")
        self.assertEqual(self.client.get(self.detail(application)).status_code, 404)

    def test_the_reason_is_one_of_two_and_a_forged_one_changes_nothing(self):
        application = self.submitted(self.anna)
        response = self.client.post(self.url(application, "reject"), {"reason": "anything", "next": "/"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())

    def test_the_note_is_optional_and_at_most_300_characters(self):
        application = self.submitted(self.anna)
        url = self.url(application, "reject")
        too_long = self.client.post(url, {"reason": "rejected", "note": "n" * 301, "next": "/"})
        self.assertEqual(too_long.status_code, 200)
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        response = self.client.post(url, {"reason": "operator_rejected", "next": "/"})
        self.assertRedirects(response, reverse("management:processes"), fetch_redirect_response=False)
        self.assertEqual(CustomerMessage.objects.get().text, "")  # the message of the kind is there, without a note

    def test_a_dialog_for_a_state_that_does_not_fit_goes_back_with_a_message(self):
        application = self.submitted(self.anna)
        for name in ("operator_confirmed", "complete"):
            with self.subTest(name=name):
                response = self.client.get(self.url(application, name), follow=True)
                self.assertContains(response, "Dieser Schritt ist im aktuellen Zustand nicht möglich")
                post = self.client.post(self.url(application, name), {"confirmed_on": "2026-07-01", "start_date": "2026-09-01"})
                self.assertEqual(post.status_code, 302)
        self.assertEqual(self.state(application), State.SUBMITTED)

    def test_every_step_needs_the_token(self):
        application = self.submitted(self.anna)
        client = self.login(self.staff, enforce_csrf_checks=True)
        for name in ("review", "return", "operator_started", "reject"):
            with self.subTest(name=name):
                self.assertEqual(client.post(self.url(application, name), {}).status_code, 403)
        self.assertEqual(self.state(application), State.SUBMITTED)

    def test_the_next_address_is_only_followed_when_it_is_a_path_of_this_site(self):
        application = self.submitted(self.anna)
        response = self.client.post(self.url(application, "review"), {"next": "https://evil.example/"})
        self.assertEqual(response["Location"], self.detail(application))

    def test_an_account_that_lost_the_staff_status_cannot_take_a_step_with_a_live_session(self):
        application = self.submitted(self.anna)
        former = staff_user("former.staff@example.org")
        client = self.login(former)
        former.set_privileges(is_staff=False, is_superuser=False)
        response = client.post(self.url(application, "review"))
        # The session no longer yields a staff account: a visitor (the login) or a page that does not exist.
        self.assertIn(response.status_code, (302, 404))
        self.assertEqual(self.state(application), State.SUBMITTED)


@tag("mariadb")
class DeregistrationPageTests(ProcessBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        today = timezone.localdate()
        cls.today = today
        place = make_location("Abmeldeweg", "4", "3000", "Bern", egid=88880001)
        cls.point = make_metering_point(
            80, location=place, registered_with_operator_on=today - timedelta(days=40),
            confirmed_on=today - timedelta(days=39), start_date=today - timedelta(days=30),
        )
        assign_person(cls.point, cls.anna, today - timedelta(days=30))
        cls.dereg = deregistrations.request_deregistration(cls.anna, metering_point_ids=[cls.point.pk], today=today)

    def detail(self):
        return reverse("management:processes_deregistration", args=[self.dereg.pk])

    def confirm_url(self):
        return reverse("management:processes_deregistration_confirm", args=[self.dereg.pk])

    def month_end(self, days):
        import calendar

        day = self.today + timedelta(days=days)
        return day.replace(day=calendar.monthrange(day.year, day.month)[1])

    def test_the_page_shows_the_steps_the_points_and_the_one_button(self):
        page = self.client.get(self.detail())
        for text in ("Abmeldung eingereicht", "Termin durch die LEG bestätigt", "Bei Netzbetreiber abgemeldet",
                     "1 von 3 Schritten erledigt", "Frühester Termin", "Gewünschter Termin", "Termin bestätigen",
                     "Anna Muster", "EGID 88880001", "Abmeldeweg 4"):
            self.assertContains(page, text)
        self.assertContains(page, self.confirm_url())
        # The third step is done by the system on the confirmed date: no button for it.
        self.assertNotContains(page, "als bei")
        # W5-06: the same pattern as the page of an application: the card "Next step" with a large button.
        html = page.content.decode()
        card = html[html.index('id="next-title"'):]
        card = card[:card.index("</section>")]
        self.assertIn("Nächster Schritt", card)
        self.assertIn("Der Kunde hat gekündigt. Bestätigen Sie den Termin", card)
        self.assertIn(f'<a class="btn btn-primary" href="{self.confirm_url()}?next=', card)
        self.assertIn('Termin bestätigen</a>', card)
        self.assertNotIn("btn-xs", html)

    def test_the_confirmation_dialog_is_filled_with_the_wish_and_names_the_earliest_date(self):
        # W5-05: the field holds the date the customer wished and the hint names the earliest date.
        page = self.client.get(self.confirm_url())
        wish = self.dereg.desired_date
        self.assertContains(page, f'value="{wish.isoformat()}"')
        self.assertContains(
            page, "Frühestens am %s (vier Monate nach der Kündigung, auf ein Monatsende)."
            % self.dereg.earliest_date.strftime("%d.%m.%Y"),
        )
        if self.dereg.wants_earliest:
            self.assertContains(
                page, "Der Kunde wünscht den frühestmöglichen Termin (%s)." % wish.strftime("%d.%m.%Y")
            )
        else:
            self.assertContains(page, "Der Kunde wünscht den %s." % wish.strftime("%d.%m.%Y"))
        self.assertContains(page, "Der Termin wird bei jedem Messpunkt der Abmeldung als Kündigungsdatum erfasst.")
        self.assertContains(page, "Der letzte Tag eines Monats, nicht in der Vergangenheit.")

    def test_a_wish_that_has_passed_is_replaced_by_the_end_of_this_month_in_the_field(self):
        Deregistration.objects.filter(pk=self.dereg.pk).update(desired_date=self.today - timedelta(days=45))
        page = self.client.get(self.confirm_url())
        self.assertContains(page, "Der gewünschte Termin ist vorbei: Vorgeschlagen wird das Ende dieses Monats.")
        self.assertContains(page, f'value="{self.month_end(0).isoformat()}"')

    def test_the_confirmation_records_the_date_at_the_point(self):
        date_text = self.month_end(130)
        page = self.client.get(self.confirm_url())
        self.assertContains(page, f'value="{self.dereg.desired_date.isoformat()}"')
        response = self.client.post(self.confirm_url(), {"confirmed_date": date_text.isoformat(), "next": self.detail()})
        self.assertRedirects(response, self.detail(), fetch_redirect_response=False)
        self.dereg.refresh_from_db()
        self.point.refresh_from_db()
        self.assertEqual(self.dereg.state, Deregistration.State.CONFIRMED)
        self.assertEqual(self.point.terminated_on, date_text)
        after = self.client.get(self.detail())
        self.assertContains(after, "2 von 3 Schritten erledigt")
        self.assertNotContains(after, self.confirm_url())
        self.assertContains(after, "Gekündigt zum")
        self.assertContains(after, "ist bestätigt. Das Portal setzt die Abmeldung an diesem Tag selbst auf wirksam.")

    def test_a_date_that_is_no_month_end_or_in_the_past_is_refused_in_the_dialog(self):
        first_of_month = (self.today.replace(day=1) + timedelta(days=64)).replace(day=1)
        response = self.client.post(self.confirm_url(), {"confirmed_date": first_of_month.isoformat(), "next": self.detail()})
        self.assertContains(response, "Geben Sie den letzten Tag eines Monats ein.")
        past = (self.today.replace(day=1) - timedelta(days=1)).isoformat()
        response = self.client.post(self.confirm_url(), {"confirmed_date": past, "next": self.detail()})
        self.assertContains(response, "Das Datum darf nicht in der Vergangenheit liegen.")
        self.dereg.refresh_from_db()
        self.assertEqual(self.dereg.state, Deregistration.State.SUBMITTED)

    def test_a_second_confirmation_is_refused_with_a_message(self):
        deregistrations.confirm(self.dereg, self.staff, self.month_end(130), today=self.today)
        response = self.client.get(self.confirm_url(), follow=True)
        self.assertContains(response, "Dieser Schritt ist im aktuellen Zustand nicht möglich")
        response = self.client.post(self.confirm_url(), {"confirmed_date": self.month_end(160).isoformat()})
        self.assertEqual(response.status_code, 302)
        self.dereg.refresh_from_db()
        self.assertEqual(self.dereg.confirmed_date, self.month_end(130))

    def test_an_unknown_deregistration_has_no_page(self):
        for name in ("processes_deregistration", "processes_deregistration_confirm"):
            self.assertEqual(self.client.get(reverse(f"management:{name}", args=[999999])).status_code, 404)

    def test_the_proof_of_the_customer_is_not_on_the_page(self):
        page = self.client.get(self.detail())
        for fragment in ("Passwort", "Code aus der App", "2FA"):
            self.assertNotContains(page, fragment)


@tag("mariadb")
class NoticePageTests(ProcessBase):
    def make_notice(self, person=None):
        return application_notices.record(
            person or self.anna, application_notices.Kind.BUILDING_UNAVAILABLE, egid=12345678,
            numbers=[full_number(31), full_number(32)],
        )

    def test_the_page_lists_the_open_notices_with_the_data_the_staff_needs(self):
        self.make_notice()
        page = self.client.get(reverse("management:processes_notices"))
        self.assertContains(page, "Anna Muster")
        self.assertContains(page, "12345678")
        self.assertContains(page, "CH1A2B3C4D5E6")
        self.assertContains(page, "Als erledigt markieren")
        self.assertContains(page, "Gebäude für den Kunden nicht verfügbar")

    def test_a_number_that_is_a_metering_point_of_the_instance_has_a_link_to_it(self):
        # W5-08: "Go to the metering point" (the staff may see whose it is on that page); a number that is unknown has none.
        known = MeteringPoint.objects.create(number=full_number(31), direction=Direction.CONSUMPTION)
        self.make_notice()
        page = self.client.get(reverse("management:processes_notices"))
        url = reverse("management:metering_points_detail", args=[known.pk])
        self.assertContains(page, f'href="{url}"', count=1)
        self.assertContains(page, "Zum Messpunkt", count=2)  # the title and the accessible name of the one link
        self.assertContains(page, full_number(32)[:2])
        self.assertContains(page, reverse("management:persons_detail", args=[self.anna.pk]))

    def test_the_page_of_an_application_shows_the_notices_of_its_person_only(self):
        application = self.submitted(self.anna)
        other = self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        self.make_notice()
        shown = self.client.get(reverse("management:processes_application", args=[application.pk]))
        self.assertContains(shown, "12345678")
        hidden = self.client.get(reverse("management:processes_application", args=[other.pk]))
        self.assertNotContains(hidden, "12345678")

    def test_resolving_deletes_the_numbers_and_the_notice_disappears(self):
        notice = self.make_notice()
        url = reverse("management:processes_notice_resolve", args=[notice.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {"next": reverse("management:processes_notices")})
        self.assertRedirects(response, reverse("management:processes_notices"), fetch_redirect_response=False)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.resolved_at)
        self.assertEqual(notice.numbers, [])
        self.assertEqual(notice.resolved_by, self.staff)
        self.assertContains(self.client.get(reverse("management:processes_notices")), "Keine offenen Hinweise.")
        # Resolving twice is not an error and changes nothing more.
        self.assertEqual(self.client.post(url).status_code, 302)

    def test_an_unknown_notice_is_a_404_and_the_token_is_needed(self):
        self.assertEqual(self.client.post(reverse("management:processes_notice_resolve", args=[999999])).status_code, 404)
        notice = self.make_notice()
        client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("management:processes_notice_resolve", args=[notice.pk])).status_code, 403)
        self.assertEqual(ApplicationNotice.objects.get(pk=notice.pk).resolved_at, None)

    def test_a_customer_cannot_see_or_resolve_a_notice(self):
        notice = self.make_notice()
        client = self.login(self.plain)
        self.assertEqual(client.get(reverse("management:processes_notices")).status_code, 404)
        self.assertEqual(client.post(reverse("management:processes_notice_resolve", args=[notice.pk])).status_code, 404)
        self.assertEqual(ApplicationNotice.objects.get(pk=notice.pk).resolved_at, None)


@tag("mariadb")
class OverviewQueueTests(ProcessBase):
    URL = reverse("management:overview")

    def test_the_queues_have_a_count_the_oldest_entries_and_a_link_into_the_filtered_list(self):
        self.submitted(self.anna, egid=90004269)
        bert = self.submitted(self.bert, egid=90004270, number=5, address={"house_number": "52"})
        application_staff.start_review(bert, self.staff)
        self.at_operator(customer("carl.process@example.org", first_name="Carl", last_name="Probe"), egid=90004271,
                         number=9, address={"house_number": "53"})
        page = self.client.get(self.URL)
        for text in ("1 Anmeldung wartet auf Prüfung", "1 Anmeldung in Prüfung", "1 Anmeldung bei Netzbetreiber",
                     "Zu erledigen", "Anna Muster", "Bert Beispiel", "Carl Probe"):
            self.assertContains(page, text)
        for state in ("submitted", "in_review", "at_operator"):
            self.assertContains(page, f'href="/management/processes/?state={state}"')
        # The way into the customer's data is the page of the application, not a draft.
        self.assertNotContains(page, "Entwurf")

    def test_a_draft_never_counts_and_never_shows(self):
        ready_draft(self.anna, egid=90004269)
        page = self.client.get(self.URL)
        self.assertNotContains(page, "Anna Muster")
        self.assertNotContains(page, "wartet auf Prüfung")

    def test_notices_deregistrations_and_a_missing_contract_have_their_rows(self):
        today = timezone.localdate()
        place = make_location("Abmeldeweg", "4", "3000", "Bern")
        point = make_metering_point(
            80, location=place, registered_with_operator_on=today - timedelta(days=40),
            confirmed_on=today - timedelta(days=39), start_date=today - timedelta(days=30),
        )
        assign_person(point, self.anna, today - timedelta(days=30))
        deregistrations.request_deregistration(self.anna, metering_point_ids=[point.pk], today=today)
        application_notices.record(self.anna, application_notices.Kind.BUILDING_UNAVAILABLE, egid=12345678)
        page = self.client.get(self.URL)
        # W5-02: wording and the first notice (what and whom).
        self.assertContains(page, "1 Abmeldung wartet auf die Terminbestätigung")
        self.assertContains(page, "Wunschtermin ")
        self.assertNotContains(page, "gewünscht auf")
        self.assertContains(page, "1 Hinweis für die Verwaltung")
        self.assertContains(page, "Gebäude für den Kunden nicht verfügbar")
        self.assertContains(page, "Anna Muster")
        self.assertContains(page, 'href="/management/processes/notices/"')
        self.assertContains(page, 'href="/management/processes/?state=deregistration_submitted"')
        self.assertNotContains(page, "Keine Vertragsfassung ist aktuell")

    def test_the_name_of_a_row_is_the_link_and_the_chain_icon_is_no_second_stop(self):
        # W5-02: the whole heading is a link of at least 44 px; the 24 px icon beside it is out of the tab order.
        self.submitted(self.anna)
        html = self.client.get(self.URL).content.decode()
        self.assertIn('<div class="mgmt-overview-name"><a href="/management/processes/?state=submitted">', html)
        self.assertRegex(
            html, r'<a class="mgmt-goto" href="/management/processes/\?state=submitted" tabindex="-1" aria-hidden="true"'
        )
        css = (Path(__file__).resolve().parents[1] / "static" / "management" / "css" / "management.css").read_text(
            encoding="utf-8"
        )
        rule = css[css.index(".mgmt-overview-name a {"):]
        self.assertIn("min-height: 44px", rule[:rule.index("}")])

    def test_a_missing_contract_is_a_row_with_the_link_and_no_number(self):
        from leg_platform.community.models import ContractVersion

        ContractVersion.objects.update(current_marker=None)
        page = self.client.get(self.URL)
        self.assertContains(page, "Keine Vertragsfassung ist aktuell. Kunden können dem Vertrag nicht zustimmen.")
        self.assertContains(page, 'href="/management/contracts/"')

    def test_the_two_cards_are_not_added_up_and_the_old_behaviour_stays(self):
        page = self.client.get(self.URL)
        self.assertContains(page, "Keine Probleme.")
        self.assertNotContains(page, "Probleme filtern")
        self.assertNotContains(page, "mgmt-overview-row")
        # Not "To do" while there is nothing (the contract is current here).
        self.assertNotContains(page, "Zu erledigen")

    def test_a_customer_has_no_overview(self):
        self.assertEqual(self.login(self.plain).get(self.URL).status_code, 404)
        self.assertEqual(Client().get(self.URL).status_code, 302)


@tag("mariadb")
class ProcessAccessTests(ProcessBase):
    """The routes of the processes for the accounts that must not use them (the walk of ``test_access`` asks every route;
    these are the pages that show customer data)."""

    def test_a_staff_account_without_the_second_factor_is_sent_to_the_setup(self):
        application = self.submitted(self.anna)
        client = Client()
        client.force_login(staff_user("nosecond.process@example.org"))
        for url in (
            reverse("management:processes"), reverse("management:processes_notices"),
            reverse("management:processes_application", args=[application.pk]),
            reverse("management:processes_application_reject", args=[application.pk]),
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertRedirects(response, reverse("accounts:two_factor_setup"), fetch_redirect_response=False)
        self.assertEqual(self.state(application), State.SUBMITTED)

    def test_the_customer_and_the_anonymous_visitor_cannot_take_a_step(self):
        application = self.submitted(self.anna)
        customer_client = self.login(self.anna)
        for client, expected in ((customer_client, 404), (Client(), 302)):
            for name in ("review", "return", "operator_started", "operator_confirmed", "complete", "reject"):
                with self.subTest(name=name, client=type(client).__name__):
                    url = reverse(f"management:processes_application_{name}", args=[application.pk])
                    self.assertEqual(client.post(url, {"reason": "rejected"}).status_code, expected)
        self.assertEqual(self.state(application), State.SUBMITTED)
