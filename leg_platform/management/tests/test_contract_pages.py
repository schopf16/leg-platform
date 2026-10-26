"""The pages of the contract versions (D-103; owner design ``verwaltung/vertraege`` and ``SCHRITT-4-VERTRAG.md``, 3a).

What is proven: the list, its states and its icon buttons with accessible names; an action that is not allowed is not
drawn and a forged request for it is refused by the service; the upload (a PDF, a date, checks by content, never the
uploaded name, a duplicate and a double submit refused, a body limit before the login); the download only as an attachment
with ``nosniff`` and a sandboxing policy, never an inline page, and logged; exactly one current version; the history
without content; the security events; the closed doors for a customer, an anonymous visitor and a staff account without the
second factor.
"""

from datetime import date
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import SecurityEvent
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import contract_pdf, contracts, history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ContractAcceptance, ContractVersion
from leg_platform.community.tests.helpers import staff_user
from leg_platform.community.tests.onboarding import customer, pdf
from leg_platform.management import navigation

LIST = reverse("management:contracts")
UPLOAD = reverse("management:contracts_upload")
CHANGED = SecurityEvent.Event.CONTRACT_VERSION_CHANGED


def upload_file(data=None, name="upload.pdf", content_type="application/pdf"):
    return SimpleUploadedFile(name, pdf(b"\n% a") if data is None else data, content_type=content_type)


@tag("mariadb")
class ContractBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("contract.page.staff@example.org")
        cls.plain = create_active_user("contract.page.plain@example.org")
        cls.anna = customer("anna.contract@example.org", first_name="Anna", last_name="Muster")
        cls.old = contracts.upload_version(cls.staff, pdf(b"\n% old"), date(2025, 3, 1))
        cls.current = contracts.upload_version(cls.staff, pdf(b"\n% current"), date(2026, 10, 7))
        contracts.make_current(cls.staff, cls.old)
        contracts.make_current(cls.staff, cls.current)  # the old one is now "replaced" (Older)
        cls.spare = contracts.upload_version(cls.staff, pdf(b"\n% spare"), date(2026, 12, 1))

    def setUp(self):
        self.client = self.login(self.staff)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def url(self, name, version):
        return reverse(f"management:contracts_{name}", args=[version.pk])

    def events(self):
        return SecurityEvent.objects.filter(event=CHANGED).count()


