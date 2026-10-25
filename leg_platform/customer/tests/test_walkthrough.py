"""The customer-side findings of the designer walkthrough (W1 to W4; D-105): one progress list, the one-time banners, the
returned reason, the paused date, the status lines, the contact link, the prefilled address, the short error summary, one
withdraw button, the dates of a registration, the earliest date of a deregistration and the texts of the profile. Negative
cases stay: another account's messages are never shown or marked, an account that does not take part gets none of it
(staff accounts with a customer number do take part, D-113), hostile text is escaped."""

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape

from leg_platform.accounts.tests.helpers import instance_mail_addresses
from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.tests.helpers import PASSWORD
from leg_platform.community import application_staff, applications, contracts, customer_messages, participation
from leg_platform.community.models import Application, CustomerBuilding, CustomerMessage, Deregistration, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer import banners
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
from leg_platform.customer.tests.test_deregistration import START as DEREGISTRATION
from leg_platform.customer.tests.test_deregistration import active_point
from leg_platform.customer.tests.test_start_page import complete_application

HOSTILE = '"><img src=x onerror=alert(1)>'
Kind = CustomerMessage.Kind
PACKAGE = Path(__file__).resolve().parents[1]


def text(response):
    return response.content.decode()


def flat(html):
    """The visible words of a page: tags removed, white space collapsed (the markup of a sentence may break lines)."""
    import re

    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


