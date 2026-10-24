"""The customer's own IBAN (D-087 amended by D-103): full validation, no password, security event, history without the
value, the mail to every address and the limit of changes."""

from django.conf import settings
from django.core import mail as django_mail
from django.core.exceptions import ValidationError
from django.test import TestCase, tag

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.models import MailJob, SecurityEvent
from leg_platform.community import history, own_iban, persons
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import PersonRecord
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import GOOD_IBAN, OTHER_IBAN, customer


@tag("mariadb")
class OwnIbanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("iban.customer@example.org")

    def test_a_customer_sets_and_reads_the_iban_in_full(self):
        self.assertEqual(own_iban.own_iban_display(self.person), "")
        self.assertTrue(own_iban.set_own_iban(self.person, "ch93 0076 2011 6238 5295 7"))

        self.assertEqual(own_iban.own_iban_display(self.person), "CH93 0076 2011 6238 5295 7")
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)
        # Stored encrypted, not as text.
        self.assertNotIn(GOOD_IBAN, PersonRecord.objects.get(person=self.person).iban_encrypted)

    def test_the_validation_is_complete(self):
        for bad in ("CH9300762011623852958", "CH93", "XX", "DE89 3704 0044 0532 0130", "٣٣" * 11):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                own_iban.set_own_iban(self.person, bad)
        self.assertEqual(own_iban.own_iban(self.person), "")

    def test_an_empty_value_is_refused_and_the_iban_stays(self):
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        for empty in ("", "   ", None):
            with self.subTest(empty=empty), self.assertRaises(persons.PersonError) as caught:
                own_iban.set_own_iban(self.person, empty)
            self.assertEqual(caught.exception.code, "iban_required")
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)

    def test_a_change_writes_event_history_without_the_value_and_one_mail_per_address(self):
        self.person.set_secondary_email("second.address@example.org")
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        MailJob.objects.all().delete()

        own_iban.set_own_iban(self.person, OTHER_IBAN)

        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.IBAN_CHANGED)
        self.assertEqual([(e.account_id, e.actor_id, e.long_retention) for e in events], [(self.person.pk,) * 2 + (True,)] * 2)
        entries = history.entries_for(self.person).filter(event=HistoryEvent.PERSON_IBAN_CHANGED)
        self.assertEqual([entry.detail for entry in entries], [{}, {}])
        jobs = MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED)
        self.assertEqual(
            sorted(jobs.values_list("address", flat=True)),
            sorted(["iban.customer@example.org", "second.address@example.org"]),
        )

    def test_the_first_entry_from_empty_sends_no_mail_but_writes_event_and_history(self):
        own_iban.set_own_iban(self.person, GOOD_IBAN)

        self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.IBAN_CHANGED).count(), 1)
        self.assertEqual(history.entries_for(self.person).filter(event=HistoryEvent.PERSON_IBAN_CHANGED).count(), 1)

    def test_the_removal_mails_every_address_and_an_empty_account_has_nothing_to_remove(self):
        self.assertFalse(own_iban.remove_own_iban(self.person))
        self.person.set_secondary_email("second.address@example.org")
        own_iban.set_own_iban(self.person, GOOD_IBAN)

        self.assertTrue(own_iban.remove_own_iban(self.person))

        self.assertEqual(own_iban.own_iban(self.person), "")
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED).count(), 2)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.IBAN_CHANGED).count(), 2)
        # From empty again: the first entry, no new mail.
        own_iban.set_own_iban(self.person, OTHER_IBAN)
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.IBAN_CHANGED).count(), 2)

    def test_no_change_no_mail_no_event(self):
        own_iban.set_own_iban(self.person, GOOD_IBAN)

        self.assertFalse(own_iban.set_own_iban(self.person, "CH93 0076 2011 6238 5295 7"))

        self.assertEqual(MailJob.objects.count(), 0)
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.IBAN_CHANGED).count(), 1)

    def test_the_mail_text_never_holds_the_iban(self):
        from leg_platform.accounts import mail_queue

        own_iban.set_own_iban(self.person, GOOD_IBAN)
        own_iban.set_own_iban(self.person, OTHER_IBAN)
        mail_queue.process_pending()

        (message,) = django_mail.outbox
        for part in (GOOD_IBAN, OTHER_IBAN, "CH93", "DE89", "0076"):
            self.assertNotIn(part, message.subject + message.body)
        self.assertIn(PortalSettings.instance_origin(), message.body)

    def test_changes_per_hour_are_limited(self):
        ibans = [GOOD_IBAN, OTHER_IBAN, "GB82WEST12345698765432", "LI21088100002324013AA", "FR1420041010050500013M02606"]
        for value in ibans:
            own_iban.set_own_iban(self.person, value)
        with self.assertRaises(persons.PersonError) as caught:
            own_iban.set_own_iban(self.person, "AT611904300234573201")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(own_iban.own_iban(self.person), ibans[-1])

    def test_a_blocked_account_and_an_account_without_a_customer_number_cannot_set_one(self):
        blocked = customer("iban.blocked@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        with self.assertRaises(persons.PersonError):
            own_iban.set_own_iban(blocked, GOOD_IBAN)

    def test_the_service_changes_only_the_account_it_is_given(self):
        other = customer("iban.other@example.org")
        own_iban.set_own_iban(self.person, GOOD_IBAN)
        self.assertEqual(own_iban.own_iban(other), "")
        self.assertEqual(own_iban.own_iban_display(other), "")

    def test_an_undecryptable_value_shows_empty_and_can_be_replaced(self):
        record = PersonRecord.objects.create(person=self.person, iban_encrypted="not a token")
        self.assertEqual(own_iban.own_iban_display(self.person), "")
        self.assertTrue(own_iban.set_own_iban(self.person, GOOD_IBAN))
        record.refresh_from_db()
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)

    def test_staff_data_changes_keep_working_unchanged(self):
        staff = staff_user("iban.staff@example.org")
        persons.update_payment_data(self.person, staff, iban=GOOD_IBAN)
        self.assertEqual(own_iban.own_iban(self.person), GOOD_IBAN)
