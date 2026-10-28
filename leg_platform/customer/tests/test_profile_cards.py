"""The cards of the profile that belong to the customer (kundenprofil/PROFIL.md; D-059, D-062, D-105): the address with the EGID
and its lock, the card "Bank or postal connection" (no password, no code; mail to every address on a change), the statuses of
the email and two-factor cards, and the deletion of the account with its release by the deregistration. Negative cases: a
staff account has no bank card and a 404 at its route, an unsafe way back is dropped, a wrong IBAN or too many changes are
refused, the deletion is refused (before any proof is used up) while a metering point runs."""

import re
from datetime import date, timedelta
from unittest.mock import patch

from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import AuthenticatorDevice, MailJob, PendingEmailChange, SecurityEvent, User
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient
from leg_platform.community import deregistrations, own_iban
from leg_platform.community.models import Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.customer import limits
from leg_platform.customer.tests.helpers import (
    GOOD_IBAN,
    OTHER_IBAN,
    customer,
    login,
)

PROFILE = reverse("accounts:profile")
BANK = reverse("customer:bank_connection")
DELETE = reverse("accounts:account_delete")
LONG_AGO = date.today() - timedelta(days=400)
HINT = "Die IBAN steht auf Ihrem Konto- oder Zahlungsauszug und auf der Rückseite Ihrer Bankkarte."


def active_point(last, person, location, direction=Direction.CONSUMPTION, start=LONG_AGO):
    point = make_metering_point(
        last, direction, location, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO, start_date=start
    )
    assign_person(point, person, valid_from=LONG_AGO)
    return point


def card(html, name):
    """The markup of one card of the profile."""
    found = re.search(rf'<section class="profile-card[^"]*" id="card-{name}".*?</section>', html, re.S)
    assert found, f"No card {name}"
    return found.group(0)


