"""The language of the pages: chosen with the language menu, kept in the language cookie (D-010, D-056).

Covers ``leg_platform/language.py`` (middleware, view, English fallback), the menu in
``templates/_corner_controls.html`` and the settings. The catalogues themselves are checked in
``accounts/tests/test_translations.py``.
"""

import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import resolve, reverse
from django.utils import translation
from django.utils.translation import trans_real

from leg_platform.accounts import second_factor
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.accounts.templatetags.language_menu import html_lang
from leg_platform.community.tests.helpers import staff_user
from leg_platform.language import ChosenLanguageMiddleware, chosen_language
from leg_platform.middleware import body_limit

SET_LANGUAGE = reverse("set_language")
LOGIN = reverse("accounts:login")
COOKIE = settings.LANGUAGE_COOKIE_NAME
CODES = ["de-ch", "fr", "it", "en"]
# Texts that only the German pages have (the default language of the instance).
GERMAN_MARKERS = ("Ausloggen", "Anmelden", "E-Mail-Adresse", "Passwort", "Mein Bereich")


def menu_of(html):
    """The language menu of a page: ``(text of the button, {code: (name, lang, aria-current)})``."""
    button = re.search(r'<button type="button" id="lang-btn"[^>]*>(.*?)</button>', html, re.S).group(1)
    items = {}
    for match in re.finditer(
        r'<button type="submit" role="menuitem" name="language" value="([^"]+)" lang="([^"]+)" aria-current="(true|false)">'
        r"(?:<img[^>]*>)?([^<]+)</button>",
        html,
    ):
        items[match.group(1)] = (match.group(4).strip(), match.group(2), match.group(3))
    return button, items


def html_lang_of(html):
    return re.search(r'<html lang="([^"]+)"', html).group(1)


class SettingsTests(SimpleTestCase):
    def test_four_languages_and_the_instance_default(self):
        self.assertEqual(settings.LANGUAGE_CODE, "de-ch")
        self.assertEqual([code for code, _name in settings.LANGUAGES], CODES)

    def test_the_middleware_sits_after_the_session_and_before_the_common_middleware(self):
        stack = settings.MIDDLEWARE
        ours = stack.index("leg_platform.language.ChosenLanguageMiddleware")
        self.assertLess(stack.index("django.contrib.sessions.middleware.SessionMiddleware"), ours)
        self.assertLess(ours, stack.index("django.middleware.common.CommonMiddleware"))
        # Django's own middleware would read the Accept-Language header of the browser.
        self.assertNotIn("django.middleware.locale.LocaleMiddleware", stack)

    def test_the_language_cookie_is_a_functional_cookie_with_safe_attributes(self):
        self.assertIn(settings.LANGUAGE_COOKIE_NAME, ("language", "__Host-language"))
        self.assertIs(settings.LANGUAGE_COOKIE_HTTPONLY, True)
        self.assertEqual(settings.LANGUAGE_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.LANGUAGE_COOKIE_PATH, "/")
        self.assertIsNone(settings.LANGUAGE_COOKIE_DOMAIN)
        self.assertEqual(settings.LANGUAGE_COOKIE_AGE, int(timedelta(days=365).total_seconds()))
        # The __Host- prefix needs Secure: the name and the flag follow the other cookies.
        self.assertEqual(settings.LANGUAGE_COOKIE_NAME.startswith("__Host-"), settings.LANGUAGE_COOKIE_SECURE)

    def test_the_route_is_public_without_a_session_and_has_a_body_limit(self):
        self.assertEqual(SET_LANGUAGE, "/language/")
        self.assertEqual(resolve(SET_LANGUAGE).view_name, "set_language")
        for views in (
            second_factor.UNRESTRICTED_VIEWS,
            second_factor.PASSWORD_CHANGE_VIEWS,
            second_factor.SESSION_FREE_VIEWS,
        ):
            self.assertIn("set_language", views)
        self.assertEqual(body_limit(SET_LANGUAGE), 8192)

    def test_html_lang_spelling(self):
        self.assertEqual(
            [html_lang(code) for code in CODES], ["de-CH", "fr", "it", "en"]
        )


