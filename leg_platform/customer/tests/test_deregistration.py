"""The deregistration pages of the customer (ABMELDUNG.md, KONZEPT 7.1 and 7.2; D-103, D-105): what the page offers, the date
that is never shown, the one proof (D-062), the IBAN of a feed-in, the result, and the negative cases: a foreign id, a wrong
or missing proof, a date that is refused, a point that cannot be deregistered, a second submit, CSRF, the request budget, the
body limit, staff and anonymous visitors. The rules themselves are tested with the service (``community/tests/``)."""

import re
from datetime import date, timedelta

from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.models import MailJob, SessionCodeFailure
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient, code_for
from leg_platform.accounts.tests.test_second_factor import create_user_with_totp
from leg_platform.community import deregistrations, history, numbers, own_iban
from leg_platform.community.application_common import add_months, month_end
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Deregistration, Direction, MeteringPoint
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer import limits, returns
from leg_platform.customer.tests.helpers import BUILDINGS, GOOD_IBAN, customer, login
from leg_platform.customer.tests.test_card_edit import submitted

START = reverse("customer:deregistration_start")
LONG_AGO = date.today() - timedelta(days=400)
EARLIEST_TEXT = "Frühestmöglich"
NOTICE = "Kündigungsfrist: vier Monate auf Monatsende. Die LEG-Verwaltung bestätigt das Datum."


def active_point(last, person, location, direction=Direction.CONSUMPTION, **extra):
    point = make_metering_point(
        last, direction, location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=LONG_AGO, **extra
    )
    assign_person(point, person, valid_from=LONG_AGO)
    return point


def checked_boxes(html):
    """The values of the metering point boxes that are ticked."""
    tags = re.findall(r'<input[^>]*name="metering_points"[^>]*>', html)
    return [re.search(r'value="(\d+)"', tag).group(1) for tag in tags if " checked" in tag]


def all_boxes(html):
    return re.findall(r'<input[^>]*name="metering_points"[^>]*value="(\d+)"', html)


