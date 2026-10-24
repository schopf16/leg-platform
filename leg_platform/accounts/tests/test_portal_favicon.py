"""The favicon (the icon of the browser tab) in the portal settings (D-073): the validation, the upload and the
removal in the administration, the public view with the protective headers, the effect on every page shell,
the audit entries and the permissions. It follows the logo tests of ``test_portal_branding``."""

import hashlib
from unittest import mock

from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import resolve, reverse

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts import second_factor
from leg_platform.accounts.logo_images import (
    FAVICON_PNG_MAX_BYTES,
    FAVICON_SVG_MAX_BYTES,
    LogoRejected,
    validate_favicon,
    validate_logo,
)
from leg_platform.accounts.models import PortalSettings, SecurityEvent
from leg_platform.accounts.portal_settings import LOGO_VERSION_LENGTH, environment_values, portal_values
from leg_platform.accounts.tests.helpers import create_active_user
from leg_platform.accounts.tests.test_logo_images import MINIMAL, make_png, svg
from leg_platform.accounts.tests.test_portal_branding import (
    DARK_LOGO_URL,
    LIGHT_LOGO_URL,
    PNG,
    PNG_DARK,
    SERVER,
    SETTINGS_URL,
    administrator,
    form_data,
    staff_with,
    store_logos,
    upload,
)
from leg_platform.middleware import body_limit

FAVICON_URL = reverse("instance_favicon")
LOGIN_URL = reverse("accounts:login")
FAVICON_PNG = make_png(width=192, height=192)
BUILT_IN_ICON = "/static/img/favicon.svg"
# The start of an ICO file (reserved 0, type 1, one image): a format the portal does not accept.
ICO = b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00" + bytes(64)


def store_favicon(data=FAVICON_PNG, kind="png"):
    """Write a validated favicon straight into the row (the form is tested separately)."""
    row = PortalSettings.load()
    row.favicon_data = data
    row.favicon_type = kind
    row.favicon_sha256 = hashlib.sha256(data).hexdigest()
    row.save()
    return row