@tag("mariadb")
class BankCardTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.bank@example.org")
        cls.other = customer("other.bank@example.org")
        cls.staff = staff_user("staff.bank@example.org")

    def setUp(self):
        self.client = login(self.mine)

    def page(self, query="", client=None):
        response = (client or self.client).get(PROFILE + query)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_card_comes_directly_after_the_personal_details_and_says_not_on_file(self):
        html = self.page()

        positions = [html.index(f'id="card-{name}"') for name in ("personal", "bank", "email", "password", "two-factor", "delete")]
        self.assertEqual(positions, sorted(positions))
        bank = card(html, "bank")
        for text in ("Bank- oder Postverbindung", "IBAN", "Nicht hinterlegt", "Ändern"):
            self.assertIn(text, bank)

    def test_the_iban_is_shown_in_full_with_its_status_and_only_the_own_one(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        own_iban.set_own_iban(self.other, OTHER_IBAN)

        html = self.page()

        bank = card(html, "bank")
        self.assertIn("CH93 0076 2011 6238 5295 7", bank)
        self.assertIn("Hinterlegt", bank)
        self.assertNotIn("Nicht hinterlegt", bank)
        self.assertNotIn("DE89", html)

    def test_change_opens_the_field_in_the_same_card_with_the_hint_and_no_proof(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)

        bank = card(self.page("?edit=bank"), "bank")

        self.assertIn('data-state="edit"', bank)
        self.assertRegex(bank, r'<input[^>]*name="iban"[^>]*value="CH93 0076 2011 6238 5295 7"')
        self.assertIn(HINT, bank)
        self.assertIn("Speichern", bank)
        self.assertIn(f'action="{BANK}"', bank)
        for proof in ("current_password", "otp_token"):
            self.assertNotIn(proof, bank)

    def test_saving_needs_no_password_and_no_code_and_the_first_entry_sends_no_mail(self):
        response = self.client.post(BANK, {"iban": "ch93 0076 2011 6238 5295 7"})

        self.assertRedirects(response, f"{PROFILE}#card-bank", fetch_redirect_response=False)
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED).count(), 0)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.IBAN_CHANGED, account_id=self.mine.pk).count(), 1)
        self.assertIn("Gespeichert.", card(self.page(), "bank"))

    def test_a_change_of_an_existing_iban_is_mailed_to_every_address(self):
        self.mine.set_secondary_email("second.bank@example.org")
        own_iban.set_own_iban(self.mine, GOOD_IBAN)

        self.client.post(BANK, {"iban": OTHER_IBAN})

        self.assertEqual(own_iban.own_iban(self.mine), OTHER_IBAN)
        self.assertEqual(
            sorted(MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED).values_list("address", flat=True)),
            ["mine.bank@example.org", "second.bank@example.org"],
        )

    def test_an_invalid_empty_or_foreign_value_is_refused_and_nothing_is_stored(self):
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        for value in ("CH93 0076 2011 6238 5295 8", "not an iban", "", "   ", "x" * 200):
            with self.subTest(value=value):
                response = self.client.post(BANK, {"iban": value})
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn('data-state="edit"', card(html, "bank"))
                self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        self.assertEqual(own_iban.own_iban(self.mine), GOOD_IBAN)

    def test_the_sixth_change_in_an_hour_is_refused_with_a_message(self):
        values = [GOOD_IBAN, OTHER_IBAN] * 3
        for value in values[:5]:
            self.client.post(BANK, {"iban": value})
        response = self.client.post(BANK, {"iban": values[5]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sie haben Ihre Bankverbindung zu oft geändert.", response.content.decode())
        self.assertEqual(own_iban.own_iban(self.mine), values[4])

    def test_the_request_budget_the_csrf_token_and_the_body_limit_hold(self):
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)
        self.assertEqual(guarded.post(BANK, {"iban": GOOD_IBAN}).status_code, 403)
        with patch.object(limits, "allow_wizard", return_value=False):
            limited = self.client.post(BANK, {"iban": GOOD_IBAN})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(self.client.post(BANK, data="iban=" + "a" * 20000, content_type="application/x-www-form-urlencoded").status_code, 413)
        self.assertEqual(own_iban.own_iban(self.mine), "")

    def test_a_staff_account_with_a_number_has_the_card_and_the_route(self):
        # D-113: staff accounts take part like everybody else, bank connection included.
        staff_client = login(self.staff)
        html = self.page(client=staff_client)
        self.assertIn('id="card-bank"', html)
        self.assertIn('id="card-bank"', self.page("?edit=bank", staff_client))
        self.assertEqual(staff_client.get(BANK).status_code, 302)
        self.assertEqual(staff_client.post(BANK, {"iban": GOOD_IBAN}).status_code, 302)
        self.assertEqual(own_iban.own_iban(self.staff), GOOD_IBAN)
        self.assertEqual(own_iban.own_iban(self.mine), "")

    def test_an_account_that_does_not_take_part_has_no_card_and_no_route(self):
        staff_client = login(self.staff)
        with patch("leg_platform.customer.access.is_customer", return_value=False):
            self.assertNotIn('id="card-bank"', self.page(client=staff_client))
            self.assertNotIn('id="card-bank"', self.page("?edit=bank", staff_client))
            self.assertEqual(staff_client.post(BANK, {"iban": GOOD_IBAN}).status_code, 404)
            self.assertEqual(staff_client.get(BANK).status_code, 404)
        self.assertEqual(own_iban.own_iban(self.staff), "")

    def test_an_anonymous_visitor_is_sent_to_the_login(self):
        self.assertEqual(Client().post(BANK, {"iban": GOOD_IBAN})["Location"], reverse("accounts:login"))

    def test_the_way_back_is_taken_only_if_it_is_a_page_of_the_customer_area(self):
        back = reverse("customer:deregistration_start") + "?point=7"

        response = self.client.post(BANK, {"iban": GOOD_IBAN, "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)

        for unsafe in ("https://evil.example/", "//evil.example/", "/profile/", "javascript:alert(1)", "/participation/zzz/"):
            with self.subTest(unsafe=unsafe):
                response = self.client.post(BANK, {"iban": OTHER_IBAN if unsafe.endswith("/") else GOOD_IBAN, "next": unsafe})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], f"{PROFILE}#card-bank")

    def test_the_card_carries_a_safe_way_back_only(self):
        safe = card(self.page("?edit=bank&next=%2Fparticipation%2Fbuildings%2F"), "bank")
        self.assertIn('<input type="hidden" name="next" value="/participation/buildings/">', safe)
        self.assertIn('href="/participation/buildings/"', safe)
        unsafe = card(self.page("?edit=bank&next=https%3A%2F%2Fevil.example%2F"), "bank")
        self.assertNotIn("evil.example", unsafe)
        self.assertNotIn('name="next"', unsafe)

    def test_an_error_shows_the_card_again_with_the_way_back(self):
        response = self.client.post(BANK, {"iban": "nonsense", "next": "/participation/buildings/"})

        bank = card(response.content.decode(), "bank")
        self.assertIn('name="next" value="/participation/buildings/"', bank)

    def test_a_get_of_the_route_leads_to_the_card(self):
        response = self.client.get(BANK, {"next": "/participation/buildings/"})
        self.assertRedirects(
            response, f"{PROFILE}?edit=bank&next=%2Fparticipation%2Fbuildings%2F#card-bank", fetch_redirect_response=False
        )


@tag("mariadb")
class PersonalCardTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer(
            "mine.address@example.org", street="Musterstrasse", house_number="1", swiss_postal_code=3063, town="Ittigen"
        )
        cls.other = customer("other.address@example.org")

    def building(self, person=None, **extra):
        location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234, **extra)
        active_point(1, person or self.mine, location)
        return location

    def get(self, query=""):
        return login(self.mine).get(PROFILE + query).content.decode()

    def post(self, **extra):
        data = {
            "first_name": "Anna", "last_name": "Example", "street": "Musterstrasse", "house_number": "1",
            "postal_code": "3063", "town": "Ittigen", "country": "CH", **extra,
        }
        return login(self.mine).post(reverse("accounts:profile_edit"), data)

    def test_the_egid_stands_in_the_address_and_an_unconfirmed_address_can_be_changed(self):
        self.building(needs_confirmation=True)

        html = card(self.get(), "personal")

        # The EGID has its own row; the note about the confirmed address is shown only in the edit state.
        self.assertIn("EGID", html)
        self.assertIn("90001234", html)
        self.assertNotIn("Die Adresse ist bestätigt", html)
        self.assertNotRegex(card(self.get("?edit=personal"), "personal"), r'<input[^>]*name="street"[^>]*disabled')
        self.assertEqual(self.post(street="Neuweg").status_code, 302)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.street, "Neuweg")

    def test_after_the_confirmation_the_address_is_locked_and_the_note_is_in_the_edit_state(self):
        self.building()

        html = card(self.get(), "personal")
        self.assertIn("90001234", html)
        self.assertNotIn("Die Adresse ist bestätigt", html)
        edit = card(self.get("?edit=personal"), "personal")
        self.assertIn("Die Adresse ist bestätigt und kann nicht geändert werden. Wenden Sie sich bei einer Änderung an die LEG.", edit)
        for name in ("street", "house_number", "postal_code", "town"):
            self.assertRegex(edit, rf'<input[^>]*name="{name}"[^>]*disabled')
        # Whatever the browser posts, the address stays; the name can still be changed.
        response = self.post(first_name="Berta", street="Hackweg", house_number="99", postal_code="8000", town="Zürich")
        self.assertEqual(response.status_code, 302)
        self.mine.refresh_from_db()
        self.assertEqual(
            (self.mine.first_name, self.mine.street, self.mine.house_number, self.mine.swiss_postal_code, self.mine.town),
            ("Berta", "Musterstrasse", "1", 3063, "Ittigen"),
        )

    def test_a_locked_address_has_no_suggestions_no_notice_and_a_forged_post_changes_nothing(self):
        self.building()

        locked = self.get("?edit=personal")
        # No widget, no panel, no notice: nothing can fill a field that is disabled (the script is not asked to).
        for marker in ("data-address-directory", "data-address-notice", "address-assist", "address-suggest", "data-endpoint"):
            self.assertNotIn(marker, locked)
        self.assertIn('id="address-locked-note"', locked)
        # The same page for an address that is not locked has them all.
        other = login(self.other).get(PROFILE + "?edit=personal").content.decode()
        self.assertEqual(other.count("data-address-directory"), 1)
        self.assertEqual(other.count("data-address-notice"), 1)
        # Whatever is posted for the address fields (also the country), the stored address stays and the name changes.
        response = self.post(first_name="Berta", street="Hackweg", house_number="99", postal_code="8000", town="Zürich", country="DE")
        self.assertEqual(response.status_code, 302)
        self.mine.refresh_from_db()
        self.assertEqual(
            (self.mine.first_name, self.mine.street, self.mine.house_number, self.mine.swiss_postal_code, self.mine.town,
             self.mine.country.code),
            ("Berta", "Musterstrasse", "1", 3063, "Ittigen", "CH"),
        )

    def test_the_lookup_endpoint_is_not_a_way_around_the_lock(self):
        # The endpoint answers public register data and writes nothing: a locked address cannot be changed through it.
        self.building()
        before = (self.mine.street, self.mine.house_number, self.mine.swiss_postal_code, self.mine.town)
        client = login(self.mine)
        for mode in ("suggest", "check", "locality"):
            client.post(
                reverse("addresses:lookup"),
                "mode=%s&street=Hackweg&house_number=99&postal_code=8000&locality=Zurich&country=CH" % mode,
                content_type="application/x-www-form-urlencoded",
            )
        self.mine.refresh_from_db()
        self.assertEqual((self.mine.street, self.mine.house_number, self.mine.swiss_postal_code, self.mine.town), before)

    def test_without_a_building_of_the_account_there_is_no_egid_and_no_lock(self):
        # Another account's building at the same address is not the account's building.
        self.building(person=self.other)

        html = card(self.get(), "personal")

        self.assertNotIn("EGID", html)
        self.assertNotIn("Die Adresse ist bestätigt", html)
        self.assertEqual(self.post(street="Neuweg").status_code, 302)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.street, "Neuweg")

    def test_a_building_at_another_address_or_an_archived_one_shows_nothing(self):
        other_place = make_location("Nebenweg", "4", "3063", "Ittigen", egid=90005555)
        active_point(2, self.mine, other_place)
        self.assertNotIn("EGID", card(self.get(), "personal"))
        make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234, is_archived=True)
        self.assertNotIn("EGID", card(self.get(), "personal"))


