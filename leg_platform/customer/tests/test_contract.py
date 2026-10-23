"""The contract for customers (SCHRITT-4-VERTRAG.md; SECURITY.md, "Contract PDF"; D-103): the download of the current version
and of the versions the account accepted, with the headers of an attachment that is never sniffed, sandboxed and never
cached; the page "Articles of association" (Information)."""

from datetime import date

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.community import contracts
from leg_platform.community.models import ContractVersion
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import pdf
from leg_platform.customer.tests.helpers import customer, login, publish_contract

DOWNLOAD = reverse("customer:contract_download")
READ = reverse("customer:contract_read")
PAGE = reverse("customer:contract")


@tag("mariadb")
class ContractDownloadTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.contract@example.org")
        cls.other = customer("other.contract@example.org")
        cls.staff = staff_user("staff.contract@example.org")
        cls.version = publish_contract(cls.staff)

    def setUp(self):
        self.client = login(self.mine)

    def test_the_current_version_is_an_attachment_with_the_fixed_name_and_the_safety_headers(self):
        response = self.client.get(DOWNLOAD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, bytes(self.version.pdf))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="contract-2026-10-07.pdf"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        for directive in ("no-cache", "no-store", "private"):
            self.assertIn(directive, response["Cache-Control"])
        self.assertNotIn("inline", response["Content-Disposition"])

    def test_the_download_name_never_comes_from_the_request(self):
        response = self.client.get(DOWNLOAD, {"name": "../../evil.pdf", "filename": "x"})
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="contract-2026-10-07.pdf"')

    def test_without_a_current_version_there_is_nothing_to_download(self):
        ContractVersion.objects.update(current_marker=None)
        self.assertEqual(self.client.get(DOWNLOAD).status_code, 404)

    def test_an_accepted_version_stays_available_to_that_account_only(self):
        contracts.accept_contract(self.mine, self.version.pk, agrees_contract=True, agrees_data_exchange=True)
        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        contracts.make_current(self.staff, newer)
        old = reverse("customer:contract_version_download", args=[self.version.pk])
        self.assertEqual(self.client.get(old).content, bytes(self.version.pdf))
        self.assertEqual(self.client.get(DOWNLOAD).content, bytes(newer.pdf))
        # Another account never accepted the replaced version: the same answer as for a missing one.
        other = login(self.other)
        self.assertEqual(other.get(old).status_code, 404)
        self.assertEqual(other.get(reverse("customer:contract_version_download", args=[987654])).status_code, 404)

    def test_a_version_that_was_never_current_and_that_nobody_accepted_is_not_available(self):
        spare = contracts.upload_version(self.staff, pdf(b"spare"), date(2026, 12, 1))
        self.assertEqual(self.client.get(reverse("customer:contract_version_download", args=[spare.pk])).status_code, 404)

    def test_a_staff_account_with_a_number_gets_what_a_customer_gets_and_a_visitor_nothing(self):
        # D-113: staff accounts take part like everybody else; the answer is the one of a customer.
        response = login(self.staff).get(DOWNLOAD)
        self.assertEqual((response.status_code, response.content), (200, bytes(self.version.pdf)))
        from django.test import Client

        self.assertEqual(Client().get(DOWNLOAD).status_code, 302)
        # A version the staff account never accepted and that is not current stays a 404 for it as for everybody.
        spare = contracts.upload_version(self.staff, pdf(b"spare2"), date(2026, 12, 2))
        self.assertEqual(
            login(self.staff).get(reverse("customer:contract_version_download", args=[spare.pk])).status_code, 404
        )

    def test_the_download_is_get_only(self):
        self.assertEqual(self.client.post(DOWNLOAD).status_code, 405)