@tag("mariadb")
class ContractListTests(ContractBase):
    def test_one_row_per_file_newest_first_with_status_date_and_acceptances(self):
        page = self.client.get(LIST)
        content = page.content.decode()
        for name in ("contract-2026-12-01.pdf", "contract-2026-10-07.pdf", "contract-2025-03-01.pdf"):
            self.assertContains(page, name)
        # Newest upload first: the spare version, then the current one, then the old one.
        self.assertLess(content.index("contract-2026-12-01.pdf"), content.index("contract-2026-10-07.pdf"))
        self.assertLess(content.index("contract-2026-10-07.pdf"), content.index("contract-2025-03-01.pdf"))
        for word in ("Aktuell", "Hochgeladen", "Ältere"):
            self.assertContains(page, '<span class="mgmt-status is-')
            self.assertContains(page, word)
        for label in ("Dateiname", "Status", "Hochgeladen", "Zustimmungen", "Aktionen"):
            self.assertContains(page, label)
        self.assertContains(page, "Neue Fassung hochladen")
        self.assertContains(page, "Eine Fassung kann nur gelöscht werden, wenn sie nicht aktiv ist und niemand zugestimmt hat.")
        # The list has no link column: a version has no page.
        self.assertNotContains(page, "mgmt-link-col")

    def test_the_sorting_works_and_an_unknown_one_falls_back(self):
        ascending = self.client.get(f"{LIST}?sort=file&dir=asc").content.decode()
        self.assertLess(ascending.index("contract-2025-03-01.pdf"), ascending.index("contract-2026-12-01.pdf"))
        descending = self.client.get(f"{LIST}?sort=file&dir=desc").content.decode()
        self.assertLess(descending.index("contract-2026-12-01.pdf"), descending.index("contract-2025-03-01.pdf"))
        self.assertEqual(self.client.get(f"{LIST}?sort=%27%3B--").status_code, 200)
        by_state = self.client.get(f"{LIST}?sort=state&dir=asc").content.decode()
        self.assertLess(by_state.index("contract-2026-10-07.pdf"), by_state.index("contract-2026-12-01.pdf"))

    def test_the_icon_buttons_have_accessible_names_and_only_the_allowed_ones_are_drawn(self):
        page = self.client.get(LIST).content.decode()

        def row(name):
            start = page.index(name)
            return page[start: page.index("</tr>", start)]

        spare, current, old = row("contract-2026-12-01.pdf"), row("contract-2026-10-07.pdf"), row("contract-2025-03-01.pdf")
        # Uploaded and never current, no acceptance: download, set as current, delete (W5-09: the name says what it
        # does and the visible short label stands next to it for hover and focus).
        self.assertIn('aria-label="contract-2026-12-01.pdf herunterladen"', spare)
        self.assertIn('aria-label="Als aktuell festlegen: contract-2026-12-01.pdf"', spare)
        self.assertIn('aria-label="Löschen: contract-2026-12-01.pdf"', spare)
        self.assertIn('<span class="mgmt-action-text" aria-hidden="true">Herunterladen</span>', spare)
        self.assertIn('<span class="mgmt-action-text" aria-hidden="true">Als aktuell festlegen</span>', spare)
        self.assertIn('<span class="mgmt-action-text" aria-hidden="true">Löschen</span>', spare)
        # The current one: only the download. An older one that was current: download, and it may be set again.
        self.assertIn("herunterladen", current)
        self.assertNotIn("Als aktuell festlegen", current)
        self.assertNotIn("Löschen", current)
        self.assertIn("Als aktuell festlegen", old)
        self.assertNotIn("Löschen", old)

    def test_the_actions_are_links_to_a_question_and_44_px_big(self):
        # W5-09: "Set as current" opens a question like the deletion (no POST button in the row any more).
        page = self.client.get(LIST).content.decode()
        self.assertIn(f'<a class="mgmt-action is-accent" href="{self.url("current", self.spare)}"', page)
        self.assertIn(f'<a class="mgmt-action is-danger" href="{self.url("delete", self.spare)}"', page)
        self.assertNotIn('<form method="post" action="' + self.url("current", self.spare), page)
        css = (Path(__file__).resolve().parents[1] / "static" / "management" / "css" / "management.css").read_text(
            encoding="utf-8"
        )
        rule = css[css.index(".mgmt-action {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("width: 44px", rule)
        self.assertIn("height: 44px", rule)
        # The label shows on hover and on keyboard focus.
        self.assertIn(".mgmt-action:focus-visible .mgmt-action-text", css)
        self.assertIn(".mgmt-action:hover .mgmt-action-text", css)

    def test_a_version_that_was_accepted_is_never_offered_for_deletion(self):
        ContractAcceptance.objects.create(person=self.anna, version=self.current, sha256=self.current.sha256,
                                          data_exchange_accepted=True)
        contracts.make_current(self.staff, self.spare)
        page = self.client.get(LIST).content.decode()
        start = page.index("contract-2026-10-07.pdf")
        row = page[start: page.index("</tr>", start)]
        self.assertNotIn("Löschen", row)
        self.assertIn("1", row)  # the acceptance count

    def test_without_a_current_version_a_banner_says_so(self):
        self.assertNotContains(self.client.get(LIST), "Keine Fassung ist aktuell.")
        ContractVersion.objects.update(current_marker=None)
        self.assertContains(self.client.get(LIST), "Keine Fassung ist aktuell.")

    def test_the_history_names_the_event_the_file_and_the_account_and_never_a_content(self):
        page = self.client.get(LIST)
        self.assertContains(page, "Verlauf")
        self.assertContains(page, "Vertragsfassung hochgeladen")
        self.assertContains(page, "Vertragsfassung als aktuell festgelegt")
        self.assertNotContains(page, "%PDF")
        self.assertNotContains(page, "%%EOF")

    def test_the_menu_has_the_entry_and_it_follows_the_mail_texts(self):
        self.assertIn("contracts", [entry.key for entry in navigation.MENU])
        html = self.client.get(reverse("management:overview")).content.decode()
        self.assertIn(f'href="{LIST}"', html)
        self.assertContains(self.client.get(LIST), 'aria-current="page"')


@tag("mariadb")
class ContractUploadTests(ContractBase):
    def post(self, **data):
        return self.client.post(UPLOAD, {"version_date": "2027-01-10", "next": LIST, **data})

    def test_the_dialog_is_a_multipart_form_for_a_pdf(self):
        page = self.client.get(UPLOAD)
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertContains(page, 'accept=".pdf,application/pdf"')
        self.assertContains(page, "Datum der Fassung")
        self.assertContains(page, "Eine neue Fassung ist noch nicht aktuell")
        self.assertContains(page, "Nur PDF, höchstens 10 MiB.")
        # W5-09: the sentence about the file that stays as it is was said twice; it is gone.
        self.assertNotContains(page, "unverändert")
        self.assertNotContains(page, "nicht verändert")

    def test_a_valid_upload_is_stored_but_not_current_and_the_customers_see_nothing_new(self):
        before = self.events()
        response = self.post(file=upload_file(pdf(b"\n% brand new")))
        self.assertRedirects(response, LIST, fetch_redirect_response=False)
        version = ContractVersion.objects.get(version_date=date(2027, 1, 10))
        self.assertFalse(version.is_current)
        self.assertEqual(contracts.current_version().pk, self.current.pk)
        self.assertEqual(version.uploaded_by, self.staff)
        self.assertEqual(self.events(), before + 1)
        self.assertEqual(history.entries_for(version).last().event, HistoryEvent.CONTRACT_UPLOADED)
        self.assertContains(self.client.get(LIST), "contract-2027-01-10.pdf")
        # The uploaded name is never kept: not in the database, not on the page.
        self.assertNotContains(self.client.get(LIST), "upload.pdf")

    def test_the_name_of_the_upload_is_never_used(self):
        self.post(file=upload_file(pdf(b"\n% name"), name="../../evil<script>.pdf"))
        page = self.client.get(LIST)
        self.assertNotContains(page, "evil")
        self.assertContains(page, "contract-2027-01-10.pdf")

    def test_a_file_that_is_no_pdf_is_refused_whatever_its_name_and_type_say(self):
        for name, data, kind in (
            ("fake.pdf", b"This is plain text, not a document." * 5, "application/pdf"),
            ("page.html", b"<html><script>alert(1)</script></html>" * 5, "text/html"),
            ("image.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png"),
        ):
            with self.subTest(name=name):
                before = ContractVersion.objects.count()
                response = self.post(file=upload_file(data, name, kind))
                self.assertContains(response, "Die Datei ist keine PDF-Datei.")
                self.assertEqual(ContractVersion.objects.count(), before)

    def test_a_file_that_is_too_large_is_refused(self):
        before = ContractVersion.objects.count()
        response = self.post(file=upload_file(pdf(size=contract_pdf.MAX_SIZE + 1)))
        self.assertContains(response, "Die PDF-Datei ist grösser als 10 MiB.")
        self.assertEqual(ContractVersion.objects.count(), before)
        # Exactly the limit passes.
        self.assertEqual(self.post(file=upload_file(pdf(size=contract_pdf.MAX_SIZE))).status_code, 302)

    def test_active_content_and_encryption_are_refused(self):
        for extra in (b"/JavaScript (app.alert(1))", b"/OpenAction 5 0 R", b"/Encrypt 7 0 R", b"/#4Aava#53cript (x)",
                      b"/EmbeddedFile 3 0 R", b"/Launch << /F (cmd.exe) >>"):
            with self.subTest(extra=extra):
                before = ContractVersion.objects.count()
                response = self.post(file=upload_file(pdf(extra)))
                self.assertContains(response, "verschlüsselt oder enthält aktive Inhalte")
                self.assertEqual(ContractVersion.objects.count(), before)

    def test_an_empty_or_missing_file_and_a_missing_date_are_refused(self):
        before = ContractVersion.objects.count()
        self.assertContains(self.post(file=upload_file(b"")), "Die Datei ist leer.")
        self.assertContains(self.client.post(UPLOAD, {"version_date": "2027-01-10", "next": LIST}), "Wählen Sie eine PDF-Datei.")
        response = self.client.post(UPLOAD, {"file": upload_file(), "next": LIST})
        self.assertContains(response, "Bitte füllen Sie dieses Feld aus.")
        response = self.post(file=upload_file(), version_date="not a date")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContractVersion.objects.count(), before)

    def test_the_same_file_twice_and_a_double_submit_make_one_version(self):
        data = pdf(b"\n% twice")
        self.assertEqual(self.post(file=upload_file(data)).status_code, 302)
        count = ContractVersion.objects.count()
        before = self.events()
        again = self.post(file=upload_file(data, name="other-name.pdf"))
        # W5-09: the refusal names the version that holds the file.
        self.assertContains(again, "Diese Datei ist schon als Fassung vom 10.01.2027 gespeichert.")
        self.assertEqual(ContractVersion.objects.count(), count)
        self.assertEqual(self.events(), before)

    def test_the_duplicate_of_an_older_version_names_that_version(self):
        same = contracts.upload_version(self.staff, pdf(b"\n% shelf"), date(2026, 5, 5))
        response = self.post(file=upload_file(bytes(ContractVersion.objects.get(pk=same.pk).pdf)))
        self.assertContains(response, "Diese Datei ist schon als Fassung vom 05.05.2026 gespeichert.")

    def test_a_forged_post_needs_the_token_and_a_staff_account(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(client.post(UPLOAD, {"file": upload_file(), "version_date": "2027-01-10"}).status_code, 403)
        for other in (self.login(self.plain), Client()):
            before = ContractVersion.objects.count()
            response = other.post(UPLOAD, {"file": upload_file(), "version_date": "2027-01-10"})
            self.assertIn(response.status_code, (302, 404))
            self.assertEqual(ContractVersion.objects.count(), before)

    def test_a_body_above_the_limit_is_refused_before_the_login_is_checked(self):
        from leg_platform.middleware import body_limit

        limit = body_limit("/management/contracts/upload/")
        self.assertGreater(limit, contract_pdf.MAX_SIZE)
        for client in (Client(), self.client):
            response = client.post(UPLOAD, b"x", content_type="multipart/form-data; boundary=x", CONTENT_LENGTH=str(limit + 1))
            self.assertEqual(response.status_code, 413)
        self.assertEqual(ContractVersion.objects.filter(version_date=date(2027, 1, 10)).count(), 0)


@tag("mariadb")
class ContractDownloadTests(ContractBase):
    def test_the_file_is_an_attachment_with_the_name_of_the_version_and_the_protective_headers(self):
        response = self.client.get(self.url("download", self.current))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="contract-2026-10-07.pdf"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response.content, bytes(ContractVersion.objects.get(pk=self.current.pk).pdf))

    def test_the_staff_and_the_customers_get_the_same_headers_from_one_helper(self):
        from leg_platform.downloads import pdf_attachment

        staff_response = self.client.get(self.url("download", self.current))
        helper = pdf_attachment(b"%PDF-1.4", "contract-2026-10-07.pdf")
        for header in (
            "Content-Type", "Content-Disposition", "X-Content-Type-Options", "Content-Security-Policy",
            "Cross-Origin-Resource-Policy", "Referrer-Policy",
        ):
            with self.subTest(header=header):
                self.assertEqual(staff_response[header], helper[header])

    def test_every_version_can_be_downloaded_by_the_staff_also_one_never_used(self):
        for version in (self.old, self.current, self.spare):
            with self.subTest(version=version.pk):
                self.assertEqual(self.client.get(self.url("download", version)).status_code, 200)

    def test_each_download_is_logged_with_numbers_only(self):
        with self.assertLogs("leg_platform.management.contract_views", "INFO") as logs:
            self.client.get(self.url("download", self.spare))
        line = logs.output[0]
        self.assertIn(f"account {self.staff.pk}", line)
        self.assertIn(f"version {self.spare.pk}", line)
        self.assertNotIn(self.staff.email, line)

    def test_an_unknown_version_is_a_404_and_other_methods_are_refused(self):
        self.assertEqual(self.client.get(reverse("management:contracts_download", args=[999999])).status_code, 404)
        self.assertEqual(self.client.post(self.url("download", self.current)).status_code, 405)

    def test_a_customer_an_anonymous_visitor_and_a_staff_account_without_the_second_factor_get_no_file(self):
        url = self.url("download", self.current)
        self.assertEqual(self.login(self.plain).get(url).status_code, 404)
        self.assertEqual(self.login(self.anna).get(url).status_code, 404)
        self.assertEqual(Client().get(url).status_code, 302)
        late = Client()
        late.force_login(staff_user("contract.nosecond@example.org"))
        self.assertRedirects(late.get(url), reverse("accounts:login_method"), fetch_redirect_response=False)


@tag("mariadb")
class ContractCurrentAndDeleteTests(ContractBase):
    def test_set_as_current_makes_exactly_one_version_current_and_the_other_older(self):
        before = self.events()
        response = self.client.post(self.url("current", self.spare))
        self.assertRedirects(response, LIST, fetch_redirect_response=False)
        self.assertEqual(ContractVersion.objects.filter(current_marker=True).count(), 1)
        self.assertEqual(contracts.current_version().pk, self.spare.pk)
        self.assertEqual(ContractVersion.objects.get(pk=self.current.pk).state, ContractVersion.State.REPLACED)
        self.assertEqual(self.events(), before + 1)
        self.assertEqual(history.entries_for(self.spare).last().event, HistoryEvent.CONTRACT_MADE_CURRENT)
        # Setting the current one again changes nothing and writes nothing.
        again = self.events()
        self.client.post(self.url("current", self.spare))
        self.assertEqual(self.events(), again)

    def test_set_as_current_asks_first_and_changes_nothing_on_a_get(self):
        # W5-09: a question like the deletion; all new customers get that version, the acceptances stay.
        page = self.client.get(self.url("current", self.spare))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Fassung vom 01.12.2026 als aktuell festlegen?")
        self.assertContains(page, "Neue Kunden stimmen dieser Fassung zu. Bisherige Zustimmungen bleiben gespeichert.")
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertContains(page, f'href="{LIST}"')  # the way back
        self.assertEqual(contracts.current_version().pk, self.current.pk)
        # The current version has no question; an unknown one is a 404.
        self.assertEqual(self.client.get(self.url("current", self.current)).status_code, 404)
        self.assertEqual(self.client.get(reverse("management:contracts_current", args=[999999])).status_code, 404)

    def test_set_as_current_is_a_post_with_a_token_for_a_known_version(self):
        self.assertEqual(self.client.post(reverse("management:contracts_current", args=[999999])).status_code, 404)
        client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(client.post(self.url("current", self.spare)).status_code, 403)
        self.assertEqual(contracts.current_version().pk, self.current.pk)
        self.assertEqual(self.login(self.plain).post(self.url("current", self.spare)).status_code, 404)
        self.assertEqual(self.login(self.plain).get(self.url("current", self.spare)).status_code, 404)
        self.assertEqual(Client().get(self.url("current", self.spare)).status_code, 302)
        self.assertEqual(contracts.current_version().pk, self.current.pk)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(self.url("current", self.spare)).status_code, 405)

    def test_delete_asks_first_and_then_removes_a_version_that_was_never_current(self):
        page = self.client.get(self.url("delete", self.spare))
        self.assertContains(page, "Fassung löschen")
        self.assertContains(page, "contract-2026-12-01.pdf")
        self.assertTrue(ContractVersion.objects.filter(pk=self.spare.pk).exists())
        before = self.events()
        response = self.client.post(self.url("delete", self.spare), {"next": LIST})
        self.assertRedirects(response, LIST, fetch_redirect_response=False)
        self.assertFalse(ContractVersion.objects.filter(pk=self.spare.pk).exists())
        self.assertEqual(self.events(), before + 1)
        entry = history.entries_for(("contract", self.spare.pk)).last()
        self.assertEqual(entry.event, HistoryEvent.CONTRACT_DELETED)
        self.assertEqual(entry.detail, {})
        # The history keeps the name of the file, built from the upload entry; never a content.
        self.assertContains(self.client.get(LIST), "contract-2026-12-01.pdf")

    def test_a_current_an_older_and_an_accepted_version_cannot_be_deleted_not_even_by_a_forged_post(self):
        ContractAcceptance.objects.create(person=self.anna, version=self.current, sha256=self.current.sha256,
                                          data_exchange_accepted=True)
        before = ContractVersion.objects.count()
        for version in (self.current, self.old):
            with self.subTest(version=version.pk):
                self.assertEqual(self.client.get(self.url("delete", version)).status_code, 404)
                response = self.client.post(self.url("delete", version), follow=True)
                self.assertContains(response, "Diese Fassung kann nicht gelöscht werden")
        self.assertEqual(ContractVersion.objects.count(), before)
        # The spare version that was accepted meanwhile (made current, accepted, replaced) stays too.
        contracts.make_current(self.staff, self.spare)
        ContractAcceptance.objects.create(person=customer("bert.contract@example.org"), version=self.spare,
                                          sha256=self.spare.sha256, data_exchange_accepted=True)
        contracts.make_current(self.staff, self.current)
        self.assertEqual(self.client.post(self.url("delete", self.spare), follow=True).status_code, 200)
        self.assertTrue(ContractVersion.objects.filter(pk=self.spare.pk).exists())

    def test_delete_needs_a_known_version_a_token_and_a_staff_account(self):
        self.assertEqual(self.client.get(reverse("management:contracts_delete", args=[999999])).status_code, 404)
        self.assertEqual(self.client.post(reverse("management:contracts_delete", args=[999999])).status_code, 404)
        client = self.login(self.staff, enforce_csrf_checks=True)
        self.assertEqual(client.post(self.url("delete", self.spare)).status_code, 403)
        self.assertEqual(self.login(self.plain).post(self.url("delete", self.spare)).status_code, 404)
        self.assertEqual(Client().post(self.url("delete", self.spare)).status_code, 302)
        self.assertTrue(ContractVersion.objects.filter(pk=self.spare.pk).exists())