@tag("mariadb")
class EmailAndTwoFactorCardTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.cards@example.org")

    def get(self, user=None, client=None):
        client = client or login(user or self.mine)
        return client.get(PROFILE).content.decode()

    def test_the_email_card_has_the_statuses_and_the_button_change(self):
        email = card(self.get(), "email")

        self.assertIn("Login (Benutzername)", email)
        self.assertIn("Bestätigt", email)
        self.assertIn("nicht hinterlegt", email)
        self.assertIn("Erhält Informationen, nicht für die Anmeldung.", email)
        self.assertIn("Ändern", email)
        self.assertNotIn("Bearbeiten", email)

    def test_a_pending_address_is_pending_with_its_deadline_and_can_be_cancelled(self):
        client = login(self.mine)
        client.post(reverse("accounts:email_change_request"), {
            "email": "mine.cards.new@example.org", "secondary_email": "", "current_password": PASSWORD,
        })
        self.assertTrue(PendingEmailChange.objects.filter(user=self.mine).exists())

        email = card(self.get(client=client), "email")

        self.assertIn("Ausstehend", email)
        self.assertIn("Anfrage abbrechen", email)
        self.assertIn("Bis dahin gilt weiter mine.cards@example.org.", email)

    def test_the_sign_in_card_names_the_state_and_a_customer_changes_it_in_the_flow(self):
        user = customer("tf.cards@example.org")
        html = card(self.get(user), "two-factor")
        self.assertIn("Nur Passwort", html)
        self.assertIn('href="/profile/sign-in-method/">Ändern</a>', html)
        AuthenticatorDevice.objects.create_device(user, confirmed=True)
        html = card(self.get(user), "two-factor")
        self.assertIn("Passwort und Zahlencode", html)
        self.assertIn('href="/profile/sign-in-method/">Ändern</a>', html)
        self.assertIn("Neue Codes erzeugen", html)
        # Turning the number code off is a step of the flow, not a button of the card.
        self.assertNotIn("2FA deaktivieren", html)
        self.assertNotIn("kann nicht abgeschaltet werden", html)

    def test_a_staff_account_keeps_the_sentence_that_two_factor_cannot_be_turned_off(self):
        staff = staff_user("staff.cards@example.org")
        AuthenticatorDevice.objects.create_device(staff, confirmed=True)

        html = card(self.get(staff), "two-factor")

        self.assertIn("kann nicht abgeschaltet werden", html)
        self.assertNotIn("2FA deaktivieren", html)