class ValidationTests(SimpleTestCase):
    def test_png_and_svg_are_accepted_as_they_are(self):
        png = validate_favicon(FAVICON_PNG)
        vector = validate_favicon(MINIMAL)

        self.assertEqual((png.kind, png.data, png.sha256), ("png", FAVICON_PNG, hashlib.sha256(FAVICON_PNG).hexdigest()))
        self.assertEqual((vector.kind, vector.data), ("svg", MINIMAL))

    def test_ico_and_other_formats_are_refused_by_their_content(self):
        for label, content in (
            ("ico", ICO),
            ("gif", b"GIF89a" + bytes(40)),
            ("jpeg", b"\xff\xd8\xff\xe0" + bytes(40)),
            ("text", b"hello"),
            ("empty", b""),
        ):
            with self.subTest(label), self.assertRaises(LogoRejected) as raised:
                validate_favicon(content)
            self.assertEqual(raised.exception.reason, "file: not a PNG or SVG file")
        # Markup that is not an SVG is refused by the SVG checks.
        with self.assertRaises(LogoRejected):
            validate_favicon(b"<html><script>alert(1)</script></html>")

    def test_a_favicon_has_smaller_limits_than_a_logo(self):
        too_big_png = FAVICON_PNG[:8] + b"\x00" * FAVICON_PNG_MAX_BYTES
        too_big_svg = svg("<g/>" * (FAVICON_SVG_MAX_BYTES // 4))

        for content, reason, limit in (
            (too_big_png, "png: too large", "128 KB"),
            (too_big_svg, "svg: too large", "64 KB"),
        ):
            with self.subTest(reason), self.assertRaises(LogoRejected) as raised:
                validate_favicon(content)
            self.assertEqual((raised.exception.reason, raised.exception.params), (reason, {"max": limit}))
        # A valid SVG between the two limits is a logo but not a favicon.
        between = svg(f'<path d="{"M0 0h1 " * 12000}"/>')
        self.assertGreater(len(between), FAVICON_SVG_MAX_BYTES)
        self.assertEqual(validate_logo(between).kind, "svg")
        with self.assertRaises(LogoRejected) as raised:
            validate_favicon(between)
        self.assertEqual(raised.exception.reason, "svg: too large")

    def test_the_content_rules_are_those_of_a_logo(self):
        for content in (svg("<script>alert(1)</script>"), svg("", ' onload="alert(1)"'), b"<!DOCTYPE svg>" + MINIMAL):
            with self.subTest(content[:40]), self.assertRaises(LogoRejected):
                validate_favicon(content)
        with self.assertRaises(LogoRejected):
            validate_favicon(FAVICON_PNG + b"<script>")
        with self.assertRaises(LogoRejected):
            validate_favicon(make_png(width=10, height=10, image=b"\x00"))


@override_settings(**SERVER)
@tag("mariadb")
class FaviconViewTests(TestCase):
    def get(self, url=FAVICON_URL, **extra):
        return self.client.get(url, **extra)

    def test_a_png_is_served_with_a_fixed_type_and_the_protective_headers(self):
        row = store_favicon()

        response = self.get(f"{FAVICON_URL}?v={row.favicon_sha256[:LOGO_VERSION_LENGTH]}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, FAVICON_PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(response["ETag"], f'"{row.favicon_sha256}"')
        self.assertNotIn("public", response["Cache-Control"])
        self.assertIn("max-age=86400", response["Cache-Control"])

    def test_an_svg_is_served_as_an_image_and_never_as_html(self):
        store_favicon(MINIMAL, "svg")

        response = self.get()

        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-cache", response["Cache-Control"])

    def test_without_a_favicon_it_is_not_found(self):
        self.assertEqual(self.get().status_code, 404)
        PortalSettings.load()
        self.assertEqual(self.get().status_code, 404)
        # The logos do not make a favicon.
        store_logos()
        self.assertEqual(self.get().status_code, 404)

    def test_a_wrong_address_digest_is_not_found(self):
        row = store_favicon()

        for query in ("?v=0000000000000000", "?v=", "?v=" + row.favicon_sha256, "?v=short"):
            with self.subTest(query=query):
                self.assertEqual(self.get(FAVICON_URL + query).status_code, 404)

    def test_the_file_is_not_chosen_by_the_visitor(self):
        store_favicon()
        store_logos()
        for path in (
            "/branding/favicon/extra/",
            "/branding/favicon/light/",
            "/branding/favicon.ico",
            "/branding/favicon/../logo/light/",
            "/branding/favicon%2F..%2Flogo%2Flight/",
            "/branding/logo/favicon/",
            "/favicon.ico",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, 404, path)
        self.assertEqual(resolve(FAVICON_URL).view_name, "instance_favicon")
        # Each file stays on its own address.
        self.assertEqual(self.get(LIGHT_LOGO_URL).content, PNG)
        self.assertEqual(self.get(DARK_LOGO_URL).content, PNG_DARK)
        self.assertEqual(self.get().content, FAVICON_PNG)

    def test_a_stored_file_that_does_not_match_its_digest_is_not_served(self):
        store_favicon()
        PortalSettings.objects.filter(pk=1).update(favicon_data=b"<html><script>alert(1)</script></html>")

        with self.assertLogs("leg_platform.presentation", "WARNING") as logs:
            response = self.get()

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"alert(1)", response.content)
        self.assertEqual(
            logs.output, ["WARNING:leg_platform.presentation:The stored favicon does not match its digest; it is not served."]
        )

    def test_a_stored_type_that_is_not_an_image_type_is_not_served(self):
        store_favicon()
        PortalSettings.objects.filter(pk=1).update(favicon_type="gif")

        self.assertEqual(self.get().status_code, 404)

    def test_no_stored_content_is_ever_served_as_html(self):
        html = b"<html><body><script>alert(1)</script></body></html>"
        store_favicon(html, "svg")

        response = self.get()

        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("text/html", response["Content-Type"])

    def test_a_conditional_request_gets_not_modified_with_the_same_headers(self):
        row = store_favicon()

        response = self.get(HTTP_IF_NONE_MATCH=f'"{row.favicon_sha256}"')

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], "sandbox; default-src 'none'")

    def test_a_wrong_version_and_a_matching_conditional_request_do_not_read_the_file(self):
        row = store_favicon()
        with mock.patch("leg_platform.presentation._favicon_data") as data:
            for query in ("?v=0000000000000000", "?v=short", "?v="):
                self.assertEqual(self.get(FAVICON_URL + query).status_code, 404)
            self.assertEqual(self.get(HTTP_IF_NONE_MATCH=f'"{row.favicon_sha256}"').status_code, 304)
        data.assert_not_called()

    def test_other_methods_are_refused_and_head_works(self):
        store_favicon()

        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(FAVICON_URL).status_code, 405)
        self.assertEqual(self.client.head(FAVICON_URL).status_code, 200)

    def test_it_needs_no_login_no_session_cookie_and_no_second_factor(self):
        row = store_favicon()
        url = f"{FAVICON_URL}?v={row.favicon_sha256[:LOGO_VERSION_LENGTH]}"
        # A staff user who has not set up the mandatory second factor, and a person who must change the
        # password, can still load the icon of the page that tells them what to do.
        staff = create_active_user("staff.nofactor@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        staff_client = Client()
        staff_client.force_login(staff)
        forced = create_active_user("forced.person@example.org")
        forced_client = Client()
        forced_client.force_login(forced)

        for label, client in (("anonymous", Client()), ("staff without factor", staff_client), ("forced", forced_client)):
            for method in ("get", "head"):
                with self.subTest(client=label, method=method):
                    response = getattr(client, method)(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("Set-Cookie", response.headers)
                    self.assertFalse(response.cookies)
                    self.assertNotIn("cookie", response.get("Vary", "").lower())

    def test_it_is_one_of_the_public_presentation_views(self):
        for views in (
            second_factor.UNRESTRICTED_VIEWS,
            second_factor.PASSWORD_CHANGE_VIEWS,
            second_factor.SESSION_FREE_VIEWS,
        ):
            self.assertIn("instance_favicon", views)
        # The upload body limit of the portal settings has room for two logos and the favicon.
        self.assertGreater(body_limit("/admin/accounts/portalsettings/1/change/"), 2 * 1024 * 1024 + FAVICON_PNG_MAX_BYTES)


@override_settings(**SERVER)
@tag("mariadb")
class AdminFormTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator()

    def post(self, **overrides):
        return self.client.post(SETTINGS_URL, form_data(**overrides))

    def row(self):
        return PortalSettings.objects.get(pk=1)

    def test_the_page_has_the_group_the_help_and_the_status(self):
        page = self.client.get(SETTINGS_URL).content.decode()

        for text in (
            "Symbol des Browser-Tabs",
            "Hochgeladenes Symbol",
            "Nicht hochgeladen. Das eingebaute Symbol wird verwendet.",
            "PNG oder SVG, bis 128 KB (PNG) oder 64 KB (SVG).",
            "Hochgeladenes Symbol entfernen und das eingebaute Symbol verwenden.",
            'name="favicon_file"',
            'name="remove_favicon"',
        ):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertNotIn("ß", page)

    def test_a_png_is_uploaded_and_stored_as_it_is(self):
        response = self.post(favicon_file=upload(FAVICON_PNG, "icon.png"))

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.favicon_type, "png")
        self.assertEqual(bytes(row.favicon_data), FAVICON_PNG)
        self.assertEqual(row.favicon_sha256, hashlib.sha256(FAVICON_PNG).hexdigest())
        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)
        page = self.client.get(SETTINGS_URL).content.decode()
        self.assertIn("Hochgeladen (PNG, Fingerabdruck", page)
        self.assertIn(f'<img src="{portal_values().favicon}"', page)

    def test_an_svg_is_uploaded(self):
        response = self.post(favicon_file=upload(MINIMAL, "icon.svg", "image/svg+xml"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.row().favicon_type, "svg")
        self.assertEqual(self.client.get(FAVICON_URL)["Content-Type"], "image/svg+xml")

    def test_the_type_comes_from_the_content_and_the_name_is_never_stored(self):
        name = "../../etc/evil\u202egnp.svg"

        response = self.post(favicon_file=upload(FAVICON_PNG, name, "text/html"))

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.favicon_type, "png")
        for field in ("favicon_type", "favicon_sha256", "name"):
            self.assertNotIn("evil", getattr(row, field))
        self.assertNotContains(self.client.get(SETTINGS_URL), "evil")

    def test_a_file_that_is_not_accepted_gives_a_message_and_changes_nothing(self):
        for label, content, name, message in (
            ("a script", svg("<script>alert(1)</script>"), "a.svg", "Diese SVG-Datei enthält Inhalte, die das Portal nicht annimmt (script)."),
            ("an ico file", ICO, "favicon.ico", "Es sind nur PNG- und SVG-Dateien erlaubt."),
            ("a text file", b"hello", "a.png", "Es sind nur PNG- und SVG-Dateien erlaubt."),
            ("a cut png", FAVICON_PNG[:-20], "a.png", "Die Datei ist kein gültiges PNG-Bild."),
            ("a small png", make_png(width=10, height=10, image=b"\x00"), "a.png", "Das Bild muss zwischen 64 und 4000 Pixel breit und hoch sein."),
            ("an svg without a view box", b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>', "a.svg", "Das SVG braucht eine viewBox"),
        ):
            with self.subTest(label):
                response = self.post(favicon_file=upload(content, name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
                self.assertFalse(PortalSettings.objects.exists() and self.row().favicon_sha256)
        script = self.post(favicon_file=upload(svg("<script/>"), "a.svg"))
        self.assertContains(script, "«einfaches SVG»")

    def test_a_file_above_the_limit_of_a_favicon_is_too_large(self):
        too_big_png = FAVICON_PNG[:8] + b"\x00" * FAVICON_PNG_MAX_BYTES
        too_big_svg = svg("<g/>" * (FAVICON_SVG_MAX_BYTES // 4))

        for content, name, limit in ((too_big_png, "a.png", "128 KB"), (too_big_svg, "a.svg", "64 KB")):
            with self.subTest(limit=limit):
                response = self.post(favicon_file=upload(content, name))
                self.assertContains(response, f"Die Datei ist zu gross (höchstens {limit}).")
                self.assertFalse(PortalSettings.objects.exists() and self.row().favicon_sha256)

    def test_the_favicon_is_independent_of_the_logos(self):
        # No "both or none" rule for the icon: one file serves both colour modes.
        response = self.post(favicon_file=upload(FAVICON_PNG))

        self.assertEqual(response.status_code, 302)
        values = portal_values()
        self.assertEqual(values.favicon_type, "image/png")
        self.assertFalse(values.uploaded_logos)
        self.assertEqual(values.logo_light, environment_values().logo_light)
        # Logos next to a stored favicon, and the removal of the logos, leave the favicon alone.
        self.post(logo_light_file=upload(PNG), logo_dark_file=upload(PNG_DARK))
        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)
        self.post(remove_logos="on")
        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).status_code, 404)

    def test_removing_the_favicon_deletes_the_file_and_goes_back_to_the_built_in_icon(self):
        store_logos()
        store_favicon()

        response = self.post(remove_favicon="on")

        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual((row.favicon_data, row.favicon_type, row.favicon_sha256), (None, "", ""))
        self.assertEqual(self.client.get(FAVICON_URL).status_code, 404)
        self.assertEqual(portal_values().favicon, "")
        # The logos stay.
        self.assertEqual(self.client.get(LIGHT_LOGO_URL).content, PNG)

    def test_removing_and_uploading_at_the_same_time_is_refused(self):
        store_favicon()

        response = self.post(remove_favicon="on", favicon_file=upload(MINIMAL, "a.svg"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wählen Sie entweder ein neues Symbol oder das Entfernen des hochgeladenen Symbols, nicht beides.")
        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)

    def test_saving_without_a_file_keeps_the_stored_favicon(self):
        store_favicon()

        self.post(name="Beispiel Gemeinde")

        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)

    def test_a_rejected_favicon_next_to_a_logo_problem_shows_both_messages(self):
        response = self.post(logo_light_file=upload(PNG), favicon_file=upload(ICO, "a.ico"))

        self.assertContains(response, "Laden Sie beide Logos hoch oder keines.")
        self.assertContains(response, "Es sind nur PNG- und SVG-Dateien erlaubt.")

    def test_saving_needs_the_proof_of_identity(self):
        self.client.auto_proof = False

        response = self.post(favicon_file=upload(FAVICON_PNG))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PortalSettings.objects.exists() and self.row().favicon_sha256)


@override_settings(**SERVER)
@tag("mariadb")
class AccessorTests(TestCase):
    def test_an_empty_row_has_no_favicon(self):
        PortalSettings.load()

        values = portal_values()

        self.assertEqual((values.favicon, values.favicon_type), ("", ""))
        self.assertEqual((environment_values().favicon, environment_values().favicon_type), ("", ""))

    def test_the_address_carries_the_start_of_the_digest(self):
        row = store_favicon()

        values = portal_values()

        self.assertEqual(values.favicon, f"{FAVICON_URL}?v={row.favicon_sha256[:LOGO_VERSION_LENGTH]}")
        self.assertEqual(values.favicon_type, "image/png")
        store_favicon(MINIMAL, "svg")
        self.assertEqual(portal_values().favicon_type, "image/svg+xml")

    def test_a_value_written_past_the_form_is_not_used(self):
        store_favicon()
        for field, value in (("favicon_type", "gif"), ("favicon_type", ""), ("favicon_sha256", "xyz"), ("favicon_sha256", "")):
            with self.subTest(field=field, value=value):
                row = PortalSettings.objects.get(pk=1)
                old = getattr(row, field)
                PortalSettings.objects.filter(pk=1).update(**{field: value})
                self.assertEqual(portal_values().favicon, "")
                PortalSettings.objects.filter(pk=1).update(**{field: old})
        self.assertNotEqual(portal_values().favicon, "")

    def test_a_database_error_gives_the_built_in_icon_and_never_raises(self):
        store_favicon()
        with mock.patch("leg_platform.accounts.portal_settings._read_row", side_effect=RuntimeError("db")):
            with self.assertLogs("leg_platform.accounts.portal_settings", "WARNING"):
                self.assertEqual(portal_values().favicon, "")

    def test_the_favicon_file_is_never_loaded_by_the_accessor(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        store_favicon()
        with CaptureQueriesContext(connection) as queries:
            portal_values()
        self.assertFalse([query for query in queries if "favicon_data" in query["sql"]])


@override_settings(**SERVER)
@tag("mariadb")
class PageShellTests(TestCase):
    """Every page shell links the favicon: the uploaded one with its digest, else the built-in icon."""

    def icon(self, page):
        import re

        links = re.findall(r'<link rel="icon"[^>]*>', page)
        self.assertEqual(len(links), 1, links)
        return links[0]

    def shells(self):
        """(label, page) of the shells: entry page, application page, error page, administration page."""
        administrator_client, _person = administrator()
        return (
            ("entry page", Client().get(LOGIN_URL).content.decode()),
            ("application page", administrator_client.get(reverse("accounts:profile")).content.decode()),
            ("error page", Client().get("/there-is-no-such-page/").content.decode()),
            ("administration page", administrator_client.get(reverse("admin:index")).content.decode()),
            ("portal settings page", administrator_client.get(SETTINGS_URL).content.decode()),
            ("server error page", render_to_string("500.html")),
        )

    def test_without_an_upload_every_shell_uses_the_built_in_icon(self):
        for label, page in self.shells():
            with self.subTest(label):
                link = self.icon(page)
                self.assertIn(BUILT_IN_ICON, link)
                self.assertIn('type="image/svg+xml"', link)
                self.assertNotIn("/branding/favicon/", link)

    def test_with_an_upload_every_shell_uses_it_with_the_digest_for_the_cache(self):
        row = store_favicon()
        expected = f'<link rel="icon" href="{FAVICON_URL}?v={row.favicon_sha256[:LOGO_VERSION_LENGTH]}" type="image/png">'

        for label, page in self.shells():
            if label == "server error page":
                # Rendered without a request and without the database: always the built-in icon (D-054).
                self.assertIn(BUILT_IN_ICON, self.icon(page))
                continue
            with self.subTest(label):
                self.assertEqual(self.icon(page), expected)

    def test_a_new_file_changes_the_address(self):
        first = store_favicon()
        first_address = portal_values().favicon
        second = store_favicon(MINIMAL, "svg")

        self.assertNotEqual(first.favicon_sha256, second.favicon_sha256)
        self.assertNotEqual(portal_values().favicon, first_address)
        self.assertIn('type="image/svg+xml"', self.icon(Client().get(LOGIN_URL).content.decode()))

    def test_the_address_is_escaped_in_the_page(self):
        # The address comes from a route and a hex digest, never from the visitor; the type is a fixed table.
        store_favicon()
        PortalSettings.objects.filter(pk=1).update(favicon_type="png", favicon_sha256="a" * 64)

        link = self.icon(Client().get(LOGIN_URL).content.decode())

        self.assertIn(f"{FAVICON_URL}?v={'a' * LOGO_VERSION_LENGTH}", link)

    def test_the_page_csp_allows_the_icon_from_the_own_origin_only(self):
        response = Client().get(LOGIN_URL)

        policy = response.headers.get("Content-Security-Policy", "")
        self.assertIn("img-src 'self'", policy)
        self.assertNotIn("data:", policy.split("img-src")[1].split(";")[0])


@override_settings(**SERVER)
@tag("mariadb")
class AuditAndPermissionTests(TestCase):
    def setUp(self):
        self.client, self.administrator = administrator()

    def saving(self, **extra):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(SETTINGS_URL, form_data(**extra))
        return response, "\n".join(logs.output)

    def events(self, name=SecurityEvent.Event.PORTAL_SETTINGS_CHANGED):
        return SecurityEvent.objects.filter(event=name)

    def test_the_favicon_is_logged_with_type_size_and_digest_start_and_never_the_content(self):
        sentinel = svg("<title>SECRET-SENTINEL-TITLE</title>")

        response, text = self.saving(favicon_file=upload(sentinel, "icon.svg", "image/svg+xml"))

        self.assertEqual(response.status_code, 302)
        digest = hashlib.sha256(sentinel).hexdigest()[:12]
        self.assertIn(f"favicon replaced (svg, {len(sentinel)} bytes, sha256 {digest})", text)
        self.assertNotIn("SENTINEL", text)
        event = self.events().get()
        self.assertTrue(event.long_retention)
        self.assertEqual(event.actor_id, self.administrator.pk)
        _response, text = self.saving(remove_favicon="on")
        self.assertIn("favicon removed", text)
        self.assertEqual(self.events().count(), 2)

    def test_an_unchanged_save_and_the_same_file_again_record_nothing(self):
        self.saving(favicon_file=upload(FAVICON_PNG))
        self.assertEqual(self.events().count(), 1)

        self.client.post(SETTINGS_URL, form_data())
        self.client.post(SETTINGS_URL, form_data(favicon_file=upload(FAVICON_PNG)))

        self.assertEqual(self.events().count(), 1)

    def test_the_history_of_the_administration_never_holds_a_file(self):
        from django.contrib.admin.models import CHANGE, LogEntry

        self.saving(favicon_file=upload(FAVICON_PNG))

        entry = LogEntry.objects.filter(user=self.administrator, object_id="1", action_flag=CHANGE).latest("id")
        self.assertNotIn("PNG", entry.change_message)
        self.assertLess(len(entry.change_message), 500)

    def test_a_rejected_favicon_is_one_event_with_a_reason_code_only(self):
        with self.assertLogs("leg_platform.security", "WARNING") as logs, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                SETTINGS_URL, form_data(favicon_file=upload(svg("<script>SECRET-SENTINEL</script>"), "a.svg"))
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).count(), 1)
        text = "\n".join(logs.output)
        self.assertIn("favicon svg: element not allowed", text)
        self.assertNotIn("SENTINEL", text)
        self.assertNotIn("script", text)
        self.assertFalse(self.events().exists())

    def test_a_person_with_only_the_view_right_sees_the_status_but_cannot_upload_or_remove(self):
        store_favicon()
        client, _person = staff_with("viewer.person@example.org", "view_portalsettings")

        page = client.get(SETTINGS_URL)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="favicon_file"')
        self.assertNotContains(page, 'name="remove_favicon"')
        self.assertContains(page, "Hochgeladen (PNG, Fingerabdruck")
        for data in (form_data(favicon_file=upload(MINIMAL, "a.svg")), form_data(remove_favicon="on")):
            self.assertEqual(client.post(SETTINGS_URL, data).status_code, 403)
        self.assertEqual(self.client.get(FAVICON_URL).content, FAVICON_PNG)

    def test_a_person_without_the_right_is_refused_and_records_nothing(self):
        client, _person = staff_with("none.person@example.org", "view_user")

        self.assertEqual(client.get(SETTINGS_URL).status_code, 403)
        response = client.post(SETTINGS_URL, form_data(favicon_file=upload(ICO, "a.ico")))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.events(SecurityEvent.Event.PORTAL_UPLOAD_REJECTED).exists())
        self.assertFalse(PortalSettings.objects.exists() and PortalSettings.objects.get(pk=1).favicon_sha256)

    def test_an_ordinary_account_and_an_anonymous_visitor_cannot_change_it(self):
        ordinary = Client()
        ordinary.force_login(create_active_user("ordinary.person@example.org"))

        for client in (ordinary, Client()):
            response = client.post(SETTINGS_URL, form_data(favicon_file=upload(FAVICON_PNG)))
            self.assertIn(response.status_code, (302, 403, 404))
        self.assertFalse(PortalSettings.objects.exists() and PortalSettings.objects.get(pk=1).favicon_sha256)
