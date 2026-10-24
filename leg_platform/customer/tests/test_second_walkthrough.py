"""The customer-side findings of the second designer walkthrough (N-01 to N-15; D-105): the way back for a returned draft,
notes that stay one paragraph, the label of the earliest date, the contact link in the Information group and the menu foot
that stays in view, the progress of a draft without points, the numbering of the overview, the submitted page, the earliest
date on the result, the card and the start page, the deletion texts, the EGID sentence, the focus ring and the word
"Aufschaltung"."""

import re
from datetime import date, timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.tests.helpers import PASSWORD
from leg_platform.community import applications, contracts, deregistrations, numbers
from leg_platform.community.models import Application, GridOperator
from leg_platform.community.tests.helpers import staff_user
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    HOME,
    START,
    candidate,
    configure_numbers,
    customer,
    directory,
    draft_with_building,
    draft_with_points,
    login,
    publish_contract,
    step_url,
)
from leg_platform.customer.tests.test_deregistration import START as DEREGISTRATION
from leg_platform.customer.tests.test_deregistration import active_point
from leg_platform.customer.tests.test_start_page import complete_application

PACKAGE = Path(__file__).resolve().parents[1]
CSS = (PACKAGE / "static" / "customer" / "css" / "customer.css").read_text(encoding="utf-8")
FRAME_CSS = (PACKAGE.parent / "static" / "css" / "frame.css").read_text(encoding="utf-8")


def text(response):
    return response.content.decode()


def complete_draft(person):
    """A draft whose five steps are done (nothing is open)."""
    application, building = draft_with_points(person)
    applications.accept_contract_step(
        person, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
    )
    applications.set_start(person, application, asap=True)
    return application, building


@tag("mariadb")
class ReturnedDraftWayBackTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        publish_contract()
        self.mine = customer("n01.mine@example.org")
        self.client = login(self.mine)
        self.application, _building = complete_draft(self.mine)
        Application.objects.filter(pk=self.application.pk).update(returned_at=timezone.now(), returned_reason="Nummer prüfen")

    def test_the_card_leads_to_the_overview_not_to_the_submit_step(self):
        html = text(self.client.get(HOME))
        card = html[html.index('class="stage-cards"') :]
        card = card[: card.index("</section>")]
        self.assertIn("Bitte nachbessern", card)
        self.assertIn(f'<a class="btn btn-primary" href="{START}">Angaben prüfen</a>', card)
        self.assertNotIn(">Anmeldung abschicken<", card)

    def test_the_overview_shows_the_rows_with_change_before_the_submit_button(self):
        html = text(self.client.get(START))
        self.assertIn("Angaben prüfen", html)
        self.assertGreater(html.index(">Anmeldung abschicken<"), html.index('class="overview-rows"'))
        self.assertGreaterEqual(html.count(">Ändern<span"), 3)
        self.assertNotIn("Als Nächstes", html)

    def test_the_building_card_and_the_wizard_have_the_button_in_the_note(self):
        self.assertIn(f'href="{START}">Angaben prüfen</a>', text(self.client.get(BUILDINGS)))
        page = text(self.client.get(step_url(self.application, "start_date")))
        self.assertIn(f'href="{START}">Angaben prüfen</a>', page)

    def test_every_step_has_a_back_link_to_the_step_before_or_the_overview(self):
        building = text(self.client.get(step_url(self.application, "building")))
        self.assertIn(f'<a class="back-link" href="{START}">Zurück</a>', building)
        points = text(self.client.get(step_url(self.application, "metering_points")))
        self.assertIn(f'<a class="back-link" href="{step_url(self.application, "building")}">Zurück</a>', points)
        start = text(self.client.get(step_url(self.application, "start_date")))
        self.assertIn(f'<a class="back-link" href="{step_url(self.application, "contract")}">Zurück</a>', start)

    def test_a_draft_that_was_not_returned_keeps_the_submit_button(self):
        Application.objects.filter(pk=self.application.pk).update(returned_at=None, returned_reason="")
        html = text(self.client.get(HOME))
        self.assertIn(">Anmeldung abschicken<", html)
        self.assertNotIn("Angaben prüfen", html)


