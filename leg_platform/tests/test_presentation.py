from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from leg_platform.branding import contrast, instance_static_directory, load_branding
from leg_platform.presentation import colour_mode


class BrandingTests(SimpleTestCase):
    def test_instance_static_directory_must_exist_and_be_absolute(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        self.assertIsNone(instance_static_directory({}))
        with TemporaryDirectory() as directory:
            self.assertEqual(
                instance_static_directory({"INSTANCE_STATIC_DIRECTORY": directory}),
                Path(directory),
            )
        for value in ("relative", directory):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                instance_static_directory({"INSTANCE_STATIC_DIRECTORY": value})

    def test_default_colours_reach_required_contrast(self):
        branding = load_branding({})
        self.assertGreaterEqual(contrast(branding.accent_light, branding.text_light), 4.5)
        self.assertGreaterEqual(contrast(branding.accent_dark, branding.text_dark), 4.5)
        self.assertAlmostEqual(contrast("#FFFFFF", "#000000"), 21)

    def test_contrast_threshold_is_not_rounded_up(self):
        self.assertGreaterEqual(contrast("#767676", "#FFFFFF"), 4.5)
        self.assertLess(contrast("#777777", "#FFFFFF"), 4.5)

    def test_invalid_colours_and_low_contrast_are_rejected(self):
        for name, value in (
            ("INSTANCE_ACCENT_LIGHT", "red"),
            ("INSTANCE_ACCENT_LIGHT", "#FFFFFF"),
            ("INSTANCE_ACCENT_TEXT_LIGHT", "#2F6B4F"),
            ("INSTANCE_ACCENT_DARK", "#000000"),
            ("INSTANCE_HIGHLIGHT", "#1C1917"),
            ("INSTANCE_HIGHLIGHT", "#ffffff;}body{display:none"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ImproperlyConfigured):
                    load_branding({name: value})

    def test_logos_are_paired_relative_static_files(self):
        branding = load_branding({
            "INSTANCE_LOGO_LIGHT": "instance/logo.svg",
            "INSTANCE_LOGO_DARK": "instance/logo-dark.png",
        })
        self.assertEqual(branding.logo_light, "instance/logo.svg")
        for value in ("../logo.svg", "/logo.png", "https://example.org/logo.svg",
                      "C:\\logo.png", "logo.html", "a//logo.svg", "a/%2e%2e/logo.svg"):
            with self.subTest(value=value), self.assertRaises(ImproperlyConfigured):
                load_branding({"INSTANCE_LOGO_LIGHT": value, "INSTANCE_LOGO_DARK": value})
        with self.assertRaises(ImproperlyConfigured):
            load_branding({"INSTANCE_LOGO_LIGHT": "logo.svg"})


@override_settings(INSTANCE_BRANDING=load_branding({}))
class PresentationTests(SimpleTestCase):
    def test_rendered_theme_logos_and_navigation_are_localized(self):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django.utils.translation import override
        from leg_platform.context_processors import instance

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        request.COOKIES["colour_mode"] = "dark"
        branding = load_branding({
            "INSTANCE_LOGO_LIGHT": "instance/logo.svg",
            "INSTANCE_LOGO_DARK": "instance/logo-dark.svg",
        })
        with override_settings(INSTANCE_BRANDING=branding), override("de-ch"):
            content = render_to_string("base.html", instance(request), request=request)
        self.assertIn('data-bs-theme="dark"', content)
        self.assertIn('class="portal-logo portal-logo-dark"', content)
        self.assertIn("/static/instance/logo-dark.svg", content)
        self.assertIn("Darstellung", content)
        self.assertNotIn("Colour mode", content)
        self.assertNotIn("ß", content)
        # Only the external script of the instance itself (show password), never an inline one.
        self.assertNotRegex(content, r"<script(?![^>]*\ssrc=\"/static/)")
        self.assertNotIn("style=", content)

    def test_stylesheet_is_self_hosted_css_with_validated_values(self):
        response = self.client.get(reverse("instance_stylesheet"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/css; charset=utf-8")
        self.assertContains(response, "--tblr-primary:#2F6B4F")
        self.assertContains(response, '--tblr-btn-color:var(--tblr-primary-fg)')

    def test_theme_changes_only_with_post_and_allowlisted_values(self):
        self.assertEqual(self.client.get(reverse("colour_mode")).status_code, 405)
        with self.assertLogs("leg_platform.presentation", level="WARNING"):
            self.assertEqual(
                self.client.post(reverse("colour_mode"), {"mode": "invalid"}).status_code, 400
            )
        for mode in ("auto", "light", "dark"):
            response = self.client.post(reverse("colour_mode"), {"mode": mode, "next": "/login/"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/login/")
            cookie = response.cookies["colour_mode"]
            self.assertEqual(cookie.value, mode)
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Lax")
            self.assertEqual(cookie["domain"], "")

    def test_external_redirect_is_rejected(self):
        for target in ("https://attacker.invalid/", "//attacker.invalid/", "\\\\attacker.invalid/"):
            with self.subTest(target=target):
                response = self.client.post(reverse("colour_mode"), {"mode": "dark", "next": target})
                self.assertEqual(response["Location"], "/")

    def test_csrf_is_required(self):
        from django.test import Client
        response = Client(enforce_csrf_checks=True).post(reverse("colour_mode"), {"mode": "dark"})
        self.assertEqual(response.status_code, 403)

    def test_unknown_cookie_and_query_parameters_cannot_override_mode(self):
        from django.test import RequestFactory
        request = RequestFactory().get("/?theme=dark&theme-primary=red")
        request.COOKIES["colour_mode"] = "invalid"
        self.assertEqual(colour_mode(request), "auto")

    @override_settings(COLOUR_MODE_COOKIE_NAME="__Host-colour_mode", SESSION_COOKIE_SECURE=True)
    def test_secure_host_cookie_outside_local(self):
        response = self.client.post(reverse("colour_mode"), {"mode": "dark"})
        cookie = response.cookies["__Host-colour_mode"]
        self.assertTrue(cookie["secure"])
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")
