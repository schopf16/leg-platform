"""Creating a person and the changes of the customer's data by the staff (D-087), at service level: the invitation (the
account without password and customer number, the queued email, the link, the limits, the duplicates), the changes of
the cards (only the fields of the card, the guards of the account model, the staff guard of D-012, the history and the
security log with field names only), the IBAN stored encrypted, and the second email address through the existing
confirmation mechanism. No client: the dialogs are in ``management/tests/test_person_data_pages.py``, the steps that the
person takes in ``accounts/tests/test_invitations.py``."""

import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase, override_settings, tag
from django.utils import timezone

from leg_platform.accounts import email_change, invitations, rate_limits
from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.login_protection import axes_client_ip
from leg_platform.accounts.models import AccountIntegrityError, MailJob, PendingEmailChange, SecurityEvent, VerificationToken
from leg_platform.accounts.tests.helpers import create_active_user, run_mail_queue, verification_path
from leg_platform.community import history, iban, person_list, persons
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import HistoryEntry, PersonRecord
from leg_platform.community.tests.helpers import staff_user

User = get_user_model()
GOOD_IBAN = "CH9300762011623852957"
DATA = {
    "salutation": "mrs", "first_name": "Erika", "last_name": "Neu", "street": "Neuweg", "house_number": "3",
    "swiss_postal_code": 3000, "town": "Bern", "country": "CH", "phone_number": "+41311234567",
}


def request_of(user):
    """A request of the staff account, for the limits of the mail requests (the address of the client)."""
    request = RequestFactory().post("/management/persons/", REMOTE_ADDR="198.51.100.7")
    request.user = user
    return request