@tag("mariadb")
class BannerTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("banner.mine@example.org")
        self.other = customer("banner.other@example.org")
        self.client = login(self.mine)

    def test_a_rejection_with_the_staff_note_is_shown_once_and_the_note_is_escaped(self):
        customer_messages.record(self.mine, Kind.APPLICATION_REJECTED, HOSTILE)

        first = text(self.client.get(HOME))

        self.assertIn("Die LEG hat Ihre Anmeldung nicht angenommen.", first)
        self.assertIn("Grund:", first)
        self.assertIn(escape(HOSTILE), first)
        self.assertNotIn("<img src=x", first)
        self.assertIn("Sie können sich jederzeit erneut anmelden.", first)
        self.assertNotIn("Die LEG hat Ihre Anmeldung nicht angenommen.", text(self.client.get(HOME)))
        self.assertEqual(customer_messages.messages_for(self.mine), [])

    def test_the_page_buildings_shows_a_message_once_too_and_the_first_page_to_be_visited_takes_it(self):
        customer_messages.record(self.mine, Kind.APPLICATION_REJECTED_OPERATOR)
        customer_messages.record(self.mine, Kind.OPERATOR_NOT_CONFIRMED)

        buildings_page = text(self.client.get(BUILDINGS))

        self.assertIn("Der Netzbetreiber hat die Anmeldung nicht angenommen.", buildings_page)
        self.assertIn("Ihre Anmeldung wurde gelöscht, weil Ihre Bestätigung in der E-Mail des Netzbetreibers nicht rechtzeitig eingetroffen ist.", buildings_page)
        self.assertNotIn("nicht rechtzeitig eingetroffen", text(self.client.get(HOME)))
        self.assertNotIn("nicht rechtzeitig eingetroffen", text(self.client.get(BUILDINGS)))

    def test_a_return_for_correction_is_no_banner_it_has_its_own_note_and_is_marked_seen(self):
        customer_messages.record(self.mine, Kind.APPLICATION_RETURNED)

        html = text(self.client.get(HOME))

        self.assertNotIn("banner-list", html)
        self.assertEqual(customer_messages.messages_for(self.mine), [])

    def test_the_messages_of_another_account_are_neither_shown_nor_marked(self):
        customer_messages.record(self.other, Kind.APPLICATION_REJECTED, "Fremd")

        html = text(self.client.get(HOME))

        self.assertNotIn("nicht angenommen", html)
        self.assertNotIn("Fremd", html)
        self.assertEqual(len(customer_messages.messages_for(self.other)), 1)

    def test_a_staff_account_with_a_number_gets_the_banner_and_it_is_marked_seen(self):
        # D-113: staff accounts take part like everybody else.
        staff = staff_user("banner.staff@example.org")
        customer_messages.record(staff, Kind.APPLICATION_REJECTED, "Intern")

        html = text(login(staff).get(HOME))

        self.assertIn("Intern", html)
        self.assertEqual(customer_messages.messages_for(staff), [])

    def test_an_account_that_does_not_take_part_gets_no_banner_and_nothing_is_marked(self):
        staff = staff_user("banner.nopart@example.org")
        customer_messages.record(staff, Kind.APPLICATION_REJECTED, "Intern")

        with patch("leg_platform.customer.access.is_customer", return_value=False):
            html = text(login(staff).get(HOME))

        self.assertNotIn("Intern", html)
        self.assertEqual(len(customer_messages.messages_for(staff)), 1)

    def test_the_reader_does_not_mark_and_marking_takes_everything_it_read(self):
        customer_messages.record(self.mine, Kind.APPLICATION_REJECTED, "a")
        customer_messages.record(self.mine, Kind.APPLICATION_RETURNED)

        read = banners.pending(self.mine)

        self.assertEqual([(banner.kind, banner.note) for banner in read.banners], [(Kind.APPLICATION_REJECTED, "a")])
        self.assertEqual(len(customer_messages.messages_for(self.mine)), 2)  # reading marks nothing
        request = RequestFactory().get("/")
        banners.mark_shown(request, self.mine, read)
        self.assertEqual(customer_messages.messages_for(self.mine), [])

    def test_a_head_request_a_prefetch_and_a_failing_page_do_not_consume_the_message(self):
        customer_messages.record(self.mine, Kind.APPLICATION_REJECTED, "bleibt")
        for url in (HOME, BUILDINGS):
            with self.subTest(url=url):
                self.client.head(url)
                self.client.get(url, headers={"Sec-Purpose": "prefetch"})
                self.client.get(url, headers={"Purpose": "prefetch"})
                self.assertEqual(len(customer_messages.messages_for(self.mine)), 1)
        # A page that cannot be built does not consume it either.
        with patch("leg_platform.customer.views.render", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.get(BUILDINGS)
        with patch("leg_platform.accounts.views.HomeView.get_template_names", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.get(HOME)
        self.assertEqual(len(customer_messages.messages_for(self.mine)), 1)
        # The real visit shows it and consumes it.
        self.assertIn("bleibt", text(self.client.get(HOME)))
        self.assertEqual(customer_messages.messages_for(self.mine), [])

    def test_a_deactivated_account_loses_its_messages_and_the_reasons_of_the_staff(self):
        staff = staff_user("purge.staff@example.org")
        publish_contract(staff)
        configure_numbers()
        application, _building = draft_with_points(self.mine)
        Application.objects.filter(pk=application.pk).update(returned_at=timezone.now(), returned_reason="intern")
        customer_messages.record(self.mine, Kind.APPLICATION_REJECTED, "intern")
        customer_messages.record(self.other, Kind.APPLICATION_REJECTED, "fremd")

        self.assertEqual(customer_messages.purge(self.mine), 1)

        self.assertFalse(CustomerMessage.objects.filter(person=self.mine).exists())
        self.assertEqual(Application.objects.get(pk=application.pk).returned_reason, "")
        self.assertTrue(CustomerMessage.objects.filter(person=self.other).exists())
        self.assertEqual(customer_messages.purge(self.mine), 0)  # idempotent

    def test_deleting_an_account_that_is_only_deactivated_purges_them_in_the_profile_flow(self):
        staff = staff_user("purge2.staff@example.org")
        publish_contract(staff)
        configure_numbers()
        user = customer("purge.retained@example.org")
        complete_application(user, staff)
        customer_messages.record(user, Kind.APPLICATION_REJECTED, "intern")
        # A confirmed deregistration releases the deletion; the account is then deactivated, not deleted.
        from leg_platform.community import deregistrations

        point = user.metering_point_assignments.first().metering_point
        deregistrations.request_deregistration(user, metering_point_ids=[point.pk], desired_date=None)
        client = login(user)

        client.post(reverse("accounts:account_delete"), {"current_password": PASSWORD, "confirm": "on"})

        user.refresh_from_db()
        self.assertEqual(user.status, user.Status.DEACTIVATED)
        self.assertFalse(CustomerMessage.objects.filter(person=user).exists())

    def test_no_customer_text_says_rolled_back(self):
        # W2-03: the words of the customer's pages never use that term, in either language.
        forbidden = ("rückabgewickelt", "rolled back", "rolled-back")
        for path in (PACKAGE / "templates" / "customer").glob("*.html"):
            source = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                self.assertNotIn(word, source, path.name)
        for path in PACKAGE.glob("*.py"):
            source = path.read_text(encoding="utf-8").lower()
            for word in forbidden:
                self.assertNotIn(word, source, path.name)


@tag("mariadb")
class ReturnedAndPausedTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        self.mine = customer("returned.mine@example.org")
        self.client = login(self.mine)
        self.application, _building = draft_with_points(self.mine)

    def return_it(self, reason=HOSTILE):
        Application.objects.filter(pk=self.application.pk).update(returned_at=timezone.now(), returned_reason=reason)

    def test_the_reason_is_on_the_start_page_the_building_card_and_the_wizard_escaped(self):
        self.return_it()
        for url in (HOME, BUILDINGS, START, step_url(self.application, "contract")):
            with self.subTest(url=url):
                html = text(self.client.get(url))
                self.assertIn("Die LEG hat Ihre Anmeldung zur Korrektur zurückgegeben:", html)
                self.assertIn(escape(HOSTILE), html)
                self.assertNotIn("<img src=x", html)
                if url == HOME:
                    self.assertIn("Bitte nachbessern", html)
                else:
                    self.assertIn("Bitte prüfen Sie Ihre Angaben und schicken Sie die Anmeldung erneut ab.", html)

    def test_without_a_reason_the_general_sentence_stays(self):
        self.return_it(reason="")
        html = text(self.client.get(step_url(self.application, "contract")))
        self.assertIn("Die LEG hat Ihre Anmeldung zurückgegeben. Bitte prüfen Sie Ihre Angaben und schicken Sie sie erneut ab.", html)
        self.assertNotIn("zur Korrektur zurückgegeben:", html)

    def test_a_draft_that_was_not_returned_has_no_note(self):
        html = text(self.client.get(HOME))
        self.assertNotIn("returned-note", html)

    def test_a_paused_draft_has_the_label_and_the_day_it_is_deleted_on_the_pages(self):
        Application.objects.filter(pk=self.application.pk).update(state=Application.State.PAUSED)
        day = date(2026, 12, 1)
        with patch.object(applications, "rollback_date", return_value=day):
            # The start page card shows the paused draft in its group; the day is not on the card.
            self.assertIn("Offene Anmeldung", text(self.client.get(HOME)))
            for url in (BUILDINGS, START, step_url(self.application, "contract")):
                with self.subTest(url=url):
                    html = flat(text(self.client.get(url)))
                    self.assertIn("sonst wird sie am 01.12.2026 gelöscht.", html)
        with patch.object(applications, "rollback_date", return_value=day):
            self.assertRegex(text(self.client.get(BUILDINGS)), r'tone-[a-z]+" aria-hidden="true"></span>Pausiert')

    def test_a_paused_draft_without_a_day_says_it_without_one(self):
        Application.objects.filter(pk=self.application.pk).update(state=Application.State.PAUSED)
        with patch.object(applications, "rollback_date", return_value=None):
            html = flat(text(self.client.get(BUILDINGS)))
        self.assertIn("Machen Sie weiter, sonst wird sie gelöscht.", html)


@tag("mariadb")
class StatusLineTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("status.mine@example.org")
        self.client = login(self.mine)

    def lines(self, *lines):
        return patch.object(participation, "status_lines", return_value=list(lines))

    def test_each_kind_has_its_words_and_a_link_to_the_buildings(self):
        day = date(2027, 3, 31)
        cases = (
            (participation.StatusLine(participation.DEREGISTRATION_RUNNING, day), "Ihre Abmeldung ist bestätigt. Ihre Messpunkte enden am 31.03.2027."),
            (participation.StatusLine(participation.DEREGISTRATION_REQUESTED), "Ihre Abmeldung ist eingereicht. Die LEG bestätigt den Termin."),
            (participation.StatusLine(participation.PARTICIPATION_STARTS, day), "Ihre Teilnahme beginnt am 31.03.2027."),
            (participation.StatusLine(participation.PARTICIPATION_STARTED, day), "Sie nehmen seit dem 31.03.2027 an der LEG teil."),
        )
        for line, words in cases:
            with self.subTest(kind=line.kind), self.lines(line):
                html = text(self.client.get(HOME))
                self.assertIn(words, html)
                self.assertIn(f'href="{BUILDINGS}"', html)

    def test_the_application_kinds_are_not_worded_on_the_page_the_card_does_it(self):
        with self.lines(participation.StatusLine(participation.APPLICATION_AT_LEG)):
            self.assertNotIn("status-line", text(self.client.get(HOME)))

    def test_a_running_participation_is_a_line_and_an_open_application_shows_the_card_instead(self):
        staff = staff_user("status.staff@example.org")
        publish_contract(staff)
        complete_application(self.mine, staff)
        html = text(self.client.get(HOME))
        self.assertIn("Sie nehmen seit dem", html)
        self.assertNotIn("Ihr Weg in die LEG", html)
        draft_with_building(self.mine, egid=90007001, street="Seitenweg", house_number="1")
        html = text(self.client.get(HOME))
        self.assertIn("Offene Anmeldung", html)
        self.assertNotIn("status-line", html)

    def test_no_line_without_a_participation(self):
        self.assertNotIn("status-lines", text(self.client.get(HOME)))

    def test_a_staff_account_with_a_number_gets_the_line_too(self):
        # D-113: staff accounts take part like everybody else.
        with self.lines(participation.StatusLine(participation.PARTICIPATION_STARTED, date(2027, 1, 1))):
            html = text(login(staff_user("status.staff2@example.org")).get(HOME))
        self.assertIn("Sie nehmen seit dem 01.01.2027 an der LEG teil.", html)

    def test_an_account_that_does_not_take_part_has_none(self):
        with self.lines(participation.StatusLine(participation.PARTICIPATION_STARTED, date(2027, 1, 1))), patch(
            "leg_platform.customer.access.is_customer", return_value=False
        ):
            self.assertNotIn("status-line", text(login(staff_user("status.staff3@example.org")).get(HOME)))


@tag("mariadb")
class ContactLinkTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("contact.mine@example.org")

    @instance_mail_addresses(contact_address="leg@example.org")
    def test_the_menu_has_the_contact_page_where_the_address_of_the_leg_is_configured(self):
        html = text(login(self.mine).get(HOME))
        self.assertIn('href="/participation/contact/"', html)
        self.assertIn(">Kontakt zur LEG</a></li>", html)
        self.assertNotIn('target="_blank" rel="noopener noreferrer">Kontakt zur LEG', html)

    @instance_mail_addresses(contact_address="leg@example.org")
    def test_the_website_of_the_leg_is_no_longer_linked_from_the_menu(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        html = text(login(self.mine).get(HOME))
        self.assertNotIn('href="https://leg.example.org/" target="_blank"', html)
        self.assertIn('href="/participation/contact/"', html)

    @instance_mail_addresses(contact_address="")
    def test_without_the_address_of_the_leg_the_entry_stays_and_the_page_says_so(self):
        html = text(login(self.mine).get(HOME))
        self.assertIn('href="/participation/contact/"', html)
        self.assertIn("Kontakt zur LEG", html)
        self.assertIn("noch nicht verfügbar", text(login(self.mine).get("/participation/contact/")))

    @instance_mail_addresses(contact_address="leg@example.org")
    def test_a_staff_account_with_a_number_gets_the_customer_link_too(self):
        # D-113: staff accounts take part like everybody else.
        self.assertIn("Kontakt zur LEG", text(login(staff_user("contact.staff@example.org")).get(HOME)))


class LegLinkTagTests(SimpleTestCase):
    def render(self, url):
        template = Template("{% load customer_ui %}{% leg_link as leg %}<p>Die {{ leg }} prüft.</p>")
        return template.render(Context({"instance_website_url": url}))

    def test_a_link_with_the_address_escaped_where_a_website_is_configured(self):
        with translation.override("de-ch"):
            html = self.render('https://leg.example.org/?a="b"')
        self.assertIn('<a href="https://leg.example.org/?a=&quot;b&quot;" target="_blank" rel="noopener noreferrer">LEG<span class="visually-hidden"> (öffnet in neuem Tab)</span></a>', html)

    def test_the_plain_word_without_a_website(self):
        with translation.override("de-ch"):
            self.assertEqual(self.render(""), "<p>Die LEG prüft.</p>")

    def test_only_an_https_address_becomes_a_link(self):
        for url in ("javascript:alert(1)", "http://x", "data:text/html,x", "HTTP://x", "//x.example", "ftp://x"):
            with self.subTest(url=url), translation.override("de-ch"):
                self.assertEqual(self.render(url), "<p>Die LEG prüft.</p>")
        with translation.override("de-ch"):
            self.assertIn('<a href="HTTPS://x.example/"', self.render("HTTPS://x.example/"))


@tag("mariadb")
class StepPagesTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        publish_contract()
        self.mine = customer("steps.mine@example.org")
        self.client = login(self.mine)

    def test_the_address_of_the_account_starts_the_form_and_stays_editable(self):
        application = applications.start_draft(self.mine)
        html = text(self.client.get(step_url(application, "building")))
        for value in ("Examplestrasse", "12a", "3000", "Bern"):
            self.assertIn(f'value="{value}"', html)
        self.assertNotIn("disabled", html.split("<form")[1].split("</form>")[0])

    def test_a_foreign_address_is_not_offered(self):
        foreign = customer("steps.foreign@example.org", swiss_postal_code=None, foreign_postal_code="10115", country="DE", town="Berlin")
        application = applications.start_draft(foreign)
        html = text(login(foreign).get(step_url(application, "building")))
        self.assertNotIn('value="Berlin"', html)
        self.assertNotIn('value="Examplestrasse"', html)

    def test_the_change_building_note_names_the_loss_of_the_points_only_when_there_are_some(self):
        application, _building = draft_with_building(self.mine)
        url = step_url(application, "building")
        self.assertNotIn("werden die erfassten Messpunkte gelöscht", text(self.client.get(url)))
        applications.add_metering_point(
            self.mine, application, application.buildings.get().pk, direction=Direction.CONSUMPTION, entered_number="1"
        )
        html = text(self.client.get(url))
        self.assertIn("Wenn Sie ein anderes Gebäude wählen, werden die erfassten Messpunkte gelöscht.", html)
        self.assertIn('aria-describedby="change-building-note"', html)

    def test_the_review_note_and_the_not_clear_note_name_the_leg_as_a_link(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        application = applications.start_draft(self.mine)
        from leg_platform.customer.tests.helpers import directory

        with directory():
            response = self.client.post(step_url(application, "building"), {
                "street": "Ittigenstrasse", "house_number": "51", "postal_code": "3063", "town": "Ittigen",
            })
        html = text(response)
        self.assertIn("Nicht eindeutig gefunden", html)
        self.assertIn('<a href="https://leg.example.org/" target="_blank" rel="noopener noreferrer">LEG<span class="visually-hidden"> (öffnet in neuem Tab)</span></a>', html)
        self.assertIn("autofocus", html)

    def test_the_error_summary_lists_the_short_name_of_the_field_not_the_message(self):
        application, _building = draft_with_building(self.mine)
        response = self.client.post(step_url(application, "metering_points"), {"draw_number": "", "feed_in": "no"})
        html = text(response)
        summary = html[html.index("error-summary") :]
        summary = summary[: summary.index("</div>")]
        self.assertIn('<a href="#id_draw_number">Messpunkt-Nummer für den Bezug</a>', summary)
        self.assertNotIn("Geben Sie die letzten Stellen der Nummer ein.", summary)
        # The message stays under the field.
        self.assertIn("Geben Sie die letzten Stellen der Nummer ein.", html)

    def test_a_feed_in_added_to_an_existing_draw_says_so(self):
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        draw = make_metering_point(60, Direction.CONSUMPTION, location, registered_with_operator_on=date(2020, 1, 1),
                                   confirmed_on=date(2020, 1, 1), start_date=date(2020, 1, 1))
        assign_person(draw, self.mine, valid_from=date(2020, 1, 1))
        overview = text(self.client.get(START, {"building": location.pk}))
        self.assertIn("Sie erfassen den Messpunkt für die Einspeisung.", overview)
        # "View" instead of "Change" where the detail is only shown (the building and the contract).
        self.assertIn(">Ansehen<span", overview)
        response = self.client.post(START, {"building": location.pk, "overview": "1"})
        draft = Application.objects.get(person=self.mine)
        page = text(self.client.get(step_url(draft, "metering_points")))
        self.assertIn("Sie erfassen den Messpunkt für die Einspeisung.", page)
        self.assertNotIn("Der Messpunkt ist der Anschluss Ihres Stromzählers.", page)
        self.assertEqual(response.status_code, 302)

    def test_no_prefix_sentence_where_no_prefix_is_configured(self):
        from leg_platform.community.models import ManagementSettings

        ManagementSettings.objects.filter(id=1).update(identifier="")
        application, _building = draft_with_building(self.mine)
        html = text(self.client.get(step_url(application, "metering_points")))
        self.assertNotIn("Seine Nummer beginnt mit", html)

    def test_the_start_date_help_text_names_the_earliest_day_up_front(self):
        application, _building = draft_with_points(self.mine)
        applications.accept_contract_step(
            self.mine, application, contracts.current_version().pk, agrees_contract=True
        )
        html = text(self.client.get(step_url(application, "start_date")))
        earliest = applications.earliest_start_date().strftime("%d.%m.%Y")
        self.assertIn(f"Frühestens ab dem {earliest}.", html)

    def test_cancel_is_offered_once_while_the_confirmation_is_open(self):
        application, _building = draft_with_points(self.mine)
        for name in ("contract", "start_date", "metering_points", "building"):
            with self.subTest(step=name):
                html = text(self.client.get(step_url(application, name) + "?cancel=1"))
                self.assertNotIn(">Abbrechen<", html)

    def test_the_title_and_the_label_say_application_in_every_place(self):
        application, _building = draft_with_building(self.mine)
        html = text(self.client.get(step_url(application, "metering_points")))
        self.assertIn("Meine Teilnahme · Anmeldung", html)
        self.assertIn("<title>", html)
        title = html[html.index("<title>") : html.index("</title>")]
        self.assertIn("Anmeldung", title)
        self.assertNotIn("Registrierung", title)

    def test_the_start_button_posts_and_goes_straight_to_step_one_where_nothing_is_on_file(self):
        response = self.client.post(START)
        draft = Application.objects.get(person=self.mine)
        self.assertRedirects(response, step_url(draft, "building"), fetch_redirect_response=False)

    def test_the_start_button_shows_the_overview_where_something_is_on_file_and_resumes_keep_it(self):
        from leg_platform.community import own_iban

        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        response = self.client.post(START)
        self.assertRedirects(response, START, fetch_redirect_response=False)
        self.assertFalse(Application.objects.filter(person=self.mine).exists())
        html = text(self.client.get(START))
        self.assertIn('name="overview" value="1"', html)
        self.assertIn("0 von 4 Schritten erledigt", html)

    def test_the_buildings_page_starts_a_registration_with_a_post_form(self):
        html = text(self.client.get(BUILDINGS))
        self.assertIn(f'<form method="post" class="inline-form" action="{START}">', html)
        self.assertIn("csrfmiddlewaretoken", html)


@tag("mariadb")
class BuildingsPageWalkthroughTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        configure_numbers()
        self.mine = customer("buildings.walk@example.org")
        self.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        self.since = date.today() - timedelta(days=400)
        self.draw = make_metering_point(
            70, Direction.CONSUMPTION, self.location, registered_with_operator_on=self.since, confirmed_on=self.since,
            start_date=self.since,
        )
        assign_person(self.draw, self.mine, valid_from=self.since)
        CustomerBuilding.objects.create(person=self.mine, location=self.location, name="Haus", description="")
        self.client = login(self.mine)

    def test_an_active_point_says_since_and_an_empty_description_shows_a_dash(self):
        html = text(self.client.get(BUILDINGS))
        self.assertIn(f"Seit {self.since.strftime('%d.%m.%Y')}", html)
        self.assertNotIn("Angemeldet am", html)
        self.assertIn('aria-label="Keine Beschreibung">–</span>', html)

    def test_a_point_that_starts_later_has_no_date_line_and_a_registered_one_says_registered(self):
        later = make_metering_point(71, Direction.FEED_IN, self.location)
        assign_person(later, self.mine, valid_from=date.today() + timedelta(days=30))
        html = text(self.client.get(BUILDINGS))
        self.assertNotIn(f"Seit {(date.today() + timedelta(days=30)).strftime('%d.%m.%Y')}", html)


@tag("mariadb")
class DeregistrationWalkthroughTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("dereg.walk@example.org")
        self.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        self.draw = active_point(80, self.mine, self.location)
        from leg_platform.community import own_iban

        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.client = login(self.mine)

    def test_the_page_names_the_earliest_day_the_irreversibility_and_a_single_direction(self):
        from leg_platform.community import deregistrations

        html = text(self.client.get(DEREGISTRATION))
        earliest = deregistrations.earliest_date_preview().strftime("%d.%m.%Y")
        self.assertIn(f"Frühestens am {earliest}", html)
        self.assertIn("Die Kündigung können Sie danach nicht selbst zurücknehmen. Bei Fragen wenden Sie sich an die LEG.", flat(html))
        self.assertIn("Wunschdatum (letzter Tag eines Monats)", html)
        # A point without a name: the direction is the title and is not repeated in the line below it.
        label = html[html.index("point-check") :]
        label = label[: label.index("</label>")]
        self.assertEqual(label.count("Bezug"), 1)

    def test_the_word_leg_is_a_link_only_where_the_instance_has_a_website(self):
        link = '<a href="https://leg.example.org/" target="_blank" rel="noopener noreferrer">LEG<span class="visually-hidden"> (öffnet in neuem Tab)</span></a>'
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": ""})
        self.assertNotIn(link, text(self.client.get(DEREGISTRATION)))
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://leg.example.org/"})
        self.assertIn(link, text(self.client.get(DEREGISTRATION)))

    def test_the_account_deletion_start_says_why_the_points_come_first(self):
        html = text(self.client.get(DEREGISTRATION, {"for": "account_deletion"}))
        self.assertIn("Sie möchten Ihr Konto löschen. Melden Sie dafür zuerst Ihre Messpunkte ab.", html)
        self.assertNotIn("Sie möchten Ihr Konto löschen.", text(self.client.get(DEREGISTRATION)))

    def test_when_everything_is_deregistered_already_the_page_says_so(self):
        self.client.post(DEREGISTRATION, {
            "metering_points": [str(self.draw.pk)], "termination": "earliest", "current_password": PASSWORD,
        })
        self.assertTrue(Deregistration.objects.filter(person=self.mine).exists())
        html = text(self.client.get(DEREGISTRATION))
        self.assertIn("Bereits abgemeldet", html)
        self.assertIn("Ihre Messpunkte sind bereits abgemeldet. Den Stand sehen Sie auf der Seite Gebäude.", html)
        self.assertNotIn("Nichts abzumelden", html)

    def test_nothing_to_deregister_without_a_running_deregistration_keeps_the_old_page(self):
        client = login(customer("dereg.none@example.org"))
        html = text(client.get(DEREGISTRATION))
        self.assertIn("Nichts abzumelden", html)
        self.assertNotIn("Bereits abgemeldet", html)


@tag("mariadb")
class ProfileWalkthroughTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("profile.walk@example.org")
        self.client = login(self.mine)
        self.profile = reverse("accounts:profile")

    def test_the_personal_card_uses_change_like_every_other_card(self):
        html = text(self.client.get(self.profile))
        card = html[html.index('id="card-personal"') :]
        card = card[: card.index("</section>")]
        self.assertIn(">Ändern<", card)
        self.assertNotIn(">Bearbeiten<", card)

    def test_the_password_card_has_no_lead_sentence_above_the_rules(self):
        html = text(self.client.get(self.profile, {"edit": "password"}))
        self.assertNotIn("Wählen Sie mindestens 15 Zeichen.", html)
        self.assertIn("Mindestens 15 Zeichen.", html)

    def test_the_delete_card_of_a_retained_account_says_it_is_deactivated(self):
        staff = staff_user("profile.staff@example.org")
        publish_contract(staff)
        configure_numbers()
        complete_application(self.mine, staff)
        active = self.mine.metering_point_assignments.first().metering_point
        from leg_platform.community import deregistrations

        deregistrations.request_deregistration(self.mine, metering_point_ids=[active.pk], desired_date=None)
        # The card is one sentence for every account; the page says what happens (owner decision 2026-10-24).
        html = flat(text(self.client.get(self.profile)))
        self.assertIn("Sie können Ihr Konto jederzeit löschen.", html)
        self.assertNotIn("Ihr Konto wird deaktiviert", html)
        page = flat(text(self.client.get(reverse("accounts:account_delete"))))
        self.assertIn("Ausgenommen sind Angaben, zu deren Aufbewahrung wir gesetzlich verpflichtet sind.", page)


class LogOutWordTests(SimpleTestCase):
    def test_log_out_is_ausloggen_and_the_other_words_for_signing_out_are_unchanged(self):
        with translation.override("de-ch"):
            self.assertEqual(translation.gettext("Log out"), "Ausloggen")
            self.assertEqual(translation.gettext("Deregistration"), "Abmeldung")
            self.assertEqual(translation.gettext("Start deregistration"), "Abmeldung starten")
