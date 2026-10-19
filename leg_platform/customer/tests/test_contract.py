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

    def test_a_staff_account_and_a_visitor_get_nothing(self):
        self.assertEqual(login(self.staff).get(DOWNLOAD).status_code, 404)
        from django.test import Client

        self.assertEqual(Client().get(DOWNLOAD).status_code, 302)

    def test_the_download_is_get_only(self):
        self.assertEqual(self.client.post(DOWNLOAD).status_code, 405)


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