class FallbackTests(SimpleTestCase):
    """A text without a translation is English in every language but the default, never German."""

    PROBE = "A text that only the German catalogue knows (probe of the test)"

    def test_a_text_missing_in_french_and_italian_is_the_english_source_not_german(self):
        german = trans_real.translation("de-ch")
        german._catalog[self.PROBE] = "Nur der deutsche Katalog kennt diesen Text"
        try:
            for code in CODES:
                with self.subTest(language=code), translation.override(code):
                    shown = translation.gettext(self.PROBE)
                    if code == "de-ch":
                        self.assertEqual(shown, "Nur der deutsche Katalog kennt diesen Text")
                    else:
                        self.assertEqual(shown, self.PROBE)
        finally:
            german._catalog._catalogs[0].pop(self.PROBE, None)

    def test_the_javascript_catalogue_class_has_no_german_fallback_either(self):
        # Django builds the catalogue of a language of the JavaScript domain with the same class.
        paths = [settings.LOCALE_PATHS[0]]
        german = trans_real.DjangoTranslation("de-ch", domain="djangojs", localedirs=paths)
        french = trans_real.DjangoTranslation("fr", domain="djangojs", localedirs=paths)
        self.assertIn("Contrast %s:1 – sufficient", german._catalog)
        self.assertIsNone(french._fallback)


class ChosenLanguageTests(SimpleTestCase):
    """The language comes from the language cookie only (D-010)."""

    class Request:
        def __init__(self, cookies=None, accept=""):
            self.COOKIES = cookies or {}
            self.META = {"HTTP_ACCEPT_LANGUAGE": accept}

    def test_a_chosen_language_is_used_and_everything_else_is_the_default(self):
        for code in CODES:
            with self.subTest(code=code):
                self.assertEqual(chosen_language(self.Request({COOKIE: code})), code)
        for value in ("", "xx", "de", "fr-CA", "FR", "en-us", "de-CH", "../fr", "fr\x00"):
            with self.subTest(value=value):
                self.assertEqual(chosen_language(self.Request({COOKIE: value})), settings.LANGUAGE_CODE)

    def test_the_accept_language_header_does_not_change_the_default(self):
        for header in ("fr", "fr-CH,fr;q=0.9,en;q=0.8", "it", "en-US,en;q=0.9", "de-DE", "*"):
            with self.subTest(header=header):
                self.assertEqual(chosen_language(self.Request(accept=header)), settings.LANGUAGE_CODE)
        # Django's own function would take the header: this is why the middleware is replaced.
        request = self.Request(accept="fr")
        request.path_info = "/"
        self.assertEqual(translation.get_language_from_request(request), "fr")

    def test_the_cookie_wins_over_the_header(self):
        self.assertEqual(chosen_language(self.Request({COOKIE: "it"}, accept="fr")), "it")

    def test_the_middleware_is_a_locale_middleware(self):
        from django.middleware.locale import LocaleMiddleware

        self.assertTrue(issubclass(ChosenLanguageMiddleware, LocaleMiddleware))