@tag("mariadb")
class DraftCardAndOverviewTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        publish_contract()
        self.mine = customer("n07.mine@example.org")
        self.client = login(self.mine)

    def test_a_paused_draft_without_points_shows_progress_the_note_and_continue(self):
        application, _building = draft_with_building(self.mine)
        Application.objects.filter(pk=application.pk).update(state=Application.State.PAUSED)
        html = text(self.client.get(BUILDINGS))
        card = html[html.index('class="building-card"') :]
        self.assertIn("mp-progress", card)
        self.assertIn("Ihre Anmeldung ist pausiert", card)
        self.assertIn(f'href="{reverse("customer:application", args=[application.pk])}">Weiter anmelden</a>', card)

    def test_a_draft_with_a_returned_reason_without_points_shows_the_reason_on_the_card(self):
        application, _building = draft_with_building(self.mine)
        Application.objects.filter(pk=application.pk).update(returned_at=timezone.now(), returned_reason="Adresse prüfen")
        card = text(self.client.get(BUILDINGS))
        self.assertIn("Adresse prüfen", card)
        self.assertIn("Angaben prüfen", card)

    def test_a_draw_only_overview_numbers_the_rows_one_to_four_and_has_no_number_for_the_bank_row(self):
        html = text(self.client.get(START))
        numbers = re.findall(r'class="overview-number" aria-hidden="true">([^<]*)<', html)
        self.assertEqual(numbers, ["1", "2", "–", "3", "4"])
        self.assertIn("0 von 4 Schritten erledigt", html)
        self.assertIn("Sie wählen, ab wann Ihre Teilnahme beginnt.", html)
        self.assertNotIn("Wahl des Startdatums", html)

    def test_after_the_points_are_known_a_draw_only_draft_has_four_numbered_rows(self):
        application, _building = draft_with_points(self.mine)
        html = text(self.client.get(START))
        numbers = re.findall(r'class="overview-number" aria-hidden="true">([^<]*)<', html)
        self.assertEqual(numbers, ["1", "2", "3", "4"])
        self.assertIn("2 von 4 Schritten erledigt", html)

    def test_with_a_feed_in_the_bank_row_is_a_numbered_step(self):
        draft_with_points(self.mine, feed_in=True)
        html = text(self.client.get(START))
        numbers = re.findall(r'class="overview-number" aria-hidden="true">([^<]*)<', html)
        self.assertEqual(numbers, ["1", "2", "3", "4", "5"])


@tag("mariadb")
class SubmittedPageTests(TestCase):
    databases = {"default", "addresses"}

    def test_the_operator_is_named_once_the_points_have_numbers_and_the_wish_is_labelled(self):
        configure_numbers()
        publish_contract()
        GridOperator.objects.update_or_create(pk=1, defaults={"name": "Beispiel Netz AG"})
        mine = customer("n09.mine@example.org")
        client = login(mine)
        application, building = draft_with_points(mine, feed_in=True)
        from leg_platform.community.tests.onboarding import GOOD_IBAN

        applications.save_bank_connection(mine, application, GOOD_IBAN)
        applications.accept_contract_step(
            mine, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        wish = applications.earliest_start_date() + timedelta(days=40)
        client.post(step_url(application, "start_date"), {"start": "date", "desired_date": wish.isoformat()})

        html = text(client.get(reverse("customer:application_submitted", args=[application.pk])))

        self.assertEqual(html.count("Beispiel Netz AG"), 1)
        self.assertIn("Ihr Wunschdatum", html)
        self.assertIn(wish.strftime("%d.%m.%Y"), html)
        for point in building.metering_points.all():
            self.assertIn(numbers.format_metering_point_number(point.number), html)
        self.assertNotIn("<progress", html)


@tag("mariadb")
class DeregistrationDateTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        from leg_platform.community.tests.helpers import make_location
        from leg_platform.community.tests.onboarding import GOOD_IBAN
        from leg_platform.community import own_iban

        self.mine = customer("n10.mine@example.org")
        self.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        self.draw = active_point(90, self.mine, self.location)
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.client = login(self.mine)
        self.earliest = deregistrations.earliest_date_preview().strftime("%d.%m.%Y")

    def submit(self, **extra):
        return self.client.post(DEREGISTRATION, {
            "metering_points": [str(self.draw.pk)], "termination": "earliest", "current_password": PASSWORD, **extra,
        })

    def test_the_radio_names_the_date_on_a_second_line(self):
        html = text(self.client.get(DEREGISTRATION))
        self.assertRegex(html, r"Nächstmöglicher Termin<span class=\"meta\">Frühestens am " + self.earliest)
        self.assertIn(".entry-check .meta", CSS)
        block = CSS[CSS.index(".entry-check .meta") :]
        self.assertIn("display: block", block[: block.index("}")])

    def test_the_result_the_card_and_the_start_page_say_the_same(self):
        response = self.submit()
        words = f"Frühestens am {self.earliest}. Die LEG bestätigt den Termin."
        self.assertIn(words, text(self.client.get(response["Location"])))
        self.assertIn(words, text(self.client.get(BUILDINGS)))
        self.assertIn(f"Ihre Abmeldung ist eingereicht. {words}", text(self.client.get(HOME)))

    def test_a_desired_date_is_called_a_desired_date(self):
        from leg_platform.community.application_common import add_months, month_end

        wish = month_end(add_months(timezone.localdate(), 6))
        response = self.submit(termination="date", desired_date=wish.isoformat())
        self.assertIn(f"Wunschtermin {wish.strftime('%d.%m.%Y')}. Die LEG bestätigt den Termin.", text(self.client.get(response["Location"])))


@tag("mariadb")
class ProfileDeletionTests(TestCase):
    databases = {"default", "addresses"}

    def test_the_page_offers_deregistration_while_points_run_and_a_running_one_does_not_block(self):
        from leg_platform.community.tests.helpers import make_location

        mine = customer("n11.mine@example.org")
        active_point(91, mine, make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234))
        client = login(mine)
        # The card is one sentence and leads to the page; the page offers "Messpunkte abmelden" while points run.
        card = text(client.get(reverse("accounts:profile")))
        card = card[card.index('id="card-delete"') :]
        self.assertIn(f'href="{reverse("accounts:account_delete")}"', card)
        page = text(client.get(reverse("accounts:account_delete")))
        self.assertIn(">Messpunkte abmelden<", page)
        self.assertIn(reverse("customer:deregistration_start"), page)

        deregistrations.request_deregistration(mine, metering_point_ids=[mine.metering_point_assignments.get().metering_point_id])
        # A running deregistration is no longer a reason to refuse anything: the page still asks for the proof.
        page = text(client.get(reverse("accounts:account_delete")))
        self.assertIn("Konto unwiderruflich löschen", page)


