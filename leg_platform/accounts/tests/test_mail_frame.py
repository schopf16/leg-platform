"""The HTML frame of the system emails (D-137): escaping, links, header, footer, colours and delivery."""

import logging
import re
from datetime import timedelta
from html.parser import HTMLParser

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from leg_platform import mail as graph_mail
from leg_platform.accounts import emails, mail_frame
from leg_platform.accounts.models import EmailText, PortalSettings
from leg_platform.accounts.portal_settings import environment_values
from leg_platform.branding import MIN_CONTRAST, contrast

BASE = "https://leg.example.org"
LOGO = "/branding/logo/light/?v=0123456789abcdef"


def portal(**changes):
    values = {"name": "LEG Ittigen", "website_url": "https://leg.example.org/", "privacy_url": "https://leg.example.org/p/",
              "imprint_url": "https://leg.example.org/i/", "logo_light": "", "logo_light_type": ""}
    values.update(changes)
    return environment_values()._replace(**values)


class Collect(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.convert_charrefs = True
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, name):
        return [attrs for tag, attrs in self.tags if tag == name]


@override_settings(INSTANCE_BASE_URL=BASE)
class BodyTests(SimpleTestCase):
    def html(self, body, **changes):
        return mail_frame.build_html("Subject", body, portal(**changes))

    def test_markup_in_the_text_is_escaped(self):
        html = self.html('Hello <script>alert(1)</script> <img src=x onerror=alert(1)> Tom & Jerry "q" \'s\'')
        self.assertNotIn("<script", html)
        self.assertEqual(Collect(html).find("script"), [])
        self.assertEqual(Collect(html).find("img"), [])
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertIn("Tom &amp; Jerry &quot;q&quot;", html)

    def test_only_https_addresses_become_links(self):
        html = self.html("javascript:alert(1)\n\nhttp://plain.example.org/x\n\nmailto:a@example.org\n\nftp://x.example.org/")
        hrefs = [a["href"] for a in Collect(html).find("a")]
        self.assertEqual(hrefs, ["https://leg.example.org/", "https://leg.example.org/p/", "https://leg.example.org/i/"])
        self.assertIn("javascript:alert(1)", html)

    def test_link_text_equals_the_address_and_trailing_punctuation_is_not_part_of_it(self):
        url = "https://leg.example.org/reset/abc-DEF_123/?a=1&b=2"
        for tail in (".", ",", ")", "!", ";"):
            html = self.html(f"Open ({url}){tail}" if tail == ")" else f"Open {url}{tail}")
            link = next(a for a in Collect(html).find("a") if a["href"].startswith(url))
            self.assertEqual(link["href"], url)
            self.assertRegex(html, re.escape(f'>{url.replace("&", "&amp;")}</a>'))
            self.assertEqual(link["rel"], "noopener noreferrer")
            self.assertIn("overflow-wrap:anywhere", link["style"])
            self.assertIn("word-break:break-word", link["style"])

    def test_an_address_with_a_quote_or_angle_bracket_ends_at_it(self):
        html = self.html('https://leg.example.org/x"onmouseover="alert(1)')
        link = next(a for a in Collect(html).find("a") if a["href"].startswith("https://leg.example.org/x"))
        self.assertEqual(link["href"], "https://leg.example.org/x")
        self.assertNotIn("onmouseover", "".join(attrs.get("style", "") for attrs in Collect(html).find("a")))

    def test_a_very_long_address_stays_plain_text(self):
        long_url = "https://leg.example.org/" + "a" * mail_frame.MAX_ADDRESS_LENGTH
        html = self.html(f"Open {long_url}")
        self.assertEqual([a["href"] for a in Collect(html).find("a")][:1], ["https://leg.example.org/"])
        self.assertNotIn(f'href="{long_url}', html)
        self.assertIn(long_url, html)
        ok_url = "https://leg.example.org/" + "a" * 100
        self.assertIn(f'href="{ok_url}"', self.html(f"Open {ok_url}"))

    def test_the_dark_colours_reach_the_contrast(self):
        dark = mail_frame.DARK
        for foreground, background in (
            (dark["fg"], dark["warm"]), (dark["muted"], dark["warm"]), (dark["accent"], dark["warm"]),
            (dark["fg"], dark["panel"]), (dark["accent"], dark["panel"]),
        ):
            self.assertGreaterEqual(contrast(foreground, background), MIN_CONTRAST, (foreground, background))

    def test_the_text_fallback_has_no_light_plate_and_the_logo_has(self):
        text = self.html("x")
        self.assertNotIn("logo-box\"", text)
        self.assertIn('class="fg"', text)
        with_logo = self.html("x", logo_light=LOGO, logo_light_type="png")
        self.assertIn('class="logo-box"', with_logo)
        self.assertIn("x-apple-disable-message-reformatting", with_logo)

    def test_line_breaks_stay_and_paragraphs_are_rows(self):
        html = self.html("One\nTwo\n\nThree")
        self.assertIn("One<br>Two", html)
        self.assertIn(">Three</td>", html)

    def test_the_dark_block_holds_no_operator_data(self):
        html = self.html("x", name="Secret <Name>", accent_light="#0055AA")
        style = html[html.index("<style>"):html.index("</style>")]
        self.assertNotIn("Secret", style)
        self.assertNotIn("0055AA", style.upper())
        self.assertIn("prefers-color-scheme: dark", style)
        self.assertNotIn("<script", html.lower())


