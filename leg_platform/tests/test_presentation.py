from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from leg_platform.branding import contrast, instance_static_directory, load_branding


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

    def test_default_colours_are_the_ones_of_the_design_package_and_reach_required_contrast(self):
        branding = load_branding({})
        self.assertEqual(
            (branding.accent_light, branding.text_light, branding.accent_dark, branding.text_dark,
             branding.highlight),
            ("#005BA2", "#FFFFFF", "#6AA9E0", "#0B1520", "#DDDD22"),
        )
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
            ("INSTANCE_ACCENT_TEXT_LIGHT", "#005BA2"),
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
class PresentationTests(TestCase):
    def render_base(self, branding=None):
        from django.contrib.auth.models import AnonymousUser
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        from django.utils.translation import override
        from leg_platform.context_processors import instance

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        with override_settings(INSTANCE_BRANDING=branding or load_branding({})), override("de-ch"):
            return render_to_string("base.html", instance(request), request=request)

    def test_rendered_page_starts_light_has_both_logos_and_the_corner_buttons(self):
        branding = load_branding({
            "INSTANCE_LOGO_LIGHT": "instance/logo.svg",
            "INSTANCE_LOGO_DARK": "instance/logo-dark.svg",
        })
        content = self.render_base(branding)
        # Light by default, whatever the device prefers; the browser script switches it (D-056).
        self.assertIn('data-theme="light" data-bs-theme="light"', content)
        self.assertIn('class="portal-logo portal-logo-dark"', content)
        self.assertIn("/static/instance/logo-dark.svg", content)
        # The two corner buttons are on every page and the old select is gone.
        self.assertIn('id="lang-btn"', content)
        self.assertIn('id="theme-btn"', content)
        self.assertIn("Sprache: Deutsch", content)
        self.assertIn("Darstellung: hell. Zu dunkel wechseln.", content)
        self.assertNotIn('name="mode"', content)
        self.assertNotIn("Colour mode", content)
        self.assertNotIn("ß", content)
        # Only external scripts of the own origin, never an inline one, and no style attribute.
        self.assertNotRegex(content, r"<script(?![^>]*\ssrc=\"/static/)")
        self.assertNotIn("style=", content)

    def test_the_theme_script_is_loaded_in_the_head_without_defer(self):
        content = self.render_base()
        head = content.split("</head>")[0]
        self.assertRegex(head, r'<script src="/static/js/theme-init\.js"></script>')
        self.assertIn('<script defer src="/static/js/corner-controls.js"></script>', content)

    def test_stylesheet_is_self_hosted_css_with_validated_values(self):
        response = self.client.get(reverse("instance_stylesheet"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/css; charset=utf-8")
        self.assertContains(response, ":root{--accent:#005BA2;--accent-rgb:0, 91, 162;--on-accent:#FFFFFF;--yellow:#DDDD22;}")
        self.assertContains(response, ':root[data-theme="dark"]{--accent:#6AA9E0;--accent-rgb:106, 169, 224;--on-accent:#0B1520;}')
        self.assertNotContains(response, "prefers-color-scheme")

    def test_the_colour_mode_has_no_server_side_endpoint_and_no_cookie(self):
        # K3 (D-056): nothing about the colour mode goes to the server (the language does, D-056).
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("colour_mode")
        self.assertEqual(self.client.post("/appearance/", {"mode": "dark"}).status_code, 404)
        response = self.client.get(reverse("accounts:login"))
        self.assertFalse([name for name in response.cookies if "colour" in name or "theme" in name])

    def test_query_parameters_cannot_set_the_theme_or_the_branding(self):
        response = self.client.get(reverse("accounts:login") + "?theme=dark&theme-primary=red")
        self.assertContains(response, 'data-theme="light" data-bs-theme="light"')
        self.assertNotContains(response, "theme-primary")
