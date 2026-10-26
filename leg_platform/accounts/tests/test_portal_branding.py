"""The portal settings in the administration (D-073): the accessor and its precedence, the logo
view, the form (uploads, colours, name, website), the permissions, the audit entries, the server
configuration panel, the effect on pages and emails, and the absence of a data migration.

The validation of the files themselves is in ``test_logo_images``."""

import importlib
import json
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection
from django.db.migrations import RunPython, RunSQL
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings, tag
from django.test.utils import CaptureQueriesContext
from django.urls import resolve, reverse
from django.utils.csp import CSP

from leg_platform.accounts.models import GraphConfiguration, PortalSettings
from leg_platform import branding
from leg_platform.accounts import emails, second_factor
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    EmailText,
    SecurityEvent,
    SessionCodeFailure,
    User,
)
from leg_platform.accounts.portal_settings import (
    LOGO_VERSION_LENGTH,
    effective_colours,
    environment_values,
    legal_links,
    portal_values,
)
from leg_platform.accounts.portal_settings_form import PortalSettingsForm
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    ProvingClient,
    age_authentication,
    code_for,
    create_active_user,
)
from leg_platform.accounts.tests.test_logo_images import MINIMAL, make_png, svg
from leg_platform.branding import colour_problems, load_branding
from leg_platform.middleware import body_limit

SETTINGS_URL = reverse("admin:accounts_portalsettings_change", args=(1,))
GRAPH_URL = reverse("admin:accounts_graphconfiguration_change", args=(1,))
PREVIEW_CSS_URL = reverse("admin:accounts_portalsettings_preview_css")
LIGHT_LOGO_URL = reverse("instance_logo", kwargs={"variant": "light"})
DARK_LOGO_URL = reverse("instance_logo", kwargs={"variant": "dark"})
LOGIN_URL = reverse("accounts:login")
SERVER = {
    "INSTANCE_NAME": "Server name",
    "INSTANCE_BASE_URL": "https://server.example.org",
    "INSTANCE_WEBSITE_URL": "https://server.example.org/",
    "INSTANCE_PRIVACY_URL": "https://server.example.org/privacy",
}
GOOD = {
    "accent_light": "#004E86",
    "text_light": "#FFFFFF",
    "accent_dark": "#7FB5E8",
    "text_dark": "#0B1520",
    "highlight": "#CCCC11",
}
PNG = make_png()
PNG_DARK = make_png(width=128, height=64, colour_type=6)


def store_logos(light=PNG, dark=PNG_DARK, kinds=("png", "png")):
    """Write validated logos straight into the row (the form is tested separately)."""
    import hashlib

    row = PortalSettings.load()
    for variant, data, kind in (("light", light, kinds[0]), ("dark", dark, kinds[1])):
        setattr(row, f"logo_{variant}_data", data)
        setattr(row, f"logo_{variant}_type", kind)
        setattr(row, f"logo_{variant}_sha256", hashlib.sha256(data).hexdigest())
    row.save()
    return row


def form_data(**overrides):
    data = {
        "name": "",
        "website_url": "",
        "privacy_url": "",
        "imprint_url": "",
        "accent_light": "",
        "text_light": "",
        "accent_dark": "",
        "text_dark": "",
        "highlight": "",
        "_save": "Sichern",
    }
    data.update(overrides)
    return data


def upload(content, name="logo.png", content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def administrator():
    person = create_active_user("administrator@example.org")
    person.set_privileges(is_staff=True, is_superuser=True)
    client = ProvingClient()
    client.force_login(person)
    return client, person


def staff_with(email, *codenames):
    person = create_active_user(email)
    person.set_privileges(is_staff=True, is_superuser=False)
    person.user_permissions.set(Permission.objects.filter(codename__in=codenames))
    client = ProvingClient()
    client.force_login(person)
    return client, person


@override_settings(**SERVER)
@tag("mariadb")
class AccessorTests(TestCase):
    def test_an_empty_row_gives_exactly_the_server_configuration(self):
        for values in (portal_values(), environment_values()):
            self.assertEqual(values.name, "Server name")
            self.assertEqual(values.website_url, "https://server.example.org/")
            self.assertEqual(values.website_host, "server.example.org")
            self.assertEqual(values.privacy_url, "https://server.example.org/privacy")
            self.assertEqual(values.imprint_url, "")
            self.assertEqual(
                (values.accent_light, values.text_light, values.accent_dark, values.text_dark, values.highlight),
                ("#005BA2", "#FFFFFF", "#6AA9E0", "#0B1520", "#DDDD22"),
            )
            self.assertEqual((values.logo_light, values.logo_dark, values.uploaded_logos), ("", "", False))

    def test_the_database_value_wins_for_every_field(self):
        PortalSettings.objects.update_or_create(
            pk=1,
            defaults={
                "name": "Row name",
                "website_url": "https://row.example.org/",
                "privacy_url": "https://row.example.org/privacy",
                "imprint_url": "https://row.example.org/imprint",
                **GOOD,
            },
        )

        values = portal_values()

        self.assertEqual(values.name, "Row name")
        self.assertEqual(values.website_url, "https://row.example.org/")
        self.assertEqual(values.privacy_url, "https://row.example.org/privacy")
        self.assertEqual(values.imprint_url, "https://row.example.org/imprint")
        self.assertEqual(values.accent_light, "#004E86")
        self.assertEqual(values.accent_dark, "#7FB5E8")
        self.assertEqual(values.highlight, "#CCCC11")

    def test_an_empty_field_falls_back_field_by_field(self):
        PortalSettings.objects.update_or_create(
            pk=1, defaults={"name": "Row name", "accent_light": "#004E86", "text_light": "#FFFFFF"}
        )

        values = portal_values()

        self.assertEqual(values.name, "Row name")
        self.assertEqual(values.website_url, "https://server.example.org/")
        self.assertEqual(values.accent_light, "#004E86")
        self.assertEqual(values.accent_dark, "#6AA9E0")
        self.assertEqual(values.highlight, "#DDDD22")

    def test_the_text_colour_is_chosen_automatically_next_to_a_stored_accent(self):
        PortalSettings.objects.update_or_create(
            pk=1, defaults={"accent_light": "#004E86", "accent_dark": "#7FB5E8"}
        )

        values = portal_values()

        self.assertEqual(values.text_light, "#FFFFFF")
        self.assertEqual(values.text_dark, "#0B1520")
        # The rule of the function, with the one candidate that is better in each mode.
        self.assertEqual(branding.auto_text_colour("#004E86"), "#FFFFFF")
        self.assertEqual(branding.auto_text_colour("#7FB5E8"), "#0B1520")

    @override_settings(INSTANCE_BRANDING=load_branding({"INSTANCE_ACCENT_DARK": "#7FB5E8"}))
    def test_a_stored_text_colour_next_to_the_server_accent_is_checked_again(self):
        # The text colour passes alone (it is a hex colour) but not with the accent of the server.
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Row name", "text_dark": "#FFFFFF"})

        with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
            values = portal_values()

        self.assertEqual(values.text_dark, "#0B1520")
        # The fields that are fine are still used.
        self.assertEqual(values.name, "Row name")

    def test_values_written_past_the_form_are_not_used(self):
        for label, defaults, field, expected in (
            ("a name with a line break", {"name": "Evil\r\nBcc: someone@example.org"}, "name", "Server name"),
            ("a name with spaces around it", {"name": " Row name "}, "name", "Server name"),
            ("a website without https", {"website_url": "http://row.example.org/"}, "website_url", "https://server.example.org/"),
            ("a script address", {"privacy_url": "javascript:alert(1)"}, "privacy_url", "https://server.example.org/privacy"),
            ("an imprint with credentials", {"imprint_url": "https://a:b@row.example.org/"}, "imprint_url", ""),
        ):
            with self.subTest(label):
                PortalSettings.objects.all().delete()
                PortalSettings.objects.create(pk=1, **defaults)
                with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
                    values = portal_values()
                self.assertEqual(getattr(values, field), expected)

    def test_colours_that_fail_the_rules_fall_back_as_a_whole(self):
        for label, defaults in (
            ("not a colour", {"accent_light": "red"}),
            ("an injection", {"accent_light": "#0;}p{}"}),
            ("too weak on the light surfaces", {"accent_light": "#777777", "text_light": "#FFFFFF"}),
            ("too weak with the text", {"accent_light": "#004E86", "text_light": "#0B1520"}),
            ("too weak on the dark surfaces", {"accent_dark": "#2B6CB0", "text_dark": "#FFFFFF"}),
            ("a weak highlight", {"highlight": "#555500"}),
        ):
            with self.subTest(label):
                PortalSettings.objects.all().delete()
                PortalSettings.objects.create(pk=1, **defaults)
                with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
                    values = portal_values()
                self.assertEqual(
                    (values.accent_light, values.text_light, values.accent_dark, values.highlight),
                    ("#005BA2", "#FFFFFF", "#6AA9E0", "#DDDD22"),
                )

    def test_a_database_error_gives_the_server_configuration_and_never_raises(self):
        PortalSettings.objects.all().delete()
        PortalSettings.objects.create(pk=1, name="Row name")
        for error in (OperationalError("down"), RuntimeError("anything"), ValueError("odd")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(PortalSettings.objects, "filter", side_effect=error), self.assertLogs(
                    "leg_platform.accounts.portal_settings", "WARNING"
                ):
                    values = portal_values()
                self.assertEqual(values.name, "Server name")
                self.assertEqual(values.accent_light, "#005BA2")

    def test_an_unexpected_failure_while_applying_the_values_gives_the_server_configuration(self):
        PortalSettings.objects.all().delete()
        PortalSettings.objects.create(pk=1, name="Row name")
        with mock.patch(
            "leg_platform.accounts.portal_settings._stored_logos", side_effect=RuntimeError("boom")
        ), self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
            values = portal_values()

        self.assertEqual(values.name, "Server name")

    def test_one_query_per_request_and_no_query_for_the_files(self):
        store_logos()
        request = RequestFactory().get("/")

        with CaptureQueriesContext(self._connection()) as queries:
            first = portal_values(request)
            second = portal_values(request)
            legal_links(request)

        self.assertIs(first, second)
        self.assertEqual(len(queries), 1)
        sql = queries[0]["sql"]
        self.assertNotIn("logo_light_data", sql)
        self.assertNotIn("logo_dark_data", sql)

    @staticmethod
    def _connection():
        from django.db import connection

        return connection

    def test_it_works_without_a_request_and_reads_the_settings_when_it_is_called(self):
        self.assertEqual(portal_values().name, "Server name")
        with override_settings(INSTANCE_NAME="Another name"):
            self.assertEqual(portal_values().name, "Another name")
            self.assertEqual(environment_values().name, "Another name")

    def test_a_change_is_visible_at_once(self):
        self.assertEqual(portal_values().name, "Server name")
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "First"})
        self.assertEqual(portal_values().name, "First")
        PortalSettings.objects.filter(pk=1).update(name="Second")
        self.assertEqual(portal_values().name, "Second")
        PortalSettings.objects.filter(pk=1).update(name="")
        self.assertEqual(portal_values().name, "Server name")

    def test_the_values_are_immutable(self):
        values = portal_values()

        with self.assertRaises(AttributeError):
            values.name = "changed"

    def test_uploaded_logos_are_addresses_with_the_start_of_the_digest(self):
        row = store_logos()

        values = portal_values()

        self.assertTrue(values.uploaded_logos)
        self.assertEqual(values.logo_light, f"{LIGHT_LOGO_URL}?v={row.logo_light_sha256[:LOGO_VERSION_LENGTH]}")
        self.assertEqual(values.logo_dark, f"{DARK_LOGO_URL}?v={row.logo_dark_sha256[:LOGO_VERSION_LENGTH]}")

    def test_only_one_uploaded_logo_is_not_used(self):
        row = store_logos()
        PortalSettings.objects.filter(pk=1).update(logo_dark_sha256="", logo_dark_type="")

        with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
            values = portal_values()

        self.assertEqual((values.logo_light, values.logo_dark, values.uploaded_logos), ("", "", False))
        self.assertTrue(row.logo_light_sha256)

    def test_a_logo_with_a_bad_digest_or_type_is_not_used(self):
        store_logos()
        for field, value in (("logo_light_sha256", "NOT-HEX"), ("logo_light_type", "gif")):
            with self.subTest(field=field):
                store_logos()
                PortalSettings.objects.filter(pk=1).update(**{field: value})
                with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
                    self.assertFalse(portal_values().uploaded_logos)

    @override_settings(INSTANCE_BRANDING=load_branding({"INSTANCE_LOGO_LIGHT": "instance/l.svg", "INSTANCE_LOGO_DARK": "instance/d.svg"}))
    def test_the_logos_of_the_server_are_used_until_logos_are_uploaded(self):
        values = portal_values()
        self.assertEqual((values.logo_light, values.logo_dark), ("/static/instance/l.svg", "/static/instance/d.svg"))
        self.assertFalse(values.uploaded_logos)

        store_logos()
        values = portal_values()
        self.assertTrue(values.logo_light.startswith(LIGHT_LOGO_URL))

        PortalSettings.objects.filter(pk=1).update(
            logo_light_sha256="", logo_light_type="", logo_dark_sha256="", logo_dark_type=""
        )
        self.assertEqual(portal_values().logo_light, "/static/instance/l.svg")