@tag("mariadb")
class DeleteCardTests(TestCase):
    """The deletion is always possible (owner decision 2026-10-24): the card is one sentence that leads to the page
    "Delete account"; the page offers "Messpunkte abmelden" for a customer with running points; the deletion deregisters
    the running points by itself and deactivates the account while an assignment is in force."""

    @classmethod
    def setUpTestData(cls):
        cls.location = make_location()

    def setUp(self):
        self.mine = customer("mine.delete@example.org")
        self.client = login(self.mine)

    def test_the_card_is_one_sentence_and_leads_to_the_page(self):
        html = card(self.client.get(PROFILE).content.decode(), "delete")

        self.assertIn(f'href="{DELETE}"', html)
        self.assertNotIn('name="current_password"', html)
        self.assertIn("Sie können Ihr Konto jederzeit löschen.", html)

    def test_the_page_offers_the_deregistration_next_to_the_button_for_a_customer_with_points(self):
        active_point(1, self.mine, self.location)

        html = self.client.get(DELETE).content.decode()

        self.assertIn("Konto unwiderruflich löschen", html)
        self.assertIn(f'href="{reverse("customer:deregistration_start")}"', html)
        self.assertIn("Messpunkte abmelden", html)

    def test_a_running_point_is_deregistered_by_the_deletion_and_the_account_is_deactivated(self):
        point = active_point(1, self.mine, self.location)

        response = self.client.post(DELETE, {"current_password": PASSWORD, "confirm": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/account_deleted.html")
        self.mine.refresh_from_db()
        # The assignment is in force until the earliest date: the account is only deactivated, its sign-in is gone.
        self.assertEqual(self.mine.status, User.Status.DEACTIVATED)
        self.assertFalse(self.mine.has_usable_password())
        self.assertTrue(
            deregistrations.Deregistration.objects.filter(person=self.mine, items__metering_point=point).exists()
        )

    def test_an_account_without_points_is_deleted_at_once(self):
        response = self.client.post(DELETE, {"current_password": PASSWORD, "confirm": "on"})

        self.assertTemplateUsed(response, "accounts/account_deleted.html")
        self.assertFalse(User.objects.filter(pk=self.mine.pk).exists())
