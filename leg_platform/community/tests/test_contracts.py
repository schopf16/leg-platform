"""The contract (D-103): the checks of the PDF, the versions, the one current version, the acceptance and who may download."""

import zlib
from datetime import date

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, tag

from leg_platform.accounts.models import SecurityEvent
from leg_platform.community import contract_pdf, contracts, history
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ContractAcceptance, ContractVersion
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import customer, pdf, publish_contract


class PdfChecksTests(SimpleTestCase):
    def refused(self, data):
        with self.assertRaises(ValidationError) as caught:
            contract_pdf.validate_pdf(data)
        return caught.exception.code

    def test_a_plain_pdf_passes(self):
        contract_pdf.validate_pdf(pdf())

    def test_the_type_comes_from_the_content(self):
        self.assertEqual(self.refused(b"<html>" + b"x" * 200 + b"</html>"), "not_a_pdf")
        self.assertEqual(self.refused(b"%PDF-1.4" + b"x" * 200), "not_a_pdf")  # no end marker
        self.assertEqual(self.refused(b"x" * 10), "not_a_pdf")
        self.assertEqual(self.refused("text"), "not_a_pdf")
        self.assertEqual(self.refused(b"\xef\xbb\xbf" + pdf()), "not_a_pdf")  # the signature must be at the start

    def test_the_size_is_limited_to_ten_mebibytes(self):
        contract_pdf.validate_pdf(pdf(size=contract_pdf.MAX_SIZE))
        self.assertEqual(self.refused(pdf(size=contract_pdf.MAX_SIZE + 1)), "too_large")

    def test_active_content_and_encryption_are_refused(self):
        for name in (b"/JavaScript", b"/JS", b"/OpenAction", b"/AA", b"/Launch", b"/EmbeddedFile", b"/RichMedia",
                     b"/XFA", b"/SubmitForm", b"/GoToR", b"/Encrypt"):
            with self.subTest(name=name):
                self.assertEqual(self.refused(pdf(b"<< " + name + b" 1 0 R >>")), "active_content")

    def test_names_written_with_escapes_are_decoded(self):
        self.assertEqual(self.refused(pdf(b"<< /Java#53cript (x) >>")), "active_content")

    def test_active_content_in_a_compressed_stream_is_found(self):
        stream = zlib.compress(b"<< /JavaScript (app.alert(1)) >>")
        data = pdf(b"4 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream\nendobj")
        self.assertEqual(self.refused(data), "active_content")

    def test_a_harmless_compressed_stream_and_similar_names_pass(self):
        stream = zlib.compress(b"BT /F1 12 Tf (Hello) Tj ET")
        contract_pdf.validate_pdf(pdf(b"<< /AAPL#3AKeywords (x) /JSON 1 >>\nstream\n" + stream + b"\nendstream"))

    def test_a_decompression_bomb_is_cut(self):
        bomb = zlib.compress(b"\0" * (50 * 1024 * 1024))
        contract_pdf.validate_pdf(pdf(b"stream\n" + bomb + b"\nendstream"))

    def test_stream_keywords_without_an_end_are_refused_in_linear_time(self):
        import time

        for chunk in (b"stream\n", b"stream\r\n", b"stream\r"):
            data = pdf(chunk * ((contract_pdf.MAX_SIZE - 400) // len(chunk)))
            self.assertLessEqual(len(data), contract_pdf.MAX_SIZE)
            started = time.perf_counter()
            self.assertEqual(self.refused(data), "not_a_pdf")
            self.assertLess(time.perf_counter() - started, 1.0, chunk)

    def test_many_empty_streams_are_refused_in_linear_time(self):
        import time

        data = pdf(b"stream\nendstream\n" * ((contract_pdf.MAX_SIZE - 400) // 17))
        started = time.perf_counter()
        self.assertEqual(self.refused(data), "not_a_pdf")
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_the_word_in_a_text_and_a_few_complete_streams_are_fine(self):
        contract_pdf.validate_pdf(pdf(b"(a stream of text) stream endstream stream\nabc\nendstream\n stream\r\nxyz\r\nendstream"))


@tag("mariadb")
class VersionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("version.staff@example.org")
        cls.customer = customer("version.customer@example.org")

    def test_upload_stores_the_file_with_digest_and_size_and_is_not_current(self):
        data = pdf(b"first")
        version = contracts.upload_version(self.staff, data, date(2026, 10, 7))

        self.assertEqual(bytes(version.pdf), data)
        self.assertEqual(len(version.sha256), 64)
        self.assertEqual(version.size, len(data))
        self.assertEqual(version.state, ContractVersion.State.UPLOADED)
        self.assertIsNone(contracts.current_version())
        self.assertTrue(
            history.entries_for(version).filter(event=HistoryEvent.CONTRACT_UPLOADED, actor=self.staff).exists()
        )
        self.assertTrue(SecurityEvent.objects.filter(event=SecurityEvent.Event.CONTRACT_VERSION_CHANGED).exists())

    def test_a_file_that_is_stored_already_is_refused(self):
        contracts.upload_version(self.staff, pdf(b"same"), date(2026, 10, 7))
        with self.assertRaises(ValidationError) as caught:
            contracts.upload_version(self.staff, pdf(b"same"), date(2026, 11, 1))
        self.assertEqual(caught.exception.code, "duplicate")

    def test_a_bad_file_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(ValidationError):
            contracts.upload_version(self.staff, b"not a pdf at all" * 10, date(2026, 10, 7))
        with self.assertRaises(ValidationError):
            contracts.upload_version(self.staff, pdf(b"/OpenAction"), date(2026, 10, 7))
        self.assertEqual(ContractVersion.objects.count(), 0)

    def test_exactly_one_version_is_current_and_the_old_one_stays(self):
        first = publish_contract(self.staff, pdf(b"one"), date(2026, 1, 1))
        second = contracts.upload_version(self.staff, pdf(b"two"), date(2026, 10, 7))
        contracts.make_current(self.staff, second)

        self.assertEqual(ContractVersion.objects.filter(current_marker=True).count(), 1)
        self.assertEqual(contracts.current_version().pk, second.pk)
        first.refresh_from_db()
        self.assertEqual(first.state, ContractVersion.State.REPLACED)
        self.assertIsNotNone(first.made_current_at)
        # Making the current one current again changes nothing.
        contracts.make_current(self.staff, second)
        self.assertEqual(ContractVersion.objects.filter(current_marker=True).count(), 1)

    def test_only_a_version_that_never_was_current_and_has_no_acceptance_is_deleted(self):
        current = publish_contract(self.staff, pdf(b"one"))
        spare = contracts.upload_version(self.staff, pdf(b"spare"), date(2026, 5, 1))
        replaced = contracts.upload_version(self.staff, pdf(b"older"), date(2026, 2, 1))
        contracts.make_current(self.staff, replaced)
        contracts.make_current(self.staff, current)

        contracts.delete_version(self.staff, spare)
        self.assertFalse(ContractVersion.objects.filter(pk=spare.pk).exists())
        for version in (current, replaced):
            with self.subTest(version=version.pk), self.assertRaises(ApplicationError) as caught:
                contracts.delete_version(self.staff, version)
            self.assertEqual(caught.exception.code, "wrong_state")

    def test_a_version_with_an_acceptance_is_kept(self):
        version = publish_contract(self.staff)
        contracts.accept_contract(self.customer, version.pk, agrees_contract=True, agrees_data_exchange=True)
        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        contracts.make_current(self.staff, newer)
        with self.assertRaises(ApplicationError):
            contracts.delete_version(self.staff, version)

    def test_only_active_staff_manage_versions(self):
        for actor in (self.customer, None):
            with self.subTest(actor=actor), self.assertRaises(ApplicationError) as caught:
                contracts.upload_version(actor, pdf(), date(2026, 10, 7))
            self.assertEqual(caught.exception.code, "not_allowed")
        version = contracts.upload_version(self.staff, pdf(b"x"), date(2026, 10, 7))
        for call in (
            lambda: contracts.make_current(self.customer, version),
            lambda: contracts.delete_version(self.customer, version),
            lambda: contracts.staff_download(self.customer, version.pk),
        ):
            with self.assertRaises(ApplicationError):
                call()
        self.assertEqual(ContractVersion.objects.filter(current_marker=True).count(), 0)

    def test_a_blocked_staff_account_is_refused(self):
        staff = staff_user("blocked.version.staff@example.org")
        staff.set_status(type(staff).Status.BLOCKED)
        with self.assertRaises(ApplicationError):
            contracts.upload_version(staff, pdf(), date(2026, 10, 7))

    def test_the_download_name_is_built_from_the_version_date(self):
        version = contracts.upload_version(self.staff, pdf(), date(2026, 10, 7))
        self.assertEqual(contracts.download_name(version), "contract-2026-10-07.pdf")


@tag("mariadb")
class AcceptanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("acceptance.staff@example.org")
        cls.customer = customer("acceptance.customer@example.org")

    def setUp(self):
        self.version = publish_contract(self.staff)

    def test_the_acceptance_stores_time_version_digest_and_the_second_tick(self):
        acceptance = contracts.accept_contract(
            self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True
        )

        self.assertEqual(acceptance.person, self.customer)
        self.assertEqual(acceptance.version, self.version)
        self.assertEqual(acceptance.sha256, self.version.sha256)
        self.assertTrue(acceptance.data_exchange_accepted)
        self.assertIsNotNone(acceptance.accepted_at)
        self.assertTrue(contracts.has_accepted_current(self.customer))
        entry = history.entries_for(self.customer).get(event=HistoryEvent.CONTRACT_ACCEPTED)
        self.assertEqual(entry.detail, {"version_id": self.version.pk})

    def test_the_acceptance_sets_the_retention_obligation_once_and_says_so_in_the_security_log(self):
        from leg_platform.accounts.models import SecurityEvent

        self.assertFalse(type(self.customer).objects.get(pk=self.customer.pk).retention_obligation)
        contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.retention_obligation)
        events = SecurityEvent.objects.filter(event=SecurityEvent.Event.RETENTION_OBLIGATION_SET)
        self.assertEqual([(e.account_id, e.actor_id) for e in events], [(self.customer.pk, self.customer.pk)])

    def test_a_refused_acceptance_leaves_the_flag_alone(self):
        with self.assertRaises(ApplicationError):
            contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=False)
        self.assertFalse(type(self.customer).objects.get(pk=self.customer.pk).retention_obligation)

    def test_the_acceptance_survives_the_deletion_of_the_account(self):
        contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        self.assertEqual(type(self.customer).objects.get(pk=self.customer.pk).delete_or_deactivate(), "deactivated")
        self.assertEqual(ContractAcceptance.objects.filter(person=self.customer).count(), 1)

    def test_both_ticks_are_required(self):
        for contract, exchange in ((True, False), (False, True), (False, False), (None, True), ("on", "on")):
            with self.subTest(contract=contract, exchange=exchange), self.assertRaises(ApplicationError) as caught:
                contracts.accept_contract(
                    self.customer, self.version.pk, agrees_contract=contract, agrees_data_exchange=exchange
                )
            self.assertEqual(caught.exception.code, "both_ticks_required")
        self.assertEqual(ContractAcceptance.objects.count(), 0)

    def test_once_per_person_and_version(self):
        contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        with self.assertRaises(ApplicationError) as caught:
            contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        self.assertEqual(caught.exception.code, "already_accepted")

    def test_a_version_that_is_not_current_any_more_is_refused(self):
        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        contracts.make_current(self.staff, newer)
        with self.assertRaises(ApplicationError) as caught:
            contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        self.assertEqual(caught.exception.code, "version_changed")
        self.assertFalse(contracts.has_accepted_current(self.customer))

    def test_without_a_current_version_nothing_can_be_accepted(self):
        ContractVersion.objects.update(current_marker=None)
        with self.assertRaises(ApplicationError) as caught:
            contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        self.assertEqual(caught.exception.code, "no_contract")

    def test_an_account_without_a_customer_number_or_blocked_cannot_accept(self):
        blocked = customer("blocked.acceptance@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        for person in (blocked, None):
            with self.subTest(person=person), self.assertRaises(ApplicationError) as caught:
                contracts.accept_contract(person, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
            self.assertEqual(caught.exception.code, "not_allowed")

    def test_a_customer_downloads_the_current_version_and_the_versions_the_account_accepted(self):
        contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        name, data = contracts.customer_download(self.customer)
        self.assertEqual((name, data), ("contract-2026-10-07.pdf", bytes(self.version.pdf)))
        self.assertEqual(contracts.customer_download(self.customer, self.version.pk)[0], name)

        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        # An uploaded version that is not current and that nobody accepted is not available.
        with self.assertRaises(ApplicationError) as caught:
            contracts.customer_download(self.customer, newer.pk)
        self.assertEqual(caught.exception.code, "not_found")
        contracts.make_current(self.staff, newer)
        # A replaced version stays available to the account that accepted it (the evidence of what it agreed to).
        self.assertEqual(contracts.customer_download(self.customer, self.version.pk)[0], name)
        self.assertEqual(contracts.customer_download(self.customer)[0], "contract-2026-11-01.pdf")
        # Another account that never accepted the replaced version cannot get it: the same answer as for a missing one.
        other = customer("other.download@example.org")
        for version_id in (self.version.pk, 0, "x"):
            with self.subTest(version_id=version_id), self.assertRaises(ApplicationError) as caught:
                contracts.customer_download(other, version_id)
            self.assertEqual(caught.exception.code, "not_found")

    def test_the_versions_a_customer_sees_are_the_current_one_and_the_accepted_ones(self):
        other = customer("other.versions@example.org")
        contracts.accept_contract(self.customer, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        spare = contracts.upload_version(self.staff, pdf(b"spare"), date(2026, 12, 1))
        contracts.make_current(self.staff, newer)
        mine = contracts.customer_versions(self.customer)
        self.assertEqual([(v.pk, a is not None) for v, a in mine], [(newer.pk, False), (self.version.pk, True)])
        self.assertEqual([(v.pk, a) for v, a in contracts.customer_versions(other)], [(newer.pk, None)])
        self.assertNotIn(spare.pk, [v.pk for v, _a in mine])

    def test_the_download_is_refused_without_a_current_version_and_for_an_inactive_account(self):
        blocked = customer("blocked.download@example.org")
        blocked.set_status(type(blocked).Status.BLOCKED)
        with self.assertRaises(ApplicationError) as caught:
            contracts.customer_download(blocked)
        self.assertEqual(caught.exception.code, "not_allowed")
        ContractVersion.objects.update(current_marker=None)
        with self.assertRaises(ApplicationError) as caught:
            contracts.customer_download(self.customer)
        self.assertEqual(caught.exception.code, "no_contract")

    def test_the_staff_downloads_every_version(self):
        spare = contracts.upload_version(self.staff, pdf(b"spare"), date(2026, 5, 1))
        self.assertEqual(contracts.staff_download(self.staff, spare.pk)[1], bytes(spare.pdf))
        with self.assertRaises(ApplicationError):
            contracts.staff_download(self.staff, 0)