class DeregistrationCase(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.dereg@example.org")
        cls.other = customer("other.dereg@example.org")
        cls.staff = staff_user("staff.dereg@example.org")
        cls.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        cls.draw = active_point(1, cls.mine, cls.location, name="Wohnhaus")
        cls.feed_in = active_point(2, cls.mine, cls.location, Direction.FEED_IN, name="PV-Anlage")
        cls.foreign = active_point(3, cls.other, cls.location, name="Fremd")

    def setUp(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        self.client = login(self.mine)

    def earliest(self):
        return deregistrations.earliest_termination_date()

    def data(self, points=None, **extra):
        points = [self.draw.pk, self.feed_in.pk] if points is None else points
        return {"metering_points": [str(pk) for pk in points], "termination": "earliest", "current_password": PASSWORD, **extra}

    def submit(self, client=None, url=START, **kwargs):
        return (client or self.client).post(url, self.data(**kwargs))


@tag("mariadb")
class PageTests(DeregistrationCase):
    def test_the_page_offers_the_own_points_and_chooses_all_of_them(self):
        response = self.client.get(START)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        for text in (
            "<title>Messpunkte abmelden", "Welche Messpunkte sollen abgemeldet werden?",
            "Wann soll die Abmeldung gelten?", EARLIEST_TEXT, "Wunschdatum (letzter Tag eines Monats)", NOTICE,
            "Messpunkte verbindlich abmelden",
            "data-dereg-summary", "Abbrechen", "Sie melden 2 Messpunkte ab.", "Meine Teilnahme · Abmeldung",
        ):
            self.assertIn(text, html)
        # The heading names a building only for ?building=<id>; no computed date anywhere.
        self.assertNotIn("Abmeldung: Musterstrasse", html)
        self.assertNotIn("Frühestens am", html)
        self.assertNotIn("Verbindlich kündigen", html)
        self.assertCountEqual(all_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertIn(numbers.format_metering_point_number(self.draw.number), html)
        self.assertIn("Wohnhaus", html)
        self.assertRegex(html, r"Bezug · Seit \d\d\.\d\d\.\d{4}")
        self.assertIn("Einspeisung", html)
        # Nothing of the other customer at the same building.
        self.assertNotIn(numbers.format_metering_point_number(self.foreign.number), html)
        self.assertNotIn(self.foreign.number, html)
        self.assertNotIn("Fremd", html)

    def test_the_summary_in_the_page_and_for_the_script_is_german_for_one_and_for_many(self):
        from django.utils import translation

        from leg_platform.customer import deregistration as dereg_views

        html = self.client.get(START).content.decode()
        # The script replaces the placeholder in the "many" text; the percent sign must not be doubled by the template
        # (then the catalogue entry is not found and the English text shows).
        self.assertIn('data-many="Sie melden %(count)s Messpunkte ab."', html)
        self.assertIn('data-one="Sie melden 1 Messpunkt ab."', html)
        self.assertNotIn("You are deregistering", html)
        with translation.override("de-ch"):
            self.assertEqual(dereg_views.summary_text(1), "Sie melden 1 Messpunkt ab.")
            self.assertEqual(dereg_views.summary_text(2), "Sie melden 2 Messpunkte ab.")
            self.assertEqual(dereg_views.summary_text(0), "")

    def test_no_computed_date_is_shown_and_the_page_is_not_stored(self):
        # Owner decision 2026-10-29: the customer picks "earliest possible" or a month end; the LEG confirms the date.
        response = self.client.get(START)
        html = response.content.decode()

        self.assertNotIn("Frühestens am", html)
        self.assertNotIn(self.earliest().strftime("%d.%m.%Y"), html.split("Wunschdatum")[0])
        self.assertFalse(hasattr(deregistrations, "earliest_date_preview"))
        for directive in ("no-cache", "no-store", "private"):
            self.assertIn(directive, response["Cache-Control"])

    def test_a_get_changes_nothing(self):
        before = (Deregistration.objects.count(), MailJob.objects.count())
        self.client.get(START)
        self.client.get(START, {"for": "account_deletion"})
        self.assertEqual((Deregistration.objects.count(), MailJob.objects.count()), before)

    def test_the_button_of_a_building_lists_only_that_building_with_all_points_chosen(self):
        second = make_location("Zweitweg", "2", "3063", "Ittigen")
        elsewhere = active_point(7, self.mine, second)

        response = self.client.get(START, {"building": self.location.pk})
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(all_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertNotIn(str(elsewhere.pk), all_boxes(html))
        self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertIn("Abmeldung: Musterstrasse 1", html)

    def test_a_building_with_one_point_has_it_chosen(self):
        other_building = make_location("Nebenweg", "4", "3063", "Ittigen")
        single = active_point(8, self.mine, other_building)

        html = self.client.get(START, {"building": other_building.pk}).content.decode()

        self.assertEqual(all_boxes(html), [str(single.pk)])
        self.assertEqual(checked_boxes(html), [str(single.pk)])

    def test_several_buildings_are_shown_by_building(self):
        other_building = make_location("Nebenweg", "4", "3063", "Ittigen")
        active_point(8, self.mine, other_building)

        html = self.client.get(START).content.decode()

        self.assertEqual(html.count('<fieldset class="point-group">'), 2)
        self.assertIn("<legend>Musterstrasse 1", html)
        self.assertIn("<legend>Nebenweg 4", html)

    def test_the_cancel_target_follows_from_and_never_a_free_address(self):
        buildings_url = reverse("customer:buildings")
        cases = (
            ({"from": "end"}, reverse("accounts:account_end")),
            ({"from": "delete"}, reverse("accounts:account_delete")),
            ({}, buildings_url),
            ({"building": self.location.pk}, buildings_url),
            ({"from": "https://evil.example/"}, buildings_url),
            ({"from": "//evil.example"}, buildings_url),
            ({"from": "profile"}, buildings_url),
        )
        for query, target in cases:
            with self.subTest(query=query):
                html = self.client.get(START, query).content.decode()
                self.assertRegex(html, rf'<a class="btn btn-secondary-action end-action" href="{re.escape(target)}">Abbrechen</a>')
                self.assertNotIn("evil.example", html)

    def test_the_removed_account_deletion_gate_is_gone(self):
        before = (Deregistration.objects.count(), MailJob.objects.count())
        html = self.client.get(START, {"for": "account_deletion"}).content.decode()

        self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertNotIn("#card-delete", html)
        self.assertNotIn("Konto löschen", html)
        self.assertEqual((Deregistration.objects.count(), MailJob.objects.count()), before)

    def test_points_named_in_the_query_are_chosen(self):
        html = self.client.get(START, {"point": [self.feed_in.pk]}).content.decode()

        self.assertEqual(checked_boxes(html), [str(self.feed_in.pk)])
        self.assertIn("Sie melden 1 Messpunkt ab.", html)

    def test_a_foreign_or_unknown_id_is_a_404(self):
        for query in (
            {"point": [self.foreign.pk]}, {"point": [987654]}, {"building": 987654}, {"building": "x"},
            {"point": [self.draw.pk, self.foreign.pk]},
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(START, query).status_code, 404)

    def test_a_building_of_another_account_is_a_404(self):
        stranger = make_location("Fremdstrasse", "9", "3063", "Ittigen")
        assign_person(make_metering_point(9, Direction.CONSUMPTION, stranger), self.other)
        self.assertEqual(self.client.get(START, {"building": stranger.pk}).status_code, 404)

    def test_without_a_running_point_the_page_says_so_and_has_no_form(self):
        html = login(customer("empty.dereg@example.org")).get(START).content.decode()

        self.assertIn("Sie haben keinen Messpunkt, der abgemeldet werden kann.", html)
        self.assertIn("Zurück zu den Gebäuden", html)
        self.assertNotIn('name="metering_points"', html)
        self.assertNotIn("Messpunkte verbindlich abmelden", html)

    def test_a_point_that_is_not_active_yet_is_offered_like_any_other_and_one_in_a_deregistration_is_only_listed(self):
        future = make_metering_point(
            5, Direction.CONSUMPTION, self.location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=date.today() + timedelta(days=30),
        )
        assign_person(future, self.mine, valid_from=LONG_AGO)
        deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk])

        html = self.client.get(START).content.decode()

        self.assertCountEqual(all_boxes(html), [str(self.feed_in.pk), str(future.pk)])
        self.assertCountEqual(checked_boxes(html), [str(self.feed_in.pk), str(future.pk)])
        # Read-only block below the form, without a box for the point.
        self.assertIn("Bereits in Abmeldung", html)
        self.assertGreater(html.index("Bereits in Abmeldung"), html.index("Messpunkte verbindlich abmelden"))
        self.assertIn("Datum wird bestätigt", html)

    def test_a_feed_in_without_an_iban_is_offered_without_a_detour(self):
        own_iban.remove_own_iban(self.mine)

        html = self.client.get(START).content.decode()

        self.assertNotIn("Für die Vergütung der Einspeisung", html)
        self.assertNotIn("edit=bank", html)
        self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])

    def test_a_registration_in_progress_can_be_withdrawn_with_an_unticked_box_and_the_rest_is_shown_only(self):
        from leg_platform.community.models import Application

        application, _building = submitted(self.mine, feed_in=False, egid=90004270, number=11)
        self.assertIn(application.state, Application.WITHDRAWABLE_STATES)

        html = self.client.get(START).content.decode()

        self.assertIn("Anmeldung in Bearbeitung", html)
        self.assertRegex(
            html, rf'<input[^>]*name="withdraw_applications"[^>]*value="{application.pk}"(?![^>]*checked)[^>]*>'
        )
        self.assertIn("Anmeldung zurückziehen: ", html)

    def test_withdrawal_selection_and_error_link_survive_an_invalid_submission(self):
        person = customer("withdraw.only@example.org")
        application, _building = submitted(person, feed_in=False, egid=90004271, number=12)
        client = login(person)

        html = client.post(START, {}).content.decode()
        self.assertIn('href="#progress-title">Anmeldung in Bearbeitung</a>', html)
        self.assertIn('id="id_withdraw_applications_error">Wählen Sie mindestens einen Eintrag aus.', html)
        self.assertIn("Geben Sie Ihr aktuelles Passwort ein.", html)

        html = client.post(START, {"withdraw_applications": [str(application.pk)]}).content.decode()
        self.assertRegex(html, rf'name="withdraw_applications" value="{application.pk}" checked')
        self.assertIn("Geben Sie Ihr aktuelles Passwort ein.", html)

    def test_the_proof_is_the_password_without_a_second_factor(self):
        html = self.client.get(START).content.decode()

        self.assertRegex(html, r'<input[^>]*name="current_password"')
        self.assertNotRegex(html, r'name="otp_token"')
        # No field takes the focus on arrival.
        self.assertNotIn("autofocus", re.search(r'<input[^>]*name="current_password"[^>]*>', html).group(0))

    def test_the_proof_is_the_code_with_a_second_factor_and_never_both(self):
        user, _device = create_user_with_totp("code.dereg@example.org")
        active_point(20, user, self.location)
        client = Client()
        client.force_login(user)

        html = client.get(START).content.decode()

        self.assertRegex(html, r'<input[^>]*name="otp_token"')
        self.assertNotRegex(html, r'name="current_password"')
        self.assertIn("2FA-Authentifizierungscode", html)

    def test_the_page_has_no_inline_script_or_style(self):
        html = self.client.get(START).content.decode()
        # The form carries data-submit-once and the base loads the script that uses it.
        self.assertIn("data-submit-once", html)
        self.assertIn('<script defer src="/static/js/submit-once.js"></script>', html)
        self.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")
        self.assertNotRegex(html, r'\sstyle="')
        self.assertNotRegex(html, r"(?i)\son(click|load|change|submit)=")


