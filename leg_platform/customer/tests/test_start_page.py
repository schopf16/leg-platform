"""The start page for customers (START-SEITE.md, LEERE-ZUSTAENDE.md; D-103): the card "Your way into the LEG" (states A, B, C),
the tiles about the participation, the progress and the next step."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import HiddenTask
from leg_platform.community import application_staff, applications, contracts
from leg_platform.community.models import Application
from leg_platform.community.tests.helpers import staff_user
from leg_platform.customer import journey
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    GOOD_IBAN,
    HOME,
    START,
    configure_numbers,
    customer,
    draft_with_building,
    draft_with_points,
    login,
    publish_contract,
    step_url,
)


def text(response):
    return response.content.decode()


def complete_application(person, staff, *, feed_in=False, egid=90004269, number=1, **address):
    """An application that the staff completed (the participation runs)."""
    application, _building = draft_with_points(person, feed_in=feed_in, egid=egid, number=number, **address)
    if feed_in:
        applications.save_bank_connection(person, application, GOOD_IBAN)
    if not contracts.has_accepted_current(person):
        applications.accept_contract_step(
            person, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
    applications.set_start(person, application, asap=True)
    applications.submit(person, application)
    today = date.today()
    application_staff.mark_operator_started(application, staff, started_on=today - timedelta(days=150))
    application_staff.record_operator_confirmation(application, staff, today - timedelta(days=140))
    application_staff.complete(application, staff, today - timedelta(days=20))
    return application


@tag("mariadb")
class StartPageCardTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.start@example.org")

    def setUp(self):
        configure_numbers()
        publish_contract(self.staff)
        self.mine = customer("mine.start@example.org")
        self.client = login(self.mine)

    # --- no application: the tiles, no card -----------------------------------------------------------------

    def test_without_a_building_the_tile_register_building_is_blue_and_there_is_no_card(self):
        html = text(self.client.get(HOME))
        self.assertNotIn("Ihr Weg in die LEG", html)
        tile = html[html.index('id="task-register_building-title"') - 40 :]
        tile = tile[: tile.index("</article>")]
        for marker in ("Gebäude anmelden", "Offen", "Melden Sie Ihr Gebäude an, um an der LEG teilzunehmen.", "btn-primary", "Ausblenden"):
            self.assertIn(marker, tile)
        self.assertIn(f'action="{START}"', tile)
        self.assertIn('method="post"', tile)
        # The two tiles of the empty state of the owner: register building, then two-factor sign-in.
        self.assertLess(html.index("task-register_building-title"), html.index("task-two_factor-title"))

    def test_hiding_the_tile_is_final(self):
        self.client.post(reverse("accounts:task_hide", args=["register_building"]))
        self.assertTrue(HiddenTask.objects.filter(user=self.mine, task_key="register_building").exists())
        self.assertNotIn("task-register_building-title", text(self.client.get(HOME)))

    def test_the_tile_is_gone_while_an_application_runs_and_after_it(self):
        draft_with_building(self.mine)
        self.assertNotIn("task-register_building-title", text(self.client.get(HOME)))

    # --- state A: the card -----------------------------------------------------------------------------------

    def test_the_card_shows_progress_steps_with_dot_and_word_and_the_next_step(self):
        application, _building = draft_with_points(self.mine)
        html = text(self.client.get(HOME))
        card = html[html.index('class="journey"') :]
        card = card[: card.index("</section>")]
        for marker in (
            "Ihr Weg in die LEG", "2 von 4 erledigt", "<progress", "<span>Gebäude</span>", "<span>Messpunkte</span>",
            "<span>Vertrag</span>", "<span>Startdatum</span>",
            "Sie müssen dem Gesellschaftsvertrag zustimmen.", "Vertrag lesen und zustimmen",
        ):
            self.assertIn(marker, card)
        # One list (W1-01): the names of the wizard, no extra final step.
        for gone in ("Bei der LEG angemeldet", "Standort erfassen", "Messpunkte erfassen", "Aufschaltung"):
            self.assertNotIn(gone, card)
        self.assertEqual(card.count("Als Nächstes"), 1)
        self.assertEqual(card.count("Erledigt"), 2)
        self.assertEqual(card.count("Offen"), 1)
        self.assertNotIn("Bank- oder Postverbindung", card)
        self.assertIn(f'href="{step_url(application, "contract")}"', card)
        self.assertEqual(card.count("btn-primary"), 1)
        # The card cannot be hidden and is not a tile.
        self.assertNotIn("Ausblenden", card)
        self.assertNotIn("empty-state", html)

    def test_the_start_date_is_visible_but_greyed_before_the_contract(self):
        draft_with_points(self.mine)
        html = text(self.client.get(HOME))
        self.assertRegex(html, r'<li class="is-muted"><span class="dot[^>]*></span><span>Startdatum</span>')

    def test_the_bank_step_is_in_the_card_only_with_a_feed_in(self):
        application, _building = draft_with_points(self.mine, feed_in=True)
        html = text(self.client.get(HOME))
        self.assertIn("Bank- oder Postverbindung", html)
        self.assertIn("Sie müssen Ihre Bank- oder Postverbindung angeben.", html)
        self.assertIn(f'href="{step_url(application, "bank_connection")}"', html)
        self.assertIn("2 von 5 erledigt", html)

    def test_a_complete_draft_is_ready_to_submit(self):
        application, _building = draft_with_points(self.mine)
        applications.accept_contract_step(
            self.mine, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        applications.set_start(self.mine, application, asap=True)
        html = text(self.client.get(HOME))
        self.assertIn("Ihre Angaben sind vollständig. Schicken Sie die Anmeldung jetzt ab.", html)
        self.assertIn(">Anmeldung abschicken<", html)

    def test_a_submitted_application_waits_for_the_leg(self):
        application, _building = draft_with_points(self.mine)
        applications.accept_contract_step(
            self.mine, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        applications.set_start(self.mine, application, asap=True)
        applications.submit(self.mine, application)
        html = text(self.client.get(HOME))
        self.assertIn("1 von 5 erledigt", html)
        self.assertIn("Sie bestätigen die Anfrage in der E-Mail von", html)
        self.assertNotIn("Bei der LEG angemeldet", html)
        self.assertIn("Ihre Anmeldung ist eingereicht. Sie können sie ändern, bis die LEG mit der Prüfung beginnt.", html)
        self.assertNotIn("Sie müssen nichts tun.", html)
        application_staff.start_review(application, self.staff)
        self.assertIn("Die LEG prüft Ihre Angaben. Sie müssen nichts tun.", text(self.client.get(HOME)))
        self.assertIn(f'href="{BUILDINGS}"', html)
        application_staff.mark_operator_started(application, self.staff)
        self.assertIn("Die LEG hat Sie beim Netzbetreiber angemeldet.", text(self.client.get(HOME)))

    def test_a_paused_draft_says_so_in_the_card(self):
        application, _building = draft_with_points(self.mine)
        Application.objects.filter(pk=application.pk).update(state=Application.State.PAUSED)
        self.assertIn("Ihre Anmeldung ist pausiert", text(self.client.get(HOME)))

    # --- state B and C ----------------------------------------------------------------------------------------

    def test_after_the_completion_the_card_is_gone_and_the_feed_in_can_be_reported_later(self):
        complete_application(self.mine, self.staff)
        html = text(self.client.get(HOME))
        self.assertNotIn("Ihr Weg in die LEG", html)
        self.assertNotIn("task-register_building-title", html)
        tile = html[html.index("task-feed_in-title") - 40 :]
        tile = tile[: tile.index("</article>")]
        for marker in ("Einspeisung nachmelden", "Optional", "Haben Sie eine PV-Anlage? Dann melden Sie den Messpunkt für die Einspeisung an.", "Messpunkt hinzufügen"):
            self.assertIn(marker, tile)
        # One building: "Add metering point" starts the application with it chosen.
        self.assertRegex(tile, r'href="/participation/application/\?building=\d+"')

    def test_the_feed_in_tile_is_gone_with_a_feed_in_and_while_an_application_runs(self):
        complete_application(self.mine, self.staff, feed_in=True)
        self.assertNotIn("task-feed_in-title", text(self.client.get(HOME)))
        other = customer("other.start@example.org")
        complete_application(
            other, self.staff, egid=90006666, number=30, street="Zweiweg", house_number="2", postal_code="3000", town="Bern"
        )
        client = login(other)
        self.assertIn("task-feed_in-title", text(client.get(HOME)))
        draft_with_building(other, egid=90007000, street="Seitenweg", house_number="1")
        self.assertNotIn("task-feed_in-title", text(client.get(HOME)))

    def test_with_nothing_to_do_the_empty_state_remains(self):
        complete_application(self.mine, self.staff, feed_in=True)
        HiddenTask.objects.create(user=self.mine, task_key="two_factor")
        html = text(self.client.get(HOME))
        self.assertIn("Keine offenen Aufgaben", html)

    def test_a_staff_account_with_a_number_gets_the_card_and_the_tiles_of_a_participant(self):
        # D-113: staff accounts take part like everybody else.
        html = text(login(self.staff).get(HOME))
        self.assertNotIn("Ihr Weg in die LEG", html)  # no application is open
        self.assertIn("task-register_building-title", html)
        draft_with_building(self.staff, egid=90004288, street="Staffstrasse", house_number="3")
        html = text(login(self.staff).get(HOME))
        self.assertIn("Ihr Weg in die LEG", html)

    def test_an_account_that_does_not_take_part_gets_no_card_and_no_tile_about_the_participation(self):
        with patch("leg_platform.customer.access.is_customer", return_value=False):
            html = text(login(self.staff).get(HOME))
        self.assertNotIn("Ihr Weg in die LEG", html)
        self.assertNotIn("task-register_building-title", html)
        self.assertNotIn("task-feed_in-title", html)


class JourneyUnitTests(TestCase):
    def test_the_texts_for_every_next_step_exist(self):
        for key in applications.STEPS:
            self.assertIn(key, journey.NEXT)
            self.assertIn(key, journey.STEP_NAMES)