@tag("mariadb")
class CreatePersonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.support@example.org")
        cls.plain = create_active_user("plain.support@example.org")
        cls.active = create_active_user("active.support@example.org")
        cls.blocked = create_active_user("blocked.support@example.org")
        cls.blocked.set_status(User.Status.BLOCKED)
        cls.gone = create_active_user("gone.support@example.org")
        cls.gone.set_status(User.Status.DEACTIVATED)

    def create(self, email="neu.person@example.org", **values):
        return persons.create_person(self.staff, email, {**DATA, **values})

    def test_a_person_is_created_without_password_customer_number_or_privileges_and_invited(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            person = self.create("  Neu.Person@Example.ORG ")
        person = User.objects.get(pk=person.pk)
        self.assertEqual((person.email, person.status, person.is_active), ("neu.person@example.org", "unverified", False))
        self.assertFalse(person.has_usable_password())
        self.assertIsNone(person.customer_number_id)  # issued when the person completes the registration (D-021)
        self.assertEqual((person.is_staff, person.is_superuser, person.retention_obligation), (False, False, False))
        self.assertFalse(person.groups.exists() or person.user_permissions.exists())
        self.assertIsNotNone(person.invited_at)
        self.assertEqual((person.first_name, person.swiss_postal_code, person.phone_number.as_e164), ("Erika", 3000, "+41311234567"))
        # It is a person of the list, with its own state, from the moment of its creation.
        self.assertTrue(persons.person_queryset().filter(pk=person.pk).exists())
        row = person_list.base_queryset().get(pk=person.pk)
        self.assertEqual(person_list.account_state(row), person_list.AccountState.INVITED)
        self.assertEqual(person_list.AccountState.INVITED.label, "Eingeladen")
        # The history and the security log name the actor and the account, never a value.
        self.assertEqual(
            [(e.event, e.actor) for e in history.entries_for(person)],
            [(HistoryEvent.CREATED, self.staff), (HistoryEvent.PERSON_INVITATION_SENT, self.staff)],
        )
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.PERSON_INVITED)
        self.assertEqual((event.account_id, event.actor_id), (person.pk, self.staff.pk))
        line = "\n".join(logs.output)
        self.assertIn(f"account {person.pk}", line)
        self.assertIn(f"actor {self.staff.pk}", line)
        for secret in ("Erika", "Neu", "Neuweg", "neu.person", "1234567"):
            self.assertNotIn(secret, line)

    def test_the_invitation_mark_is_a_controlled_field_that_nothing_but_the_creation_writes(self):
        """L-1: like the status or the rights, it cannot be changed by a save, a bulk update or a plain creation."""
        person = self.create()
        stale = User.objects.get(pk=person.pk)
        stale.invited_at = None
        with self.assertRaises(AccountIntegrityError):
            stale.save()
        with self.assertRaises(AccountIntegrityError):
            User.objects.filter(pk=person.pk).update(invited_at=None)
        self.plain.invited_at = timezone.now()
        with self.assertRaises(AccountIntegrityError):
            self.plain.save()
        with self.assertRaises(AccountIntegrityError):
            User(email="mark.person@example.org", invited_at=timezone.now()).save()
        self.assertIsNotNone(User.objects.get(pk=person.pk).invited_at)
        self.assertIsNone(User.objects.get(pk=self.plain.pk).invited_at)
        # Every other save of the account leaves the mark as it is.
        again = User.objects.get(pk=person.pk)
        again.first_name = "Erica"
        again.save()
        self.assertIsNotNone(User.objects.get(pk=person.pk).invited_at)

    def test_only_a_staff_account_creates_a_person(self):
        for actor in (self.plain, self.gone, self.blocked, None):
            with self.subTest(actor=getattr(actor, "email", None)):
                with self.assertRaises(persons.PersonError) as caught:
                    persons.create_person(actor, "x.person@example.org", DATA)
                self.assertEqual(caught.exception.code, "not_allowed")
        # The staff status is read from the database: an object that still says "staff" after the demotion is no use.
        stale = User.objects.get(pk=self.staff.pk)
        self.staff.set_privileges(is_staff=False, is_superuser=False)
        with self.assertRaises(persons.PersonError):
            persons.create_person(stale, "y.person@example.org", DATA)
        self.assertFalse(User.objects.filter(email__in=("x.person@example.org", "y.person@example.org")).exists())

    def test_the_account_logic_accepts_only_the_registration_data_and_never_a_privilege(self):
        for forbidden in ("is_staff", "is_superuser", "status", "customer_number", "retention_obligation", "invited_at",
                          "password", "groups", "title", "secondary_email"):
            with self.subTest(field=forbidden):
                with self.assertRaises(ValueError):
                    User.objects.create_invited_user("forbidden.person@example.org", **{**DATA, forbidden: True})
        # The plain way of creating an account cannot mark an invitation either.
        with self.assertRaises(ValueError):
            User.objects.create_user("forbidden.person@example.org", invited_at=timezone.now())
        self.assertFalse(User.objects.filter(email="forbidden.person@example.org").exists())

    def test_the_data_follows_the_rules_of_the_registration_and_nothing_is_created_otherwise(self):
        before = User.objects.count()
        for bad in (
            {"first_name": "", "last_name": ""},  # a name or a company
            {"street": "", "post_office_box_number": None},
            {"town": ""},
            {"swiss_postal_code": 99},  # four digits from 1000
            {"country": "DE", "swiss_postal_code": 3000},  # a foreign address has the foreign postal code
            {"phone_number": "123"},
            {"first_name": "x" * 31},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.create("bad.person@example.org", **bad)
        self.assertEqual(User.objects.count(), before)
        # A company without a contact person is valid; a foreign address with its postal code too.
        self.create("company.person@example.org", organisation_name="Beispiel AG", first_name="", last_name="")
        self.create("abroad.person@example.org", country="DE", swiss_postal_code=None, foreign_postal_code="10115")
        with self.assertRaises(persons.PersonError) as caught:
            self.create("not an address")
        self.assertEqual(caught.exception.code, "email_invalid")

    def test_an_address_in_use_is_refused_without_telling_anything_about_the_other_account(self):
        for account in (self.active, self.blocked, self.staff):
            with self.subTest(account=account.email), self.assertRaises(persons.PersonError) as caught:
                self.create(account.email.upper())
            self.assertEqual(str(caught.exception), "email_in_use")
        invited = self.create("invited.person@example.org")
        with self.assertRaises(persons.PersonError) as caught:
            self.create("invited.person@example.org")
        self.assertEqual(caught.exception.code, "email_in_use")
        self.assertEqual(User.objects.filter(email="invited.person@example.org").count(), 1)
        self.assertEqual(MailJob.objects.filter(account=invited).count(), 1)
        # An address that only a deactivated account had is free (D-016); a pending self-registration does not hold it.
        self.create(self.gone.email)
        pending = User.objects.create_user("pending.person@example.org")
        replaced = self.create("pending.person@example.org")
        self.assertFalse(User.objects.filter(pk=pending.pk).exists())
        self.assertIsNotNone(User.objects.get(pk=replaced.pk).invited_at)

    def test_the_limits_apply_and_a_refusal_leaves_nothing_behind(self):
        with patch.object(rate_limits, "INVITATION_ACTOR_LIMITS", ((1, timedelta(hours=1)),)):
            self.create("first.person@example.org")
            with self.assertRaises(persons.PersonError) as caught:
                self.create("second.person@example.org")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertFalse(User.objects.filter(email="second.person@example.org").exists())
        self.assertEqual(MailJob.objects.filter(kind=MailJob.Kind.INVITATION).count(), 1)
        self.assertFalse(HistoryEntry.objects.filter(subject_type="person").exclude(actor=self.staff).exists())
        # The limit of the address is the one of every email (three in ten minutes); no limit of the client address.
        person = User.objects.get(email="first.person@example.org")
        self.assertEqual(rate_limits.MAIL_IP_LIMITS, ((20, timedelta(hours=1)),))
        with patch.object(rate_limits, "INVITATION_ACCOUNT_LIMITS", ((1000, timedelta(seconds=1)),)):
            for _ in range(2):
                persons.resend_invitation(person, self.staff)
            with self.assertRaises(persons.PersonError) as caught:
                persons.resend_invitation(person, self.staff)
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_the_worker_sends_one_neutral_email_with_a_link_that_is_valid_for_seven_days(self):
        person = self.create()
        job = MailJob.objects.get(account=person)
        self.assertEqual((job.kind, job.address, job.suppressed), (MailJob.Kind.INVITATION, "neu.person@example.org", False))
        self.assertFalse(VerificationToken.objects.filter(user=person).exists())  # no token is stored in a job
        run_mail_queue()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["neu.person@example.org"])
        path = verification_path(message)
        token = VerificationToken.objects.get(user=person)
        self.assertEqual(token.purpose, VerificationToken.Purpose.REGISTRATION)
        self.assertAlmostEqual(
            (token.expires_at - timezone.now()).total_seconds(), invitations.INVITATION_LIFETIME.total_seconds(), delta=60
        )
        # The text names no data of the account.
        for secret in ("Erika", "Neuweg", "Bern", "1234567"):
            self.assertNotIn(secret, message.subject + message.body)
        self.assertIn("7", message.body)
        self.assertFalse(MailJob.objects.filter(account=person).exists())
        self.assertFalse(token.digest in path)  # the digest is not the link
        # A job for an account that is no invitation any more sends nothing.
        MailJob.objects.create(kind=MailJob.Kind.INVITATION, address=self.active.email, account=self.active, next_attempt_at=timezone.now())
        mail.outbox.clear()
        run_mail_queue()
        self.assertEqual(mail.outbox, [])

    def test_sending_again_replaces_the_link_and_is_only_for_an_invited_person(self):
        person = self.create()
        run_mail_queue()
        first = verification_path(mail.outbox[0])
        with patch.object(rate_limits, "INVITATION_ACCOUNT_LIMITS", ((1000, timedelta(seconds=1)),)):
            persons.resend_invitation(person, self.staff)
        run_mail_queue()
        second = verification_path(mail.outbox[1])
        self.assertNotEqual(first, second)
        self.assertEqual(VerificationToken.objects.filter(user=person).count(), 1)  # one valid link at a time
        self.assertIsNone(VerificationToken.objects.find_valid(first.rstrip("/").rsplit("/", 1)[1], VerificationToken.Purpose.REGISTRATION))
        self.assertIsNotNone(VerificationToken.objects.find_valid(second.rstrip("/").rsplit("/", 1)[1], VerificationToken.Purpose.REGISTRATION))
        self.assertEqual(
            [e.event for e in history.entries_for(person)],
            [HistoryEvent.CREATED, HistoryEvent.PERSON_INVITATION_SENT, HistoryEvent.PERSON_INVITATION_SENT],
        )
        self.assertEqual(SecurityEvent.objects.filter(event=SecurityEvent.Event.INVITATION_RESENT, account_id=person.pk).count(), 1)
        for other in (self.active, self.blocked):
            with self.assertRaises(persons.PersonError) as caught:
                persons.resend_invitation(other, self.staff)
            self.assertIn(caught.exception.code, ("not_invited", "not_a_person"))
        with self.assertRaises(persons.PersonError):
            persons.resend_invitation(person, self.plain)

    def test_an_expired_invitation_can_be_sent_again_and_is_deleted_like_every_unverified_account(self):
        from leg_platform.accounts import registration

        person = self.create()
        # The email is queued but not sent: the account is kept although it has no link yet.
        self.assertEqual(registration.delete_expired_registrations(), (0, 0))
        self.assertEqual(persons.invitation_state(person), {"expires_at": None, "sending": True})
        run_mail_queue()
        self.assertEqual(registration.delete_expired_registrations(), (0, 0))
        state = persons.invitation_state(person)
        self.assertIsNotNone(state["expires_at"])
        VerificationToken.objects.filter(user=person).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(persons.invitation_state(person), {"expires_at": None, "sending": False})
        with patch.object(rate_limits, "INVITATION_ACCOUNT_LIMITS", ((1000, timedelta(seconds=1)),)):
            persons.resend_invitation(person, self.staff)
        run_mail_queue()
        self.assertIsNotNone(persons.invitation_state(person)["expires_at"])
        # Without a new invitation the account and its data are deleted once the link has expired (D-016).
        VerificationToken.objects.filter(user=person).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(registration.delete_expired_registrations(), (1, 0))
        self.assertFalse(User.objects.filter(pk=person.pk).exists())

    def test_the_person_who_registers_the_address_themselves_gets_a_link_and_the_invitation_stays(self):
        from leg_platform.accounts import registration

        person = self.create()
        run_mail_queue()
        mail.outbox.clear()
        send = registration.process_registration("neu.person@example.org")
        send()
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(User.objects.filter(pk=person.pk, invited_at__isnull=False, first_name="Erika").exists())
        self.assertEqual(VerificationToken.objects.filter(user=person).count(), 1)
        self.assertEqual(len(re.findall(r"/register/verify/", mail.outbox[0].body)), 1)