@tag("mariadb")
class SubmitTests(DeregistrationCase):
    def test_a_whole_building_is_deregistered_with_the_password_and_every_address_is_told(self):
        self.mine.set_secondary_email("second.dereg@example.org")

        response = self.submit()

        dereg = Deregistration.objects.get()
        self.assertRedirects(response, reverse("customer:deregistration_done", args=[dereg.pk]))
        self.assertEqual(dereg.person, self.mine)
        self.assertEqual(sorted(dereg.items.values_list("metering_point_id", flat=True)), [self.draw.pk, self.feed_in.pk])
        self.assertEqual((dereg.state, dereg.wants_earliest), (Deregistration.State.SUBMITTED, True))
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.DEREGISTRATION_SUBMITTED).values_list("address", flat=True)),
            ["mine.dereg@example.org", "second.dereg@example.org"],
        )
        entry = history.entries_for(dereg).get(event=HistoryEvent.DEREGISTRATION_SUBMITTED)
        self.assertEqual(entry.actor, self.mine)
        # Nothing is terminated by the request, and the other customer's point is untouched.
        self.assertTrue(all(point.terminated_on is None for point in MeteringPoint.objects.all()))
        self.assertFalse(Deregistration.objects.filter(items__metering_point=self.foreign).exists())

    def test_the_result_page_says_what_happens_and_shows_no_computed_date(self):
        response = self.submit()
        page = self.client.get(response["Location"])
        html = page.content.decode()

        self.assertEqual(page.status_code, 200)
        for text in (
            "Ihre Abmeldung ist abgeschickt", numbers.format_metering_point_number(self.draw.number), "Zu den Gebäuden",
        ):
            self.assertIn(text, html)
        self.assertNotIn("Konto löschen", html)
        for directive in ("no-cache", "no-store", "private"):
            self.assertIn(directive, page["Cache-Control"])

    def test_one_point_with_a_later_month_end(self):
        wish = month_end(add_months(self.earliest(), 2))

        response = self.client.post(
            START, self.data([self.feed_in.pk], termination="date", desired_date=wish.isoformat())
        )

        dereg = Deregistration.objects.get()
        self.assertEqual(list(dereg.items.values_list("metering_point_id", flat=True)), [self.feed_in.pk])
        self.assertEqual((dereg.desired_date, dereg.wants_earliest), (wish, False))
        self.assertIn(wish.strftime("%d.%m.%Y"), self.client.get(response["Location"]).content.decode())

    def test_a_date_that_is_refused_changes_nothing_and_keeps_the_choice(self):
        today = timezone.localdate()
        cases = (
            (today - timedelta(days=3), "Das Datum darf nicht in der Vergangenheit liegen."),
            (add_months(self.earliest(), 2).replace(day=15), "Wählen Sie den letzten Tag eines Monats."),
            (month_end(add_months(today, 1)), "Dieser Termin ist zu früh. Die Kündigungsfrist beträgt vier Monate auf ein Monatsende."),
        )
        for wish, message in cases:
            with self.subTest(wish=wish):
                response = self.client.post(START, self.data(termination="date", desired_date=wish.isoformat()))
                html = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, html)
                # The choice stays on the page.
                self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        missing = self.client.post(START, self.data(termination="date"))
        self.assertIn("Wählen Sie ein Datum.", missing.content.decode())
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_date_error_does_not_use_up_the_code(self):
        user, device = create_user_with_totp("early.dereg@example.org")
        active_point(21, user, self.location)
        client = Client()
        client.force_login(user)
        point = user.metering_point_assignments.get().metering_point_id
        wish = month_end(add_months(timezone.localdate(), 1)).isoformat()

        refused = client.post(START, {
            "metering_points": [str(point)], "termination": "date", "desired_date": wish, "otp_token": code_for(device),
        })

        self.assertIn("zu früh", refused.content.decode())
        # The code was not checked (nothing else was wrong only then), so no failure is counted either.
        self.assertFalse(SessionCodeFailure.objects.filter(account=user).exists())
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_without_a_point_nothing_happens(self):
        response = self.submit(points=[])

        self.assertEqual(response.status_code, 200)
        self.assertIn("Wählen Sie mindestens einen Messpunkt aus.", response.content.decode())
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_wrong_password_changes_nothing_is_counted_for_the_session_and_not_written_back(self):
        response = self.submit(current_password="not the password at all")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Das Passwort stimmt nicht", response.content.decode())
        self.assertNotIn("not the password at all", response.content.decode())
        self.assertEqual(SessionCodeFailure.objects.get(account=self.mine).failures, 1)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_the_fifth_wrong_entry_ends_the_session(self):
        for _attempt in range(5):
            last = self.submit(current_password="wrong wrong wrong")
        self.assertRedirects(last, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_no_proof_at_all_changes_nothing(self):
        payload = self.data()
        del payload["current_password"]

        response = self.client.post(START, payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Geben Sie Ihr aktuelles Passwort ein.", response.content.decode())
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_foreign_point_in_the_post_is_a_404_and_nothing_changes(self):
        for points in ([self.foreign.pk], [self.draw.pk, self.foreign.pk], [987654]):
            with self.subTest(points=points):
                self.assertEqual(self.submit(points=points).status_code, 404)
        self.assertEqual(Deregistration.objects.count(), 0)
        self.assertEqual(SessionCodeFailure.objects.count(), 0)

    def test_an_own_point_that_cannot_be_deregistered_is_refused_neutrally(self):
        deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk])

        response = self.submit(points=[self.draw.pk])

        self.assertEqual(response.status_code, 200)
        self.assertIn("Einer der Messpunkte kann nicht (mehr) abgemeldet werden.", response.content.decode())
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_second_submit_of_the_same_points_makes_no_second_deregistration(self):
        self.assertEqual(self.submit().status_code, 302)

        again = self.submit()

        self.assertEqual(again.status_code, 200)
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_missing_iban_never_blocks_a_deregistration_of_a_draw_or_a_feed_in(self):
        own_iban.remove_own_iban(self.mine)
        wish = month_end(add_months(self.earliest(), 1))

        draw = self.submit(points=[self.draw.pk])
        feed_in = self.client.post(
            START, self.data([self.feed_in.pk], termination="date", desired_date=wish.isoformat())
        )

        self.assertEqual((draw.status_code, feed_in.status_code), (302, 302))
        self.assertEqual(Deregistration.objects.count(), 2)
        self.assertNotIn("edit=bank", feed_in["Location"])

    def test_a_point_that_is_not_active_yet_gets_a_real_deregistration_like_an_active_one(self):
        later = date.today() + timedelta(days=60)
        kinds = {
            "registered": {},
            "notified": {"registered_with_operator_on": LONG_AGO},
            "confirmed": {"registered_with_operator_on": LONG_AGO, "confirmed_on": LONG_AGO, "start_date": later},
        }
        points = {}
        for number, (status, extra) in enumerate(kinds.items(), start=30):
            point = make_metering_point(number, Direction.CONSUMPTION, self.location, **extra)
            assign_person(point, self.mine, valid_from=LONG_AGO)
            points[status] = point

        html = self.client.get(START).content.decode()
        self.assertCountEqual(
            all_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)] + [str(point.pk) for point in points.values()]
        )

        response = self.submit(points=[point.pk for point in points.values()])

        self.assertEqual(response.status_code, 302)
        dereg = Deregistration.objects.get()
        self.assertEqual(sorted(dereg.items.values_list("metering_point_id", flat=True)), sorted(p.pk for p in points.values()))
        self.assertEqual((dereg.state, dereg.wants_earliest), (Deregistration.State.SUBMITTED, True))
        # The customer sees no difference on the result page.
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_the_confirmation_of_a_deregistration_with_a_point_that_is_not_active_yet_ends_that_point(self):
        pending = make_metering_point(
            30, Direction.CONSUMPTION, self.location, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=date.today() + timedelta(days=60),
        )
        assign_person(pending, self.mine, valid_from=LONG_AGO)
        response = self.submit(points=[pending.pk])
        dereg = Deregistration.objects.get()
        day = month_end(add_months(timezone.localdate(), 6))

        deregistrations.confirm(dereg, self.staff, day)

        pending.refresh_from_db()
        dereg.refresh_from_db()
        self.assertEqual((dereg.state, dereg.confirmed_date), (Deregistration.State.CONFIRMED, day))
        self.assertEqual(pending.terminated_on, day)
        self.assertIn(day.strftime("%d.%m.%Y"), self.client.get(response["Location"]).content.decode())

    def test_the_code_is_the_proof_with_a_second_factor_and_a_password_is_no_proof(self):
        user, device = create_user_with_totp("proof.dereg@example.org")
        point = active_point(22, user, self.location)
        client = Client()
        client.force_login(user)
        payload = {"metering_points": [str(point.pk)], "termination": "earliest"}

        only_password = client.post(START, {**payload, "current_password": PASSWORD})
        self.assertIn("Geben Sie den 2FA-Authentifizierungscode ein.", only_password.content.decode())
        wrong = client.post(START, {**payload, "otp_token": "123456"})
        self.assertIn("falsch oder abgelaufen", wrong.content.decode())
        self.assertNotIn('value="123456"', wrong.content.decode())
        self.assertFalse(Deregistration.objects.exists())

        right = client.post(START, {**payload, "otp_token": code_for(device)})
        self.assertEqual(right.status_code, 302)
        self.assertEqual(Deregistration.objects.get().person, user)

    def test_a_post_needs_the_csrf_token_and_has_a_budget_and_a_body_limit(self):
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)
        self.assertEqual(guarded.post(START, self.data()).status_code, 403)

        from unittest.mock import patch

        with patch.object(limits, "allow_wizard", return_value=False):
            limited = self.submit()
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited["Retry-After"], str(limits.RETRY_AFTER))

        big = self.client.post(START, data="x=" + "a" * 20000, content_type="application/x-www-form-urlencoded")
        self.assertEqual(big.status_code, 413)
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_successful_submit_logs_no_warning(self):
        # No number, IBAN or password can be in a log that holds nothing.
        with self.assertNoLogs(level="WARNING"):
            self.assertEqual(self.submit().status_code, 302)