class PreviewLogoBoxTests(SimpleTestCase):
    """The logo of the preview has an explicit size (D-073, browser review): an SVG file with only
    a viewBox has no intrinsic size, and a box with only a maximum showed it as 0 x 0 pixels."""

    def rule(self, selector):
        css = (Path(settings.BASE_DIR) / "leg_platform/static/css/admin-portal.css").read_text(encoding="utf-8")
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(match, selector)
        return match.group(1)

    def test_the_image_has_a_width_and_a_height_and_is_fitted_into_the_box(self):
        rule = self.rule(".portal-preview-logo img")

        self.assertRegex(rule, r"(?<![-\w])width:\s*\d+px")
        self.assertRegex(rule, r"(?<![-\w])height:\s*\d+px")
        self.assertRegex(rule, r"object-fit:\s*contain")
        self.assertNotRegex(rule, r"(?<![-\w])(width|height):\s*(auto|0)\b")

    def test_the_height_is_the_one_of_the_header_of_the_portal(self):
        frame = (Path(settings.BASE_DIR) / "leg_platform/static/css/frame.css").read_text(encoding="utf-8")
        header = re.search(r"\.frame-header \.portal-logo,[^{]*\{([^}]*)\}", frame).group(1)

        self.assertIn("height: 48px", header)
        self.assertIn("height: 48px", self.rule(".portal-preview-logo img"))

    def test_the_dark_plate_keeps_its_background(self):
        self.assertIn("background: var(--pv-plate)", self.rule(".portal-preview-plate"))