@override_settings(INSTANCE_BASE_URL=BASE)
class HeaderAndFooterTests(SimpleTestCase):
    def html(self, **changes):
        return mail_frame.build_html("S", "Body", portal(**changes))

    def test_a_png_logo_is_an_absolute_https_image_with_the_name_as_alt_text(self):
        html = self.html(logo_light=LOGO, logo_light_type="png")
        images = Collect(html).find("img")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["src"], BASE + LOGO)
        self.assertEqual(images[0]["alt"], "LEG Ittigen")
        self.assertEqual(images[0]["height"], "64")

    def test_the_name_is_text_without_a_png_logo(self):
        for changes in ({}, {"logo_light": LOGO, "logo_light_type": "svg"}, {"logo_light": "/static/x.svg", "logo_light_type": "svg"}):
            html = self.html(**changes)
            self.assertEqual(Collect(html).find("img"), [], changes)
            self.assertIn(">LEG Ittigen</span>", html)

    def test_the_name_is_text_when_the_base_address_is_not_https(self):
        with override_settings(INSTANCE_BASE_URL="http://127.0.0.1:8000"):
            html = self.html(logo_light=LOGO, logo_light_type="png")
        self.assertEqual(Collect(html).find("img"), [])
        self.assertIn(">LEG Ittigen</span>", html)

    def test_a_protocol_relative_or_foreign_logo_address_is_never_used(self):
        for path in ("//evil.example.org/x.png", "https://evil.example.org/x.png", "javascript:x.png"):
            self.assertEqual(Collect(self.html(logo_light=path, logo_light_type="png")).find("img"), [], path)

    def test_the_footer_has_the_name_the_links_and_the_two_sentences(self):
        html = self.html()
        self.assertIn("<b", html)
        self.assertIn(">Website</a>", html)
        self.assertIn(">Datenschutz</a>", html)
        self.assertIn(">Impressum</a>", html)
        self.assertIn("automatische Nachricht", html)
        self.assertIn("bei LEG Ittigen betrifft", html)

    def test_only_configured_links_are_shown_and_only_clean_https(self):
        html = self.html(website_url="", privacy_url="http://leg.example.org/", imprint_url='https://x.example.org/"><script>')
        self.assertEqual(Collect(html).find("a"), [])
        self.assertNotIn("<script", html)

    def test_operator_values_are_escaped(self):
        html = self.html(name='LEG <b>"T"</b> & Co', privacy_url="https://leg.example.org/p?a=1&b=2")
        self.assertNotIn("<b>\"T\"", html)
        self.assertIn("LEG &lt;b&gt;&quot;T&quot;&lt;/b&gt; &amp; Co", html)
        self.assertIn('href="https://leg.example.org/p?a=1&amp;b=2"', html)

    def test_the_tint_is_the_accent_mixed_over_the_background(self):
        self.assertEqual(mail_frame.mix("#005BA2", 9, "#F6F7F8"), "#E0E9F0")
        self.assertIn('bgcolor="#E0E9F0"', self.html(accent_light="#005BA2"))

    def test_the_text_and_link_colours_fall_back_when_the_contrast_is_too_low(self):
        weak = "#6FA8DC"
        colours = mail_frame._colours(portal(accent_light=weak))
        self.assertEqual(colours["link"], mail_frame.LIGHT["fg"])
        self.assertEqual(colours["tint_link"], mail_frame.LIGHT["fg"])
        default = mail_frame._colours(portal())
        self.assertEqual(default["link"], "#005BA2")
        self.assertEqual(default["muted"], mail_frame.LIGHT["muted"])
        for key, surface in (("muted", default["warm"]), ("tint_link", default["warm"])):
            self.assertGreaterEqual(contrast(default[key], surface), MIN_CONTRAST)
        # A dark accent makes the tint dark enough that the muted text needs the main text colour.
        dark = mail_frame._colours(portal(accent_light="#000000"))
        self.assertGreaterEqual(contrast(dark["muted"], dark["warm"]), MIN_CONTRAST)

    def test_an_invalid_accent_uses_the_platform_default(self):
        self.assertEqual(mail_frame._colours(portal(accent_light="red"))["link"], "#005BA2")


