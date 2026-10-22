"""The shared building blocks of the management pages (D-077): the includes, the stylesheet, the
script, the dialog and the rule for the way back. The includes are rendered on their own, without a
page, so the tests need no account."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, tag

from leg_platform.community.history import HistoryEvent
from leg_platform.community.legs import DiscountTier, LegProblem
from leg_platform.community.status import MeteringPointStatus
from leg_platform.management import returns

STATIC = Path(settings.BASE_DIR) / "leg_platform" / "management" / "static" / "management"
CSS = (STATIC / "css" / "management.css").read_text(encoding="utf-8")
JS = (STATIC / "js" / "info-bubble.js").read_text(encoding="utf-8")
LIST_JS = (STATIC / "js" / "list.js").read_text(encoding="utf-8")


def render(template, **context):
    return render_to_string(f"management/{template}", context).strip()


class IncludeTests(SimpleTestCase):
    def test_the_info_button_is_a_14_px_i_with_a_bubble_that_names_what_it_explains(self):
        html = render("_info.html", name="Produktionsquote", tip_id="tip-quota", text="Wert aus dem <Netzbetreiber> Portal")

        self.assertIn('<button type="button" class="mgmt-info-btn" aria-label="Information: Produktionsquote"', html)
        self.assertIn('aria-describedby="tip-quota"', html)
        self.assertIn('id="tip-quota"', html)
        self.assertIn('role="tooltip"', html)
        self.assertIn(">i</button>", html)
        self.assertIn("Wert aus dem &lt;Netzbetreiber&gt; Portal", html)  # the text is escaped
        self.assertNotIn("is-left", html)
        self.assertIn("is-left-up", render("_info.html", name="x", tip_id="t", text="y", placement="left-up"))
        left = render("_info.html", name="x", tip_id="t", text="y", placement="left")
        self.assertIn("mgmt-info is-left", left)
        self.assertNotIn("is-left-up", left)

    def test_the_link_to_the_detail_page_has_an_accessible_name(self):
        html = render("_goto.html", url="/management/legs/3/", label="LEG Bahnhof öffnen")

        self.assertIn('<a class="mgmt-goto" href="/management/legs/3/" aria-label="LEG Bahnhof öffnen"', html)
        self.assertIn('<use href="#m-goto"/>', html)
        self.assertIn('aria-hidden="true"', html)

    def test_a_status_is_a_dot_and_a_word(self):
        self.assertEqual(
            render("_status.html", tone="ring", label="bestätigt"),
            '<span class="mgmt-status is-ring">bestätigt</span>',
        )
        from leg_platform.management.templatetags import management_ui as ui

        tones = {status.value: ui.status_tone(status.value) for status in MeteringPointStatus}
        self.assertEqual(tones, {"registered": "warn", "notified": "accent", "confirmed": "ring",
                                 "active": "ok", "terminated": "muted"})
        # Every tone has a style.
        for tone in set(tones.values()):
            self.assertIn(f".mgmt-status.is-{tone}", CSS)

    def test_the_direction_badge_says_bezug_or_einspeisung(self):
        consumption = render("_direction.html", direction="consumption")
        feed_in = render("_direction.html", direction="feed_in")

        self.assertEqual(consumption, '<span class="mgmt-direction is-consumption">Bezug</span>')
        self.assertEqual(feed_in, '<span class="mgmt-direction is-feed-in">Einspeisung</span>')
        self.assertIn(".mgmt-direction.is-consumption", CSS)
        self.assertIn(".mgmt-direction.is-feed-in", CSS)

    def test_the_warning_triangle_always_comes_with_a_text(self):
        html = render("_warning.html", text="Problem: Adresskorrektur offen")

        self.assertIn('title="Problem: Adresskorrektur offen"', html)
        self.assertIn('<span class="visually-hidden">Problem: Adresskorrektur offen</span>', html)
        self.assertIn('<use href="#m-warning"/>', html)
        self.assertIn('aria-hidden="true"', html)

    def test_the_banner_has_its_text_and_optional_buttons(self):
        plain = render("_banner.html", text="Dieser Messpunkt ist keiner LEG zugeordnet.")
        with_buttons = render("_banner.html", text="Text", actions="management/_suggestion_actions.html")

        self.assertIn('<div class="mgmt-banner" role="status">', plain)
        self.assertIn("<p>Dieser Messpunkt ist keiner LEG zugeordnet.</p>", plain)
        self.assertNotIn("mgmt-banner-actions", plain)
        self.assertIn("mgmt-banner-actions", with_buttons)
        self.assertIn(">Übernehmen</button>", with_buttons)
        self.assertIn(">Nicht übernehmen</button>", with_buttons)

    def test_the_empty_list_is_one_row_over_the_full_width(self):
        html = render("_empty_row.html", colspan=5, text="Noch keine Standorte. Legen Sie den ersten Standort an.")
        self.assertEqual(
            html,
            '<tr class="mgmt-empty"><td colspan="5">Noch keine Standorte. Legen Sie den ersten Standort an.</td></tr>',
        )

    def test_the_default_button_is_hidden_and_not_reachable(self):
        html = render("_default_submit.html")
        self.assertIn('name="action" value="save"', html)
        self.assertIn("visually-hidden", html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn('aria-hidden="true"', html)

    def test_the_symbols_exist(self):
        html = render("_icons.html")
        self.assertIn('id="m-goto"', html)
        self.assertIn('id="m-warning"', html)


class StyleAndScriptTests(SimpleTestCase):
    def test_the_stylesheet_uses_only_the_tokens_of_the_design(self):
        # No colour value of its own (the templates of the owner are only a picture): the light and the
        # dark mode and the colours of the instance apply through the tokens of portal.css.
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", CSS))
        self.assertIsNone(re.search(r"\brgba?\(", CSS))
        for token in ("--accent", "--warn", "--ok", "--bad", "--muted", "--line", "--panel", "--fg", "--warm"):
            self.assertIn(f"var({token}", CSS)
        self.assertNotIn("!important", CSS)
        self.assertNotIn("@import", CSS)
        self.assertNotIn("url(", CSS)

    def test_the_sizes_of_the_specification(self):
        info_button = re.search(r"\.mgmt-info-btn \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("width: 14px", info_button)
        self.assertIn("height: 14px", info_button)
        self.assertIn("inset: -6px", re.search(r"\.mgmt-info-btn::after \{(.*?)\}", CSS, re.S).group(1))  # 26 px to hit
        goto = re.search(r"\.mgmt-goto \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("width: 24px", goto)
        self.assertIn("height: 24px", goto)
        icon = re.search(r"\.mgmt-goto-icon \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("width: 16px", icon)
        self.assertIn("width: 320px", re.search(r"\.mgmt-tip \{(.*?)\}", CSS, re.S).group(1))
        # The bubble wraps and never scrolls.
        tip = re.search(r"\.mgmt-tip \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("white-space: normal", tip)
        self.assertIn("overflow-wrap: anywhere", tip)
        self.assertNotIn("overflow: auto", tip)
        self.assertNotIn("overflow: scroll", tip)
        # It shows on hover and while the button has the focus.
        self.assertIn(".mgmt-info:focus-within .mgmt-tip", CSS)
        self.assertIn(".mgmt-info:hover .mgmt-tip", CSS)

    def test_the_dialog_is_a_card_on_a_desktop_and_the_full_view_on_a_phone(self):
        self.assertIn("width: min(560px, 100%)", CSS)
        phone = CSS[CSS.index("@media (max-width: 760px)"):]
        self.assertIn(".mgmt-dialog-layer", phone)
        self.assertIn("border-radius: 0", phone)
        self.assertIn("min-height: 100%", phone)

    def test_the_script_only_dismisses_a_bubble(self):
        for forbidden in ("eval(", "Function(", "fetch(", "XMLHttpRequest", "innerHTML", "document.write",
                          "localStorage", "sessionStorage", "document.cookie"):
            self.assertNotIn(forbidden, JS)
        self.assertIn('event.key !== "Escape"', JS)

    def test_the_comments_of_the_stylesheet_are_balanced(self):
        # A "*/" inside a comment ends it early and swallows the rules that follow (found in block 2).
        self.assertEqual(CSS.count("/*"), CSS.count("*/"))
        depth = 0
        for token in re.findall(r"/\*|\*/", CSS):
            depth += 1 if token == "/*" else -1
            self.assertIn(depth, (0, 1))

    def test_the_script_of_the_lists_only_saves_through_the_endpoints_with_the_csrf_header(self):
        for forbidden in ("eval(", "Function(", "XMLHttpRequest", "innerHTML", "document.write",
                          "localStorage", "sessionStorage", "document.cookie", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(forbidden, LIST_JS)
        self.assertIn('"X-CSRFToken"', LIST_JS)
        self.assertEqual(LIST_JS.count("fetch("), 1)  # one helper posts the widths and the opened rows
        self.assertNotIn("http://", LIST_JS)
        self.assertNotIn("https://", LIST_JS)

    def test_the_whole_row_toggles_but_links_buttons_and_a_text_selection_do_not(self):
        # D-110: the click on the row (a card on a phone) opens it; the elements inside that act on their own do not.
        self.assertIn('"tr.mgmt-site.is-expandable, article.mgmt-place.is-expandable"', LIST_JS)
        self.assertIn('"a, button, input, select, textarea, label, [data-expand-for]"', LIST_JS)
        self.assertIn('window.getSelection().toString() !== ""', LIST_JS)
        self.assertIn('button.setAttribute("aria-expanded"', LIST_JS)  # the arrow button follows the state
        self.assertNotIn("onclick", LIST_JS)
        for rule in (".mgmt-site.is-expandable,\n.mgmt-place.is-expandable {\n  cursor: pointer;",
                     ".mgmt-site.is-expandable:focus-within > td", ".mgmt-site.is-expandable:hover > td"):
            self.assertIn(rule, CSS)

    def test_a_goto_link_leads_only_to_a_path_of_this_site(self):
        for url in ("https://evil.example/", "//evil.example/", "javascript:alert(1)", "legs/3/", "", "\\evil"):
            self.assertEqual(render("_goto.html", url=url, label="x"), "", url)
        self.assertIn('href="/management/legs/3/"', render("_goto.html", url="/management/legs/3/", label="x"))

    def test_a_link_to_a_page_that_does_not_exist_yet_is_empty(self):
        from leg_platform.management.templatetags import management_ui as ui

        # Every area has its pages now: an unknown name is the only case that gives an empty address.
        self.assertEqual(ui.optional_url("management:no_such_page", 1), "")
        self.assertEqual(ui.optional_url("management:persons_detail", 1), "/management/persons/1/")
        self.assertEqual(ui.optional_url("management:legs_detail", 7), "/management/legs/7/")
        self.assertEqual(ui.optional_url("management:locations_detail", 5), "/management/locations/5/")


@tag("mariadb")
class DialogTests(TestCase):
    def dialog(self, **context):
        request = RequestFactory().get("/management/x/")
        request.user = AnonymousUser()
        return render_to_string(
            "management/dialog.html",
            {"page_title": "LEG anlegen", "back_url": "/management/legs/?page=2", **context},
            request=request,
        )

    def test_the_dialog_is_a_normal_page_with_a_form_and_a_way_back(self):
        html = self.dialog(dialog_lead="Eine LEG ist ein virtueller Netzzusammenschluss.")

        self.assertIn('role="dialog" aria-labelledby="dialog-title"', html)
        self.assertIn('<h1 id="dialog-title">LEG anlegen</h1>', html)
        self.assertIn("Eine LEG ist ein virtueller Netzzusammenschluss.", html)
        self.assertIn('<form method="post"', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn('<input type="hidden" name="next" value="/management/legs/?page=2">', html)
        # A dialog without fields (a question) puts the cursor on "Cancel", never on the button of the action (D-094).
        self.assertIn(
            '<a class="btn btn-secondary-action" href="/management/legs/?page=2" data-first-field>Abbrechen</a>', html
        )
        self.assertIn(">Speichern</button>", html)
        # No script is needed: the only scripts are the files of the page frame.
        self.assertNotRegex(html, r"<script(?![^>]*\ssrc=\"/static/)")
        self.assertIn('href="/static/management/css/management.css"', html)

    def test_the_button_can_be_named_and_the_address_is_escaped(self):
        html = self.dialog(submit_label="Als angemeldet setzen", back_url='/x/?a="><script>')

        self.assertIn(">Als angemeldet setzen</button>", html)
        self.assertNotIn('"><script>', html)
        self.assertIn("&quot;&gt;&lt;script&gt;", html)


class ReturnAddressTests(SimpleTestCase):
    def request(self, method="get", **data):
        factory = RequestFactory()
        return getattr(factory, method)("/management/x/", data)

    def test_only_a_path_of_this_site_is_accepted(self):
        request = self.request()
        for safe in ("/management/legs/", "/management/legs/?page=2&q=a%20b", "/", "/a/b#c"):
            with self.subTest(value=safe):
                self.assertTrue(returns.is_safe_return_url(safe, request))
        for unsafe in (
            "", None, 5, "//evil.example.org/", "///evil.example.org", "https://evil.example.org/x",
            "http://testserver/management/", "/\\evil.example.org", "\\\\evil.example.org", "javascript:alert(1)",
            "/ok\nX: 1", "/ok\r", "/x\x00", "evil", "/" + "a" * 2001, " /management/", "data:text/html,x",
        ):
            with self.subTest(value=unsafe):
                self.assertFalse(returns.is_safe_return_url(unsafe, request))

    def test_the_form_value_wins_over_the_link_and_an_unsafe_one_falls_back(self):
        default = "/management/"
        post = RequestFactory().post("/management/x/?next=/from/link/", {"next": "/from/form/"})
        self.assertEqual(returns.return_url(post, default), "/from/form/")
        link = self.request(next="/from/link/")
        self.assertEqual(returns.return_url(link, default), "/from/link/")
        self.assertEqual(returns.return_url(self.request(), default), default)
        evil = self.request(next="https://evil.example.org/")
        self.assertEqual(returns.return_url(evil, default), default)
        # An unsafe form value does not fall through to the link.
        mixed = RequestFactory().post("/management/x/?next=/from/link/", {"next": "//evil.example.org/"})
        self.assertEqual(returns.return_url(mixed, default), default)

    def test_the_link_to_a_dialog_carries_the_current_page(self):
        request = RequestFactory().get("/management/legs/", {"q": "a b", "page": "2"})
        self.assertEqual(
            returns.with_next("/management/legs/new/", request),
            "/management/legs/new/?next=%2Fmanagement%2Flegs%2F%3Fq%3Da%2Bb%26page%3D2",
        )

    def test_the_host_of_the_request_is_the_one_allowed(self):
        request = RequestFactory().get("/x/", HTTP_HOST="testserver", secure=True)
        self.assertTrue(returns.is_safe_return_url("/management/", request))


class TextsOfTheSpecificationTests(SimpleTestCase):
    """The Swiss German texts that the domain shows (the final texts of the specification)."""

    def test_status_words_are_written_in_lower_case(self):
        self.assertEqual(
            [str(status.label) for status in MeteringPointStatus],
            ["registriert", "angemeldet", "bestätigt", "aktiv", "gekündigt"],
        )

    def test_the_discount_tiers_and_the_problems(self):
        self.assertEqual(str(DiscountTier.OPTIMISED.label), "Preisoptimiert")
        self.assertEqual(str(DiscountTier.BASIC.label), "Basis-Rabatt")
        self.assertEqual(str(LegProblem.NO_PRODUCER.label), "kein Produzent in der LEG")

    def test_the_history_events(self):
        self.assertEqual(str(HistoryEvent.CREATED.label), "Angelegt")
        self.assertEqual(str(HistoryEvent.ADDRESS_CORRECTION_ACCEPTED.label), "Adresskorrektur übernommen")

    def test_no_text_of_the_area_uses_the_letter_that_swiss_german_does_not_use(self):
        text = (Path(settings.BASE_DIR) / "locale" / "de_CH" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
        self.assertNotIn("\u00df", text)