class SharedColourRulesTests(SimpleTestCase):
    """The environment, the form and the accessor use one implementation (``colour_problems``)."""

    DEFAULTS = dict(branding.COLOUR_DEFAULTS)

    def test_the_rules_give_the_same_verdict_at_startup_and_for_a_set_of_colours(self):
        names = branding.COLOUR_ENVIRONMENT_NAMES
        for field, value, fails in (
            ("accent_light", "red", True),
            ("accent_light", "#FFFFFF", True),
            ("accent_light", "#004E86", False),
            ("text_light", "#005BA2", True),
            ("accent_dark", "#000000", True),
            ("accent_dark", "#7FB5E8", False),
            ("highlight", "#1C1917", True),
            ("highlight", "#CCCC11", False),
            ("highlight", "#ffffff;}body{display:none", True),
        ):
            with self.subTest(field=field, value=value):
                colours = {**self.DEFAULTS, field: value}
                self.assertEqual(bool(colour_problems(colours)), fails)
                if fails:
                    with self.assertRaises(branding.ImproperlyConfigured):
                        load_branding({names[field]: value})
                else:
                    load_branding({names[field]: value})

    def test_a_problem_names_the_colour_that_has_to_change_and_the_ratio(self):
        weak = colour_problems({**self.DEFAULTS, "accent_light": "#777777"})
        self.assertIn(("accent_light", "accent_contrast"), [(p.field, p.code) for p in weak])
        self.assertLess(weak[0].ratio, 4.5)
        text = colour_problems({**self.DEFAULTS, "text_light": "#0B1520"})
        self.assertEqual([(p.field, p.code) for p in text], [("text_light", "text_contrast")])
        dot = colour_problems({**self.DEFAULTS, "highlight": "#555500"})
        self.assertEqual([(p.field, p.code) for p in dot], [("highlight", "highlight_contrast")])
        # A format problem stops the contrast checks.
        self.assertEqual([p.code for p in colour_problems({**self.DEFAULTS, "accent_dark": "blue"})], ["format"])

    def test_the_ratio_is_cut_and_never_rounded_up(self):
        self.assertEqual(branding.floor_ratio(4.499), 4.49)
        self.assertEqual(branding.floor_ratio(4.5), 4.5)
        self.assertEqual(branding.floor_ratio(6.9649), 6.96)

    def test_the_effective_colours_stored_win_and_the_text_is_automatic(self):
        base = load_branding({})
        self.assertEqual(effective_colours({}, base), {**self.DEFAULTS})
        colours = effective_colours({"accent_light": "#004E86"}, base)
        self.assertEqual((colours["accent_light"], colours["text_light"]), ("#004E86", "#FFFFFF"))
        colours = effective_colours({"accent_dark": "#7FB5E8", "text_dark": "#FFFFFF"}, base)
        self.assertEqual(colours["text_dark"], "#FFFFFF")

    def test_the_script_of_the_preview_uses_the_same_constants(self):
        from pathlib import Path

        script = (Path(settings.BASE_DIR) / "leg_platform/static/js/portal-preview.js").read_text(encoding="utf-8")
        for value in (*branding.LIGHT_SURFACES, *branding.DARK_SURFACES, branding.HIGHLIGHT_TEXT,
                      branding.LIGHT_TEXT_DEFAULT, branding.DARK_TEXT_DEFAULT, "0.04045", "0.2126", "0.7152", "0.0722"):
            with self.subTest(value=value):
                self.assertIn(value, script)
        self.assertIn(f"var MINIMUM = {branding.MIN_CONTRAST};", script)

    def test_the_script_uses_no_blob_and_no_inline_script(self):
        from pathlib import Path

        script = (Path(settings.BASE_DIR) / "leg_platform/static/js/portal-preview.js").read_text(encoding="utf-8")
        for forbidden in ("createObjectURL", "blob:", "innerHTML", "eval(", "document.write", "setAttribute(\"style\"", "cssText"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        self.assertEqual(settings.SECURE_CSP["img-src"], [CSP.SELF])
        self.assertEqual(settings.SECURE_CSP["style-src"], [CSP.SELF])
        self.assertEqual(settings.SECURE_CSP["script-src"], [CSP.SELF])


@override_settings(**SERVER)
@tag("mariadb")
class LogoViewTests(TestCase):
    def get(self, url=LIGHT_LOGO_URL, **extra):
        return self.client.get(url, **extra)

    def test_a_png_is_served_with_a_fixed_type_and_the_protective_headers(self):
        row = store_logos()

        response = self.get(f"{LIGHT_LOGO_URL}?v={row.logo_light_sha256[:16]}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response["ETag"], f'"{row.logo_light_sha256}"')
        self.assertNotIn("public", response["Cache-Control"])
        self.assertIn("max-age=86400", response["Cache-Control"])

    def test_an_svg_is_served_as_an_image_and_never_as_html(self):
        store_logos(light=MINIMAL, dark=MINIMAL, kinds=("svg", "svg"))

        for url in (LIGHT_LOGO_URL, DARK_LOGO_URL):
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/svg+xml")
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")

    def test_the_dark_logo_is_the_dark_file(self):
        store_logos()

        self.assertEqual(self.get(DARK_LOGO_URL).content, PNG_DARK)

    def test_no_logo_is_not_found_and_never_html_of_a_file(self):
        self.assertEqual(self.get().status_code, 404)
        PortalSettings.load()
        self.assertEqual(self.get().status_code, 404)
        self.assertEqual(self.get(DARK_LOGO_URL).status_code, 404)

    def test_a_wrong_address_digest_is_not_found_and_a_missing_one_is_not_cached_long(self):
        row = store_logos()

        for query in ("?v=0000000000000000", "?v=", "?v=" + row.logo_light_sha256, "?v=short"):
            with self.subTest(query=query):
                self.assertEqual(self.get(LIGHT_LOGO_URL + query).status_code, 404)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertNotIn("max-age=86400", response["Cache-Control"])

    def test_the_file_is_not_chosen_by_the_visitor(self):
        store_logos()
        for path in (
            "/branding/logo/other/",
            "/branding/logo/LIGHT/",
            "/branding/logo/light/../dark/",
            "/branding/logo/../../etc/passwd",
            "/branding/logo/light%2F..%2Fdark/",
            "/branding/logo/%2e%2e/",
            "/branding/logo/light/extra/",
            "/branding/logo/",
            "/branding/logo/light.png",
            "/branding/logo/1/",
            "/branding/logo/light\\..\\dark/",
            "/media/logo.png",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, 404, path)
        # Nothing serves a media directory: no setting for it and no route (Django's default).
        self.assertEqual(settings.MEDIA_ROOT, "")
        # Only the two variants resolve.
        self.assertEqual(resolve("/branding/logo/light/").kwargs, {"variant": "light"})

    def test_a_stored_file_that_does_not_match_its_digest_is_not_served(self):
        store_logos()
        PortalSettings.objects.filter(pk=1).update(logo_light_data=b"<html><script>alert(1)</script></html>")

        with self.assertLogs("leg_platform.presentation", "WARNING"):
            response = self.get()

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"alert(1)", response.content)

    def test_a_stored_type_that_is_not_an_image_type_is_not_served(self):
        store_logos()
        PortalSettings.objects.filter(pk=1).update(logo_light_type="gif")

        self.assertEqual(self.get().status_code, 404)

    def test_no_stored_content_is_ever_served_as_html(self):
        # Whatever the file holds, the type is the fixed one of the stored type.
        html = b"<html><body><script>alert(1)</script></body></html>"
        store_logos(light=html, dark=html, kinds=("svg", "png"))

        for url, expected in ((LIGHT_LOGO_URL, "image/svg+xml"), (DARK_LOGO_URL, "image/png")):
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response["Content-Type"], expected)
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("text/html", response["Content-Type"])

    def test_a_conditional_request_gets_not_modified_with_the_same_headers(self):
        row = store_logos()

        response = self.get(HTTP_IF_NONE_MATCH=f'"{row.logo_light_sha256}"')

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["ETag"], f'"{row.logo_light_sha256}"')

    def test_other_methods_are_refused_and_head_works(self):
        store_logos()

        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(LIGHT_LOGO_URL).status_code, 405)
        self.assertEqual(self.client.head(LIGHT_LOGO_URL).status_code, 200)

    def test_it_needs_no_login_and_no_session_cookie(self):
        store_logos()

        response = Client().get(LIGHT_LOGO_URL)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.cookies)

    def test_the_response_has_no_set_cookie_and_no_vary_cookie_for_anyone(self):
        # Security review of D-073, L-2: the view does not touch the session, also not to write
        # the last activity (once a minute for a logged-in account), so nothing of a session can
        # reach a shared cache.
        row = store_logos()
        administrator_client, _person = administrator()
        # An activity that is older than the write interval would make the timeout middleware
        # write the session (and Django set the cookie) on an ordinary page.
        session = administrator_client.session
        session["session_last_activity"] = session["session_last_activity"] - 3600
        session.save()
        ordinary_client = Client()
        ordinary_client.force_login(create_active_user("ordinary.person@example.org"))
        ordinary_session = ordinary_client.session
        ordinary_session["session_last_activity"] = ordinary_session["session_last_activity"] - 3600
        ordinary_session.save()
        urls = (
            LIGHT_LOGO_URL,
            f"{LIGHT_LOGO_URL}?v={row.logo_light_sha256[:16]}",
            DARK_LOGO_URL,
        )

        for label, client in (
            ("anonymous", Client()),
            ("administrator", administrator_client),
            ("ordinary account", ordinary_client),
        ):
            for url in urls:
                for method in ("get", "head"):
                    with self.subTest(client=label, url=url, method=method):
                        response = getattr(client, method)(url)
                        self.assertEqual(response.status_code, 200)
                        self.assertNotIn("Set-Cookie", response.headers)
                        self.assertFalse(response.cookies)
                        self.assertNotIn("cookie", response.get("Vary", "").lower())
        # The premise of the test: an ordinary page does write the cookie for the same session.
        self.assertTrue(ordinary_client.get(reverse("home")).cookies)

    def test_a_logged_in_session_is_neither_extended_nor_ended_by_the_logo(self):
        store_logos()
        client, _person = administrator()
        session = client.session
        old = session["session_last_activity"] - 3600
        session["session_last_activity"] = old
        session.save()

        client.get(LIGHT_LOGO_URL)

        self.assertEqual(client.session["session_last_activity"], old)
        # An expired session is not ended by a request for the logo, and the logo is still public.
        session = client.session
        session["session_last_activity"] = old - 10**6
        session.save()
        self.assertEqual(client.get(LIGHT_LOGO_URL).status_code, 200)
        self.assertIn("_auth_user_id", client.session)

    def test_a_wrong_version_and_a_matching_conditional_request_do_not_read_or_hash_the_file(self):
        # Security review of D-073, L-1: the file (up to 1 MB) is only read for a body.
        row = store_logos()
        digest = row.logo_light_sha256
        with mock.patch("leg_platform.presentation._logo_data") as data, mock.patch(
            "leg_platform.presentation.hashlib.sha256"
        ) as sha:
            for query in ("?v=0000000000000000", "?v=short", "?v="):
                self.assertEqual(self.get(LIGHT_LOGO_URL + query).status_code, 404)
            self.assertEqual(self.get(HTTP_IF_NONE_MATCH=f'"{digest}"').status_code, 304)
            self.assertEqual(
                self.get(f"{LIGHT_LOGO_URL}?v={digest[:16]}", HTTP_IF_NONE_MATCH=f'"{digest}"').status_code, 304
            )
            self.assertEqual(self.get(DARK_LOGO_URL + "?v=0000000000000000").status_code, 404)

        data.assert_not_called()
        sha.assert_not_called()

    def test_the_queries_of_a_conditional_request_do_not_select_the_file(self):
        row = store_logos()

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.get(HTTP_IF_NONE_MATCH=f'"{row.logo_light_sha256}"').status_code, 304)
            self.assertEqual(self.get(LIGHT_LOGO_URL + "?v=0000000000000000").status_code, 404)

        self.assertTrue(queries)
        self.assertFalse([query for query in queries if "logo_light_data" in query["sql"]])

    def test_a_body_is_only_served_after_the_file_was_checked_against_its_digest(self):
        row = store_logos()

        response = self.get(f"{LIGHT_LOGO_URL}?v={row.logo_light_sha256[:16]}")

        self.assertEqual(response.content, PNG)
        # A conditional request with another ETag still gets the body, which is checked.
        self.assertEqual(self.get(HTTP_IF_NONE_MATCH='"other"').content, PNG)
        # An empty file with a digest is not served.
        PortalSettings.objects.filter(pk=1).update(logo_light_data=b"")
        self.assertEqual(self.get().status_code, 404)

    def test_a_failing_database_gives_not_found_and_no_exception(self):
        with mock.patch("leg_platform.presentation._logo_row", side_effect=OperationalError("down")), self.assertLogs(
            "leg_platform.presentation", "WARNING"
        ):
            self.assertEqual(self.get().status_code, 404)

    def test_the_logo_is_shown_to_an_account_with_an_incomplete_second_factor_or_a_forced_password_change(self):
        store_logos()
        staff = create_active_user("staff.person@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        without_code = Client()
        without_code.force_login(staff)  # no completed second factor
        forced = create_active_user("forced.person@example.org")
        forced.password_change_required = True
        forced.save()
        forced_client = Client()
        forced_client.force_login(User.objects.get(pk=forced.pk))

        for client in (without_code, forced_client):
            with self.subTest(client=client):
                self.assertEqual(client.get(reverse("home")).status_code, 302)
                response = client.get(LIGHT_LOGO_URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/png")


@override_settings(**SERVER)
@tag("mariadb")
class StylesheetTests(TestCase):
    def test_the_stylesheet_shows_the_effective_colours_and_a_change_at_once(self):
        url = reverse("instance_stylesheet")
        self.assertContains(self.client.get(url), "--accent:#005BA2;")
        PortalSettings.objects.update_or_create(pk=1, defaults={"accent_light": "#004E86", "text_light": "#FFFFFF"})

        response = self.client.get(url)

        self.assertContains(response, ":root{--accent:#004E86;--accent-rgb:0, 78, 134;--on-accent:#FFFFFF;")
        self.assertContains(response, "--accent:#6AA9E0;")

    def test_the_stylesheet_has_an_etag_and_asks_again_every_time(self):
        url = reverse("instance_stylesheet")

        response = self.client.get(url)
        etag = response["ETag"]
        again = self.client.get(url, HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertEqual(again.status_code, 304)
        PortalSettings.objects.update_or_create(pk=1, defaults={"highlight": "#CCCC11"})
        changed = self.client.get(url, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed["ETag"], etag)

    def test_a_stored_value_that_is_not_a_colour_never_reaches_the_stylesheet(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"accent_light": "#0;}p{}"})

        with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
            response = self.client.get(reverse("instance_stylesheet"))

        self.assertNotContains(response, "p{}")
        self.assertContains(response, "--accent:#005BA2;")


@override_settings(**SERVER)
@tag("mariadb")
class EffectOnPagesAndEmailsTests(TestCase):
    def test_the_name_of_the_settings_is_on_the_pages_and_in_the_administration(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Beispiel Gemeinde"})
        client, _person = administrator()

        login = self.client.get(LOGIN_URL).content.decode()
        admin = client.get(reverse("admin:index")).content.decode()

        self.assertIn("Beispiel Gemeinde", login)
        self.assertNotIn("Server name", login)
        self.assertIn("Beispiel Gemeinde", admin)

    def test_the_logo_and_the_website_of_the_settings_are_on_the_entry_page(self):
        row = store_logos()
        PortalSettings.objects.filter(pk=1).update(website_url="https://row.example.org/")

        page = self.client.get(LOGIN_URL).content.decode()

        self.assertIn(f'src="{LIGHT_LOGO_URL}?v={row.logo_light_sha256[:16]}"', page)
        self.assertIn(f'src="{DARK_LOGO_URL}?v={row.logo_dark_sha256[:16]}"', page)
        self.assertIn(">row.example.org</a>", page)

    def test_a_page_reads_the_settings_once(self):
        store_logos()
        PortalSettings.objects.filter(pk=1).update(name="Beispiel Gemeinde", website_url="https://row.example.org/")

        with CaptureQueriesContext(self._connection()) as queries:
            self.client.get(LOGIN_URL)

        self.assertEqual(len([q for q in queries if "accounts_portalsettings" in q["sql"]]), 1)

    @staticmethod
    def _connection():
        from django.db import connection

        return connection

    def test_a_change_of_the_name_is_on_the_next_page_without_a_restart(self):
        self.assertIn("Server name", self.client.get(LOGIN_URL).content.decode())
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "First name"})
        self.assertIn("First name", self.client.get(LOGIN_URL).content.decode())
        PortalSettings.objects.filter(pk=1).update(name="Second name")
        page = self.client.get(LOGIN_URL).content.decode()
        self.assertIn("Second name", page)
        self.assertNotIn("First name", page)

    def test_the_subject_of_an_email_uses_the_name_of_the_settings(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Beispiel Gemeinde"})

        subject, body = emails.preview(EmailText.Key.PASSWORD_CHANGED, "", "")

        self.assertIn("Beispiel Gemeinde", subject)
        self.assertNotIn("Server name", subject)

    def test_the_contact_address_of_an_email_is_the_website_of_the_settings(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"website_url": "https://row.example.org/"})

        self.assertEqual(emails._values({})["contact_url"], "https://row.example.org/")

    def test_a_name_with_a_line_break_can_never_reach_a_subject(self):
        # Written past the form: the accessor checks the name again (the injection rule).
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Evil\r\nBcc: someone@example.org"})

        with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
            subject, _body = emails.preview(EmailText.Key.PASSWORD_CHANGED, "", "")

        self.assertNotIn("Bcc", subject)
        self.assertNotIn("\n", subject)
        self.assertIn("Server name", subject)

    def test_the_issuer_of_a_new_authenticator_is_the_name_of_the_settings(self):
        from leg_platform.accounts.models import AuthenticatorDevice

        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Beispiel: Gemeinde"})
        person = create_active_user("issuer.person@example.org")
        device = AuthenticatorDevice.objects.create_device(person, confirmed=False)

        self.assertIn("issuer=Beispiel%20Gemeinde", device.config_url)

    def test_the_error_pages_use_the_settings_when_there_is_a_request_and_the_server_without_one(self):
        from django.http import Http404
        from django.views.defaults import page_not_found, server_error

        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Beispiel Gemeinde"})
        store_logos()

        not_found = page_not_found(RequestFactory().get("/x/"), Http404()).content.decode()
        with override_settings(ROOT_URLCONF="leg_platform.tests.urls", DEBUG=False):
            failed = server_error(RequestFactory().get("/error/")).content.decode()

        self.assertIn("Beispiel Gemeinde", not_found)
        self.assertIn(LIGHT_LOGO_URL, not_found)
        # The page of a server error needs no database (D-054): the settings of the server.
        self.assertIn("Server name", failed)
        self.assertNotIn("Beispiel Gemeinde", failed)
        self.assertNotIn(LIGHT_LOGO_URL, failed)


@override_settings(**SERVER)
@tag("mariadb")
class AdminFormTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator()

    def post(self, **overrides):
        return self.client.post(SETTINGS_URL, form_data(**overrides))

    def row(self):
        return PortalSettings.objects.get(pk=1)

    def test_the_page_has_the_groups_the_help_and_the_server_hints(self):
        page = self.client.get(SETTINGS_URL).content.decode()

        for text in (
            "Name und Website",
            "Datenschutz und Impressum",
            "Farben",
            "Vorschau",
            "Serverkonfiguration (nur lesen)",
            "Hier legen Sie fest, wie sich dieses Portal präsentiert. Änderungen sind sofort für alle Besucher sichtbar.",
            "Name des Portals",
            "Adresse der Website",
            "Logo für den hellen Modus",
            "Logo für den dunklen Modus",
            "Akzentfarbe, heller Modus",
            "Textfarbe auf der Akzentfarbe, dunkler Modus",
            "Hervorhebungsfarbe",
            "Farben der Serverkonfiguration wieder verwenden",
            "Hochgeladene Logos entfernen und die Serverkonfiguration verwenden.",
            "Serverkonfiguration: Server name",
            "Serverkonfiguration: #005BA2",
            "Serverkonfiguration: https://server.example.org/privacy",
            "PNG oder SVG, bis 1 MB (PNG) oder 200 KB (SVG). Ein PNG sollte mindestens 720 px breit sein. Verwenden Sie die Version mit dunkler Schrift.",
            "mit heller Schrift.",
            "Leer lassen: weisse oder dunkle Schrift wird automatisch gewählt.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn("js/portal-preview.js", page)
        self.assertIn(PREVIEW_CSS_URL, page)
        self.assertNotIn("ß", page)
        # Save and continue editing; no "add another" and no delete.
        self.assertIn("_continue", page)
        self.assertNotIn("_addanother", page)
        self.assertNotIn("deletelink", page)

    def test_a_value_of_the_server_is_escaped_in_the_hint(self):
        with override_settings(INSTANCE_NAME="A <b>&</b> name"):
            page = self.client.get(SETTINGS_URL).content.decode()

        self.assertNotIn("<b>&</b>", page)
        self.assertIn("A &lt;b&gt;&amp;&lt;/b&gt; name", page)

    def test_the_text_fields_and_the_colours_are_saved_and_visible_at_once(self):
        response = self.post(
            name="  Beispiel Gemeinde ",
            website_url="https://www.beispiel.example/",
            privacy_url="https://www.beispiel.example/datenschutz",
            imprint_url="https://www.beispiel.example/impressum",
            accent_light="#004e86",
            accent_dark="#7fb5e8",
            highlight="#cccc11",
        )

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.name, "Beispiel Gemeinde")
        self.assertEqual((row.accent_light, row.accent_dark, row.highlight), ("#004E86", "#7FB5E8", "#CCCC11"))
        # The text colours were left empty: they are chosen automatically.
        self.assertEqual((row.text_light, row.text_dark), ("", ""))
        values = portal_values()
        self.assertEqual((values.text_light, values.text_dark), ("#FFFFFF", "#0B1520"))
        page = self.client.get(LOGIN_URL).content.decode()
        self.assertIn("Beispiel Gemeinde", page)
        self.assertIn("beispiel.example</a>", page)
        self.assertContains(self.client.get(reverse("instance_stylesheet")), "--accent:#004E86;")

    def test_saving_says_what_happened_and_goes_back_to_the_page(self):
        response = self.client.post(SETTINGS_URL, form_data(name="Beispiel Gemeinde"), follow=True)

        self.assertContains(response, "Gespeichert. Die Änderungen sind jetzt auf allen Seiten sichtbar.")
        self.assertEqual(response.redirect_chain[-1][0], SETTINGS_URL)

    def test_an_empty_text_field_is_the_reset(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Row name", "website_url": "https://row.example.org/"})

        self.post()

        values = portal_values()
        self.assertEqual((values.name, values.website_url), ("Server name", "https://server.example.org/"))

    def test_a_wrong_name_is_refused_with_a_message(self):
        for name in ("Line\nbreak", "Tab\there", "Zero\u200bwidth", "Sep\u2028arator"):
            with self.subTest(name=name):
                response = self.post(name=name)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "keine Steuerzeichen enthalten")
        response = self.post(name="x" * 101)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "höchstens 100 Zeichen")
        self.assertFalse(PortalSettings.objects.exists() and self.row().name)

    def test_a_website_must_be_a_clean_https_address(self):
        response = self.post(website_url="http://www.example.org/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bitte geben Sie eine vollständige Adresse mit https:// ein.")
        for value in ("javascript:alert(1)", "https://a:b@example.org/", "https://exa mple.org/"):
            with self.subTest(value=value):
                response = self.post(website_url=value)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "errorlist")
        self.assertFalse(PortalSettings.objects.exists() and self.row().website_url)

    def test_wrong_colours_are_refused_with_the_messages_of_the_design(self):
        for field, value, message in (
            ("accent_light", "red", "Geben Sie die Farbe als # mit sechs Ziffern oder Buchstaben von A bis F ein, zum Beispiel #005BA2."),
            ("accent_light", "#12345", "Geben Sie die Farbe als # mit sechs Ziffern"),
            ("accent_light", "#0;}p{}", "Geben Sie die Farbe als # mit sechs Ziffern"),
            ("accent_light", "#ffffff;}body{display:none", "höchstens 7 Zeichen"),
            ("accent_light", "#777777", "Diese Akzentfarbe ist auf dem hellen Hintergrund zu schwach (Kontrast 4."),
            ("accent_dark", "#2B6CB0", "Diese Akzentfarbe ist auf dem dunklen Hintergrund zu schwach (Kontrast 3."),
            ("highlight", "#555500", "Die Hervorhebungsfarbe ist mit dunkler Schrift zu schwach (Kontrast 2."),
        ):
            with self.subTest(field=field, value=value):
                response = self.post(**{field: value})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
        # Never a decimal comma and never a ratio that is rounded up to the limit.
        page = self.post(accent_light="#777777").content.decode()
        self.assertRegex(page, r"Kontrast 4\.\d\d:1, verlangt sind 4\.5:1\)\. Wählen Sie eine dunklere Farbe\.")
        self.assertFalse(PortalSettings.objects.exists() and self.row().accent_light)

    def test_a_text_colour_that_is_too_weak_on_the_accent_is_refused(self):
        response = self.post(accent_light="#004E86", text_light="#0B1520")

        self.assertRegex(
            response.content.decode(),
            r"Die Textfarbe auf der Akzentfarbe ist zu schwach \(Kontrast 2\.\d\d:1, verlangt sind 4\.5:1\)\.",
        )
        # A text colour with the accent of the server is checked too.
        response = self.post(text_dark="#FFFFFF")
        self.assertContains(response, "Die Textfarbe auf der Akzentfarbe ist zu schwach")

    def test_the_server_colours_again_empties_every_colour(self):
        self.post(**GOOD)
        self.assertEqual(self.row().accent_light, "#004E86")

        response = self.post(**{**GOOD, "reset_colours": "on"})

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual([getattr(row, field) for field in PortalSettings.COLOUR_FIELDS], [""] * 5)
        self.assertEqual(portal_values().accent_light, "#005BA2")

    def test_the_reset_also_drops_a_wrong_value_that_was_typed(self):
        response = self.post(accent_light="not a colour", reset_colours="on")

        self.assertEqual(response.status_code, 302)

    def test_both_logos_are_uploaded_and_stored_as_they_are(self):
        response = self.client.post(
            SETTINGS_URL,
            form_data(logo_light_file=upload(PNG), logo_dark_file=upload(MINIMAL, "dark.svg", "image/svg+xml")),
        )

        self.assertEqual(response.status_code, 302)
        row = PortalSettings.objects.get(pk=1)
        self.assertEqual((row.logo_light_type, row.logo_dark_type), ("png", "svg"))
        self.assertEqual(bytes(row.logo_light_data), PNG)
        self.assertEqual(bytes(row.logo_dark_data), MINIMAL)
        self.assertEqual(len(row.logo_light_sha256), 64)
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).content, PNG)
        self.assertEqual(self.client.get(DARK_LOGO_URL)["Content-Type"], "image/svg+xml")
        # The page shows the saved logos in the preview and the status.
        page = self.client.get(SETTINGS_URL).content.decode()
        self.assertIn(f'<img src="{portal_values().logo_light}"', page)
        self.assertIn("Hochgeladen (PNG, Fingerabdruck", page)

    def test_the_type_comes_from_the_content_and_the_name_is_never_stored(self):
        name = "../../etc/evil\u202egnp.svg"
        response = self.client.post(
            SETTINGS_URL,
            form_data(logo_light_file=upload(PNG, name, "text/html"), logo_dark_file=upload(PNG_DARK, "x.html", "text/html")),
        )

        self.assertEqual(response.status_code, 302)
        row = PortalSettings.objects.get(pk=1)
        self.assertEqual((row.logo_light_type, row.logo_dark_type), ("png", "png"))
        for field in ("logo_light_type", "logo_light_sha256", "logo_dark_type", "logo_dark_sha256", "name"):
            self.assertNotIn("evil", getattr(row, field))
        self.assertNotContains(self.client.get(SETTINGS_URL), "evil")

    def test_one_logo_alone_is_refused(self):
        response = self.client.post(SETTINGS_URL, form_data(logo_light_file=upload(PNG)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Laden Sie beide Logos hoch oder keines. Das Portal braucht je eine Version für den hellen und den dunklen Modus.",
        )
        self.assertFalse(PortalSettings.objects.exists() and self.row().logo_light_sha256)

    def test_a_second_logo_next_to_a_stored_one_is_enough_to_replace_one(self):
        store_logos()
        new = make_png(width=200, height=100)

        response = self.client.post(SETTINGS_URL, form_data(logo_light_file=upload(new)))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).content, new)
        self.assertEqual(self.client.get(DARK_LOGO_URL).content, PNG_DARK)

    def test_a_logo_that_is_not_accepted_gives_a_message_and_changes_nothing(self):
        for label, content, name, message in (
            ("a script", svg("<script>alert(1)</script>"), "a.svg", "Diese SVG-Datei enthält Inhalte, die das Portal nicht annimmt (script)."),
            ("an event handler", svg("", ' onload="alert(1)"'), "a.svg", "nicht annimmt (onload)."),
            ("a doctype", b"<!DOCTYPE svg>" + MINIMAL, "a.svg", "nicht annimmt (doctype)."),
            ("a text file", b"hello", "a.png", "Es sind nur PNG- und SVG-Dateien erlaubt."),
            ("a cut png", PNG[:-20], "a.png", "Die Datei ist kein gültiges PNG-Bild."),
            ("a small png", make_png(width=10, height=10, image=b"\x00"), "a.png", "Das Bild muss zwischen 64 und 4000 Pixel breit und hoch sein."),
            ("a png with data behind it", PNG + b"<script>", "a.png", "Die Datei ist kein gültiges PNG-Bild."),
            ("an svg without a view box", b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>', "a.svg", "Das SVG braucht eine viewBox"),
        ):
            with self.subTest(label):
                response = self.client.post(
                    SETTINGS_URL, form_data(logo_light_file=upload(content, name), logo_dark_file=upload(PNG_DARK))
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
                self.assertFalse(PortalSettings.objects.exists() and self.row().logo_dark_sha256)
        script = self.client.post(
            SETTINGS_URL,
            form_data(logo_light_file=upload(svg("<script/>"), "a.svg"), logo_dark_file=upload(PNG_DARK)),
        )
        self.assertContains(script, "«einfaches SVG»")

    def test_a_file_above_the_limit_is_too_large(self):
        too_big_png = make_png()[:8] + b"\x00" * (1024 * 1024)
        too_big_svg = svg("<g/>" * 60000)

        for content, name, limit in ((too_big_png, "a.png", "1 MB"), (too_big_svg, "a.svg", "200 KB")):
            with self.subTest(limit=limit):
                response = self.client.post(
                    SETTINGS_URL, form_data(logo_light_file=upload(content, name), logo_dark_file=upload(PNG_DARK))
                )
                self.assertContains(response, f"Die Datei ist zu gross (höchstens {limit}).")

    def test_removing_the_logos_deletes_the_files_and_goes_back_to_the_server(self):
        store_logos()

        response = self.post(remove_logos="on")

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual((row.logo_light_data, row.logo_dark_data), (None, None))
        self.assertEqual((row.logo_light_sha256, row.logo_light_type, row.logo_dark_sha256, row.logo_dark_type), ("", "", "", ""))
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).status_code, 404)
        self.assertFalse(portal_values().uploaded_logos)

    def test_removing_and_uploading_at_the_same_time_is_refused(self):
        store_logos()

        response = self.client.post(
            SETTINGS_URL, form_data(remove_logos="on", logo_light_file=upload(PNG), logo_dark_file=upload(PNG_DARK))
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wählen Sie entweder neue Logos oder das Entfernen der hochgeladenen Logos, nicht beides.")
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).content, PNG)

    def test_saving_without_a_file_keeps_the_stored_logos(self):
        store_logos()

        self.post(name="Beispiel Gemeinde")

        self.assertEqual(self.client.get(LIGHT_LOGO_URL).content, PNG)
        self.assertEqual(self.client.get(DARK_LOGO_URL).content, PNG_DARK)

    def test_the_preview_style_sheet_has_the_saved_colours_and_is_not_cached(self):
        self.post(**GOOD)

        response = self.client.get(PREVIEW_CSS_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/css; charset=utf-8")
        text = response.content.decode()
        self.assertIn(".portal-preview-light{--accent:#004E86;", text)
        self.assertIn(".portal-preview-dark{--accent:#7FB5E8;", text)
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_policy_of_the_page_is_not_widened(self):
        response = self.client.get(SETTINGS_URL)

        policy = response["Content-Security-Policy"]
        self.assertNotIn("blob:", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertIn("img-src 'self'", policy)
        self.assertNotRegex(response.content.decode(), r"(?i)<script(?![^>]*\ssrc=)")
        self.assertNotRegex(response.content.decode(), r'(?i)\sstyle="')

    def test_the_upload_page_has_a_body_limit_before_the_form_is_read(self):
        # Every spelling of an address below the page is limited, not one path (review of D-073, M-1).
        limit = body_limit(SETTINGS_URL)
        self.assertEqual(limit, 3 * 1024 * 1024)
        for path in (
            SETTINGS_URL,
            "/admin/accounts/portalsettings/01/change/",
            "/admin/accounts/portalsettings/+1/change/",
            "/admin/accounts/portalsettings/%201/change/",
            "/admin/accounts/portalsettings/_31/change/",
            "/admin/accounts/portalsettings/add/",
            "/admin/accounts/portalsettings/1/history/",
        ):
            with self.subTest(path=path):
                response = Client(enforce_csrf_checks=True).post(
                    path, data=b"x" * (limit + 1), content_type="multipart/form-data; boundary=x"
                )

                self.assertEqual(response.status_code, 413)

    def test_the_prefix_of_the_body_limit_covers_every_route_of_the_page(self):
        names = ("change", "history", "delete")
        for name in names:
            path = reverse(f"admin:accounts_portalsettings_{name}", args=(1,))
            self.assertIsNotNone(body_limit(path), path)
        for name in ("add", "changelist", "preview_css"):
            path = reverse(f"admin:accounts_portalsettings_{name}")
            self.assertIsNotNone(body_limit(path), path)

    def test_an_authenticated_request_of_the_page_is_not_limited_below_the_limit(self):
        response = self.post(name="Beispiel Gemeinde")

        self.assertEqual(response.status_code, 302)


@override_settings(**SERVER)
@tag("mariadb")
class SaveProofTests(TestCase):
    """Saving the portal settings needs one proof of identity (D-088); it replaced the rule "log in
    again within five minutes" of D-073, which forced a logout and login."""

    def setUp(self):
        self.client, self.administrator = administrator()

    def row_is_empty(self):
        return not PortalSettings.objects.exclude(name="").exists()

    def test_the_session_age_does_not_matter_any_more(self):
        age_authentication(self.client, 3600)

        response = self.client.post(SETTINGS_URL, form_data(name="Changed"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PortalSettings.objects.get(pk=1).name, "Changed")

    def test_the_page_has_no_logout_warning_and_no_logout_form_any_more(self):
        age_authentication(self.client, 3600)

        html = self.client.get(SETTINGS_URL).content.decode()

        self.assertNotIn("portal-login-warning", html)
        self.assertNotIn("portal-logout-form", html)
        self.assertNotIn("in den letzten 5 Minuten", html)
        # The field of the proof is there: the code of the app (staff always have a second factor).
        self.assertIn('name="otp_token"', html)
        self.assertNotIn('name="current_password"', html)
        self.assertIn("Bestätigung", html)

    def test_a_save_without_the_proof_is_refused_and_changes_nothing(self):
        self.client.auto_proof = False

        response = self.client.post(SETTINGS_URL, form_data(name="Changed", **GOOD))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Geben Sie den 2FA-Authentifizierungscode ein.")
        self.assertTrue(self.row_is_empty())
        self.assertFalse(SecurityEvent.objects.filter(event=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED).exists())

    def test_a_wrong_code_is_refused_counted_and_nothing_is_saved(self):
        self.client.auto_proof = False

        response = self.client.post(SETTINGS_URL, form_data(name="Changed", otp_token="000000"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Der 2FA-Authentifizierungscode ist falsch oder abgelaufen.")
        self.assertTrue(self.row_is_empty())
        self.assertEqual(SessionCodeFailure.objects.get(account=self.administrator).failures, 1)

    def test_the_password_is_no_proof_for_an_account_with_a_second_factor(self):
        self.client.auto_proof = False

        response = self.client.post(SETTINGS_URL, form_data(name="Changed", current_password=PASSWORD))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.row_is_empty())

    def test_the_fifth_wrong_code_ends_the_session(self):
        self.client.auto_proof = False
        for _attempt in range(second_factor.SESSION_CODE_LIMIT):
            response = self.client.post(SETTINGS_URL, form_data(name="Changed", otp_token="000000"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(LOGIN_URL))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(self.row_is_empty())

    def test_a_staff_user_with_only_the_change_right_needs_the_proof_as_well(self):
        client, _person = staff_with("change.person@example.org", "view_portalsettings", "change_portalsettings")
        client.auto_proof = False

        response = client.post(SETTINGS_URL, form_data(name="Changed"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.row_is_empty())
        client.auto_proof = True
        self.assertEqual(client.post(SETTINGS_URL, form_data(name="Changed")).status_code, 302)

    def test_a_wrong_code_stores_no_upload_even_for_an_old_session(self):
        age_authentication(self.client, 3600)
        self.client.auto_proof = False

        with mock.patch("leg_platform.accounts.portal_settings_form.validate_logo") as validate:
            self.client.post(
                SETTINGS_URL, form_data(logo_light_file=upload(PNG), logo_dark_file=upload(PNG_DARK), otp_token="000000")
            )

        # The wrong code is only looked at last, so the upload was validated; but nothing was stored.
        self.assertFalse(PortalSettings.objects.exclude(logo_light_sha256="").exists())
        self.assertTrue(validate.called)

    def test_a_code_is_not_used_up_by_a_form_with_another_error(self):
        device = AuthenticatorDevice.objects.get(user=self.administrator)
        self.client.auto_proof = False
        code = code_for(device)

        refused = self.client.post(
            SETTINGS_URL, form_data(name="Changed", accent_light="#FFFFFF", text_light="#FFFFFF", otp_token=code)
        )

        self.assertEqual(refused.status_code, 200)
        # The code was not checked, so the corrected form can use the same code.
        again = self.client.post(SETTINGS_URL, form_data(name="Changed", otp_token=code, **GOOD))
        self.assertEqual(again.status_code, 302)

    def test_the_form_without_a_request_fails_closed(self):
        form = PortalSettingsForm(form_data(name="Changed"), instance=PortalSettings.load_without_logo_files())

        self.assertFalse(form.is_valid())
        self.assertIn("proof", [error.code for error in form.non_field_errors().as_data()])

    def form_for(self, person, data):
        request = mock.Mock(user=person)
        form_class = type("ProvedForm", (PortalSettingsForm,), {"request": request, "actor": person})
        return form_class(data, instance=PortalSettings.load_without_logo_files())

    def test_an_account_without_a_second_factor_confirms_with_the_password(self):
        person = create_active_user("password.person@example.org")

        with mock.patch.object(second_factor, "verify_password", return_value=False) as verify:
            wrong = self.form_for(person, form_data(name="Changed", current_password="wrong"))
            self.assertFalse(wrong.is_valid())
            self.assertIn("current_password", wrong.errors)
            self.assertEqual(wrong.proof, "password")
            verify.assert_called_once()
        with mock.patch.object(second_factor, "verify_password", return_value=True):
            right = self.form_for(person, form_data(name="Changed", current_password=PASSWORD))
            self.assertTrue(right.is_valid(), right.errors)
        # No password, nothing else is looked at.
        with mock.patch("leg_platform.accounts.portal_settings_form.validate_logo") as validate:
            missing = self.form_for(person, form_data(name="Changed", logo_light_file=upload(PNG)))
            self.assertFalse(missing.is_valid())
            validate.assert_not_called()

    def test_a_person_who_can_only_view_gets_no_proof_field_and_still_cannot_save(self):
        client, _person = staff_with("view.person@example.org", "view_portalsettings")

        html = client.get(SETTINGS_URL).content.decode()
        self.assertNotIn('name="current_password"', html)
        self.assertNotIn('name="otp_token"', html)
        self.assertEqual(client.post(SETTINGS_URL, form_data(name="Changed")).status_code, 403)
        self.assertTrue(self.row_is_empty())

    def test_the_proof_field_is_never_stored_on_the_model(self):
        self.assertNotIn("current_password", [field.name for field in PortalSettings._meta.get_fields()])
        self.assertNotIn("otp_token", [field.name for field in PortalSettings._meta.get_fields()])


@override_settings(**SERVER)
@tag("mariadb")
class PermissionTests(TestCase):
    """Negative authorization: no right, view only, change only, anonymous (D-073)."""

    def attempt(self, client, **data):
        payload = form_data(name="Changed by test", **data)
        return {
            "get": client.get(SETTINGS_URL),
            "post": client.post(SETTINGS_URL, payload),
            "list": client.get(reverse("admin:accounts_portalsettings_changelist")),
            "preview css": client.get(PREVIEW_CSS_URL),
            "history": client.get(reverse("admin:accounts_portalsettings_history", args=(1,))),
            "add": client.get(reverse("admin:accounts_portalsettings_add")),
            "delete": client.post(reverse("admin:accounts_portalsettings_delete", args=(1,)), {"post": "yes"}),
        }

    def unchanged(self):
        return not PortalSettings.objects.filter(pk=1).exclude(name="").exists()

    def test_a_staff_user_without_the_right_gets_403_everywhere(self):
        client, _person = staff_with("none.person@example.org", "view_user")

        for label, response in self.attempt(client).items():
            with self.subTest(label):
                self.assertEqual(response.status_code, 403)
        self.assertTrue(self.unchanged())
        self.assertNotContains(client.get(reverse("admin:index")), "Portal-Einstellungen")

    def test_with_only_the_view_right_the_page_is_read_only_and_nothing_can_be_changed(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Stored"})
        client, _person = staff_with("view.person@example.org", "view_portalsettings")

        results = self.attempt(client)

        self.assertEqual(results["get"].status_code, 200)
        page = results["get"].content.decode()
        self.assertNotIn('name="logo_light_file"', page)
        self.assertNotIn('name="remove_logos"', page)
        self.assertNotIn('name="name"', page)
        self.assertNotIn('name="_save"', page)
        self.assertIn("Stored", page)
        self.assertEqual(results["post"].status_code, 403)
        self.assertEqual(results["preview css"].status_code, 200)
        for label in ("history", "add", "delete"):
            self.assertEqual(results[label].status_code, 403, label)
        self.assertEqual(PortalSettings.objects.get(pk=1).name, "Stored")
        self.assertContains(client.get(reverse("admin:index")), "Portal-Einstellungen")

    def test_with_the_change_right_a_staff_user_saves_but_cannot_add_delete_or_read_the_history(self):
        client, _person = staff_with("change.person@example.org", "change_portalsettings")

        results = self.attempt(client)

        self.assertEqual(results["get"].status_code, 200)
        self.assertEqual(results["post"].status_code, 302)
        self.assertEqual(PortalSettings.objects.get(pk=1).name, "Changed by test")
        for label in ("history", "add", "delete"):
            self.assertEqual(results[label].status_code, 403, label)
        self.assertNotIn("/history/", results["get"].content.decode())

    def test_a_system_administrator_passes_without_any_assigned_right(self):
        client, _person = administrator()

        results = self.attempt(client)

        self.assertEqual(results["get"].status_code, 200)
        self.assertEqual(results["post"].status_code, 302)
        self.assertEqual(results["history"].status_code, 200)
        self.assertIn("/history/", results["get"].content.decode())
        self.assertEqual(results["add"].status_code, 403)
        self.assertEqual(results["delete"].status_code, 403)

    def test_a_visitor_and_an_ordinary_account_are_refused(self):
        ordinary = Client()
        ordinary.force_login(create_active_user("ordinary.person@example.org"))

        for client in (Client(), ordinary):
            for label, response in self.attempt(client).items():
                with self.subTest(label=label):
                    self.assertIn(response.status_code, (302, 403))
        self.assertTrue(self.unchanged())

    def test_a_staff_user_without_a_completed_second_factor_cannot_use_the_rights(self):
        person = create_active_user("nofactor.person@example.org")
        person.set_privileges(is_staff=True, is_superuser=False)
        person.user_permissions.set(Permission.objects.filter(codename__in=("view_portalsettings", "change_portalsettings")))
        client = Client()
        client.force_login(person)

        response = client.post(SETTINGS_URL, form_data(name="Changed by test"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.unchanged())

    def test_a_forged_object_number_gives_nothing(self):
        client, _person = administrator()

        for number in ("2", "0", "abc"):
            with self.subTest(number=number):
                url = reverse("admin:accounts_portalsettings_change", args=(number,))
                self.assertIn(client.get(url).status_code, (302, 404))
        self.assertFalse(PortalSettings.objects.filter(pk=2).exists())

    def test_the_rights_are_assignable_to_a_group_and_the_group_page_explains_them(self):
        client, _person = administrator()

        page = client.get(reverse("admin:auth_group_add")).content.decode()

        self.assertIn("Portal-Einstellungen: ansehen", page)
        self.assertIn("Portal-Einstellungen: ändern", page)
        self.assertIn("Name, Logo, Farben und Adressen des Portals ändern", page)
        self.assertIn(
            "Dieses Recht ändert, was alle Besucher sehen und welche Links das Portal zeigt. "
            "Geben Sie es nur Personen, die für das Portal verantwortlich sind.",
            page,
        )
        self.assertNotIn("die Portal-Einstellungen und den E-Mail-Versand ändern", page)

    def test_no_group_holds_the_right_after_the_migrations(self):
        self.assertFalse(Group.objects.filter(permissions__content_type__model="portalsettings").exists())
        self.assertEqual(Group.objects.count(), 0)

    def test_the_codenames_are_english_and_only_view_and_change_exist(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="accounts", content_type__model="portalsettings")
            .values_list("codename", flat=True)
        )

        self.assertEqual(codenames, {"view_portalsettings", "change_portalsettings"})

    def test_obsolete_add_and_delete_rows_of_an_older_database_offer_and_grant_nothing(self):
        # A database that was created before D-073 still has the default add and delete rows
        # (review of D-073, L-7): they are not offered on the pages of rights, and holding them
        # opens nothing, because the administration refuses add and delete for everyone.
        from django.contrib.contenttypes.models import ContentType

        from leg_platform.accounts.permission_names import assignable_permissions_filter

        content_type = ContentType.objects.get_for_model(PortalSettings)
        rows = [
            Permission.objects.get_or_create(
                content_type=content_type, codename=f"{action}_portalsettings", defaults={"name": f"Can {action}"}
            )[0]
            for action in ("add", "delete")
        ]
        offered = Permission.objects.filter(assignable_permissions_filter())
        self.assertFalse(offered.filter(pk__in=[row.pk for row in rows]).exists())
        self.assertTrue(offered.filter(codename="change_portalsettings", content_type=content_type).exists())
        client, person = staff_with("old.person@example.org", "view_portalsettings")
        person.user_permissions.add(*rows)

        self.assertEqual(client.get(reverse("admin:accounts_portalsettings_add")).status_code, 403)
        self.assertEqual(
            client.post(reverse("admin:accounts_portalsettings_delete", args=(1,)), {"post": "yes"}).status_code, 403
        )


@override_settings(**SERVER)
@tag("mariadb")
class ServerConfigurationPanelTests(TestCase):
    def test_only_a_system_administrator_sees_the_panel(self):
        client, _person = administrator()
        staff, _staff = staff_with("change.person@example.org", "view_portalsettings", "change_portalsettings")

        admin_page = client.get(SETTINGS_URL).content.decode()
        staff_page = staff.get(SETTINGS_URL).content.decode()

        self.assertIn("Serverkonfiguration (nur lesen)", admin_page)
        self.assertIn("Basisadresse des Portals", admin_page)
        self.assertNotIn("Serverkonfiguration (nur lesen)", staff_page)
        self.assertNotIn("Basisadresse des Portals", staff_page)

    def test_a_staff_user_cannot_change_the_sender_or_the_contact_address(self):
        # The E-Mail chapter is for system administrators only (D-116).
        staff, _staff = staff_with("change.addresses@example.org", "view_portalsettings", "change_portalsettings")
        before = (GraphConfiguration.load().sender_address, GraphConfiguration.load().contact_address)
        response = staff.post(
            GRAPH_URL, {"sender_address": "x@evil.example.org", "contact_address": "x@evil.example.org"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual((GraphConfiguration.load().sender_address, GraphConfiguration.load().contact_address), before)

    def test_the_panel_shows_the_public_values_read_only_and_no_secret(self):
        client, _person = administrator()

        page = client.get(SETTINGS_URL).content.decode()

        panel = page[page.index('class="portal-server"'):]
        panel = panel[: panel.index("</table>")]
        for text in ("Umgebung", "local",
                     "Erlaubte Host-Namen", "E-Mail-Versand",
                     "Sicherheitsprotokoll: Aufbewahrung in Tagen", "HSTS, Sekunden"):
            with self.subTest(text=text):
                self.assertIn(text, panel)
        self.assertNotIn("<input", panel)
        for secret in (settings.SECRET_KEY, settings.DATABASES["default"]["PASSWORD"]):
            if secret:
                self.assertNotIn(secret, page)
        for name in ("DB_PASSWORD", "DB_USER", "DJANGO_SECRET_KEY", "BOOTSTRAP_SYSTEM_ADMIN_EMAIL",
                     "BOOTSTRAP_SYSTEM_ADMIN_PASSWORD", "ADDRESS_", "PRIVATE KEY"):
            self.assertNotIn(name, page)

    def test_the_addresses_are_not_fields_of_the_portal_settings_and_a_foreign_host_is_refused(self):
        # The base address comes from the environment and the sender and contact address from the E-Mail chapter (D-116).
        client, _person = administrator()
        page = client.get(SETTINGS_URL).content.decode()

        for name in ("allowed_hosts", "host", "csrf_trusted_origins", "email_mode", "base_url", "sender_address",
                     "contact_address"):
            self.assertNotIn(f'name="{name}"', page)
        before = (list(settings.ALLOWED_HOSTS), settings.INSTANCE_BASE_URL)
        client.post(SETTINGS_URL, form_data(allowed_hosts="evil.example", base_url="https://evil.example"))
        self.assertEqual((list(settings.ALLOWED_HOSTS), settings.INSTANCE_BASE_URL), before)
        self.assertFalse([f.name for f in PortalSettings._meta.get_fields() if "host" in f.name])


@override_settings(**SERVER)
@tag("mariadb")
class AuditTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator()

    def saving(self, **extra):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(SETTINGS_URL, form_data(**extra))
        return response, "\n".join(logs.output)

    def events(self, name=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED):
        return SecurityEvent.objects.filter(event=name)

    def test_one_event_with_a_line_per_changed_field(self):
        response, text = self.saving(
            name="Beispiel Gemeinde",
            website_url="https://www.beispiel.example/secret?token=1",
            accent_light="#004E86",
            highlight="#CCCC11",
        )

        self.assertEqual(response.status_code, 302)
        events = self.events()
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertTrue(event.long_retention)
        self.assertEqual(event.actor_id, self.administrator.pk)
        self.assertIn('name changed, "" -> "Beispiel Gemeinde"', text)
        self.assertIn("website_url changed, host - -> www.beispiel.example", text)
        self.assertIn("accent_light changed, - -> #004E86", text)
        self.assertIn("highlight changed, - -> #CCCC11", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("token=1", text)

    def test_the_old_and_the_new_value_are_logged_and_an_unchanged_save_records_nothing(self):
        self.saving(name="First", accent_light="#004E86")
        _response, text = self.saving(name="Second", accent_light="#2B6CB0")
        self.assertIn('name changed, "First" -> "Second"', text)
        self.assertIn("accent_light changed, #004E86 -> #2B6CB0", text)
        self.assertEqual(self.events().count(), 2)

        self.client.post(SETTINGS_URL, form_data(name="Second", accent_light="#2B6CB0"))

        self.assertEqual(self.events().count(), 2)

    def test_a_name_is_quoted_so_that_it_cannot_add_a_line_to_the_log(self):
        PortalSettings.objects.update_or_create(pk=1, defaults={"name": "Old\nINJECTED line"})

        _response, text = self.saving(name="New")

        self.assertNotIn("\nINJECTED", text)
        self.assertIn(json.dumps("Old\nINJECTED line"), text)

    def test_a_reset_of_the_colours_is_one_line(self):
        self.saving(**GOOD)

        _response, text = self.saving(**GOOD, reset_colours="on")

        self.assertIn("colours reset to server configuration", text)
        self.assertNotIn("accent_light changed", text)

    def test_the_logos_are_logged_with_type_size_and_digest_start_and_never_the_content(self):
        sentinel = svg("<title>SECRET-SENTINEL-TITLE</title>")
        response, text = self.saving(
            logo_light_file=upload(PNG), logo_dark_file=upload(sentinel, "dark.svg", "image/svg+xml")
        )

        self.assertEqual(response.status_code, 302)
        import hashlib

        light = hashlib.sha256(PNG).hexdigest()[:12]
        dark = hashlib.sha256(sentinel).hexdigest()[:12]
        self.assertIn(f"logo_light replaced (png, {len(PNG)} bytes, sha256 {light})", text)
        self.assertIn(f"logo_dark replaced (svg, {len(sentinel)} bytes, sha256 {dark})", text)
        self.assertNotIn("SENTINEL", text)
        _response, text = self.saving(remove_logos="on")
        self.assertIn("logo_light removed", text)
        self.assertIn("logo_dark removed", text)

    def test_the_history_of_the_administration_names_the_fields_but_never_a_file(self):
        from django.contrib.admin.models import CHANGE, LogEntry

        self.saving(logo_light_file=upload(PNG), logo_dark_file=upload(PNG_DARK))

        entry = LogEntry.objects.filter(user=self.administrator, object_id="1", action_flag=CHANGE).latest("id")
        self.assertNotIn("PNG", entry.change_message.replace("Logo", ""))
        self.assertLess(len(entry.change_message), 500)

    def test_a_rejected_upload_is_one_event_with_a_reason_code_only(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                SETTINGS_URL,
                form_data(
                    logo_light_file=upload(svg("<script>SECRET-SENTINEL</script>"), "a.svg"),
                    logo_dark_file=upload(b"hello", "b.png"),
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).count(), 1)
        text = "\n".join(logs.output)
        self.assertIn("An upload in the portal settings was rejected", text)
        self.assertIn("svg: element not allowed", text)
        self.assertIn("file: not a PNG or SVG file", text)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("script", text)
        event = self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).get()
        self.assertEqual(event.actor_id, self.administrator.pk)
        self.assertFalse(self.events().exists())

    def test_repeated_rejected_uploads_of_one_session_are_one_event_per_minute(self):
        # M-5: with the app code as the proof, the upload is judged before the code, so the log is collapsed per session.
        def bad():  # a new upload each time: a posted file is read once
            return form_data(logo_light_file=upload(b"hello", "b.png"))

        for _round in range(3):  # five wrong proofs end the session (D-062): stay below it
            self.assertEqual(self.client.post(SETTINGS_URL, bad()).status_code, 200)
        self.assertEqual(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).count(), 1)
        session = self.client.session
        session["upload_rejected_logged_at"] -= 61
        session.save()
        self.client.post(SETTINGS_URL, bad())
        self.assertEqual(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).count(), 2)

    def test_a_rejected_upload_of_a_person_without_the_right_records_nothing(self):
        client, _person = staff_with("none.person@example.org", "view_user")

        response = client.post(SETTINGS_URL, form_data(logo_light_file=upload(b"hello", "b.png")))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).exists())

    def test_the_new_event_has_a_translated_name_in_the_log(self):
        self.assertEqual(str(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED.label), "Hochladen in den Portal-Einstellungen abgelehnt")


class NoDataMigrationTests(SimpleTestCase):
    def test_the_migration_changes_the_schema_only(self):
        module = importlib.import_module("leg_platform.accounts.migrations.0025_portal_settings_branding")

        operations = module.Migration.operations

        self.assertFalse([op for op in operations if isinstance(op, (RunPython, RunSQL))])
        self.assertTrue(operations)

    def test_no_migration_writes_instance_values_or_roles(self):
        from pathlib import Path

        directory = Path(settings.BASE_DIR) / "leg_platform/accounts/migrations"
        text = (directory / "0025_portal_settings_branding.py").read_text(encoding="utf-8")

        for forbidden in ("RunPython", "RunSQL", "Group", "objects.create", "bulk_create", "get_or_create"):
            self.assertNotIn(forbidden, text)


@tag("mariadb")
class NoRowAfterTheMigrationsTests(TestCase):
    def test_a_fresh_database_has_no_portal_settings_row_and_no_group(self):
        self.assertEqual(PortalSettings.objects.count(), 0)
        self.assertEqual(Group.objects.count(), 0)


@tag("mariadb")
class FirstStepsBoxTests(TestCase):
    def test_the_box_says_whose_settings_name_logo_and_colours_are(self):
        client, _person = administrator()

        self.assertIn("Serverkonfiguration", client.get(reverse("admin:index")).content.decode())
        PortalSettings.objects.update_or_create(pk=1, defaults={"accent_light": "#004E86"})
        self.assertIn("eigene Einstellungen", client.get(reverse("admin:index")).content.decode())

    def test_the_box_reads_no_logo_file(self):
        client, _person = administrator()
        store_logos()

        with CaptureQueriesContext(self._connection()) as queries:
            client.get(reverse("admin:index"))

        self.assertFalse([q for q in queries if "logo_light_data" in q["sql"]])

    @staticmethod
    def _connection():
        from django.db import connection

        return connection


@tag("mariadb")
class AdminLoadsNoFilesTests(TestCase):
    def test_the_form_page_does_not_load_the_files_to_show_the_status(self):
        client, _person = administrator()
        store_logos()

        with CaptureQueriesContext(self._connection()) as queries:
            response = client.get(SETTINGS_URL)

        self.assertEqual(response.status_code, 200)
        selects = [q["sql"] for q in queries if "accounts_portalsettings" in q["sql"] and q["sql"].startswith("SELECT")]
        self.assertTrue(selects)
        self.assertFalse([sql for sql in selects if "logo_light_data" in sql])

    @staticmethod
    def _connection():
        from django.db import connection

        return connection