@override_settings(INSTANCE_BASE_URL=BASE)
class DeliveryTests(TestCase):
    def setUp(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "LEG Ittigen", "website_url": "https://leg.example.org/", "privacy_url": "https://leg.example.org/p/"})

    def test_a_system_mail_has_the_text_and_the_framed_html_and_the_full_link_in_both(self):
        token = "T" * 43
        emails.send_password_reset("a@example.org", token, timedelta(hours=2))
        message = mail.outbox[-1]
        self.assertEqual(message.alternatives[0][1], "text/html")
        html = message.alternatives[0][0]
        link = re.search(r"https://\S+/" + token + r"/", message.body).group()
        self.assertIn(link, message.body)
        self.assertIn(f'href="{link}"', html)
        self.assertIn(f">{link}</a>", html)
        self.assertEqual(message.extra_headers["Auto-Submitted"], "auto-generated")
        self.assertEqual(message.attachments, [])

    def test_the_text_part_has_no_html(self):
        emails.send_password_changed("a@example.org")
        self.assertNotIn("<", mail.outbox[-1].body)

    def test_the_registration_code_mail_keeps_its_own_layout(self):
        emails.send_registration_code("a@example.org", "123456", timedelta(minutes=15))
        html = mail.outbox[-1].alternatives[0][0]
        parser = Collect(html)
        self.assertEqual(parser.find("img"), [])
        self.assertEqual(len(parser.find("a")), 1)
        self.assertNotIn("prefers-color-scheme", html)

    def test_the_graph_test_mail_stays_plain(self):
        message = emails.graph_test_message("a@example.org", "sender@example.org")
        self.assertEqual(getattr(message, "alternatives", []), [])

    def test_the_log_notice_is_framed(self):
        emails.send_log_notification(
            "a@example.org", [{"severity": "error", "time": "2026-01-02T03:04:05+00:00", "source": "application"}]
        )
        self.assertEqual(mail.outbox[-1].alternatives[0][1], "text/html")

    def test_the_test_email_uses_the_same_frame_as_a_system_mail(self):
        key = EmailText.Key.PASSWORD_CHANGED
        with override_settings(LANGUAGE_CODE="de-ch"):
            subject, body = emails.preview(key, "Subject", "Hello\n\nA text.")
        emails.send_test("a@example.org", key, "Subject", "Hello\n\nA text.")
        message = mail.outbox[-1]
        html = message.alternatives[0][0]
        self.assertTrue(message.subject.startswith("["))
        self.assertIn("A text.", html)
        self.assertIn("A text.", body)
        self.assertEqual(html, mail_frame.build_html(message.subject, message.body))

    def test_the_password_reset_mail_with_a_png_logo_is_complete(self):
        from unittest import mock

        with mock.patch.object(mail_frame, "portal_values", return_value=portal(logo_light=LOGO, logo_light_type="png")):
            emails.send_password_reset("a@example.org", "R" * 43, timedelta(hours=2))
        message = mail.outbox[-1]
        html = message.alternatives[0][0]
        parser = Collect(html)
        self.assertEqual([i["src"] for i in parser.find("img")], [BASE + LOGO])
        link = re.search(r"https://\S+/" + "R" * 43 + r"/", message.body).group()
        self.assertIn(f">{link}</a>", html)
        self.assertIn('class="logo-box"', html)
        self.assertIn("</html>", html)

    def test_the_graph_payload_is_html_without_attachments(self):
        emails.send_password_changed("a@example.org")
        payload = graph_mail._payload(mail.outbox[-1])
        self.assertEqual(payload["message"]["body"]["contentType"], "HTML")
        self.assertIn("automatische Nachricht", payload["message"]["body"]["content"])

    def test_nothing_of_the_body_or_the_html_is_logged(self):
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Handler()
        logging.getLogger().addHandler(handler)
        try:
            emails.send_password_reset("a@example.org", "S" * 43, timedelta(hours=2))
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual([r for r in records if "S" * 10 in r or "<" in r or "https://" in r], [])
