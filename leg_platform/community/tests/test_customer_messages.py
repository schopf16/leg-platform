"""One-time messages for the customer (D-103): written by the rejection, the return for correction and the scheduler,
read and marked through the account only, capped, and cleaned up after 30 / 180 days."""

from datetime import timedelta

from django.test import TestCase, tag
from django.utils import timezone

from leg_platform.community import application_deadlines, application_staff, applications, customer_messages
from leg_platform.community.models import DEFAULT_OPERATOR_CONFIRMATION_DAYS, Application, CustomerMessage
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import TODAY, configure_numbers, customer, publish_contract, ready_draft

Kind = CustomerMessage.Kind


@tag("mariadb")
class MessageServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("message.customer@example.org")
        cls.other = customer("message.other@example.org")

    def test_a_message_is_unseen_until_it_is_marked_and_then_it_is_gone_from_the_list(self):
        first = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, "  note ")
        customer_messages.record(self.person, Kind.OPERATOR_NOT_CONFIRMED)

        unseen = customer_messages.messages_for(self.person)
        self.assertEqual([m.kind for m in unseen], [Kind.APPLICATION_REJECTED, Kind.OPERATOR_NOT_CONFIRMED])
        self.assertEqual(len(customer_messages.messages_for(self.person)), 2)  # reading does not mark
        self.assertEqual(customer_messages.mark_seen(self.person, [first.pk]), 1)
        self.assertEqual([m.kind for m in customer_messages.messages_for(self.person)], [Kind.OPERATOR_NOT_CONFIRMED])
        self.assertEqual(customer_messages.mark_seen(self.person), 1)
        self.assertEqual(customer_messages.messages_for(self.person), [])
        self.assertEqual(customer_messages.mark_seen(self.person), 0)
        self.assertEqual(CustomerMessage.objects.filter(seen_at__isnull=False).count(), 2)

    def test_an_account_sees_and_marks_only_its_own_messages(self):
        mine = customer_messages.record(self.person, Kind.APPLICATION_REJECTED)
        theirs = customer_messages.record(self.other, Kind.APPLICATION_REJECTED, "private note")

        self.assertEqual([m.pk for m in customer_messages.messages_for(self.person)], [mine.pk])
        self.assertEqual(customer_messages.mark_seen(self.person, [theirs.pk]), 0)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.seen_at)
        self.assertEqual([m.text for m in customer_messages.messages_for(self.other)], ["private note"])

    def test_only_known_kinds_are_written_and_the_text_is_cut(self):
        with self.assertRaises(ValueError):
            customer_messages.record(self.person, "something_else")
        message = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, "x" * 1000)
        self.assertEqual(len(message.text), 300)

    def test_at_most_twenty_are_kept_per_person_the_oldest_go_first(self):
        now = timezone.now()
        for index in range(25):
            customer_messages.record(
                self.person, Kind.APPLICATION_REJECTED, str(index), now=now + timedelta(seconds=index)
            )
        customer_messages.record(self.other, Kind.APPLICATION_REJECTED)

        texts = [m.text for m in customer_messages.messages_for(self.person)]
        self.assertEqual(len(texts), customer_messages.MAX_PER_PERSON)
        self.assertEqual(texts, [str(index) for index in range(5, 25)])
        self.assertEqual(CustomerMessage.objects.filter(person=self.other).count(), 1)

    def test_seen_messages_go_after_30_days_and_unseen_ones_after_180(self):
        now = timezone.now()
        old_seen = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, now=now - timedelta(days=100))
        fresh_seen = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, now=now - timedelta(days=100))
        old_unseen = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, now=now - timedelta(days=181))
        fresh_unseen = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, now=now - timedelta(days=179))
        CustomerMessage.objects.filter(pk=old_seen.pk).update(seen_at=now - timedelta(days=31))
        CustomerMessage.objects.filter(pk=fresh_seen.pk).update(seen_at=now - timedelta(days=29))

        self.assertEqual(customer_messages.cleanup(now), 2)
        self.assertEqual(
            set(CustomerMessage.objects.values_list("pk", flat=True)), {fresh_seen.pk, fresh_unseen.pk}
        )
        self.assertEqual(customer_messages.cleanup(now), 0)  # idempotent
        self.assertIsNotNone(old_unseen)

    def test_a_message_holds_nothing_but_kind_text_and_times(self):
        names = {field.name for field in CustomerMessage._meta.get_fields()}
        self.assertEqual(names, {"id", "person", "kind", "text", "created_at", "seen_at"})


@tag("mariadb")
class WrittenByTheEventsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.person = customer("message.events@example.org")
        cls.staff = staff_user("message.staff@example.org")

    def setUp(self):
        configure_numbers()
        publish_contract(self.staff)

    def submitted(self):
        application, _building = ready_draft(self.person)
        applications.submit(self.person, application, today=TODAY)
        return application

    def test_a_missing_confirmation_of_the_grid_operator_leaves_a_message(self):
        application = self.submitted()
        application_staff.mark_operator_started(application, self.staff, TODAY, today=TODAY)
        lapsed = timedelta(days=DEFAULT_OPERATOR_CONFIRMATION_DAYS + 1)
        Application.objects.filter(pk=application.pk).update(operator_started_at=timezone.now() - lapsed)

        application_deadlines.process()
        application_deadlines.process()

        message = CustomerMessage.objects.get()
        self.assertEqual((message.person, message.kind, message.text), (self.person, Kind.OPERATOR_NOT_CONFIRMED, ""))

    def test_the_scheduler_cleans_up_old_messages_and_counts_them(self):
        old = customer_messages.record(self.person, Kind.APPLICATION_REJECTED, now=timezone.now() - timedelta(days=200))
        result = application_deadlines.process()
        self.assertEqual(result["messages_deleted"], 1)
        self.assertFalse(CustomerMessage.objects.filter(pk=old.pk).exists())
        self.assertEqual(application_deadlines.process()["messages_deleted"], 0)

    def test_an_idle_rollback_leaves_only_the_mail_no_message(self):
        application, _building = ready_draft(self.person, start=False)
        Application.objects.filter(pk=application.pk).update(
            state=Application.State.PAUSED, reminded_at=timezone.now() - timedelta(days=20)
        )
        application_deadlines.process()
        self.assertFalse(Application.objects.filter(pk=application.pk).exists())
        self.assertEqual(CustomerMessage.objects.count(), 0)
