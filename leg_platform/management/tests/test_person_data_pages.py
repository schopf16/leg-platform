"""The dialogs that create a person and change the data of a customer (D-087): the pages, their negative authorization, the
guard for the data of staff users and administrators, the CSRF check, the return to a safe address, the answer for a
duplicate email address, the invitation button, and that no IBAN or other value reaches a search, a log or the history.
The rules themselves are at service level (``community/tests/test_person_support.py``)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts import rate_limits
from leg_platform.accounts.models import MailJob, PendingEmailChange, SecurityEvent, VerificationToken
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user, run_mail_queue
from leg_platform.community import history, persons
from leg_platform.community.models import HistoryEntry, PersonRecord
from leg_platform.community.tests.helpers import staff_user

User = get_user_model()
LIST = reverse("management:persons")
NEW = reverse("management:persons_new")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")
GOOD_IBAN = "CH9300762011623852957"
CREATE = {
    "email": "erika.neu@example.org", "salutation": "mrs", "organisation_name": "", "first_name": "Erika",
    "last_name": "Neu", "address_line_1": "", "street": "Neuweg", "house_number": "3", "postal_code": "3000",
    "town": "Bern", "country": "CH", "phone_number": "",
}


@tag("mariadb")
class PersonDataPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.data@example.org")
        cls.administrator = staff_user("administrator.data@example.org", superuser=True)
        cls.other_staff = staff_user("other.data@example.org")
        cls.plain = create_active_user("plain.data@example.org")
        cls.anna = create_active_user(
            "anna.data@example.org", first_name="Anna", last_name="Muster", salutation="mrs", street="Dorfstrasse",
            house_number="1", town="Ittigen", swiss_postal_code=3063, phone_number="+41791234567",
        )
        cls.gone = create_active_user("gone.data@example.org", first_name="Max", last_name="Inaktiv")
        cls.gone.set_status(User.Status.DEACTIVATED)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def url(self, name, person):
        return reverse(f"management:persons_{name}", args=[person.pk])

    # Create a person ------------------------------------------------------------------------------------

    def test_the_list_has_the_button_and_the_dialog_is_a_normal_page_with_the_registration_fields(self):
        client = self.login(self.staff)
        list_page = client.get(LIST)
        self.assertContains(list_page, f'href="{NEW}?next=')
        page = client.get(NEW, {"next": LIST})
        for text in (
            "Person anlegen", "Die Person erhält eine E-Mail mit einem Link", "E-Mail", "Anrede", "Firma", "Vorname",
            "Nachname", "Zweite Person", "Strasse", "Hausnummer", "PLZ", "Ort", "Land", "Telefon", "Abbrechen",
            "csrfmiddlewaretoken", f'<input type="hidden" name="next" value="{LIST}">',
            # The suggestions of the address directory, as in the registration and the profile.
            "data-address-directory", 'src="/static/js/address-directory.js"', "address-suggest",
        ):
            self.assertContains(page, text)
        for field in ("is_staff", "is_superuser", "password", "groups", "customer_number", "status"):
            self.assertNotContains(page, f'name="{field}"')
        self.assertNotContains(page, "Kundennummer")
        self.assertFalse(User.objects.filter(email="erika.neu@example.org").exists())  # looking creates nothing
        self.assertContains(page, f'href="{LIST}"')  # "Abbrechen" goes back where the person came from

    def test_saving_creates_an_invited_person_who_is_listed_at_once_and_goes_back(self):
        client = self.login(self.staff)
        response = client.post(NEW, {**CREATE, "next": LIST})
        person = User.objects.get(email="erika.neu@example.org")
        # The page of the new person opens with the next step; its way back is the list (D-095).
        self.assertRedirects(
            response, f"{self.url('detail', person)}?new=1&next={LIST.replace('/', '%2F')}", fetch_redirect_response=False
        )
        self.assertEqual((person.status, person.customer_number_id, person.has_usable_password()), ("unverified", None, False))
        opened = client.get(self.url("detail", person), {"new": "1"})
        self.assertContains(
            opened, "Person angelegt. Die Einladung wurde gesendet. Ordnen Sie der Person einen Messpunkt zu, sobald sie sich registriert hat."
        )
        page = client.get(LIST)
        for text in ("Erika Neu", "Eingeladen"):
            self.assertContains(page, text)
        self.assertContains(page, f'href="{self.url("detail", person)}"')
        # The list does not show a number for her; the search finds her by name and address.
        self.assertNotContains(client.get(LIST, {"q": "Neuweg"}), "Keine Personen gefunden.")
        # The detail page names the state, offers the button and shows the data that the staff entered.
        detail = client.get(self.url("detail", person))
        self.assertEqual(detail.status_code, 200)
        for text in ("Erika Neu", "Eingeladen", "Einladung erneut senden", "Neuweg 3", "3000 Bern", "Einladung gesendet"):
            self.assertContains(detail, text)
        self.assertContains(detail, self.url("details", person))
        # The history of the person names the actor.
        self.assertEqual([e.actor for e in history.entries_for(person)], [self.staff, self.staff])
        # The email goes out through the queue; then the page says how long the link is valid.
        run_mail_queue()
        self.assertEqual([m.to for m in mail.outbox], [["erika.neu@example.org"]])
        self.assertContains(client.get(self.url("detail", person)), "Der Einladungslink ist bis")
        VerificationToken.objects.filter(user=person).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertContains(client.get(self.url("detail", person)), "Der Einladungslink ist abgelaufen.")

    def test_a_duplicate_address_is_refused_without_telling_who_has_it(self):
        client = self.login(self.staff)
        for email in (self.anna.email, self.anna.email.upper(), self.staff.email, self.plain.email):
            page = client.post(NEW, {**CREATE, "email": email})
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "Diese E-Mail-Adresse wird bereits verwendet.")
            for secret in ("Muster", "Dorfstrasse", self.url("detail", self.anna), "Kundennummer"):
                self.assertNotContains(page, secret)
        self.assertEqual(User.objects.filter(invited_at__isnull=False).count(), 0)
        self.assertFalse(MailJob.objects.exists())

    def test_the_rules_of_the_registration_apply_and_the_form_is_shown_again(self):
        client = self.login(self.staff)
        for data, text in (
            ({"first_name": "", "last_name": ""}, "Geben Sie einen Vornamen oder einen Firmennamen ein."),
            ({"postal_code": "30"}, "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ({"postal_code": "ABCD"}, "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein."),
            ({"email": "no address"}, "Geben Sie eine gültige E-Mail-Adresse ein."),
            ({"street": ""}, "Geben Sie die Strasse ein."),
            ({"town": ""}, "mgmt-field-error"),
            ({"phone_number": "123"}, "mgmt-field-error"),
            ({"country": "XX"}, "mgmt-field-error"),
            ({"salutation": "king"}, "mgmt-field-error"),
        ):
            page = client.post(NEW, {**CREATE, **data})
            self.assertEqual(page.status_code, 200, data)
            self.assertContains(page, text, msg_prefix=str(data))
            # What was typed stays in the form.
            html = page.content.decode()
            self.assertTrue(any(f'value="{typed}"' in html for typed in ("Neuweg", "Erika", "Neu")), msg=str(data))
        self.assertFalse(User.objects.filter(invited_at__isnull=False).exists())
        # A company needs no contact person; a foreign address has a free postal code.
        done = client.post(NEW, {**CREATE, "email": "firma.neu@example.org", "organisation_name": "Beispiel AG", "first_name": "", "last_name": ""})
        self.assertEqual(done.status_code, 302)
        done = client.post(NEW, {**CREATE, "email": "abroad.neu@example.org", "country": "DE", "postal_code": "10115"})
        self.assertEqual(done.status_code, 302)
        self.assertEqual(User.objects.get(email="abroad.neu@example.org").foreign_postal_code, "10115")

    def test_the_account_is_never_privileged_whatever_is_posted(self):
        client = self.login(self.staff)
        client.post(NEW, {**CREATE, "is_staff": "on", "is_superuser": "on", "status": "active", "customer_number": "123456",
                          "groups": "1", "password": "x", "retention_obligation": "on", "invited_at": "2020-01-01"})
        person = User.objects.get(email="erika.neu@example.org")
        self.assertEqual(
            (person.is_staff, person.is_superuser, person.status, person.customer_number_id, person.retention_obligation),
            (False, False, "unverified", None, False),
        )
        self.assertFalse(person.groups.exists() or person.user_permissions.exists() or person.has_usable_password())
        self.assertGreater(person.invited_at, timezone.now() - timedelta(minutes=5))

    def test_a_person_created_from_the_assign_dialog_opens_her_page_and_comes_back_from_there(self):
        client = self.login(self.staff)
        dialog = "/management/metering-points/1/person/"
        response = client.post(NEW, {**CREATE, "next": dialog})
        person = User.objects.get(email="erika.neu@example.org")
        self.assertRedirects(
            response, f"{self.url('detail', person)}?new=1&next=%2Fmanagement%2Fmetering-points%2F1%2Fperson%2F",
            fetch_redirect_response=False,
        )
        self.assertContains(client.get(response["Location"]), f'href="{dialog}"')  # the way back to the dialog

    def test_a_hostile_next_value_never_leads_away_and_the_limit_is_shown(self):
        client = self.login(self.staff)
        for index, hostile in enumerate(HOSTILE):
            response = client.post(NEW, {**CREATE, "email": f"hostile{index}@example.org", "next": hostile})
            person = User.objects.get(email=f"hostile{index}@example.org")
            # The hostile value is ignored: the answer is the page of the new person with the list as the way back.
            self.assertRedirects(
                response, f"{self.url('detail', person)}?new=1&next={LIST.replace('/', '%2F')}",
                fetch_redirect_response=False,
            )
        self.assertEqual(client.get(NEW, {"next": HOSTILE[0]}).context["back_url"], LIST)
        with patch.object(rate_limits, "INVITATION_ACTOR_LIMITS", ((1, timedelta(hours=1)),)):
            page = client.post(NEW, {**CREATE, "email": "limited@example.org"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Zu viele Anfragen")
        self.assertFalse(User.objects.filter(email="limited@example.org").exists())

    # The resend button ----------------------------------------------------------------------------------

    def test_the_invitation_is_sent_again_from_the_detail_page_and_only_for_an_invited_person(self):
        client = self.login(self.staff)
        client.post(NEW, {**CREATE, "next": LIST})
        person = User.objects.get(email="erika.neu@example.org")
        post = self.url("invitation", person)
        with patch.object(rate_limits, "INVITATION_ACCOUNT_LIMITS", ((1000, timedelta(seconds=1)),)):
            response = client.post(post, {"next": self.url("detail", person)})
        self.assertRedirects(response, self.url("detail", person), fetch_redirect_response=False)
        self.assertContains(client.get(self.url("detail", person)), "Die Einladung wurde gesendet.")
        self.assertEqual(MailJob.objects.filter(account=person).count(), 2)
        # The pause: a second click at once is refused with the usual words.
        again = client.post(post, follow=True)
        self.assertContains(again, "Zu viele Anfragen")
        self.assertEqual(MailJob.objects.filter(account=person).count(), 2)
        # A person who completed the registration has no invitation.
        refused = client.post(self.url("invitation", self.anna), follow=True)
        self.assertContains(refused, "Es gibt keine Einladung")
        self.assertNotContains(client.get(self.url("detail", self.anna)), "Einladung erneut senden")
        self.assertEqual(client.get(post).status_code, 405)
        for hostile in HOSTILE:
            response = client.post(post, {"next": hostile})
            self.assertEqual(response["Location"], self.url("detail", person))

    # The dialogs of the cards ----------------------------------------------------------------------------

    def test_the_card_details_is_changed_logged_with_the_field_names_and_shown(self):
        client = self.login(self.staff)
        page = client.get(self.url("details", self.anna))
        for text in ("Angaben bearbeiten", 'value="Anna"', 'value="Muster"', 'name="salutation"', "Zweite Person"):
            self.assertContains(page, text)
        back = self.url("detail", self.anna)
        response = client.post(self.url("details", self.anna), {
            "salutation": "family", "organisation_name": "", "first_name": "Anna", "last_name": "Neumann",
            "address_line_1": "Bernhard Neumann", "next": back,
        })
        self.assertRedirects(response, back, fetch_redirect_response=False)
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.salutation, self.anna.last_name, self.anna.address_line_1), ("family", "Neumann", "Bernhard Neumann"))
        detail = client.get(back)
        for text in ("Gespeichert.", "Familie", "Neumann", "Bernhard Neumann", "Kundendaten geändert: Anrede, Nachname, Zweite Person",
                     self.staff.display_name):
            self.assertContains(detail, text)
        for entry in history.entries_for(self.anna):
            self.assertNotIn("Neumann", str(entry.detail))
        # The names rule of the account: no name and no company is refused, nothing changes.
        page = client.post(self.url("details", self.anna), {
            "salutation": "", "organisation_name": "", "first_name": "", "last_name": "", "address_line_1": "",
        })
        self.assertContains(page, "Geben Sie einen Vornamen oder einen Firmennamen ein.")
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.last_name, "Neumann")

    def test_the_billing_address_has_the_suggestions_the_post_office_box_and_the_rules_of_the_profile(self):
        client = self.login(self.staff)
        page = client.get(self.url("address", self.anna))
        for text in ("Rechnungsadresse bearbeiten", 'value="Dorfstrasse"', 'value="3063"', "data-address-directory", "Postfach",
                     'src="/static/js/address-directory.js"'):
            self.assertContains(page, text)
        data = {"street": "Hauptstrasse", "house_number": "9", "post_office_box_number": "", "postal_code": "3011",
                "town": "Bern", "country": "CH"}
        self.assertEqual(client.post(self.url("address", self.anna), data).status_code, 302)
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.street, self.anna.swiss_postal_code, self.anna.town), ("Hauptstrasse", 3011, "Bern"))
        for bad in ({"postal_code": "12"}, {"town": ""}, {"street": "", "post_office_box_number": ""}, {"country": ""}):
            page = client.post(self.url("address", self.anna), {**data, **bad})
            self.assertEqual(page.status_code, 200, bad)
            self.assertContains(page, "mgmt-field-error", msg_prefix=str(bad))
        # A post office box without a street is an address.
        self.assertEqual(client.post(self.url("address", self.anna), {**data, "street": "", "house_number": "", "post_office_box_number": "77"}).status_code, 302)
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.post_office_box_number, 77)
        self.assertContains(client.get(self.url("detail", self.anna)), "Postfach 77")
        # A foreign country: its postal code is free text and the Swiss one is cleared.
        client.post(self.url("address", self.anna), {**data, "country": "DE", "postal_code": "10115", "town": "Berlin"})
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.country.code, self.anna.swiss_postal_code, self.anna.foreign_postal_code), ("DE", None, "10115"))

    def test_the_contact_card_changes_the_phone_and_asks_the_second_address_by_the_confirmation_link(self):
        client = self.login(self.staff)
        page = client.get(self.url("contact", self.anna))
        for text in ("Kontakt bearbeiten", 'name="phone_number"', 'name="secondary_email"', "Zweite E-Mail"):
            self.assertContains(page, text)
        self.assertNotContains(page, 'name="email"')  # the login address cannot be changed here
        response = client.post(self.url("contact", self.anna), {"phone_number": "031 311 11 11", "secondary_email": "second.data@example.org"}, follow=True)
        self.assertContains(response, "Wir haben einen Bestätigungslink an die neue Adresse gesendet.")
        self.anna.refresh_from_db()
        self.assertEqual((self.anna.phone_number.as_e164, self.anna.email, self.anna.secondary_email), ("+41313111111", "anna.data@example.org", ""))
        self.assertTrue(PendingEmailChange.objects.filter(user=self.anna, address="second.data@example.org").exists())
        self.assertContains(client.get(self.url("detail", self.anna)), "Ausstehend")
        run_mail_queue()
        to_new = [m for m in mail.outbox if m.to == ["second.data@example.org"]]
        self.assertEqual(len(to_new), 1)
        self.assertIn("/email-change/verify/", to_new[0].body)
        self.assertEqual(len([m for m in mail.outbox if "/email-change/verify/" in m.body]), 1)  # the link goes nowhere else
        # The login address as the second address, a bad address, a bad phone: refused.
        for data in ({"secondary_email": self.anna.email}, {"secondary_email": "no address"}, {"phone_number": "123"}):
            page = client.post(self.url("contact", self.anna), {"phone_number": "", "secondary_email": "", **data})
            self.assertContains(page, "mgmt-field-error", msg_prefix=str(data))
        self.assertEqual(self.anna.__class__.objects.get(pk=self.anna.pk).email, "anna.data@example.org")

    def test_a_refused_second_address_keeps_the_phone_of_the_same_dialog_unchanged(self):
        """L-3: the dialog is one transaction; a limit that refuses the second address undoes the phone."""
        client = self.login(self.staff)
        before = self.anna.phone_number
        with patch.object(persons, "change_second_email", side_effect=persons.PersonError("rate_limited")):
            page = client.post(
                self.url("contact", self.anna), {"phone_number": "031 311 11 11", "secondary_email": "x.second@example.org"}
            )
        self.assertContains(page, "Zu viele Anfragen")
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.phone_number, before)
        self.assertFalse(HistoryEntry.objects.filter(subject_id=self.anna.pk).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF).exists())

    def test_the_payment_card_stores_the_iban_encrypted_shows_it_in_groups_and_never_puts_it_elsewhere(self):
        client = self.login(self.staff)
        page = client.get(self.url("payment", self.anna))
        for text in ("Zahlung und Kundennummern bearbeiten", 'name="iban"', 'name="paper_invoice"', 'name="operator_customer_number"'):
            self.assertContains(page, text)
        back = self.url("detail", self.anna)
        response = client.post(self.url("payment", self.anna), {
            "iban": "ch93 0076 2011 6238 5295 7", "paper_invoice": "1", "operator_customer_number": "OP 5", "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        record = PersonRecord.objects.get(person=self.anna)
        self.assertEqual((record.iban, record.paper_invoice, record.operator_customer_number), (GOOD_IBAN, True, "OP 5"))
        self.assertNotIn("CH93", record.iban_encrypted)
        detail = client.get(back)
        self.assertContains(detail, "CH93 0076 2011 6238 5295 7")
        self.assertContains(detail, "Kundendaten geändert: IBAN, Papierrechnung")
        # The dialog shows what is saved.
        self.assertContains(client.get(self.url("payment", self.anna)), 'value="CH93 0076 2011 6238 5295 7"')
        # Neither the history nor the search nor the print dialog of the list holds it.
        for entry in HistoryEntry.objects.all():
            self.assertNotIn("CH93", str(entry.detail))
            self.assertNotIn(GOOD_IBAN, str(entry.detail))
        for query in ("CH93", GOOD_IBAN, "0076 2011", "OP 5"):
            self.assertContains(client.get(LIST, {"q": query}), "Keine Personen gefunden.")
        for text in ("CH93", "0076"):
            self.assertNotContains(client.get(LIST), text)
        # An invalid IBAN is refused with the form shown again.
        for bad in ("CH9300762011623852958", "123", "<b>x</b>"):
            page = client.post(self.url("payment", self.anna), {"iban": bad, "paper_invoice": "0", "operator_customer_number": ""})
            self.assertContains(page, "Geben Sie eine gültige IBAN ein.", msg_prefix=bad)
        self.assertEqual(PersonRecord.objects.get(person=self.anna).iban, GOOD_IBAN)

    def test_the_security_log_and_the_history_name_the_staff_account_and_never_a_value(self):
        client = self.login(self.staff)
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            client.post(self.url("payment", self.anna), {"iban": GOOD_IBAN, "paper_invoice": "1", "operator_customer_number": ""})
            client.post(self.url("details", self.anna), {"salutation": "", "organisation_name": "", "first_name": "Anna",
                                                         "last_name": "Geheim", "address_line_1": ""})
        text = "\n".join(logs.output)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF, actor_id=self.staff.pk, account_id=self.anna.pk).count(), 2)
        self.assertIn("fields iban, paper_invoice", text)
        self.assertIn("fields salutation, last_name", text)
        for secret in (GOOD_IBAN, "CH93", "Geheim", "Anna"):
            self.assertNotIn(secret, text)
        for event in HistoryEntry.objects.filter(subject_type="person", subject_id=self.anna.pk):
            self.assertEqual(event.actor, self.staff)

    # Staff users and administrators ---------------------------------------------------------------------

    def test_the_data_of_a_staff_user_is_changed_only_by_a_system_administrator(self):
        staff_client = self.login(self.staff)
        detail = staff_client.get(self.url("detail", self.other_staff))
        self.assertEqual(detail.status_code, 200)  # every staff account sees every person (D-086)
        for name in ("details", "contact", "address"):
            self.assertNotContains(detail, self.url(name, self.other_staff))
            for method in ("get", "post"):
                response = getattr(staff_client, method)(self.url(name, self.other_staff))
                self.assertEqual(response.status_code, 403, (name, method))
        # The payment dialog stays for the number at the grid operator and offers neither the IBAN nor the paper invoice.
        page = staff_client.get(self.url("payment", self.other_staff))
        self.assertContains(page, 'name="operator_customer_number"')
        self.assertNotContains(page, 'name="iban"')
        self.assertNotContains(page, 'name="paper_invoice"')
        response = staff_client.post(self.url("payment", self.other_staff), {"iban": GOOD_IBAN, "paper_invoice": "1", "operator_customer_number": "OP 2"})
        self.assertEqual(response.status_code, 302)
        record = PersonRecord.objects.get(person=self.other_staff)
        self.assertEqual((record.iban, record.paper_invoice, record.operator_customer_number), ("", False, "OP 2"))
        # One's own account is the account of a staff user as well.
        self.assertEqual(staff_client.post(self.url("details", self.staff), {"first_name": "Self", "last_name": "Edit"}).status_code, 403)
        # A system administrator may change them.
        admin_client = self.login(self.administrator)
        self.assertContains(admin_client.get(self.url("detail", self.other_staff)), self.url("details", self.other_staff))
        response = admin_client.post(self.url("details", self.other_staff), {
            "salutation": "", "organisation_name": "", "first_name": "Staff", "last_name": "Changed", "address_line_1": ""})
        self.assertEqual(response.status_code, 302)
        self.other_staff.refresh_from_db()
        self.assertEqual(self.other_staff.last_name, "Changed")
        # The password, the second factor, the rights and the login address are never touched by any of it.
        self.assertTrue(self.other_staff.is_staff)
        self.assertEqual(self.other_staff.email, "other.data@example.org")

    def test_an_account_in_the_archive_has_no_dialog_for_its_data_and_no_button(self):
        client = self.login(self.administrator)
        detail = client.get(self.url("detail", self.gone))
        self.assertEqual(detail.status_code, 200)
        for name in ("details", "contact", "address"):
            self.assertNotContains(detail, self.url(name, self.gone))
            self.assertEqual(client.get(self.url(name, self.gone)).status_code, 403)
        # The data of the customer is not offered in the payment dialog either: only the number at the grid operator is.
        response = client.post(self.url("payment", self.gone), {"iban": GOOD_IBAN, "paper_invoice": "1", "operator_customer_number": ""})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PersonRecord.objects.filter(person=self.gone).exclude(iban_encrypted="").exists())

    # Negative authorization of every new route -----------------------------------------------------------

    def routes(self):
        return [
            (NEW, "get"), (NEW, "post"),
            *((self.url(name, self.anna), method) for name in ("details", "contact", "address", "payment", "invitation")
              for method in ("get", "post") if not (name == "invitation" and method == "get")),
        ]

    def test_anonymous_visitors_and_accounts_without_staff_status_get_nothing_and_change_nothing(self):
        before = (User.objects.count(), HistoryEntry.objects.count(), MailJob.objects.count(), PersonRecord.objects.count())
        for client, expected in ((Client(), 302), (self.login(self.plain), 404)):
            for url, method in self.routes():
                with self.subTest(user=expected, url=url, method=method):
                    response = getattr(client, method)(url, {**CREATE, "first_name": "Mallory"})
                    self.assertEqual(response.status_code, expected)
                    if expected == 302:
                        self.assertIn("/login/", response["Location"])
                    else:
                        self.assertNotContains(response, "Anna", status_code=404)
        self.assertEqual((User.objects.count(), HistoryEntry.objects.count(), MailJob.objects.count(), PersonRecord.objects.count()), before)
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.first_name, "Anna")

    def test_a_staff_account_without_the_second_factor_is_sent_to_the_setup(self):
        user = staff_user("nosecond.data@example.org")
        client = Client()
        client.force_login(user)
        for url, method in self.routes():
            response = getattr(client, method)(url, CREATE)
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response["Location"], reverse("accounts:two_factor_setup"))
        self.assertFalse(User.objects.filter(email=CREATE["email"]).exists())

    def test_every_post_needs_the_csrf_token(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        for url, method in self.routes():
            if method == "post":
                response = client.post(url, {**CREATE})
                self.assertEqual(response.status_code, 403, url)
        self.assertFalse(User.objects.filter(email=CREATE["email"]).exists())
        self.assertFalse(MailJob.objects.exists())

    def test_forged_ids_and_accounts_that_are_no_persons_are_a_404(self):
        client = self.login(self.staff)
        pending = User.objects.create_user("pending.data@example.org", first_name="P", last_name="P", street="S",
                                           house_number="1", town="Bern", swiss_postal_code=3000)
        for person in (pending,):
            for name in ("details", "contact", "address", "payment"):
                for method in ("get", "post"):
                    self.assertEqual(getattr(client, method)(self.url(name, person), {}).status_code, 404, (name, method))
            self.assertEqual(client.post(self.url("invitation", person)).status_code, 404)
        for name in ("details", "contact", "address", "payment", "invitation"):
            self.assertEqual(client.post(reverse(f"management:persons_{name}", args=[999999])).status_code, 404)
            self.assertEqual(client.post(reverse(f"management:persons_{name}", args=[2**63 + 5])).status_code, 404)
        self.assertEqual(HistoryEntry.objects.count(), 0)
