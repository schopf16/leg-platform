"""The deregistration pages of the customer (ABMELDUNG.md, KONZEPT 7.1 and 7.2; D-103, D-105): what the page offers, the date
that is never shown, the one proof (D-062), the IBAN of a feed-in, the result, and the negative cases: a foreign id, a wrong
or missing proof, a date that is refused, a point that cannot be deregistered, a second submit, CSRF, the request budget, the
body limit, staff and anonymous visitors. The rules themselves are tested with the service (``community/tests/``)."""

import re
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

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

START = reverse("customer:deregistration_start")
LONG_AGO = date.today() - timedelta(days=400)
EARLIEST_TEXT = "Nächstmöglicher Termin"
NOTICE = "Die Kündigungsfrist beträgt vier Monate auf ein Monatsende. Die LEG bestätigt den Termin."


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
            "Wählen Sie die Messpunkte und den Termin.", "Welche Messpunkte sollen abgemeldet werden?",
            "Wann soll die Abmeldung wirksam werden?", EARLIEST_TEXT, "Wunschtermin (Monatsende)", NOTICE,
            "Bestätigung", "Eine Bestätigung geht an Ihre E-Mail-Adressen.", "Verbindlich kündigen", "Abbrechen",
            "Sie melden 2 Messpunkte zum gewählten Termin ab.", "Meine Teilnahme · Abmeldung",
        ):
            self.assertIn(text, html)
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
        self.assertIn('data-many="Sie melden %(count)s Messpunkte zum gewählten Termin ab."', html)
        self.assertIn('data-one="Sie melden 1 Messpunkt zum gewählten Termin ab."', html)
        self.assertIn('data-none="Wählen Sie mindestens einen Messpunkt aus."', html)
        self.assertNotIn("You are deregistering", html)
        with translation.override("de-ch"):
            self.assertEqual(dereg_views.summary_text(1), "Sie melden 1 Messpunkt zum gewählten Termin ab.")
            self.assertEqual(dereg_views.summary_text(2), "Sie melden 2 Messpunkte zum gewählten Termin ab.")
            self.assertEqual(dereg_views.summary_text(0), "Wählen Sie mindestens einen Messpunkt aus.")

    def test_the_earliest_date_is_shown_next_to_the_earliest_option_and_the_page_is_not_stored(self):
        # Team lead decision (W3-03), a deviation from ABMELDUNG.md section 3: the earliest month end is shown as a
        # reading of the notice period; the LEG confirms the date.
        response = self.client.get(START)
        html = response.content.decode()

        self.assertIn(f"Frühestens am {self.earliest().strftime('%d.%m.%Y')}", html)
        self.assertNotIn(self.earliest().isoformat(), html)
        for directive in ("no-cache", "no-store", "private"):
            self.assertIn(directive, response["Cache-Control"])

    def test_a_get_changes_nothing(self):
        before = (Deregistration.objects.count(), MailJob.objects.count())
        self.client.get(START)
        self.client.get(START, {"for": "account_deletion"})
        self.assertEqual((Deregistration.objects.count(), MailJob.objects.count()), before)

    def test_the_button_of_a_building_lists_only_that_building_and_chooses_none_of_several_points(self):
        response = self.client.get(START, {"building": self.location.pk})
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(all_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertEqual(checked_boxes(html), [])
        self.assertIn("Abmeldung: Musterstrasse 1", html)
        self.assertIn("Wählen Sie mindestens einen Messpunkt aus.", html)

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

    def test_account_deletion_chooses_every_running_point_and_leads_back_to_the_card(self):
        html = self.client.get(START, {"for": "account_deletion"}).content.decode()

        self.assertCountEqual(checked_boxes(html), [str(self.draw.pk), str(self.feed_in.pk)])
        self.assertIn(f'href="{reverse("accounts:profile")}#card-delete"', html)

    def test_points_named_in_the_query_are_chosen(self):
        html = self.client.get(START, {"point": [self.feed_in.pk], "termination": "date", "date": "2030-06-30"}).content.decode()

        self.assertEqual(checked_boxes(html), [str(self.feed_in.pk)])
        self.assertIn('value="2030-06-30"', html)
        self.assertIn("Sie melden 1 Messpunkt zum gewählten Termin ab.", html)

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
        self.assertNotIn("Verbindlich kündigen", html)

    def test_a_point_that_is_not_active_yet_or_already_in_a_deregistration_is_not_offered(self):
        future = make_metering_point(
            5, Direction.CONSUMPTION, self.location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=date.today() + timedelta(days=30),
        )
        assign_person(future, self.mine, valid_from=LONG_AGO)
        deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk])

        html = self.client.get(START).content.decode()

        self.assertEqual(all_boxes(html), [str(self.feed_in.pk)])

    def test_a_feed_in_without_an_iban_shows_the_way_to_enter_it(self):
        own_iban.remove_own_iban(self.mine)

        html = self.client.get(START).content.decode()

        self.assertIn("Für die Vergütung der Einspeisung brauchen wir Ihre Bank- oder Postverbindung.", html)
        self.assertRegex(html, r'href="/profile/\?edit=bank&amp;next=%2Fparticipation%2Fderegistration%2F[^"]*#card-bank"')
        # Without a feed-in in the choice the notice is not shown.
        self.assertNotIn("Für die Vergütung", self.client.get(START, {"point": [self.draw.pk]}).content.decode())

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
            "Ihre Abmeldung ist abgeschickt", "So geht es weiter", "Abmeldung eingereicht", "Termin durch die LEG bestätigt",
            "Abgemeldet", "Die LEG bestätigt den Termin.", numbers.format_metering_point_number(self.draw.number),
            "Bis zum Termin bleiben die Messpunkte aktiv.", "Eine Bestätigung wurde an Ihre E-Mail-Adressen gesendet.",
        ):
            self.assertIn(text, html)
        self.assertIn(f"Frühestens am {self.earliest().strftime('%d.%m.%Y')}. Die LEG bestätigt den Termin.", html)
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
        future = make_metering_point(
            5, Direction.CONSUMPTION, self.location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=date.today() + timedelta(days=30),
        )
        assign_person(future, self.mine, valid_from=LONG_AGO)

        response = self.submit(points=[future.pk])

        self.assertEqual(response.status_code, 200)
        self.assertIn("Einer der Messpunkte kann nicht (mehr) abgemeldet werden.", response.content.decode())
        self.assertEqual(Deregistration.objects.count(), 0)

    def test_a_second_submit_of_the_same_points_makes_no_second_deregistration(self):
        self.assertEqual(self.submit().status_code, 302)

        again = self.submit()

        self.assertEqual(again.status_code, 200)
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_draw_needs_no_iban(self):
        own_iban.remove_own_iban(self.mine)
        self.assertEqual(self.submit(points=[self.draw.pk]).status_code, 302)
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_feed_in_without_an_iban_leads_to_the_profile_and_back_with_the_choice(self):
        own_iban.remove_own_iban(self.mine)
        wish = month_end(add_months(self.earliest(), 1))

        response = self.client.post(
            START, self.data([self.feed_in.pk], termination="date", desired_date=wish.isoformat())
        )

        self.assertEqual(Deregistration.objects.count(), 0)
        self.assertEqual(response.status_code, 302)
        target = urlsplit(response["Location"])
        self.assertEqual((target.path, parse_qs(target.query)["edit"]), (reverse("accounts:profile"), ["bank"]))
        back = parse_qs(target.query)["next"][0]
        self.assertEqual(returns.safe_return_path(back), back)
        self.assertIn(f"point={self.feed_in.pk}", back)
        self.assertIn(f"date={wish.isoformat()}", back)
        # The profile shows the card with the hint, the message and the way back.
        card = self.client.get(response["Location"]).content.decode()
        self.assertIn("Geben Sie sie zuerst an.", card)
        self.assertIn('name="iban"', card)
        self.assertIn(f'<input type="hidden" name="next" value="{back}">'.replace("&", "&amp;"), card)
        # Saving the IBAN leads back to the page with the choice, which then works.
        saved = self.client.post(reverse("customer:bank_connection"), {"iban": GOOD_IBAN, "next": back})
        self.assertRedirects(saved, back, fetch_redirect_response=False)
        page = self.client.get(back).content.decode()
        self.assertEqual(checked_boxes(page), [str(self.feed_in.pk)])
        self.assertIn(f'value="{wish.isoformat()}"', page)
        self.assertEqual(self.client.post(START, self.data([self.feed_in.pk])).status_code, 302)

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

    def test_staff_and_anonymous_visitors_get_nothing(self):
        for path in (START, self.done):
            with self.subTest(path=path):
                self.assertEqual(login(self.staff).get(path).status_code, 404)
                self.assertEqual(login(self.staff).post(path, self.data()).status_code, 404)
                self.assertEqual(Client().get(path).status_code, 302)
                self.assertEqual(Client().post(path, self.data())["Location"], reverse("accounts:login"))
        self.assertEqual(Deregistration.objects.count(), 1)

    def test_a_confirmed_date_is_shown_as_terminated_on(self):
        deregistrations.confirm(self.dereg, self.staff, month_end(add_months(timezone.localdate(), 5)))
        html = self.client.get(self.done).content.decode()
        self.assertIn("Gekündigt zum", html)
        self.assertNotIn("Die LEG bestätigt den Termin.", html)

    def test_after_the_start_of_the_deregistration_the_buildings_page_says_in_deregistration_and_shows_the_note(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertIn(f"Frühestens am {deregistrations.earliest_date_preview().strftime('%d.%m.%Y')}. Die LEG bestätigt den Termin.", html)

    def test_from_the_account_deletion_the_deletion_is_offered_when_it_is_released(self):
        deregistrations.request_deregistration(self.mine, metering_point_ids=[self.feed_in.pk])

        html = self.client.get(self.done, {"for": "account_deletion"}).content.decode()

        self.assertIn("Ihre Abmeldung ist gestartet. Sie können Ihr Konto jetzt löschen.", html)
        self.assertIn(f'href="{reverse("accounts:profile")}?edit=delete#card-delete"', html)

    def test_from_the_account_deletion_a_remaining_blocker_is_explained(self):
        html = self.client.get(self.done, {"for": "account_deletion"}).content.decode()

        # The feed-in is still running without a deregistration: nothing is released.
        self.assertIn("Konto löschen", html)
        self.assertNotIn(f"{reverse('accounts:profile')}?edit=delete", html)


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