@tag("mariadb")
class SwitchTests(TestCase):
    """The switch end to end: an entry page, an error page, a signed-in page, the management area and the administration."""

    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.member = create_active_user("member.person@example.org")
        cls.staff = staff_user("staff.person@example.org")
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)

    def switch(self, client, code, next_url="/login/", **extra):
        return client.post(SET_LANGUAGE, {"language": code, "next": next_url}, **extra)

    def signed_in(self, user):
        client = VerifiedClient()
        client.force_login(user)
        return client

    def test_the_default_is_swiss_german_with_the_menu_on_every_language(self):
        response = self.client.get(LOGIN)
        html = response.content.decode()
        self.assertEqual(html_lang_of(html), "de-CH")
        self.assertIn("Anmelden", html)
        button, items = menu_of(html)
        self.assertEqual(button, "DE")
        self.assertEqual(list(items), CODES)
        self.assertEqual(
            {code: entry[0] for code, entry in items.items()},
            {"de-ch": "Deutsch", "fr": "Français", "it": "Italiano", "en": "English"},
        )
        self.assertEqual(
            {code: entry[1] for code, entry in items.items()},
            {"de-ch": "de-CH", "fr": "fr", "it": "it", "en": "en"},
        )
        self.assertEqual({code: entry[2] for code, entry in items.items()}, {"de-ch": "true", "fr": "false", "it": "false", "en": "false"})
        self.assertIn('aria-label="Sprache: Deutsch"', html)
        self.assertEqual(response["Content-Language"], "de-ch")

    def test_an_entry_page_switches_to_every_language_and_back(self):
        expected = {"de-ch": ("de-CH", "DE"), "fr": ("fr", "FR"), "it": ("it", "IT"), "en": ("en", "EN")}
        for code in ("fr", "it", "en", "de-ch"):
            with self.subTest(language=code):
                response = self.switch(self.client, code)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], LOGIN)
                self.assertEqual(self.client.cookies[COOKIE].value, code)
                page = self.client.get(LOGIN)
                html = page.content.decode()
                lang, short = expected[code]
                self.assertEqual(html_lang_of(html), lang)
                button, items = menu_of(html)
                self.assertEqual(button, short)
                self.assertEqual([c for c, entry in items.items() if entry[2] == "true"], [code])
                self.assertEqual(page["Content-Language"], code)
                if code == "de-ch":
                    self.assertIn("E-Mail-Adresse", html)
                else:
                    # No German text is left on the page (an untranslated text is English).
                    for marker in GERMAN_MARKERS:
                        self.assertNotIn(marker, html)
                if code == "en":
                    for text in ("Email address", "Password", "Log in", "Trouble signing in?", "Stay signed in"):
                        self.assertIn(text, html)
                    self.assertIn(f'href="{reverse("accounts:login_help")}"', html)
                    help_page = self.client.get(reverse("accounts:login_help"))
                    self.assertContains(help_page, f'href="{reverse("accounts:password_reset")}"')
                    self.assertContains(help_page, f'href="{reverse("accounts:recovery")}"')
                    self.assertIn('aria-label="Language: English"', html)

    def test_the_other_entry_pages_and_the_error_page_follow_the_choice(self):
        self.switch(self.client, "en")
        for url in (
            reverse("accounts:registration"),
            reverse("accounts:password_reset"),
            "/this/page/does/not/exist/",
        ):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertEqual(html_lang_of(html), "en")
                self.assertIn('id="lang-btn"', html)
                self.assertEqual(menu_of(html)[0], "EN")
                for marker in GERMAN_MARKERS + ("Datenschutz", "Impressum"):
                    self.assertNotIn(marker, html)
        self.assertIn("Privacy notice", self.client.get(LOGIN).content.decode())

    def test_an_error_page_does_not_echo_the_address_and_returns_by_the_referrer(self):
        html = self.client.get("/secret-looking-path-17/").content.decode()
        self.assertIn('id="lang-btn"', html)
        self.assertNotIn("secret-looking-path", html)
        response = self.client.post(
            SET_LANGUAGE, {"language": "fr"}, headers={"Referer": "http://testserver/secret-looking-path-17/"}
        )
        self.assertEqual(urlsplit(response["Location"]).path, "/secret-looking-path-17/")

    def test_a_signed_in_page_switches(self):
        client = self.signed_in(self.member)
        profile = reverse("accounts:profile")
        self.assertIn("Ausloggen", client.get(profile).content.decode())
        self.switch(client, "en", profile)
        html = client.get(profile).content.decode()
        self.assertEqual(html_lang_of(html), "en")
        self.assertIn("Log out", html)
        self.assertNotIn("Ausloggen", html)
        self.assertNotIn("Mein Bereich", html)
        # The choice outlasts the session: after the logout the entry page is still English.
        client.post(reverse("accounts:logout"))
        self.assertEqual(html_lang_of(client.get(LOGIN).content.decode()), "en")

    def test_the_management_area_switches(self):
        client = self.signed_in(self.staff)
        overview = reverse("management:overview")
        self.assertIn("Ausloggen", client.get(overview).content.decode())
        for code in ("fr", "it", "en"):
            with self.subTest(language=code):
                self.switch(client, code, overview)
                response = client.get(overview)
                html = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(html_lang_of(html), code)
                self.assertEqual(menu_of(html)[0], code.upper())
                self.assertNotIn("Ausloggen", html)
                self.assertNotIn("Mein Bereich", html)
        self.assertIn("Log out", html)
        # The management area stays uncached whatever the language (D-077).
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_administration_switches(self):
        client = self.signed_in(self.administrator)
        index = reverse("admin:index")
        html = client.get(index).content.decode()
        self.assertEqual(html_lang_of(html), "de-ch")
        self.assertIn("Ausloggen", html)
        for code in ("fr", "it", "en"):
            with self.subTest(language=code):
                self.switch(client, code, index)
                response = client.get(index)
                html = response.content.decode()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(html_lang_of(html), code)
                self.assertNotIn("Ausloggen", html)
                self.assertNotIn("Willkommen", html)
        self.assertIn("Log out", html.replace("LOG OUT", "Log out"))

    def test_the_javascript_catalogue_follows_the_language(self):
        client = self.signed_in(self.administrator)
        catalogue = reverse("admin:jsi18n")
        self.assertIn("Kontrast %s:1", client.get(catalogue).content.decode())
        for code in ("fr", "it", "en"):
            with self.subTest(language=code):
                self.switch(client, code, reverse("admin:index"))
                text = client.get(catalogue).content.decode()
                self.assertNotIn("Kontrast", text)
                self.assertNotIn("Markieren Sie", text)

    def test_an_invalid_language_is_refused_and_sets_no_cookie(self):
        for value in ("xx", "de", "de-CH", "fr-CA", "FR", "en-gb", "", "fr; Path=/", "../fr", "<script>", "x" * 5000):
            with self.subTest(value=value[:20]):
                response = self.client.post(SET_LANGUAGE, {"language": value, "next": LOGIN})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(COOKIE, response.cookies)
        response = self.client.post(SET_LANGUAGE, {"next": LOGIN})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(COOKIE, self.client.cookies)
        self.assertEqual(html_lang_of(self.client.get(LOGIN).content.decode()), "de-CH")

    def test_the_cookie_has_the_attributes_and_holds_only_the_code(self):
        response = self.switch(self.client, "fr")
        morsel = response.cookies[COOKIE]
        self.assertEqual(morsel.value, "fr")
        self.assertEqual(morsel["max-age"], int(timedelta(days=365).total_seconds()))
        self.assertEqual(morsel["path"], "/")
        self.assertEqual(morsel["samesite"], "Lax")
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["domain"], "")
        self.assertEqual(bool(morsel["secure"]), settings.LANGUAGE_COOKIE_SECURE)
        # No other cookie is set: the view reads and writes no session.
        self.assertEqual(list(response.cookies), [COOKIE])
        self.assertNotIn("Vary", response.headers)
        self.assertIn("no-store", response["Cache-Control"])

    @override_settings(LANGUAGE_COOKIE_SECURE=True, LANGUAGE_COOKIE_NAME="__Host-language")
    def test_outside_local_the_cookie_is_secure_and_host_only(self):
        morsel = self.switch(self.client, "it", secure=True).cookies["__Host-language"]
        self.assertTrue(morsel["secure"])
        self.assertEqual(morsel["path"], "/")
        self.assertEqual(morsel["domain"], "")

    def test_next_is_checked_and_an_open_redirect_is_not_possible(self):
        for target in (
            "https://evil.example/",
            "http://evil.example/login/",
            "//evil.example/",
            "///evil.example/",
            "/\\evil.example",
            "\\\\evil.example",
            "javascript:alert(1)",
            "data:text/html,x",
            "https:evil.example",
            "http://testserver.evil.example/",
            "http://testserver@evil.example.org/",
            " //evil.example",
        ):
            with self.subTest(target=target):
                client = Client()
                response = self.switch(client, "fr", target, headers={"Referer": "https://evil.example/page/"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], "/")
                self.assertEqual(client.cookies[COOKIE].value, "fr")
        # A safe address of the own host is followed, also with a query.
        response = self.switch(self.client, "fr", "/login/?next=/management/&x=1")
        self.assertEqual(response["Location"], "/login/?next=/management/&x=1")
        # Without next the own referrer is used, a foreign one is not.
        own = self.client.post(SET_LANGUAGE, {"language": "fr"}, headers={"Referer": "http://testserver/register/"})
        self.assertEqual(urlsplit(own["Location"]).path, "/register/")
        self.assertIn(urlsplit(own["Location"]).netloc, ("", "testserver"))
        foreign = self.client.post(SET_LANGUAGE, {"language": "fr"}, headers={"Referer": "https://evil.example/"})
        self.assertEqual(foreign["Location"], "/")

    def test_a_large_body_is_refused_before_it_is_read(self):
        response = self.switch(self.client, "fr", "/" + "a" * 9000)
        self.assertEqual(response.status_code, 413)
        self.assertNotIn(COOKIE, response.cookies)

    def test_only_a_post_changes_the_language(self):
        for method in ("get", "head", "put", "delete", "patch"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(SET_LANGUAGE + "?language=fr&next=/login/")
                self.assertEqual(response.status_code, 405)
                self.assertNotIn(COOKIE, response.cookies)

    def test_the_accept_language_header_does_not_change_the_default(self):
        for header in ("fr", "fr-CH,fr;q=0.9,en;q=0.8", "it-IT", "en-US,en;q=0.9"):
            with self.subTest(header=header):
                html = self.client.get(LOGIN, headers={"Accept-Language": header}).content.decode()
                self.assertEqual(html_lang_of(html), "de-CH")
                self.assertIn("E-Mail-Adresse", html)
        # The page depends on the cookie, not on the header.
        response = self.client.get(LOGIN, headers={"Accept-Language": "fr"})
        self.assertIn("Cookie", response["Vary"])
        self.assertNotIn("Accept-Language", response["Vary"])

    def test_the_cookie_is_the_only_input_a_wrong_value_gives_the_default(self):
        for value in ("xx", "de", "fr-CA", "FR", "fr; Path=/"):
            with self.subTest(value=value):
                client = Client()
                client.cookies[COOKIE] = value
                html = client.get(LOGIN, headers={"Accept-Language": "fr"}).content.decode()
                self.assertEqual(html_lang_of(html), "de-CH")

    def test_the_form_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        refused = client.post(SET_LANGUAGE, {"language": "fr", "next": LOGIN})
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn(COOKIE, refused.cookies)
        # The page carries the token in the language form; with it the switch works.
        html = client.get(LOGIN).content.decode()
        form = re.search(r'<form class="lang-form" method="post" action="/language/" role="none">(.*?)</form>', html, re.S).group(1)
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', form).group(1)
        done = client.post(SET_LANGUAGE, {"language": "fr", "next": LOGIN, "csrfmiddlewaretoken": token})
        self.assertEqual(done.status_code, 302)
        self.assertEqual(done.cookies[COOKIE].value, "fr")

    def test_the_form_carries_no_address_and_returns_by_the_referrer(self):
        # Nothing of the request is written into the page again (the query may hold anything).
        address = LOGIN + "?next=/management/&search=Uniqueterm"
        html = self.client.get(address).content.decode()
        form = re.search(r'<form class="lang-form".*?</form>', html, re.S).group(0)
        self.assertNotIn('name="next"', form)
        self.assertNotIn("Uniqueterm", html)
        response = self.client.post(
            SET_LANGUAGE, {"language": "fr"}, headers={"Referer": "http://testserver" + address}
        )
        location = urlsplit(response["Location"])
        self.assertEqual((location.path, location.query), ("/login/", "next=/management/&search=Uniqueterm"))
        # Without a referrer the start page is the way back.
        self.assertEqual(self.client.post(SET_LANGUAGE, {"language": "fr"})["Location"], "/")

    def test_the_public_route_works_without_login_and_without_the_second_factor(self):
        # An account that must have a second factor but has not completed it (the usual middleware
        # sends every other page to the set-up), and an account that must change its password.
        staff_client = Client()
        staff_client.force_login(self.staff)
        self.assertEqual(staff_client.get(reverse("accounts:profile")).status_code, 302)
        response = self.switch(staff_client, "it", "/anywhere/")
        self.assertEqual((response.status_code, response["Location"]), (302, "/anywhere/"))
        self.assertEqual(staff_client.cookies[COOKIE].value, "it")

        forced = create_active_user("forced.person@example.org")
        forced.password_change_required = True
        forced.save()
        forced_client = Client()
        forced_client.force_login(forced)
        self.assertEqual(
            forced_client.get(reverse("accounts:profile"))["Location"], reverse("accounts:password_change")
        )
        response = self.switch(forced_client, "en", "/anywhere/")
        self.assertEqual((response.status_code, response["Location"]), (302, "/anywhere/"))

        # An anonymous visitor, with a stale session cookie as well.
        anonymous = Client()
        anonymous.cookies[settings.SESSION_COOKIE_NAME] = "stale"
        self.assertEqual(self.switch(anonymous, "fr", "/login/").status_code, 302)

    def test_the_language_form_does_not_end_or_extend_a_session(self):
        client = self.signed_in(self.member)
        session_key = client.session.session_key
        response = self.switch(client, "fr", "/")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(client.session.session_key, session_key)

    def test_no_page_has_an_inline_script_or_style_in_any_language(self):
        for code in CODES:
            self.switch(self.client, code)
            for url in (LOGIN, "/this/page/does/not/exist/"):
                with self.subTest(language=code, url=url):
                    html = self.client.get(url).content.decode()
                    self.assertNotRegex(html, r"<script(?![^>]*\ssrc=\"/static/)")
                    self.assertNotRegex(html, r"<style|\sstyle=|\sonclick=|\sonchange=|javascript:")
                    self.assertNotIn("data:image", html)

    def test_the_menu_is_a_post_form_with_one_button_per_language(self):
        html = self.client.get(LOGIN).content.decode()
        form = re.search(r'<form class="lang-form"[^>]*>', html).group(0)
        self.assertIn('method="post"', form)
        self.assertIn(f'action="{SET_LANGUAGE}"', form)
        self.assertEqual(html.count('name="language"'), 4)
        for flag in ("de", "fr", "it", "gb"):
            self.assertIn(f"/static/img/flags/{flag}.svg", html)
        # The language names are written in their own language and are not translated.
        for code in ("fr", "it", "en"):
            self.switch(self.client, code)
            names = [entry[0] for entry in menu_of(self.client.get(LOGIN).content.decode())[1].values()]
            self.assertEqual(names, ["Deutsch", "Français", "Italiano", "English"])

    def test_the_server_error_page_has_no_language_form_but_the_colour_button(self):
        # Rendered without a request (and so without a CSRF token): a form would be refused.
        with translation.override("fr"):
            html = render_to_string("500.html")
        self.assertEqual(html_lang_of(html), "fr")
        self.assertNotIn('id="lang-btn"', html)
        self.assertNotIn("/language/", html)
        self.assertIn('id="theme-btn"', html)
