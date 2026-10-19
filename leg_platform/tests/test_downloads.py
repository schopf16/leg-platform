"""The one helper that serves a stored PDF (``leg_platform.downloads``): the staff's and the customers' downloads carry the same
protective headers, and the name is encoded by Django."""

from django.test import SimpleTestCase

from leg_platform.downloads import PDF_CONTENT_SECURITY_POLICY, pdf_attachment


class PdfAttachmentTests(SimpleTestCase):
    def test_every_protective_header_is_set(self):
        response = pdf_attachment(b"%PDF-1.4", "contract-2026-10-07.pdf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-1.4")
        self.assertEqual(
            {name: response[name] for name in (
                "Content-Type", "Content-Disposition", "X-Content-Type-Options", "Content-Security-Policy",
                "Cross-Origin-Resource-Policy", "Referrer-Policy",
            )},
            {
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="contract-2026-10-07.pdf"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": PDF_CONTENT_SECURITY_POLICY,
                "Cross-Origin-Resource-Policy": "same-origin",
                "Referrer-Policy": "no-referrer",
            },
        )
        self.assertEqual(PDF_CONTENT_SECURITY_POLICY, "sandbox; default-src 'none'")

    def test_a_quote_or_a_non_ascii_letter_in_the_name_is_encoded(self):
        quoted = pdf_attachment(b"x", 'a"b.pdf')["Content-Disposition"]
        self.assertEqual(quoted, 'attachment; filename="a\\"b.pdf"')
        umlaut = pdf_attachment(b"x", "Vertrag-ä.pdf")["Content-Disposition"]
        self.assertTrue(umlaut.startswith("attachment; filename*=utf-8''"), umlaut)
        self.assertTrue(umlaut.isascii())
