"""The page Buildings and its pencils (GEBAEUDE.md, LEERE-ZUSTAENDE.md; D-103): the account's own buildings and metering
points only, statuses as a dot plus a word, the progress of a running registration or deregistration, the names."""

from datetime import date, timedelta

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.community import applications, buildings, deregistrations
from leg_platform.community.models import CustomerBuilding, Direction, GridOperator, MeteringPoint
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    configure_numbers,
    customer,
    draft_with_building,
    draft_with_points,
    login,
    publish_contract,
)

TODAY = date.today()
LONG_AGO = TODAY - timedelta(days=400)


def active_point(last, person, location, direction=Direction.CONSUMPTION, **extra):
    point = make_metering_point(
        last, direction, location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=LONG_AGO, **extra
    )
    assign_person(point, person, valid_from=LONG_AGO)
    return point


@tag("mariadb")
class BuildingsPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.buildings@example.org")
        cls.other = customer("other.buildings@example.org")
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        cls.draw = active_point(1, cls.mine, cls.location, name="Wohnhaus")
        cls.foreign = active_point(2, cls.other, cls.location, name="Fremd")

    def setUp(self):
        self.client = login(self.mine)

    def page(self, client=None):
        response = (client or self.client).get(BUILDINGS)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # --- the empty state --------------------------------------------------------------------------------------

    def test_without_a_building_one_card_leads_to_the_registration_and_there_are_no_buttons_above(self):
        html = self.page(login(customer("nothing.buildings@example.org")))
        self.assertIn("Noch kein Gebäude angemeldet", html)
        self.assertIn("Melden Sie Ihr Gebäude an, um an der LEG teilzunehmen.", html)
        self.assertEqual(html.count("Anmeldung starten"), 1)
        self.assertNotIn("Abmeldung starten", html)
        self.assertNotIn('class="head-actions"', html)

    # --- the card ---------------------------------------------------------------------------------------------

    def test_the_card_shows_the_address_the_facts_and_only_the_own_metering_points(self):
        GridOperator.objects.update_or_create(pk=1, defaults={"name": "Example Grid AG"})
        CustomerBuilding.objects.create(person=self.mine, location=self.location, description="Block A")
        html = self.page()
        self.assertIn("<h2", html)
        self.assertIn("Musterstrasse 1", html)
        self.assertIn("3063 Ittigen", html)
        self.assertIn("<dt>EGID</dt><dd>90001234</dd>", html)
        self.assertIn("<dt>Netzbetreiber</dt><dd>Example Grid AG</dd>", html)
        self.assertIn("Block A", html)
        self.assertNotIn("EGRID", html)
        self.assertIn(self.draw.number, html)
        self.assertIn("Wohnhaus", html)
        # The other customer of the shared building: nothing of theirs.
        self.assertNotIn(self.foreign.number, html)
        self.assertNotIn("Fremd", html)

    def test_without_a_grid_operator_name_the_row_is_left_out(self):
        GridOperator.objects.filter(pk=1).delete()
        self.assertNotIn("<dt>Netzbetreiber</dt>", self.page())

    def test_the_own_name_is_the_heading_and_the_address_stands_under_it(self):
        CustomerBuilding.objects.create(person=self.mine, location=self.location, name="Mein Haus")
        html = self.page()
        self.assertRegex(html, r"<h2[^>]*>Mein Haus")
        self.assertIn("Musterstrasse 1, 3063 Ittigen", html)
        # Another customer's name of the building is never shown.
        CustomerBuilding.objects.create(person=self.other, location=self.location, name="Fremder Name")
        self.assertNotIn("Fremder Name", self.page())

    def test_a_status_is_a_dot_and_a_word(self):
        html = self.page()
        self.assertRegex(html, r'<span class="dot tone-ok" aria-hidden="true"></span>Teilnehmend')
        self.assertRegex(html, r'<span class="dot tone-ok" aria-hidden="true"></span>Aktiv')
        self.assertRegex(html, r"Seit \d\d\.\d\d\.\d{4}")

    def test_the_two_main_buttons_and_the_buttons_of_the_card(self):
        html = self.page()
        self.assertIn(">Anmeldung starten<", html)
        self.assertIn(">Abmeldung starten<", html)
        self.assertLess(html.index(">Anmeldung starten<"), html.index(">Abmeldung starten<"))
        self.assertIn(f'href="{reverse("customer:application_start")}?building={self.location.pk}"', html)
        self.assertIn(">Messpunkt hinzufügen<", html)
        self.assertIn(">Messpunkt entfernen<", html)
        self.assertIn(reverse("customer:deregistration_start"), html)

    def test_without_an_active_point_there_is_no_button_to_deregister(self):
        other_location = make_location("Neustrasse", "2", "3063", "Ittigen")
        point = make_metering_point(
            5, Direction.CONSUMPTION, other_location, registered_with_operator_on=TODAY, confirmed_on=TODAY,
            start_date=TODAY + timedelta(days=30),
        )
        newcomer = customer("newcomer.buildings@example.org")
        assign_person(point, newcomer, valid_from=TODAY)
        html = self.page(login(newcomer))
        self.assertNotIn("Abmeldung starten", html)
        self.assertNotIn("Messpunkt entfernen", html)
        self.assertRegex(html, r"Start am \d\d\.\d\d\.\d{4}")

    def test_a_building_with_a_draw_and_a_feed_in_has_no_button_to_add_a_point(self):
        active_point(3, self.mine, self.location, Direction.FEED_IN)
        self.assertNotIn(">Messpunkt hinzufügen<", self.page())

    def test_a_deregistration_shows_its_status_and_progress(self):
        deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk])
        html = self.page()
        self.assertRegex(html, r'<span class="dot tone-bad" aria-hidden="true"></span>In Abmeldung')
        self.assertIn("Abmeldung: 1 von 3 erledigt", html)
        self.assertIn(f"Frühestens am {deregistrations.earliest_date_preview().strftime('%d.%m.%Y')}. Die LEG bestätigt den Termin.", html)
        self.assertNotIn("Abmeldung starten", html)

    def test_a_terminated_point_is_ended(self):
        MeteringPoint.objects.filter(pk=self.draw.pk).update(terminated_on=LONG_AGO + timedelta(days=100))
        self.assertIn("Beendet", self.page())

    def test_an_archived_point_is_not_shown(self):
        MeteringPoint.objects.filter(pk=self.draw.pk).update(is_archived=True)
        self.assertNotIn(self.draw.number, self.page())

    # --- running registrations --------------------------------------------------------------------------------

    def test_a_draft_shows_its_progress_and_the_way_to_continue(self):
        publish_contract()
        configure_numbers()
        newcomer = customer("draft.buildings@example.org")
        application, _building = draft_with_points(newcomer, number=50)
        html = self.page(login(newcomer))
        self.assertRegex(html, r'<span class="dot tone-muted" aria-hidden="true"></span>Entwurf')
        self.assertRegex(html, r"Anmeldung: \d von \d erledigt")
        self.assertIn("<progress", html)
        self.assertIn(reverse("customer:application", args=[application.pk]), html)
        self.assertIn(">Weiter anmelden<", html)
        self.assertIn("Ittigenstrasse 51", html)

    def test_a_draft_without_a_building_yet_is_a_card_and_not_the_empty_state(self):
        """Walkthrough 3: the start page shows the progress of the draft, so the page Buildings lists it too, marked as
        not completely recorded, with the way to continue (never "No building registered yet")."""
        newcomer = customer("started.buildings@example.org")
        application = applications.start_draft(newcomer)
        html = self.page(login(newcomer))
        self.assertNotIn("Noch kein Gebäude angemeldet", html)
        self.assertIn("Gebäude noch nicht erfasst", html)
        self.assertRegex(html, r'<span class="dot tone-muted" aria-hidden="true"></span>Entwurf, noch nicht vollständig erfasst')
        self.assertRegex(html, r"Anmeldung: 0 von \d erledigt")
        self.assertIn(reverse("customer:application", args=[application.pk]), html)
        self.assertIn(">Weiter anmelden<", html)
        # The same draft is what the start page follows.
        home = login(newcomer).get(reverse("home")).content.decode()
        self.assertIn("Ihr Weg in die LEG", home)

    def test_a_draft_with_a_building_is_marked_as_not_completely_recorded(self):
        newcomer = customer("marked.buildings@example.org")
        draft_with_building(newcomer, egid=90005556, street="Seitenweg", house_number="10", postal_code="3000", town="Bern")
        html = self.page(login(newcomer))
        self.assertIn("Seitenweg 10", html)
        self.assertRegex(html, r'<span class="dot tone-muted" aria-hidden="true"></span>Entwurf, noch nicht vollständig erfasst')

    def test_a_submitted_application_can_be_changed_until_the_leg_takes_it_into_review(self):
        publish_contract()
        configure_numbers()
        newcomer = customer("changeable.buildings@example.org")
        application, _building = draft_with_points(newcomer, number=50)
        from leg_platform.community import application_staff, contracts

        applications.accept_contract_step(
            newcomer, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        applications.set_start(newcomer, application, asap=True)
        applications.submit(newcomer, application)
        change = reverse("customer:application_change", args=[application.pk])
        html = self.page(login(newcomer))
        self.assertEqual(html.count(f'href="{change}"'), 1)
        self.assertIn(">Anmeldung ändern<", html)
        self.assertNotIn("Sie können sie hier nicht mehr ändern", html)
        staff = staff_user("staff.changeable@example.org")
        application_staff.start_review(application, staff)
        html = self.page(login(newcomer))
        self.assertNotIn(change, html)
        self.assertIn("Die LEG prüft Ihre Anmeldung. Sie können sie hier nicht mehr ändern.", html)
        # The withdrawal is the documented way until the registration at the grid operator starts.
        self.assertIn(reverse("customer:application_withdraw", args=[application.pk]), html)

    def test_a_submitted_application_can_be_withdrawn_until_the_operator_starts(self):
        publish_contract()
        configure_numbers()
        newcomer = customer("submitted.buildings@example.org")
        application, _building = draft_with_points(newcomer, number=50)
        from leg_platform.community import contracts

        applications.accept_contract_step(
            newcomer, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        applications.set_start(newcomer, application, asap=True)
        applications.submit(newcomer, application)
        html = self.page(login(newcomer))
        self.assertRegex(html, r'<span class="dot tone-warn" aria-hidden="true"></span>Bei der LEG in Prüfung')
        self.assertIn("Anmeldung: 1 von 5 erledigt", html)
        for word in ("Angaben abgeschickt", "Prüfung durch die LEG", "Wird von"):
            self.assertIn(word, html)
        # ONE withdraw button (W1-15): the card's, red outline; the progress block does not repeat it and no second
        # wording ("Remove metering point") is offered for the same action.
        withdraw = reverse("customer:application_withdraw", args=[application.pk])
        self.assertEqual(html.count(f'href="{withdraw}"'), 1)
        self.assertRegex(html, rf'btn btn-danger-outline end-action" href="{withdraw}">Anmeldung zurückziehen<')
        self.assertNotIn(">Messpunkt entfernen<", html)
        # From the start of the registration at the grid operator on nothing can be withdrawn.
        from leg_platform.community import application_staff

        staff = staff_user("staff.buildings@example.org")
        application_staff.mark_operator_started(application, staff)
        html = self.page(login(newcomer))
        self.assertNotIn(reverse("customer:application_withdraw", args=[application.pk]), html)
        self.assertNotIn(">Messpunkt entfernen<", html)
        self.assertIn("Abmeldung noch nicht möglich, bis der Messpunkt aktiv ist.", html)

    def test_a_paused_draft_says_so(self):
        newcomer = customer("paused.buildings@example.org")
        application, _building = draft_with_points(newcomer, number=50)
        type(application).objects.filter(pk=application.pk).update(state="paused")
        self.assertIn("Ihre Anmeldung ist pausiert", self.page(login(newcomer)))

    def test_a_building_of_a_draft_without_points_is_a_card_with_its_address(self):
        newcomer = customer("nopoints.buildings@example.org")
        draft_with_building(newcomer, egid=90005555, street="Seitenweg", house_number="9", postal_code="3000", town="Bern")
        html = self.page(login(newcomer))
        self.assertIn("Seitenweg 9", html)
        self.assertIn("<dt>EGID</dt><dd>90005555</dd>", html)

    def test_the_page_has_no_inline_script_or_style(self):
        html = self.page()
        self.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")
        self.assertNotRegex(html, r'\sstyle="')
        self.assertNotRegex(html, r"(?i)\son(click|load|change|submit)=")


@tag("mariadb")
class NamePagesTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.names@example.org")
        cls.other = customer("other.names@example.org")
        cls.location = make_location("Namestrasse", "3", "3063", "Ittigen")
        cls.point = active_point(7, cls.mine, cls.location, name="Alt")
        cls.other_point = active_point(8, cls.other, cls.location)

    def setUp(self):
        self.client = login(self.mine)
        self.details = reverse("customer:building_details", args=[self.location.pk])
        self.point_name = reverse("customer:metering_point_name", args=[self.point.pk])

    def test_the_pencil_of_a_building_changes_the_own_name_and_description_only(self):
        CustomerBuilding.objects.create(person=self.other, location=self.location, name="Fremd", description="Fremde Beschreibung")
        page = self.client.get(self.details).content.decode()
        self.assertIn("Name des Gebäudes", page)
        self.assertIn("Namestrasse 3", page)
        self.assertNotIn("Fremd", page)
        response = self.client.post(self.details, {"name": "Mein Haus", "description": "Block A"})
        self.assertRedirects(response, BUILDINGS)
        self.assertEqual(buildings.building_details(self.mine, self.location), ("Mein Haus", "Block A"))
        self.assertEqual(buildings.building_details(self.other, self.location), ("Fremd", "Fremde Beschreibung"))
        self.assertIn("Mein Haus", self.client.get(BUILDINGS).content.decode())

    def test_a_text_with_control_characters_is_refused_with_a_message_and_nothing_is_saved(self):
        response = self.client.post(self.details, {"name": "Bad\x07Name", "description": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bitte prüfen Sie Ihre Angaben", response.content.decode())
        self.assertEqual(buildings.building_details(self.mine, self.location), ("", ""))

    def test_a_text_that_is_too_long_is_refused(self):
        response = self.client.post(self.details, {"name": "x" * 81, "description": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(buildings.building_details(self.mine, self.location), ("", ""))

    def test_the_pencil_of_a_metering_point_changes_its_name(self):
        self.assertIn(self.point.number, self.client.get(self.point_name).content.decode())
        response = self.client.post(self.point_name, {"name": "Neu"})
        self.assertRedirects(response, BUILDINGS)
        self.point.refresh_from_db()
        self.assertEqual(self.point.name, "Neu")
        self.assertIn("Neu", self.client.get(BUILDINGS).content.decode())

    def test_the_name_of_an_archived_point_cannot_be_changed(self):
        MeteringPoint.objects.filter(pk=self.point.pk).update(is_archived=True)
        self.assertEqual(self.client.get(self.point_name).status_code, 404)
        self.assertEqual(self.client.post(self.point_name, {"name": "X"}).status_code, 404)

    def test_the_deregistration_page_takes_the_building_of_the_account_only(self):
        url = reverse("customer:deregistration_start")
        self.assertEqual(self.client.get(url, {"building": self.location.pk}).status_code, 200)
        # A building of another account (or none) is a 404, like a missing one.
        stranger = make_location("Fremdstrasse", "9", "3063", "Ittigen")
        assign_person(make_metering_point(9, Direction.CONSUMPTION, stranger), self.other)
        self.assertEqual(self.client.get(url, {"building": stranger.pk}).status_code, 404)

    def test_a_former_holder_gets_a_404_on_the_pages_of_the_point_and_the_building_and_changes_nothing(self):
        holder = customer("holder.names@example.org")
        successor = customer("successor.names@example.org")
        place = make_location("Wechselweg", "6", "3063", "Ittigen")
        point = make_metering_point(11, Direction.CONSUMPTION, place, name="Neu")
        assign_person(point, holder, date(2024, 1, 1), TODAY - timedelta(days=30))
        assign_person(point, successor, TODAY - timedelta(days=29))
        CustomerBuilding.objects.create(person=holder, location=place, name="Alt", description="alt")
        former = login(holder)
        name_url = reverse("customer:metering_point_name", args=[point.pk])
        details_url = reverse("customer:building_details", args=[place.pk])

        for url, data in ((name_url, {"name": "Gehackt"}), (details_url, {"name": "Gehackt", "description": "x"})):
            with self.subTest(url=url):
                self.assertEqual(former.get(url).status_code, 404)
                self.assertEqual(former.post(url, data).status_code, 404)
        point.refresh_from_db()
        self.assertEqual(point.name, "Neu")
        self.assertEqual(buildings.building_details(holder, place), ("", ""))
        self.assertEqual(CustomerBuilding.objects.get(person=holder, location=place).name, "Alt")
        # The current holder has both pages.
        current = login(successor)
        self.assertEqual(current.get(name_url).status_code, 200)
        self.assertEqual(current.get(details_url).status_code, 200)
        # A former holder cannot start from the building either.
        start = reverse("customer:application_start")
        self.assertEqual(former.get(start, {"building": place.pk}).status_code, 404)
        self.assertEqual(former.get(reverse("customer:deregistration_start"), {"building": place.pk}).status_code, 404)

    def test_a_post_beyond_the_request_budget_is_refused_neutrally_with_retry_after(self):
        from unittest.mock import patch

        from leg_platform.customer import limits

        with patch.object(limits, "allow_wizard", return_value=False):
            response = self.client.post(self.point_name, {"name": "Zu viel"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], str(limits.RETRY_AFTER))
        self.assertIn("Zu viele Anfragen", response.content.decode())
        self.point.refresh_from_db()
        self.assertEqual(self.point.name, "Alt")