@tag("mariadb")
class StepOneAndMenuTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        self.mine = customer("n13.mine@example.org")
        self.client = login(self.mine)

    def test_the_egid_is_explained_once_per_state_where_it_is_shown(self):
        sentence = "Die EGID ist die Nummer Ihres Gebäudes im eidgenössischen Gebäuderegister."
        application = applications.start_draft(self.mine)
        url = step_url(application, "building")
        with directory(candidate(90004269)):
            self.client.post(url, {"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"})
        self.assertEqual(text(self.client.get(url)).count(sentence), 1)
        application.buildings.all().delete()
        two = [candidate(90004269), candidate(90004270)]
        with directory(*two):
            response = self.client.post(url, {"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"})
        self.assertEqual(text(response).count(sentence), 1)
        # Before anything is shown there is no sentence.
        application.buildings.all().delete()
        self.assertNotIn(sentence, text(self.client.get(url)))

    def test_the_contact_link_is_in_the_information_group_not_in_the_foot(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        html = text(self.client.get(HOME))
        menu = html[html.index('<nav class="frame-menu') :]; menu = menu[: menu.index("</nav>")]
        information = menu[menu.index('<ul class="frame-sub">', menu.index("Information")) :]
        information = information[: information.index("</ul>")]
        self.assertIn('href="https://leg.example.org/"', information)
        self.assertIn('<span class="visually-hidden"> (öffnet in neuem Tab)</span>', information)
        foot = menu[menu.index("frame-menu-foot") :]
        self.assertNotIn("Kontakt zur LEG", foot)
        self.assertIn("Ausloggen", foot)

    def test_the_foot_with_log_out_stays_in_view(self):
        block = FRAME_CSS[FRAME_CSS.index(".frame-menu-foot {") :]
        block = block[: block.index("}")]
        self.assertIn("position: sticky", block)
        self.assertIn("bottom: 0", block)
        self.assertIn("background: var(--panel)", block)


class StyleTests(SimpleTestCase):
    def test_a_note_with_a_link_in_its_sentence_is_not_a_grid(self):
        # N-02: ``p.note-warn`` etc. are blocks; the grid stays only for a note that holds several blocks.
        self.assertRegex(CSS, r"p\.note-bad,\s*p\.note-warn,\s*p\.note-info,\s*span\.note-warn \{\s*display: block;")

    def test_the_focused_result_fieldset_uses_the_portal_ring_and_has_room(self):
        block = CSS[CSS.index('fieldset.question[tabindex="-1"]:focus,') :]
        block = block[: block.index("}")]
        self.assertIn("padding: 12px", block)
        self.assertIn("outline: 2px solid var(--accent)", block)
        self.assertNotIn("#000", block)

    def test_no_translation_of_the_customer_or_staff_pages_says_aufschaltung(self):
        po = (PACKAGE.parents[1] / "locale" / "de_CH" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
        self.assertNotIn("Aufschaltung", po)
        with translation.override("de-ch"):
            self.assertEqual(translation.gettext("Start date"), "Startdatum")