@tag("mariadb")
class ContractReadingTests(TestCase):
    """The reading view (D-108): the same file inline, framed by the own origin only, for the account only."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.reading@example.org")
        cls.staff = staff_user("staff.reading@example.org")
        cls.version = publish_contract(cls.staff)

    def setUp(self):
        self.client = login(self.mine)

    def test_the_current_version_is_inline_and_may_be_framed_by_the_own_origin_only(self):
        response = self.client.get(READ)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, bytes(self.version.pdf))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'inline; filename="contract-2026-10-07.pdf"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["Content-Security-Policy"], "default-src 'none'; frame-ancestors 'self'")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        for directive in ("no-cache", "no-store", "private"):
            self.assertIn(directive, response["Cache-Control"])

    def test_the_exception_is_this_one_response_only(self):
        # The site-wide ban stays: every other page and the download cannot be framed.
        for path in (PAGE, DOWNLOAD, reverse("customer:buildings"), reverse("home")):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response["X-Frame-Options"], "DENY")
                # The download carries its own sandbox policy; the framing ban for it is the header above.
                if path != DOWNLOAD:
                    self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])

    def test_the_name_and_the_version_never_come_from_the_request(self):
        response = self.client.get(READ, {"name": "../evil.pdf", "version": "1", "download": "1"})
        self.assertEqual(response["Content-Disposition"], 'inline; filename="contract-2026-10-07.pdf"')

    def test_without_a_current_version_there_is_nothing_to_read(self):
        ContractVersion.objects.update(current_marker=None)
        self.assertEqual(self.client.get(READ).status_code, 404)

    def test_a_staff_account_with_a_number_gets_what_a_customer_gets_and_a_visitor_nothing(self):
        from django.test import Client

        response = login(self.staff).get(READ)
        self.assertEqual((response.status_code, response.content), (200, bytes(self.version.pdf)))
        response = Client().get(READ)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.content, bytes(self.version.pdf))

    def test_the_reading_view_is_get_only(self):
        self.assertEqual(self.client.post(READ).status_code, 405)

    def test_the_page_and_the_step_embed_the_view_in_a_frame_and_keep_the_download(self):
        page = login(self.mine).get(PAGE).content.decode()
        self.assertIn(f'<iframe class="contract-frame" src="{READ}#view=FitH&amp;navpanes=0"', page)
        self.assertNotIn("sandbox=", page)
        # The page's own policy needs no change for the frame: ``frame-src`` falls back to ``default-src 'self'``.
        response = self.client.get(PAGE)
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertNotIn("frame-src", response["Content-Security-Policy"])
        self.assertIn("object-src 'none'", response["Content-Security-Policy"])
        self.assertNotIn("<object", page)
        self.assertNotIn("<embed", page)
        self.assertIn(DOWNLOAD, page)

    def test_without_a_contract_the_page_has_no_frame(self):
        ContractVersion.objects.update(current_marker=None)
        page = self.client.get(PAGE).content.decode()
        self.assertNotIn("<iframe", page)


@tag("mariadb")
class ContractPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("mine.contractpage@example.org")
        cls.staff = staff_user("staff.contractpage@example.org")

    def test_without_a_contract_the_page_says_so(self):
        html = login(self.mine).get(PAGE).content.decode()
        self.assertIn("Der Vertrag ist noch nicht verfügbar", html)
        self.assertNotIn("Vertrag herunterladen", html)

    def test_the_page_lists_the_current_version_and_the_versions_the_account_accepted(self):
        version = publish_contract(self.staff)
        contracts.accept_contract(self.mine, version.pk, agrees_contract=True, agrees_data_exchange=True)
        newer = contracts.upload_version(self.staff, pdf(b"newer"), date(2026, 11, 1))
        contracts.make_current(self.staff, newer)
        spare = contracts.upload_version(self.staff, pdf(b"spare"), date(2026, 12, 1))
        html = login(self.mine).get(PAGE).content.decode()
        self.assertIn("Fassung vom 01.11.2026", html)
        self.assertIn("Aktuelle Fassung", html)
        self.assertIn("Noch nicht zugestimmt", html)
        self.assertIn("Fassung vom 07.10.2026", html)
        self.assertIn("Ersetzte Fassung", html)
        self.assertIn("Sie haben am", html)
        self.assertNotIn("01.12.2026", html)
        self.assertIn(reverse("customer:contract_version_download", args=[version.pk]), html)
        self.assertNotIn(reverse("customer:contract_version_download", args=[spare.pk]), html)
        # Another account sees the current version and nothing it did not accept.
        other = login(customer("other.contractpage@example.org")).get(PAGE).content.decode()
        self.assertNotIn("07.10.2026", other)
        self.assertIn("01.11.2026", other)
