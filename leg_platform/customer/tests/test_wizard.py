"""The registration wizard (EINSTIEG, SCHRITT-1 to SCHRITT-5, ABBRECHEN, ANMELDUNG-ABGESCHICKT; D-103): every step and every
state of it as a normal form POST, the server-held draft, resume, cancel with rollback, withdraw, the page that is shown once."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import MailJob
from leg_platform.community import application_staff, applications, buildings, contracts, history, numbers, own_iban
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ContractAcceptance,
    Direction,
    Location,
    ManagementSettings,
    MeteringPoint,
)
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import pdf
from leg_platform.customer import limits
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    GOOD_IBAN,
    HOME,
    IDENTIFIER,
    START,
    candidate,
    configure_numbers,
    customer,
    directory,
    draft_with_building,
    draft_with_points,
    full_number,
    login,
    publish_contract,
    step_url,
)

ADDRESS_FORM = {"street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen"}


def text(response):
    return response.content.decode()


class WizardCase(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("mine.wizard@example.org")
        self.client = login(self.mine)
        configure_numbers()
        self.version = publish_contract()

    def draft(self):
        return Application.objects.get(person=self.mine)


@tag("mariadb")
class StartPageTests(WizardCase):
    def test_without_a_draft_five_rows_with_their_states_and_one_main_button(self):
        html = text(self.client.get(START))
        self.assertIn("Was müssen wir noch wissen?", html)
        self.assertIn("Bereits hinterlegte Angaben übernehmen wir. Sie können sie jederzeit ändern.", html)
        for name in ("Gebäude", "Messpunkte", "Bank- oder Postverbindung", "Vertrag", "Startdatum"):
            self.assertIn(name, html)
        self.assertIn("nur bei Einspeisung", html)
        self.assertIn("0 von 4 Schritten erledigt", html)
        self.assertNotIn("Bereits hinterlegt</span>", html.replace("Bereits hinterlegte", ""))
        self.assertEqual(html.count('class="btn btn-primary"'), 1)
        self.assertNotIn("Abbrechen", html)

    def test_what_is_on_file_is_green_and_counts_as_done(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        publish = contracts.current_version()
        contracts.accept_contract(self.mine, publish.pk, agrees_contract=True)
        html = text(self.client.get(START))
        self.assertIn("1 von 4 Schritten erledigt", html)
        self.assertEqual(html.count("Bereits hinterlegt</span>"), 2)
        # "Change" leads to where the detail is kept: the profile card for the bank connection (with the safe way back
        # to this page), the contract page.
        self.assertIn(f'href="{reverse("accounts:profile")}?edit=bank&amp;next=%2Fparticipation%2Fapplication%2F', html)
        self.assertIn(f'href="{reverse("customer:contract")}"', html)

    def test_the_starting_point_add_a_metering_point_makes_the_building_green(self):
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        point = make_metering_point(60, location=location)
        assign_person(point, self.mine, valid_from=date(2025, 1, 1))
        html = text(self.client.get(START, {"building": location.pk}))
        self.assertIn("1 von 4 Schritten erledigt", html)
        self.assertIn("Musterstrasse 1, 3063 Ittigen", html)
        response = self.client.post(START, {"building": location.pk})
        # Something is on file (the building): the overview shows it first (W1-05).
        self.assertRedirects(response, f"{START}?building={location.pk}")
        self.assertFalse(Application.objects.filter(person=self.mine).exists())
        response = self.client.post(START, {"building": location.pk, "overview": "1"})
        draft = self.draft()
        self.assertEqual(draft.buildings.get().location, location)
        self.assertRedirects(response, step_url(draft, "metering_points"))

    def test_continue_creates_the_draft_and_leads_to_the_first_open_step(self):
        response = self.client.post(START)
        draft = self.draft()
        self.assertRedirects(response, step_url(draft, "building"))
        self.assertEqual(draft.state, Application.State.DRAFT)

    def test_a_draft_is_offered_to_continue_and_a_second_one_is_never_made(self):
        draft, _building = draft_with_points(self.mine)
        html = text(self.client.get(START))
        self.assertIn("Sie haben eine Anmeldung begonnen. Sie können dort weitermachen, wo Sie aufgehört haben.", html)
        self.assertIn("Noch nicht abgeschlossen", html)
        self.assertIn(">Weiter anmelden<", html)
        self.assertIn(">Neu beginnen<", html)
        self.assertIn("Wenn Sie neu beginnen, werden die bisherigen Angaben dieser Anmeldung gelöscht.", html)
        self.assertIn(step_url(draft, "contract"), html)
        self.client.post(START)
        self.assertEqual(Application.objects.filter(person=self.mine).count(), 1)

    def test_start_again_deletes_the_draft_only_and_shows_the_start_page(self):
        draft, _building = draft_with_points(self.mine)
        response = self.client.post(reverse("customer:application_cancel", args=[draft.pk]), {"restart": "1"})
        self.assertRedirects(response, START)
        self.assertFalse(Application.objects.filter(person=self.mine).exists())

    def test_a_paused_draft_says_so_on_the_start_page_and_on_every_step(self):
        draft, _building = draft_with_points(self.mine)
        Application.objects.filter(pk=draft.pk).update(state=Application.State.PAUSED)
        banner = "Ihre Anmeldung ist pausiert, weil Sie sie länger nicht bearbeitet haben. Machen Sie weiter, sonst wird sie"
        self.assertIn(banner, text(self.client.get(START)))
        self.assertIn(banner, text(self.client.get(step_url(draft, "contract"))))
        # Any change ends the pause.
        self.client.post(step_url(draft, "contract"), {"version": self.version.pk, "agrees_contract": "on"})
        self.assertEqual(self.draft().state, Application.State.DRAFT)

    def test_a_draft_returned_by_the_leg_says_so(self):
        draft, _building = draft_with_points(self.mine)
        Application.objects.filter(pk=draft.pk).update(returned_at=draft.created_at)
        self.assertIn("Die LEG hat Ihre Anmeldung zurückgegeben.", text(self.client.get(step_url(draft, "contract"))))


@tag("mariadb")
class ResumeTests(WizardCase):
    def test_the_resume_page_leads_to_the_next_open_step(self):
        application = applications.start_draft(self.mine)
        resume = reverse("customer:application", args=[application.pk])
        self.assertRedirects(self.client.get(resume), step_url(application, "building"), fetch_redirect_response=False)
        with directory(candidate(90004269)):
            applications.choose_building(self.mine, application, **ADDRESS_FORM)
        self.assertRedirects(self.client.get(resume), step_url(application, "metering_points"), fetch_redirect_response=False)

    def test_a_step_that_is_not_available_yet_leads_to_the_resume_page(self):
        application = applications.start_draft(self.mine)
        for name in ("metering_points", "bank_connection", "contract", "start_date"):
            with self.subTest(step=name):
                response = self.client.get(step_url(application, name))
                self.assertRedirects(
                    response, reverse("customer:application", args=[application.pk]), fetch_redirect_response=False
                )
                post = self.client.post(step_url(application, name), {})
                self.assertEqual(post.status_code, 302)

    def test_a_submitted_application_is_no_wizard_any_more(self):
        application, _building = draft_with_points(self.mine)
        applications.accept_contract_step(
            self.mine, application, self.version.pk, agrees_contract=True
        )
        applications.set_start(self.mine, application, asap=True)
        applications.submit(self.mine, application)
        for name in ("building", "metering_points", "contract", "start_date"):
            self.assertRedirects(self.client.get(step_url(application, name)), BUILDINGS, fetch_redirect_response=False)


@tag("mariadb")
class BuildingStepTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application = applications.start_draft(self.mine)
        self.url = step_url(self.application, "building")

    def test_the_input_state_has_the_fields_the_progress_and_the_two_buttons(self):
        html = text(self.client.get(self.url))
        for marker in (
            "Meine Teilnahme · Anmeldung", "Schritt 1 von 4: Gebäude", "Wo liegt Ihr Gebäude?",
            "Geben Sie die Adresse ein, an der sich Ihr Strombezug befindet.", "Name des Gebäudes", "(optional)",
            'name="street"', 'name="house_number"', 'name="postal_code"', 'name="town"', "z. B. Wohnhaus oder Block A",
            ">Weiter<", ">Abbrechen<", "<progress",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('value="Ittigenstrasse"', html)

    def test_a_found_building_is_chosen_and_shown_with_its_address_and_egid(self):
        with directory(candidate(90004269)):
            response = self.client.post(self.url, {**ADDRESS_FORM, "name": "Wohnhaus"})
        self.assertRedirects(response, self.url)
        row = self.application.buildings.get()
        self.assertEqual((row.egid, row.name), (90004269, "Wohnhaus"))
        html = text(self.client.get(self.url))
        self.assertIn("Gebäude gefunden", html)
        self.assertIn("Ittigenstrasse 51, 3063 Ittigen", html)
        self.assertIn("EGID 90004269", html)
        self.assertNotIn("schon angelegt", html)
        self.assertIn(reverse("customer:application", args=[self.application.pk]), html)
        self.assertIn(">Anderes Gebäude wählen<", html)

    def test_an_existing_building_is_attached_to_and_the_customer_is_told(self):
        existing = make_location("Ittigenstrasse", "51", "3063", "Ittigen", egid=90004269)
        with directory(candidate(90004269)):
            self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(self.application.buildings.get().location, existing)
        self.assertIn(
            "Dieses Gebäude ist schon angelegt. Ihre Messpunkte werden hier zugeordnet.", text(self.client.get(self.url))
        )

    def test_several_buildings_at_one_address_are_a_list_with_entrance_and_egid(self):
        two = [candidate(90004269, house_number="51"), candidate(90004270, house_number="51")]
        two[0]["entrance_id"], two[1]["entrance_id"] = 0, 1
        with directory(*two):
            response = self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(response.status_code, 200)
        html = text(response)
        self.assertIn("Zu dieser Adresse gibt es mehrere Gebäude. Welches ist Ihres?", html)
        self.assertIn('type="radio" name="egid" value="90004269"', html)
        self.assertIn("Hauseingang 1", html)
        self.assertIn("EGID 90004270", html)
        self.assertFalse(self.application.buildings.exists())
        with directory(*two):
            chosen = self.client.post(self.url, {**ADDRESS_FORM, "egid": "90004270"})
        self.assertRedirects(chosen, self.url)
        self.assertEqual(self.application.buildings.get().egid, 90004270)

    def test_a_building_that_is_not_found_is_a_red_state_with_the_owners_text_and_nothing_is_stored(self):
        with directory():
            response = self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(response.status_code, 200)
        html = text(response)
        self.assertIn("Nicht eindeutig gefunden", html)
        self.assertIn(
            "Diese Adresse konnte nicht eindeutig einem Gebäude zugeordnet werden. Bitte prüfen Sie die Eingabe oder wenden Sie sich an die LEG.",
            html,
        )
        self.assertIn("is-error", html)
        self.assertFalse(self.application.buildings.exists())

    def test_an_address_that_needs_a_review_is_kept_for_the_leg_and_the_customer_goes_on(self):
        from leg_platform.addresses import buildings as directory_module

        for result in (
            directory_module.BuildingSearch((), directory_module.NOT_FOUND),
            directory_module.BuildingSearch((), directory_module.UNAVAILABLE, reason=directory_module.REASON_LIMIT),
            directory_module.BuildingSearch((), directory_module.UNAVAILABLE, reason=directory_module.REASON_CIRCUIT_OPEN),
            directory_module.BuildingSearch((), directory_module.UNAVAILABLE, reason=directory_module.REASON_FAILED),
        ):
            with self.subTest(result=result):
                self.application.buildings.all().delete()
                with patch.object(directory_module, "find_buildings", return_value=result), patch.object(
                    buildings, "suggest_candidates", return_value=[]
                ):
                    response = self.client.post(self.url, ADDRESS_FORM)
                self.assertRedirects(response, self.url)
                row = self.application.buildings.get()
                self.assertEqual((row.egid, row.street, row.town), (None, "Ittigenstrasse", "Ittigen"))
                page = text(self.client.get(self.url))
                self.assertIn("Wird von der LEG geprüft", page)
                self.assertIn("Diese Adresse wurde im Gebäuderegister noch nicht gefunden. Die LEG prüft sie. Sie können weitermachen.", page)
                self.assertIn(reverse("customer:application", args=[self.application.pk]), page)
                # The next step is open: the customer can continue with the metering points.
                self.assertEqual(self.client.get(step_url(self.application, "metering_points")).status_code, 200)

    def test_a_visitor_who_cannot_be_asked_stays_an_error_and_nothing_is_stored(self):
        from leg_platform.addresses import buildings as directory_module

        result = directory_module.BuildingSearch(
            (), directory_module.UNAVAILABLE, reason=directory_module.REASON_NOT_SIGNED_IN
        )
        with patch.object(directory_module, "find_buildings", return_value=result), patch.object(
            buildings, "suggest_candidates", return_value=[]
        ):
            response = self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Die Adresssuche ist im Moment nicht verfügbar.", text(response))
        self.assertFalse(self.application.buildings.exists())

    def test_an_address_search_that_cannot_be_made_is_a_message_not_an_error_page(self):
        with patch.object(buildings, "find_candidates", side_effect=ApplicationError("directory_unavailable")), patch.object(
            buildings, "suggest_candidates", return_value=[]
        ):
            response = self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Die Adresssuche ist im Moment nicht verfügbar.", text(response))

    def test_typing_mistakes_get_suggestions_and_the_portal_never_takes_one_by_itself(self):
        suggestion = candidate(90004269, street="Ittigenstrasse")
        suggestion["entrance_id"] = None
        with directory(candidate(90004269), suggestions=[suggestion]) as lookup:
            response = self.client.post(self.url, {**ADDRESS_FORM, "street": "Ittigenstrase"})
        html = text(response)
        self.assertIn("Meinten Sie eine dieser Adressen?", html)
        self.assertIn("Ittigenstrasse 51, 3063 Ittigen", html)
        self.assertIn("EGID 90004269", html)
        self.assertIn('name="suggestion" value="0"', html)
        # The card is the one of the address script (inside the notice); its link has the shared text (G2).
        self.assertIn(">Nein, meine Adresse stimmt so<", html)
        self.assertIn('class="suggest address-proposal"', html)
        # The server's card is INSIDE the notice of the address widget (card_template), the one overlay of the script; there
        # is no second card in the page and no card class of the old layout.
        self.assertRegex(
            html, r'<div class="address-notice" id="address-notice" data-address-notice role="status">\s*<div class="suggest address-proposal"',
        )
        self.assertEqual(html.count("address-proposal-keep"), 1)
        self.assertEqual(html.count('class="suggest address-proposal"'), 1)
        self.assertNotIn("address-card-server", html)
        self.assertNotIn("suggest-option", html)
        self.assertFalse(self.application.buildings.exists())
        # The external search comes only after the person decided: nothing was looked up so far.
        lookup.assert_not_called()

    def test_choosing_a_suggestion_adds_that_building(self):
        suggestion = candidate(90004269, street="Ittigenstrasse")
        with directory(candidate(90004269), suggestions=[suggestion]):
            response = self.client.post(self.url, {**ADDRESS_FORM, "street": "Ittigenstrase", "suggestion": "0"})
        self.assertRedirects(response, self.url)
        self.assertEqual(self.application.buildings.get().street, "Ittigenstrasse")

    def test_a_forged_suggestion_number_is_no_building(self):
        with directory(suggestions=[candidate(90004269)]):
            response = self.client.post(self.url, {**ADDRESS_FORM, "suggestion": "7"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.application.buildings.exists())

    def test_my_entry_is_correct_goes_on_to_the_search_without_suggestions(self):
        with directory(candidate(90004269), suggestions=[candidate(90004269)]) as lookup:
            response = self.client.post(self.url, {**ADDRESS_FORM, "confirm_typed": "1"})
        self.assertRedirects(response, self.url)
        lookup.assert_called()

    def test_a_chosen_building_can_be_changed_but_a_preselected_one_cannot(self):
        with directory(candidate(90004269)):
            self.client.post(self.url, ADDRESS_FORM)
        self.client.post(self.url, {"action": "change"})
        self.assertFalse(self.application.buildings.exists())
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        point = make_metering_point(61, location=location)
        assign_person(point, self.mine, valid_from=date(2025, 1, 1))
        applications.cancel(self.mine, self.application)
        fixed = applications.start_draft(self.mine, location=location)
        page = text(self.client.get(step_url(fixed, "building")))
        self.assertNotIn("Anderes Gebäude wählen", page)
        self.client.post(step_url(fixed, "building"), {"action": "change"})
        self.assertTrue(fixed.buildings.exists())

    def test_an_incomplete_or_invalid_address_shows_the_error_summary(self):
        for values in ({}, {**ADDRESS_FORM, "postal_code": "30"}, {**ADDRESS_FORM, "street": ""}):
            with self.subTest(values=values):
                response = self.client.post(self.url, values)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Bitte prüfen Sie Ihre Angaben", text(response))
                self.assertIn('role="alert"', text(response))
        self.assertFalse(self.application.buildings.exists())

    def test_a_name_with_control_characters_is_refused(self):
        with directory(candidate(90004269)):
            response = self.client.post(self.url, {**ADDRESS_FORM, "name": "Bad\x07"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.application.buildings.exists())

    def test_the_building_search_has_its_own_budget_and_a_refusal_changes_nothing(self):
        with patch.object(limits, "allow_search", return_value=False), directory(candidate(90004269)) as lookup:
            response = self.client.post(self.url, ADDRESS_FORM)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], str(limits.RETRY_AFTER))
        lookup.assert_not_called()
        self.assertFalse(self.application.buildings.exists())

    def test_every_post_of_the_wizard_has_a_budget(self):
        with patch.object(limits, "allow_wizard", return_value=False):
            for name in ("building", "metering_points", "bank_connection", "contract", "start_date", "cancel", "withdraw"):
                with self.subTest(step=name):
                    response = self.client.post(reverse(f"customer:application_{name}", args=[self.application.pk]), {})
                    self.assertEqual(response.status_code, 429)
            self.assertEqual(self.client.post(START, {}).status_code, 429)
        self.assertTrue(Application.objects.filter(pk=self.application.pk).exists())

    def test_the_real_budget_counts_per_account(self):
        # 12 searches a minute: the 13th is refused although every one of them was refused by the service.
        statuses = []
        with directory():
            for _attempt in range(14):
                statuses.append(self.client.post(self.url, ADDRESS_FORM).status_code)
        self.assertEqual(statuses[:12], [200] * 12)
        self.assertEqual(statuses[12:], [429, 429])
        other = login(customer("other.budget@example.org"))
        applications.start_draft(type(self.mine).objects.get(email="other.budget@example.org"))
        self.assertEqual(other.get(START).status_code, 200)

    def test_nothing_of_the_address_is_logged(self):
        with self.assertNoLogs("leg_platform", level="INFO"), directory(candidate(90004269)):
            self.client.post(self.url, ADDRESS_FORM)
            self.client.get(self.url)
        self.client.post(self.url, {"action": "change"})
        with self.assertNoLogs("leg_platform", level="INFO"), directory():
            self.client.post(self.url, ADDRESS_FORM)


@tag("mariadb")
class MeteringPointStepTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application, self.building = draft_with_building(self.mine)
        self.url = step_url(self.application, "metering_points")

    def test_the_fields_the_fixed_beginning_and_the_hints_of_the_documents(self):
        html = text(self.client.get(self.url))
        for marker in (
            "Schritt 2: Messpunkte", "Welche Messpunkte gehören zu Ihrem Gebäude?",
            "Der Messpunkt ist der Anschluss Ihres Stromzählers.", f"Seine Nummer beginnt mit CH{IDENTIFIER}.",
            "Messpunkt-Nummer für den Bezug", f">CH{IDENTIFIER}<",
            "Die Nummer finden Sie auf Ihrer Stromrechnung, bei den Angaben zu Ihrer Bezugsstelle.",
            "Name des Messpunkts", "z. B. Wohnhaus", "Speisen Sie Solarstrom ein?", ">Nein<", ">Ja<",
            "Messpunkt-Nummer für die Einspeisung",
            "Die Nummer finden Sie in den Unterlagen Ihres Netzbetreibers, bei den Angaben zu Ihrer Einspeisestelle.",
            "Diese Angaben sind freiwillig und dienen nur der Statistik.", ">Einspeisung<",
            "PV-Leistung in kWp", "Batteriespeicher in kWh", "Wallbox in kW",
            'data-reveal-name="feed_in"',
        ):
            self.assertIn(marker, html)
        # No total in step 2: a feed-in adds a step (W1-01).
        self.assertNotIn("Schritt 2 von", html)
        # The answer "No" is chosen first; the part of the feed-in is hidden by the script, never by the server.
        self.assertRegex(html, r'value="no" checked|checked[^>]*value="no"')
        self.assertNotIn("is-open", html)

    def test_both_number_hints_share_one_structure_in_every_language(self):
        """Where to find it, then what it is called and what it is not (the same words for draw and feed-in)."""
        from django.utils import translation

        from leg_platform.customer.forms import MeteringPointsForm

        tail = {"de-ch": "Sie heisst auch Messpunkt-ID und ist nicht die kürzere Zählernummer.",
                "fr": "Il s’appelle aussi ID du point de mesure", "it": "Si chiama anche ID del punto di misura"}
        for language, ending in tail.items():
            with translation.override(language):
                form = MeteringPointsForm()
                draw, feed_in = str(form.fields["draw_number"].help_text), str(form.fields["feed_in_number"].help_text)
            self.assertIn(ending, draw)
            self.assertIn(ending, feed_in)
            self.assertNotEqual(draw, feed_in)
            self.assertNotIn("ß", draw + feed_in)

    def test_a_draw_only(self):
        response = self.client.post(self.url, {"draw_number": "12345", "draw_name": "Wohnhaus", "feed_in": "no"})
        self.assertRedirects(response, step_url(self.application, "contract"), fetch_redirect_response=False)
        row = self.building.metering_points.get()
        self.assertEqual((row.direction, row.number, row.name), (Direction.CONSUMPTION, full_number(12345), "Wohnhaus"))
        # Without a feed-in the bank connection is no step: the contract is next ("Step 3 of 4").
        self.assertIn("Schritt 3 von 4: Vertrag", text(self.client.get(step_url(self.application, "contract"))))

    def test_a_feed_in_with_figures_leads_to_the_bank_connection(self):
        response = self.client.post(self.url, {
            "draw_number": "1", "feed_in": "yes", "feed_in_number": "2", "feed_in_name": "PV-Anlage",
            "pv_power_kwp": "9,5", "battery_kwh": "10", "wallbox_kw": "",
        })
        self.assertRedirects(response, step_url(self.application, "bank_connection"), fetch_redirect_response=False)
        feed_in = self.building.metering_points.get(direction=Direction.FEED_IN)
        self.assertEqual((str(feed_in.pv_power_kwp), str(feed_in.battery_kwh), feed_in.wallbox_kw), ("9.50", "10.00", None))
        # Back on the step the values are there again; answering "No" removes the feed-in.
        page = text(self.client.get(self.url))
        self.assertIn('value="9.50"', page)
        self.client.post(self.url, {"draw_number": "1", "feed_in": "no"})
        self.assertFalse(self.building.metering_points.filter(direction=Direction.FEED_IN).exists())

    def test_a_number_that_is_too_long_gets_the_owners_text(self):
        response = self.client.post(self.url, {"draw_number": "1" * 21, "feed_in": "no"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Die Messpunkt-Nummer ist zu lang. Bitte prüfen Sie sie auf Ihrer Stromrechnung.", text(response))
        self.assertIn('aria-invalid="true"', text(response))
        self.assertFalse(self.building.metering_points.exists())

    def test_zeros_on_the_left_are_added_and_removed_by_the_rule(self):
        self.client.post(self.url, {"draw_number": "0" * 25 + "7", "feed_in": "no"})
        self.assertEqual(self.building.metering_points.get().number, full_number(7))

    def test_an_empty_or_forbidden_entry_is_a_message_at_the_field(self):
        for value, message in (("", "Geben Sie die letzten Stellen der Nummer ein."), ("12 3!", "Erlaubt sind nur die Buchstaben")):
            with self.subTest(value=value):
                response = self.client.post(self.url, {"draw_number": value, "feed_in": "no"})
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, text(response))

    def test_the_feed_in_needs_its_number_when_the_answer_is_yes(self):
        response = self.client.post(self.url, {"draw_number": "1", "feed_in": "yes", "feed_in_number": ""})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Geben Sie die letzten Stellen der Nummer ein.", text(response))

    def test_a_number_of_another_account_gets_the_neutral_refusal_without_any_detail(self):
        location = make_location("Fremdweg", "1", "3000", "Bern")
        foreign = make_metering_point(77, location=location)
        assign_person(foreign, customer("owner.wizard@example.org"), valid_from=date(2025, 1, 1))
        response = self.client.post(self.url, {"draw_number": "77", "feed_in": "no"})
        html = text(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Diese Messpunkt-Nummer kann nicht angelegt werden. Bitte melden Sie sich bei der LEG.", html)
        for leak in ("owner.wizard", "Fremdweg", "Kunde", "gehört"):
            self.assertNotIn(leak, html)
        self.assertFalse(self.building.metering_points.exists())

    def test_an_own_number_twice_is_named(self):
        self.client.post(self.url, {"draw_number": "5", "feed_in": "yes", "feed_in_number": "5"})
        self.assertIn("Dieser Messpunkt ist bei diesem Gebäude schon angelegt.", text(self.client.get(self.url)) + text(
            self.client.post(self.url, {"draw_number": "5", "feed_in": "yes", "feed_in_number": "5"})
        ))

    def test_an_unusually_large_figure_is_a_warning_and_still_saved(self):
        response = self.client.post(self.url, {
            "draw_number": "1", "feed_in": "yes", "feed_in_number": "2", "pv_power_kwp": "5000",
        }, follow=True)
        self.assertIn("ist ungewöhnlich gross", text(response))
        self.assertEqual(str(self.building.metering_points.get(direction=Direction.FEED_IN).pv_power_kwp), "5000.00")

    def test_a_figure_that_is_no_number_is_refused(self):
        for value in ("abc", "-3"):
            with self.subTest(value=value):
                response = self.client.post(self.url, {"draw_number": "1", "feed_in": "yes", "feed_in_number": "2", "pv_power_kwp": value})
                self.assertEqual(response.status_code, 200)
                self.assertIn("Geben Sie eine Zahl", text(response))

    def test_without_the_identifier_of_the_settings_nothing_can_be_entered(self):
        ManagementSettings.objects.filter(id=1).update(identifier="")
        page = text(self.client.get(self.url))
        self.assertIn("Messpunkt-Nummern können noch nicht eingegeben werden.", page)
        response = self.client.post(self.url, {"draw_number": "1", "feed_in": "no"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.building.metering_points.exists())

    def test_where_the_account_has_a_draw_already_only_the_feed_in_is_asked(self):
        location = make_location("Bestehend", "4", "3063", "Ittigen", egid=90007777)
        draw = make_metering_point(90, Direction.CONSUMPTION, location, registered_with_operator_on=date(2025, 1, 1), confirmed_on=date(2025, 1, 1), start_date=date(2025, 2, 1))
        assign_person(draw, self.mine, valid_from=date(2025, 2, 1))
        applications.cancel(self.mine, self.application)
        application = applications.start_draft(self.mine, location=location)
        url = step_url(application, "metering_points")
        html = text(self.client.get(url))
        self.assertNotIn("Messpunkt-Nummer für den Bezug", html)
        self.assertNotIn("Speisen Sie Solarstrom ein?", html)
        self.assertIn("Messpunkt-Nummer für die Einspeisung", html)
        response = self.client.post(url, {"feed_in_number": "91", "pv_power_kwp": "7"})
        self.assertRedirects(response, step_url(application, "bank_connection"), fetch_redirect_response=False)
        self.assertEqual(application.buildings.get().metering_points.get().direction, Direction.FEED_IN)


@tag("mariadb")
class BankStepTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application, self.building = draft_with_points(self.mine, feed_in=True)
        self.url = step_url(self.application, "bank_connection")

    def test_the_page_with_the_hint_and_the_empty_field(self):
        html = text(self.client.get(self.url))
        for marker in (
            "Schritt 3 von 5: Bank- oder Postverbindung", "Wohin sollen wir Ihre Vergütung überweisen?",
            "Damit bezahlen wir Ihnen die Vergütung für den eingespeisten Strom.",
            "IBAN", "CH93 0076 2011 6238 5295 7", "Die IBAN steht auf Ihrem Konto- oder Zahlungsauszug und auf der Rückseite Ihrer Bankkarte.",
            'autocomplete="off"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn('value="CH', html)

    def test_a_valid_iban_with_spaces_is_stored_and_the_contract_follows(self):
        response = self.client.post(self.url, {"iban": "CH93 0076 2011 6238 5295 7"})
        self.assertRedirects(response, step_url(self.application, "contract"), fetch_redirect_response=False)
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)
        # The first entry from empty sends no mail (owner decision).
        self.assertFalse(MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED).exists())

    def test_an_invalid_iban_gets_the_owners_text_and_nothing_is_stored(self):
        for value in ("CH93 0076 2011 6238 5295 8", "hello", ""):
            with self.subTest(value=value):
                response = self.client.post(self.url, {"iban": value})
                self.assertEqual(response.status_code, 200)
        response = self.client.post(self.url, {"iban": "CH93 0076 2011 6238 5295 8"})
        self.assertIn("Die IBAN ist nicht gültig. Bitte prüfen Sie die Eingabe.", text(response))
        self.assertEqual(own_iban.own_iban(self.mine), "")

    def test_the_iban_is_shown_to_its_owner_only_and_never_logged(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.assertIn("CH93 0076 2011 6238 5295 7", text(self.client.get(self.url)))
        other = customer("other.bank@example.org")
        other_client = login(other)
        other_application, _building = draft_with_points(other, feed_in=True, egid=90009999, number=40)
        self.assertNotIn('value="CH93', text(other_client.get(step_url(other_application, "bank_connection"))))
        with self.assertNoLogs("leg_platform", level="INFO"):
            self.client.post(self.url, {"iban": "DE89 3704 0044 0532 0130 00"})

    def test_too_many_changes_are_refused_with_a_message(self):
        with patch.object(own_iban, "MAX_CHANGES_PER_HOUR", 0):
            response = self.client.post(self.url, {"iban": GOOD_IBAN})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sie haben Ihre Bankverbindung zu oft geändert.", text(response))

    def test_without_a_feed_in_the_step_is_not_there(self):
        self.building.metering_points.filter(direction=Direction.FEED_IN).delete()
        self.assertRedirects(
            self.client.get(self.url), reverse("customer:application", args=[self.application.pk]), fetch_redirect_response=False
        )
        self.client.post(self.url, {"iban": GOOD_IBAN})
        self.assertEqual(own_iban.own_iban(self.mine), "")


@tag("mariadb")
class ContractStepTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application, self.building = draft_with_points(self.mine)
        self.url = step_url(self.application, "contract")

    def test_the_page_with_the_version_the_download_and_two_empty_boxes(self):
        html = text(self.client.get(self.url))
        for marker in (
            "Schritt 3 von 4: Vertrag", "Vertrag lesen und zustimmen", "Der Vertrag gilt für alle Ihre Gebäude. Sie stimmen ihm einmal zu.",
            "Vertragsfassung vom 7. Oktober 2026", "Vertrag herunterladen", reverse("customer:contract_download"),
            '<iframe class="contract-frame" src="/participation/contract/read/#view=FitH&amp;navpanes=0"',
            "Wenn der Vertrag hier nicht angezeigt wird, laden Sie ihn bitte herunter.",
            "Den Vertrag finden Sie auch unter", 'href="/participation/contract/"', "Information › Gesellschaftsvertrag",
            "Ich habe die aktuelle Fassung vom 07.10.2026 gelesen und stimme ihr zu.",
            'data-gate-all="id_agrees_contract"',
        ):
            self.assertIn(marker, html)
        self.assertEqual(html.count('type="checkbox"'), 1)
        self.assertNotIn("checked", html.split("<main")[1])

    def post(self, **values):
        return self.client.post(self.url, {"version": self.version.pk, **values})

    def test_the_tick_saves_the_acceptance_and_leads_to_the_start_date(self):
        response = self.post(agrees_contract="on")
        self.assertRedirects(response, step_url(self.application, "start_date"), fetch_redirect_response=False)
        acceptance = ContractAcceptance.objects.get(person=self.mine)
        self.assertEqual((acceptance.version_id, acceptance.sha256, acceptance.data_exchange_accepted),
                         (self.version.pk, self.version.sha256, True))

    def test_without_the_tick_nothing_is_stored_and_the_page_says_so(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bitte setzen Sie das Häkchen, um zuzustimmen.", text(response))
        self.assertFalse(ContractAcceptance.objects.exists())

    def test_a_version_that_was_replaced_meanwhile_shows_the_new_one_with_empty_boxes(self):
        staff = staff_user("contract.staff.wizard@example.org")
        newer = contracts.upload_version(staff, pdf(b"new"), date(2026, 11, 3))
        contracts.make_current(staff, newer)
        response = self.post(agrees_contract="on")
        html = text(response)
        self.assertIn("Der Vertrag wurde inzwischen ersetzt. Bitte lesen Sie die neue Fassung.", html)
        self.assertIn("Vertragsfassung vom 3. November 2026", html)
        self.assertFalse(ContractAcceptance.objects.exists())

    def test_without_a_published_contract_the_page_says_so(self):
        from leg_platform.community.models import ContractVersion

        ContractVersion.objects.update(current_marker=None)
        self.assertIn("Der Vertrag ist noch nicht verfügbar.", text(self.client.get(self.url)))
        self.assertEqual(self.client.post(self.url, {"version": 1, "agrees_contract": "on"}).status_code, 200)
        self.assertFalse(ContractAcceptance.objects.exists())

    def test_an_accepted_contract_is_green_and_skipped(self):
        contracts.accept_contract(self.mine, self.version.pk, agrees_contract=True)
        html = text(self.client.get(self.url))
        self.assertIn("Bereits zugestimmt", html)
        self.assertEqual(html.count('type="checkbox"'), 0)
        # A new application of the same account: the wizard goes past the contract (it was accepted before).
        Application.objects.filter(pk=self.application.pk).delete()
        fresh, _fresh_building = draft_with_points(self.mine, egid=90008888, number=20)
        self.assertRedirects(
            self.client.get(reverse("customer:application", args=[fresh.pk])), step_url(fresh, "start_date"),
            fetch_redirect_response=False,
        )


@tag("mariadb")
class StartDateStepTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application, self.building = draft_with_points(self.mine)
        applications.accept_contract_step(
            self.mine, self.application, self.version.pk, agrees_contract=True
        )
        self.url = step_url(self.application, "start_date")

    def test_the_page_has_as_soon_as_possible_chosen_and_an_empty_date(self):
        html = text(self.client.get(self.url))
        for marker in (
            "Schritt 4 von 4: Startdatum", "Ab wann soll Ihre Teilnahme beginnen?",
            "Wählen Sie den frühesten Termin oder ein Wunschdatum.", "Wann soll es losgehen?", "So bald wie möglich",
            "Wunschdatum", 'type="date"', "Anmeldung abschicken", "Den endgültigen Termin bestätigt Netzbetreiber.",
            'data-reveal-name="start"', "Frühestens ab dem",
        ):
            self.assertIn(marker, html)
        self.assertRegex(html, r'value="asap" checked|checked[^>]*value="asap"')
        self.assertNotRegex(html, r'type="date"[^>]*value="\d')
        self.assertRegex(html, r'type="date"[^>]*min="\d{4}-\d\d-\d\d"|min="\d{4}-\d\d-\d\d"[^>]*type="date"')

    def test_as_soon_as_possible_submits_and_shows_the_page_once(self):
        response = self.client.post(self.url, {"start": "asap"})
        self.assertRedirects(response, reverse("customer:application_submitted", args=[self.application.pk]), fetch_redirect_response=False)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.SUBMITTED)
        self.assertEqual(self.application.start_choice, "asap")
        page = self.client.get(reverse("customer:application_submitted", args=[self.application.pk]))
        self.assertEqual(page.status_code, 200)
        again = self.client.get(reverse("customer:application_submitted", args=[self.application.pk]))
        self.assertRedirects(again, BUILDINGS, fetch_redirect_response=False)

    def test_a_desired_date_is_stored(self):
        wish = applications.earliest_start_date() + timedelta(days=40)
        self.client.post(self.url, {"start": "date", "desired_date": wish.isoformat()})
        self.application.refresh_from_db()
        self.assertEqual((self.application.start_choice, self.application.desired_start_date), ("date", wish))

    def test_a_date_that_is_too_early_or_missing_is_refused_with_a_message(self):
        early = (date.today() + timedelta(days=30)).isoformat()
        response = self.client.post(self.url, {"start": "date", "desired_date": early})
        self.assertEqual(response.status_code, 200)
        self.assertRegex(text(response), r"Wählen Sie einen Tag ab dem \d\d\.\d\d\.\d{4}\.")
        missing = self.client.post(self.url, {"start": "date", "desired_date": ""})
        self.assertIn("Wählen Sie ein Datum.", text(missing))
        garbage = self.client.post(self.url, {"start": "date", "desired_date": "not a date"})
        self.assertEqual(garbage.status_code, 200)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.DRAFT)

    def test_a_submit_with_missing_details_leads_to_the_missing_step(self):
        error = ApplicationError("incomplete", missing=["bank_connection"])
        with patch.object(applications, "submit", side_effect=error):
            response = self.client.post(self.url, {"start": "asap"})
        self.assertRedirects(response, step_url(self.application, "bank_connection"), fetch_redirect_response=False)

    def test_a_number_that_belongs_to_someone_else_meanwhile_is_a_neutral_message(self):
        with patch.object(applications, "submit", side_effect=ApplicationError("number_unavailable")):
            response = self.client.post(self.url, {"start": "asap"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Diese Messpunkt-Nummer kann nicht angelegt werden.", text(response))

    def test_the_confirmation_mail_goes_out_once_and_a_second_post_changes_nothing(self):
        self.client.post(self.url, {"start": "asap"})
        jobs = MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count()
        self.client.post(self.url, {"start": "asap"})
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED).count(), jobs)


@tag("mariadb")
class SubmittedAndWithdrawTests(WizardCase):
    def setUp(self):
        super().setUp()
        self.application, self.building = draft_with_points(self.mine, feed_in=True)
        applications.save_bank_connection(self.mine, self.application, GOOD_IBAN)
        applications.accept_contract_step(
            self.mine, self.application, self.version.pk, agrees_contract=True
        )
        self.client.post(step_url(self.application, "start_date"), {"start": "asap"})
        self.submitted = reverse("customer:application_submitted", args=[self.application.pk])

    def test_the_page_shows_the_five_steps_the_details_and_the_two_buttons_once(self):
        html = text(self.client.get(self.submitted))
        for marker in (
            "Ihre Anmeldung ist abgeschickt",
            "Die LEG prüft Ihre Angaben und meldet Ihr Gebäude bei Netzbetreiber an. Danach erhalten Sie eine E-Mail vom Netzbetreiber. Bitte bestätigen Sie die Anmeldung dort.",
            "So geht es weiter", "Angaben abgeschickt", "Prüfung durch die LEG", "Anmeldung beim Netzbetreiber",
            "Sie bestätigen die Anfrage in der E-Mail vom Netzbetreiber", "Wird vom Netzbetreiber festgelegt",
            "Bitte bestätigen Sie die Anmeldung innerhalb von 14 Tagen nach der E-Mail vom Netzbetreiber.",
            "Ihre Angaben", "Ittigenstrasse 51", "EGID 90004269",
            "Bezug", "Einspeisung", "Startdatum", "So bald wie möglich",
            "Sie können Ihre Angaben noch ändern, bis die LEG mit der Prüfung beginnt.",
            ">Zur Startseite<", ">Anmeldung ändern<", ">Anmeldung zurückziehen<",
            "Zurückziehen ist möglich, solange die LEG die Anmeldung noch nicht beim Netzbetreiber eingereicht hat.",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("Änderungen sind jetzt nur noch über die LEG möglich.", html)
        # N-09: every point with its number (the customer can check what was typed), the operator named once, no bar.
        for point in self.building.metering_points.all():
            self.assertIn(numbers.format_metering_point_number(point.number), html)
        self.assertNotIn("<progress", html)
        # One list, no second count (W1-14): the bar only; the old words are gone.
        self.assertNotIn("step-note", html)
        for gone in ("Kundenanfrage durch", "Warten auf Startdatum", "Bestätigung durch Netzbetreiber offen"):
            self.assertNotIn(gone, html)
        self.assertIn(f'href="{HOME}"', html)
        # No button for another building: a further one is made with "Start registration" on the page Buildings.
        self.assertNotIn("weiteres Gebäude", html)

    def test_the_days_come_from_the_settings(self):
        ManagementSettings.objects.filter(id=1).update(operator_confirmation_days=9)
        self.assertIn("innerhalb von 9 Tagen", text(self.client.get(self.submitted)))

    def test_a_page_without_the_flag_of_the_session_is_no_page(self):
        other_session = login(self.mine)
        self.assertRedirects(other_session.get(self.submitted), BUILDINGS, fetch_redirect_response=False)

    def test_changing_a_submitted_application_asks_first_and_makes_a_draft_again_with_all_its_details(self):
        url = reverse("customer:application_change", args=[self.application.pk])
        page = text(self.client.get(url))
        self.assertIn("Anmeldung ändern?", page)
        self.assertIn("Die LEG prüft sie erst wieder, wenn Sie sie neu abschicken.", page)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.SUBMITTED)
        response = self.client.post(url)
        self.assertRedirects(response, START, fetch_redirect_response=False)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.DRAFT)
        self.assertIsNone(self.application.submitted_at)
        self.assertEqual(self.building.metering_points.count(), 2)
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)
        entry = history.entries_for(self.application).filter(event=HistoryEvent.APPLICATION_WITHDRAWN).get()
        self.assertEqual(entry.detail, {"purpose": "change"})
        # The details can be changed on the steps (a "Change" link on every finished row) and submitted again.
        start = text(self.client.get(START))
        self.assertIn(step_url(self.application, "metering_points"), start)
        self.client.post(step_url(self.application, "start_date"), {"start": "asap"})
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.SUBMITTED)

    def test_the_change_is_locked_once_the_leg_took_the_application_into_review(self):
        staff = staff_user("staff.change@example.org")
        application_staff.start_review(self.application, staff)
        url = reverse("customer:application_change", args=[self.application.pk])
        text_in_review = "Die LEG prüft Ihre Anmeldung bereits"
        response = self.client.get(url, follow=True)
        self.assertRedirects(response, BUILDINGS)
        self.assertContains(response, text_in_review)
        response = self.client.post(url, follow=True)
        self.assertContains(response, text_in_review)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.IN_REVIEW)
        # The card says why there is no button; the withdrawal stays as documented (until the grid operator started).
        page = text(self.client.get(BUILDINGS))
        self.assertIn("Die LEG prüft Ihre Anmeldung. Sie können sie hier nicht mehr ändern.", page)
        self.assertNotIn(url, page)

    def test_the_change_is_refused_for_a_draft_a_foreign_application_and_after_the_registration_started(self):
        url = reverse("customer:application_change", args=[self.application.pk])
        staff = staff_user("staff.change2@example.org")
        application_staff.mark_operator_started(self.application, staff)
        self.assertRedirects(self.client.get(url), BUILDINGS, fetch_redirect_response=False)
        self.client.post(url)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.OPERATOR_STARTED)
        stranger = login(customer("stranger.change@example.org"))
        self.assertEqual(stranger.get(url).status_code, 404)
        self.assertEqual(stranger.post(url).status_code, 404)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.OPERATOR_STARTED)

    def test_a_change_races_the_review_and_exactly_one_wins(self):
        """The staff took the application into review first: the customer's click is refused; the other way round the
        staff's click is refused (the state is read again under the row lock)."""
        staff = staff_user("staff.race@example.org")
        stale = Application.objects.get(pk=self.application.pk)  # the customer's page was built while submitted
        application_staff.start_review(self.application, staff)
        with self.assertRaises(ApplicationError) as refused:
            applications.reopen_for_change(self.mine, stale)
        self.assertEqual(refused.exception.code, "review_started")
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.IN_REVIEW)
        other, _building = draft_with_points(self.mine, number=60, egid=90004271, house_number="61")
        applications.set_start(self.mine, other, asap=True)
        applications.submit(self.mine, other)
        stale_staff = Application.objects.get(pk=other.pk)
        applications.reopen_for_change(self.mine, other)
        with self.assertRaises(ApplicationError) as refused:
            application_staff.start_review(stale_staff, staff)
        self.assertEqual(refused.exception.code, "wrong_state")
        self.assertEqual(Application.objects.get(pk=other.pk).state, Application.State.DRAFT)

    def test_the_change_needs_a_free_place_among_the_drafts_like_the_withdrawal(self):
        for _count in range(3):
            applications.start_draft(self.mine)
        url = reverse("customer:application_change", args=[self.application.pk])
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, BUILDINGS)
        self.assertContains(response, "Sie haben zu viele Entwürfe.")
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.SUBMITTED)

    def test_the_change_has_no_cap_and_the_resubmission_sends_no_second_mail(self):
        """Owner decision 2026-10-22: no cap of change cycles; the mail only at the first submission (D-107). The queue
        shows the application once, with the newest submission time."""
        url = reverse("customer:application_change", args=[self.application.pk])
        mails = MailJob.objects.filter(kind=MailJob.Kind.APPLICATION_SUBMITTED)
        self.assertEqual(mails.count(), 1)  # the first submission (setUp)
        for _cycle in range(12):
            self.assertEqual(self.client.post(url).status_code, 302)
            self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.DRAFT)
            applications.submit(self.mine, self.application)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.SUBMITTED)
        self.assertEqual(mails.count(), 1)
        self.assertEqual(Application.objects.filter(state=Application.State.SUBMITTED).count(), 1)
        # Through the wizard: the normal confirmation page (it promises no mail from the portal), no second mail.
        self.client.post(url)
        response = self.client.post(step_url(self.application, "start_date"), {"start": "asap"})
        self.assertRedirects(response, self.submitted, fetch_redirect_response=False)
        html = text(self.client.get(self.submitted))
        self.assertIn("Ihre Anmeldung ist abgeschickt", html)
        self.assertNotIn("bestätigung per E-Mail", html.lower())
        self.assertEqual(mails.count(), 1)

    def test_withdrawing_asks_first_and_makes_a_draft_again_with_all_its_details(self):
        url = reverse("customer:application_withdraw", args=[self.application.pk])
        page = text(self.client.get(url))
        self.assertIn("Anmeldung zurückziehen?", page)
        self.assertIn("Ihre Anmeldung wird wieder ein Entwurf.", page)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.SUBMITTED)
        response = self.client.post(url)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.DRAFT)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.building.metering_points.count(), 2)
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)

    def test_withdrawing_is_refused_with_a_message_while_the_account_has_too_many_drafts(self):
        for _count in range(3):
            applications.start_draft(self.mine)
        url = reverse("customer:application_withdraw", args=[self.application.pk])

        response = self.client.post(url, follow=True)

        self.assertRedirects(response, BUILDINGS)
        self.assertContains(response, "Sie haben zu viele Entwürfe. Bitte schliessen Sie einen ab oder brechen Sie einen ab.")
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.SUBMITTED)

    def test_once_the_registration_at_the_grid_operator_started_nothing_can_be_withdrawn(self):
        staff = staff_user("staff.withdraw@example.org")
        application_staff.mark_operator_started(self.application, staff)
        url = reverse("customer:application_withdraw", args=[self.application.pk])
        self.assertRedirects(self.client.get(url), BUILDINGS, fetch_redirect_response=False)
        self.client.post(url)
        self.assertEqual(Application.objects.get(pk=self.application.pk).state, Application.State.OPERATOR_STARTED)