@tag("mariadb")
class ResultAndAccessTests(DeregistrationCase):
    def setUp(self):
        super().setUp()
        self.dereg = deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk])
        self.done = reverse("customer:deregistration_done", args=[self.dereg.pk])

    def test_the_result_is_read_through_the_account(self):
        self.assertEqual(self.client.get(self.done).status_code, 200)
        self.assertEqual(login(self.other).get(self.done).status_code, 404)
        self.assertEqual(self.client.get(reverse("customer:deregistration_done", args=[987654])).status_code, 404)

    def test_staff_with_a_number_cannot_reach_the_deregistration_of_a_customer_and_anonymous_visitors_nothing(self):
        # D-113: a staff account takes part like everybody else, but an id of another account is a 404 for it too.
        for path in (self.done,):
            with self.subTest(path=path):
                self.assertEqual(login(self.staff).get(path).status_code, 404)
        for query in ({"point": [self.draw.pk]}, {"building": self.location.pk}):
            with self.subTest(query=query):
                self.assertEqual(login(self.staff).get(START, query).status_code, 404)
        self.assertEqual(login(self.staff).post(START, self.data()).status_code, 404)
        for path in (START, self.done):
            with self.subTest(path=path):
                self.assertEqual(Client().get(path).status_code, 302)
                self.assertEqual(Client().post(path, self.data())["Location"], reverse("accounts:login"))
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_confirmed_date_is_shown_as_deregistered_on(self):
        day = month_end(add_months(timezone.localdate(), 5))
        deregistrations.confirm(self.dereg, self.staff, day)
        html = self.client.get(self.done).content.decode()
        self.assertIn(f"Abgemeldet per {day.strftime('%d.%m.%Y')}", html)
        self.assertNotIn("Gekündigt zum", html)
        self.assertNotIn("Die LEG bestätigt den Termin.", html)

    def test_after_the_start_of_the_deregistration_the_buildings_page_says_in_deregistration(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertIn("Abmeldung", html)
        self.assertNotIn("Frühestens am", html)

    def test_the_done_page_has_no_account_deletion_gate(self):
        html = self.client.get(self.done, {"for": "account_deletion"}).content.decode()

        self.assertNotIn("Sie können Ihr Konto jetzt löschen", html)
        self.assertNotIn("edit=delete", html)


class SafeReturnPathTests(SimpleTestCase):
    def test_only_a_page_of_the_customer_area_is_accepted(self):
        for value in (
            "/participation/buildings/", "/participation/deregistration/?point=1&point=2",
            "/participation/application/5/", "/participation/application/?building=3",
        ):
            with self.subTest(value=value):
                self.assertEqual(returns.safe_return_path(value), value)

    def test_everything_else_is_dropped(self):
        for value in (
            "", None, 5, "https://evil.example/participation/buildings/", "//evil.example/participation/buildings/",
            "/\\evil.example/", "\\\\evil.example", "javascript:alert(1)", "/profile/", "/management/", "/admin/",
            "/participation/unknown/", "participation/buildings/", "/participation/buildings/#x", "/participation/\n",
            "/participation/buildings/\r\nSet-Cookie: x=1", "/participation/" + "a" * 600, "http:/participation/buildings/",
        ):
            with self.subTest(value=value):
                self.assertEqual(returns.safe_return_path(value), "")

    def test_the_card_address_carries_a_safe_way_back_only(self):
        self.assertEqual(
            returns.bank_change_url("/participation/buildings/"),
            "/profile/?edit=bank&next=%2Fparticipation%2Fbuildings%2F#card-bank",
        )
        self.assertEqual(returns.bank_change_url("https://evil.example/"), "/profile/?edit=bank#card-bank")
        self.assertEqual(returns.bank_change_url(), "/profile/?edit=bank#card-bank")