@tag("mariadb")
class ChangeCustomerDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("staff.change@example.org")
        cls.other_staff = staff_user("other.change@example.org")
        cls.administrator = staff_user("administrator.change@example.org", superuser=True)
        staff_user("second.change@example.org", superuser=True)
        cls.customer = create_active_user(
            "customer.change@example.org", first_name="Anna", last_name="Muster", salutation="mrs", street="Dorfstrasse",
            house_number="1", town="Ittigen", swiss_postal_code=3063,
        )
        cls.blocked = create_active_user("blocked.change@example.org")
        cls.blocked.set_status(User.Status.BLOCKED)
        cls.gone = create_active_user("gone.change@example.org")
        cls.gone.set_status(User.Status.DEACTIVATED)

    def reload(self, person):
        return User.objects.get(pk=person.pk)

    def test_only_the_fields_of_the_card_that_changed_are_written_logged_and_never_the_customer_told(self):
        before = self.reload(self.customer)
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            saved, changed = persons.update_customer_data(
                self.customer, persons.GROUP_DETAILS,
                {"salutation": "mr", "organisation_name": "", "first_name": "Anna", "last_name": "Neumann", "address_line_1": "c/o Treuhand"},
                self.staff,
            )
        self.assertEqual(changed, ["salutation", "last_name", "address_line_1"])
        after = self.reload(self.customer)
        self.assertEqual((after.salutation, after.first_name, after.last_name, after.address_line_1), ("mr", "Anna", "Neumann", "c/o Treuhand"))
        # Nothing else of the account changed: not the login address, the status, the rights, the number, the password.
        for name in ("email", "status", "is_staff", "is_superuser", "retention_obligation", "customer_number_id",
                     "password", "street", "town", "phone_number", "session_version"):
            self.assertEqual(getattr(after, name), getattr(before, name), name)
        entry = history.entries_for(self.customer).get(event=HistoryEvent.PERSON_DATA_CHANGED)
        self.assertEqual((entry.actor, entry.detail), (self.staff, {"fields": ["salutation", "last_name", "address_line_1"]}))
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF)
        self.assertEqual((event.account_id, event.actor_id, event.long_retention), (self.customer.pk, self.staff.pk, True))
        line = "\n".join(logs.output)
        self.assertIn("fields salutation, last_name, address_line_1", line)
        for value in ("Neumann", "Treuhand"):
            self.assertNotIn(value, line)
        # The customer is not notified (owner decision): no email is queued.
        self.assertFalse(MailJob.objects.exists())
        # The same values again change nothing and write nothing.
        count = HistoryEntry.objects.count()
        _saved, changed = persons.update_customer_data(
            self.customer, persons.GROUP_DETAILS,
            {"salutation": "mr", "first_name": "Anna", "last_name": "Neumann", "address_line_1": "c/o Treuhand"}, self.staff,
        )
        self.assertEqual((changed, HistoryEntry.objects.count()), ([], count))

    def test_a_card_accepts_only_its_own_fields(self):
        for group, values in (
            (persons.GROUP_DETAILS, {"email": "other@example.org"}),
            (persons.GROUP_DETAILS, {"is_staff": True}),
            (persons.GROUP_CONTACT, {"status": "blocked"}),
            (persons.GROUP_ADDRESS, {"first_name": "Mallory"}),
            (persons.GROUP_ADDRESS, {"customer_number": "123456"}),
            ("password", {"password": "x"}),
        ):
            with self.subTest(group=group, values=values), self.assertRaises(persons.PersonError) as caught:
                persons.update_customer_data(self.customer, group, values, self.staff)
            self.assertEqual(caught.exception.code, "fields_invalid")
        after = self.reload(self.customer)
        self.assertEqual((after.email, after.first_name, after.is_staff, after.status), ("customer.change@example.org", "Anna", False, "active"))

    def test_the_rules_of_the_account_apply_and_nothing_is_written_when_they_are_broken(self):
        before = self.reload(self.customer)
        for group, values in (
            (persons.GROUP_DETAILS, {"first_name": "", "last_name": "", "organisation_name": ""}),
            (persons.GROUP_DETAILS, {"first_name": "x" * 31}),
            (persons.GROUP_CONTACT, {"phone_number": "123"}),
            (persons.GROUP_ADDRESS, {"street": "", "town": "Bern"}),
            (persons.GROUP_ADDRESS, {"town": ""}),
            (persons.GROUP_ADDRESS, {"swiss_postal_code": 12}),
            (persons.GROUP_ADDRESS, {"country": "DE", "foreign_postal_code": ""}),
            (persons.GROUP_ADDRESS, {"country": "DE", "swiss_postal_code": 3000, "foreign_postal_code": "10115"}),
        ):
            with self.subTest(group=group, values=values), self.assertRaises(ValidationError):
                persons.update_customer_data(self.customer, group, values, self.staff)
        self.assertEqual(self.reload(self.customer).town, before.town)
        self.assertFalse(HistoryEntry.objects.filter(event=HistoryEvent.PERSON_DATA_CHANGED).exists())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF).exists())
        # The address of a foreign country and its postal code go in together.
        persons.update_customer_data(
            self.customer, persons.GROUP_ADDRESS,
            {"country": "DE", "swiss_postal_code": None, "foreign_postal_code": "10115", "street": "Hauptstr.", "town": "Berlin"},
            self.staff,
        )
        after = self.reload(self.customer)
        self.assertEqual((after.country.code, after.swiss_postal_code, after.foreign_postal_code), ("DE", None, "10115"))

    def test_the_data_of_a_staff_user_or_an_administrator_is_changed_only_by_an_administrator(self):
        values = {"first_name": "Changed", "last_name": "Name"}
        for target in (self.other_staff, self.administrator, self.staff):  # also one's own account
            with self.subTest(target=target.email), self.assertRaises(persons.PersonError) as caught:
                persons.update_customer_data(target, persons.GROUP_DETAILS, values, self.staff)
            self.assertEqual(caught.exception.code, "not_allowed")
            self.assertNotEqual(self.reload(target).first_name, "Changed")
        persons.update_customer_data(self.other_staff, persons.GROUP_DETAILS, values, self.administrator)
        self.assertEqual(self.reload(self.other_staff).first_name, "Changed")
        # A demoted account is judged by the database, not by the object in the request.
        stale = self.reload(self.administrator)
        self.administrator.set_privileges(is_staff=True, is_superuser=False)
        with self.assertRaises(persons.PersonError):
            persons.update_customer_data(self.other_staff, persons.GROUP_DETAILS, {"first_name": "Again"}, stale)
        self.administrator.set_privileges(is_staff=True, is_superuser=True)

    def test_the_archive_and_other_accounts_than_persons_are_refused(self):
        with self.assertRaises(persons.PersonError):
            persons.update_customer_data(self.gone, persons.GROUP_DETAILS, {"first_name": "Back"}, self.staff)
        with self.assertRaises(persons.PersonError):
            persons.update_customer_data(self.gone, persons.GROUP_DETAILS, {"first_name": "Back"}, self.administrator)
        pending = User.objects.create_user("pending.change@example.org")
        with self.assertRaises(persons.PersonError) as caught:
            persons.update_customer_data(pending, persons.GROUP_DETAILS, {"first_name": "Mallory"}, self.staff)
        self.assertEqual(caught.exception.code, "not_a_person")
        with self.assertRaises(persons.PersonError):
            persons.update_customer_data(self.customer, persons.GROUP_DETAILS, {"first_name": "Mallory"}, self.customer)
        # A blocked account may be corrected.
        persons.update_customer_data(self.blocked, persons.GROUP_DETAILS, {"first_name": "Ruth", "last_name": "Gesperrt"}, self.staff)

    def test_a_person_who_was_invited_can_be_corrected_before_the_registration_is_completed(self):
        invited = persons.create_person(self.staff, "invited.change@example.org", {
            "first_name": "Erika", "last_name": "Neu", "street": "Neuweg", "house_number": "3",
            "swiss_postal_code": 3000, "town": "Bern", "country": "CH",
        })
        persons.update_customer_data(invited, persons.GROUP_DETAILS, {"first_name": "Erica", "last_name": "Neu"}, self.staff)
        self.assertEqual(self.reload(invited).first_name, "Erica")
        self.assertIsNone(self.reload(invited).customer_number_id)
        self.assertEqual(self.reload(invited).status, "unverified")

    def test_the_iban_is_stored_encrypted_normalised_and_never_logged(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            changed = persons.update_payment_data(
                self.customer, self.staff, iban="ch93 0076-2011 6238 5295 7", paper_invoice=True, operator_customer_number="OP 7"
            )
        self.assertEqual(sorted(changed), ["iban", "operator_customer_number", "paper_invoice"])
        record = PersonRecord.objects.get(person=self.customer)
        self.assertEqual(record.iban, GOOD_IBAN)
        self.assertEqual(iban.display(record.iban), "CH93 0076 2011 6238 5295 7")
        self.assertTrue(record.iban_encrypted.startswith("gAAAA"))  # a Fernet token, not the number
        for part in (GOOD_IBAN, "CH93", "0076201162385"):
            self.assertNotIn(part, record.iban_encrypted)
        # The stored text differs per write (Fernet): there is nothing to look the IBAN up by.
        self.assertNotEqual(record.iban_encrypted, iban.encrypt_iban(GOOD_IBAN, self.customer.pk))
        with self.assertRaises(Exception):
            PersonRecord.objects.filter(iban=GOOD_IBAN)
        self.assertFalse(PersonRecord.objects.filter(iban_encrypted__contains=GOOD_IBAN).exists())
        # No log, no history entry and no security event holds the IBAN or the other values.
        everything = "\n".join(logs.output) + repr(list(HistoryEntry.objects.values_list("detail", "event"))) + repr(
            list(SecurityEvent.objects.values()))
        for secret in (GOOD_IBAN, "CH93", "0076", "OP 7"):
            self.assertNotIn(secret, everything)
        self.assertIn("fields iban, paper_invoice", "\n".join(logs.output))
        details = [e.detail for e in history.entries_for(self.customer)]
        self.assertIn({"fields": ["iban", "paper_invoice"]}, details)

    def test_an_invalid_iban_is_refused_and_an_empty_one_removes_it(self):
        for bad in ("CH9300762011623852958", "XX", "123", "CH93 0076 2011 6238 5295 7 1", "ÄÄ9300762011623852957", "<script>"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                persons.update_payment_data(self.customer, self.staff, iban=bad)
        self.assertFalse(PersonRecord.objects.filter(person=self.customer).exists())
        # Another country's valid IBAN is accepted (the spec does not limit it).
        persons.update_payment_data(self.customer, self.staff, iban="DE89 3704 0044 0532 0130 00")
        self.assertEqual(PersonRecord.objects.get(person=self.customer).iban, "DE89370400440532013000")
        persons.update_payment_data(self.customer, self.staff, iban="")
        self.assertEqual(PersonRecord.objects.get(person=self.customer).iban, "")
        self.assertEqual(PersonRecord.objects.get(person=self.customer).iban_encrypted, "")

    def test_the_iban_and_the_paper_invoice_follow_the_staff_guard_but_the_number_at_the_grid_operator_does_not(self):
        with self.assertRaises(persons.PersonError) as caught:
            persons.update_payment_data(self.other_staff, self.staff, iban=GOOD_IBAN)
        self.assertEqual(caught.exception.code, "not_allowed")
        with self.assertRaises(persons.PersonError):
            persons.update_payment_data(self.other_staff, self.staff, paper_invoice=True)
        self.assertFalse(PersonRecord.objects.filter(person=self.other_staff).exists())
        persons.update_payment_data(self.other_staff, self.staff, operator_customer_number="OP 1")  # staff data (D-081)
        self.assertEqual(PersonRecord.objects.get(person=self.other_staff).operator_customer_number, "OP 1")
        persons.update_payment_data(self.other_staff, self.administrator, iban=GOOD_IBAN)
        self.assertEqual(PersonRecord.objects.get(person=self.other_staff).iban, GOOD_IBAN)
        with self.assertRaises(persons.PersonError):
            persons.update_payment_data(self.gone, self.administrator, iban=GOOD_IBAN)

    def test_the_iban_is_no_search_field(self):
        persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN, operator_customer_number="OP 8")
        for word in ("CH93", GOOD_IBAN, "0076", "OP 8"):
            found = person_list.apply_search(person_list.base_queryset(), word)
            self.assertFalse(found.filter(pk=self.customer.pk).exists(), word)

    def test_an_iban_that_cannot_be_decrypted_is_not_shown_and_an_empty_submission_leaves_it(self):
        """M-2: the form shows it empty; saving that empty value is "unchanged" (no write, no history, no event);
        only a valid IBAN replaces it."""
        persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN)
        record = PersonRecord.objects.get(person=self.customer)
        before = record.iban_encrypted
        with override_settings(SECRET_KEY="another secret key for this test only, not used anywhere else, 123456"):
            with self.assertRaises(DecryptionError):
                PersonRecord.objects.get(pk=record.pk).iban
            with self.assertLogs("leg_platform.community.iban", "ERROR") as logs:
                self.assertEqual(iban.readable(PersonRecord.objects.get(pk=record.pk)), "")
            self.assertNotIn(GOOD_IBAN, "\n".join(logs.output))
            self.assertIn(str(self.customer.pk), "\n".join(logs.output))
            events = (HistoryEntry.objects.count(), SecurityEvent.objects.count())
            self.assertEqual(persons.update_payment_data(self.customer, self.staff, iban=""), [])
            self.assertEqual(persons.update_payment_data(self.customer, self.staff, iban="", paper_invoice=False), [])
            self.assertEqual(PersonRecord.objects.get(pk=record.pk).iban_encrypted, before)
            self.assertEqual((HistoryEntry.objects.count(), SecurityEvent.objects.count()), events)
            # The other values of the dialog still save; a valid IBAN overwrites the unreadable one.
            self.assertEqual(persons.update_payment_data(self.customer, self.staff, iban="", paper_invoice=True), ["paper_invoice"])
            self.assertEqual(PersonRecord.objects.get(pk=record.pk).iban_encrypted, before)
            self.assertEqual(persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN), ["iban"])
            self.assertEqual(PersonRecord.objects.get(pk=record.pk).iban, GOOD_IBAN)

    def test_a_stored_iban_cannot_be_moved_to_the_record_of_another_person(self):
        """L-7: the account is part of the encrypted text."""
        persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN)
        token = PersonRecord.objects.get(person=self.customer).iban_encrypted
        other = PersonRecord.objects.create(person=self.blocked, iban_encrypted=token)
        with self.assertRaises(DecryptionError):
            other.iban
        with self.assertLogs("leg_platform.community.iban", "ERROR"):
            self.assertEqual(iban.readable(other), "")
        with self.assertRaises(ValueError):
            PersonRecord(iban=GOOD_IBAN)  # no person yet: nothing to bind it to
        # A text from before the binding (migration 0005) is readable and is bound by the command.
        from leg_platform.accounts.encryption import encrypt

        PersonRecord.objects.filter(pk=other.pk).update(iban_encrypted=encrypt(GOOD_IBAN.encode(), iban.PERSON_IBAN_PURPOSE))
        self.assertEqual(PersonRecord.objects.get(pk=other.pk).iban, GOOD_IBAN)
        out = StringIO()
        call_command("reencrypt_ibans", stdout=out)
        self.assertIn("1 IBANs were encrypted again", out.getvalue())
        self.assertTrue(iban.is_bound(PersonRecord.objects.get(pk=other.pk).iban_encrypted))
        self.assertEqual(PersonRecord.objects.get(pk=other.pk).iban, GOOD_IBAN)

    def test_the_command_fails_when_an_iban_cannot_be_decrypted(self):
        """L-6: a script must not go on and drop the old key."""
        persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN)
        with override_settings(SECRET_KEY="another secret key for this test only, not used anywhere else, 123456"):
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command("reencrypt_ibans", stdout=out)
            self.assertIn("1 could not be decrypted", out.getvalue())

    def test_a_replaced_secret_key_keeps_the_iban_readable_and_the_command_encrypts_it_again(self):
        persons.update_payment_data(self.customer, self.staff, iban=GOOD_IBAN)
        old = PersonRecord.objects.get(person=self.customer).iban_encrypted
        from django.conf import settings

        with override_settings(SECRET_KEY="a new secret key for this test only, not used anywhere else, 0987", SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            self.assertEqual(PersonRecord.objects.get(person=self.customer).iban, GOOD_IBAN)  # the fallback key reads it
            out = StringIO()
            call_command("reencrypt_ibans", stdout=out)
            self.assertIn("1 IBANs were encrypted again", out.getvalue())
            self.assertNotEqual(PersonRecord.objects.get(person=self.customer).iban_encrypted, old)
            out = StringIO()
            call_command("reencrypt_ibans", stdout=out)
            self.assertIn("1 were already current", out.getvalue())
        with override_settings(SECRET_KEY="a new secret key for this test only, not used anywhere else, 0987", SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(PersonRecord.objects.get(person=self.customer).iban, GOOD_IBAN)

    def test_a_second_address_is_requested_with_the_existing_mechanism_and_changes_nothing_before_the_confirmation(self):
        request = request_of(self.staff)
        self.customer.set_secondary_email("old.second@example.org")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(persons.change_second_email(request, self.customer, " New.Second@Example.org ", self.staff))
        after = self.reload(self.customer)
        self.assertEqual((after.email, after.secondary_email), ("customer.change@example.org", "old.second@example.org"))
        pending = PendingEmailChange.objects.get(user=self.customer)
        self.assertEqual((pending.kind, pending.address), ("secondary", "new.second@example.org"))
        run_mail_queue()
        recipients = sorted(address for message in mail.outbox for address in message.to)
        # The link goes to the new address only; the addresses that are valid now get the notice with the stop link.
        self.assertEqual(recipients, ["customer.change@example.org", "new.second@example.org", "old.second@example.org"])
        for message in mail.outbox:
            if message.to == ["new.second@example.org"]:
                self.assertRegex(message.body, r"/email-change/verify/")
            else:
                self.assertRegex(message.body, r"/email-change/stop/")
                self.assertNotIn("/email-change/verify/", message.body)
        event = SecurityEvent.objects.get(event=SecurityEvent.Event.EMAIL_CHANGE_REQUESTED)
        self.assertEqual((event.account_id, event.actor_id), (self.customer.pk, self.staff.pk))
        # M-3: the request is also "account data changed by staff" (field name only), in the history too.
        changed = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF)
        self.assertEqual((changed.account_id, changed.actor_id, changed.long_retention), (self.customer.pk, self.staff.pk, True))
        self.assertIn(
            {"fields": ["secondary_email"]}, [e.detail for e in history.entries_for(self.customer)]
        )
        self.assertEqual(PendingEmailChange.objects.get(user=self.customer).requested_by, self.staff.pk)
        # Whoever holds the link confirms it; only then does the address change. The log says who asked for it.
        token = VerificationToken.objects.get(user=self.customer, purpose=VerificationToken.Purpose.SECONDARY_EMAIL)
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            email_change.confirm_change(token)
        self.assertIn(f"requested by staff account {self.staff.pk}", "\n".join(logs.output))
        self.assertNotIn("new.second", "\n".join(logs.output))
        after = self.reload(self.customer)
        self.assertEqual((after.email, after.secondary_email), ("customer.change@example.org", "new.second@example.org"))

    def test_the_login_address_cannot_be_changed_or_used_as_the_second_address_and_the_guards_apply(self):
        request = request_of(self.staff)
        with self.assertRaises(persons.PersonError) as caught:
            persons.change_second_email(request, self.customer, self.customer.email.upper(), self.staff)
        self.assertEqual(caught.exception.code, "email_invalid")
        with self.assertRaises(persons.PersonError) as caught:
            persons.change_second_email(request, self.customer, "not an address", self.staff)
        self.assertEqual(caught.exception.code, "email_invalid")
        with self.assertRaises(persons.PersonError) as caught:
            persons.change_second_email(request, self.blocked, "x.second@example.org", self.staff)
        self.assertEqual(caught.exception.code, "not_active")
        with self.assertRaises(persons.PersonError) as caught:
            persons.change_second_email(request, self.other_staff, "x.second@example.org", self.staff)
        self.assertEqual(caught.exception.code, "not_allowed")
        self.assertFalse(PendingEmailChange.objects.exists() or MailJob.objects.exists())
        self.assertEqual(self.reload(self.customer).email, "customer.change@example.org")
        # No address and no second address: nothing to do.
        self.assertFalse(persons.change_second_email(request, self.customer, "", self.staff))

    def test_a_second_address_is_removed_at_once_and_the_limits_of_the_mail_requests_refuse(self):
        request = request_of(self.staff)
        self.customer.set_secondary_email("old.second@example.org")
        self.assertTrue(persons.change_second_email(request, self.customer, "", self.staff))
        self.assertEqual(self.reload(self.customer).secondary_email, "")
        self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.SECONDARY_EMAIL_CHANGED).exists())
        removed = SecurityEvent.objects.get(event=SecurityEvent.Event.ACCOUNT_DATA_CHANGED_BY_STAFF)
        self.assertEqual((removed.account_id, removed.actor_id), (self.customer.pk, self.staff.pk))
        key = rate_limits.ip_key(axes_client_ip(request))
        with patch.object(rate_limits, "MAIL_IP_LIMITS", ((1, timedelta(hours=1)),)):
            rate_limits.consume(rate_limits.SCOPE_MAIL_IP, key, rate_limits.MAIL_IP_LIMITS)  # the client has used its budget
            with self.assertRaises(persons.PersonError) as caught:
                persons.change_second_email(request, self.customer, "new.second@example.org", self.staff)
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertFalse(PendingEmailChange.objects.exists())