@tag("mariadb")
class CancelTests(WizardCase):
    def test_every_step_opens_the_confirmation_on_the_same_page(self):
        application, _building = draft_with_points(self.mine)
        for name in ("contract",):
            html = text(self.client.get(step_url(application, name) + "?cancel=1"))
            self.assertIn("Anmeldung abbrechen?", html)
            self.assertIn("Ihre Angaben zu Gebäude und Messpunkten werden gelöscht.", html)
            # The bank connection is named only where there is one (W2-05); "Cancel" is not offered twice.
            self.assertNotIn("Bank- oder Postverbindung bleibt erhalten", html)
            self.assertNotIn(">Abbrechen<", html)
            self.assertIn(">Weiter anmelden<", html)
            self.assertIn(">Anmeldung abbrechen<", html)
            self.assertRegex(html, r'<a class="btn btn-primary" href="[^"]+" autofocus>Weiter anmelden</a>')
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.assertIn(
            "Ihre Bank- oder Postverbindung bleibt erhalten, weil sie zu Ihrer Person gehört.",
            text(self.client.get(step_url(application, "contract") + "?cancel=1")),
        )
        plain = text(self.client.get(step_url(application, "contract")))
        self.assertNotIn("Anmeldung abbrechen?", plain)
        self.assertIn(">Abbrechen<", plain)
        # "Cancel" is a link to that state, no direct abort.
        self.assertIn("?cancel=1", plain)
        self.assertEqual(Application.objects.filter(person=self.mine).count(), 1)

    def test_a_get_of_the_cancel_address_only_opens_the_confirmation(self):
        application, _building = draft_with_points(self.mine)
        response = self.client.get(reverse("customer:application_cancel", args=[application.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("?cancel=1"))
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())

    def test_cancelling_is_a_rollback_of_the_draft_only_and_sends_no_mail(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        stored = make_location("Bleibt", "1", "3000", "Bern", egid=90002222)
        point = make_metering_point(88, location=stored)
        application, _building = draft_with_points(self.mine, feed_in=True)
        jobs = MailJob.objects.count()
        response = self.client.post(reverse("customer:application_cancel", args=[application.pk]), follow=True)
        self.assertRedirects(response, HOME)
        self.assertIn("Ihre Anmeldung wurde abgebrochen.", text(response))
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertFalse(ApplicationBuilding.objects.filter(application_id=application.pk).exists())
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)
        self.assertTrue(Location.objects.filter(pk=stored.pk).exists())
        self.assertTrue(MeteringPoint.objects.filter(pk=point.pk).exists())
        self.assertEqual(MailJob.objects.count(), jobs)
        # The start page offers the tile "Register building" again.
        self.assertIn("Gebäude anmelden", text(self.client.get(HOME)))

    def test_a_submitted_application_is_not_cancelled_this_way(self):
        application, _building = draft_with_points(self.mine)
        applications.accept_contract_step(self.mine, application, self.version.pk, agrees_contract=True)
        applications.set_start(self.mine, application, asap=True)
        applications.submit(self.mine, application)
        self.client.post(reverse("customer:application_cancel", args=[application.pk]))
        self.assertTrue(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(Application.objects.get(pk=application.pk).state, Application.State.SUBMITTED)
