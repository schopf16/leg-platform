"""Password managers leave the fields of the management area alone (D-141): no field there is a credential, and
``autocomplete="off"`` alone does not stop them (owner walk-through 2026-11-27: the fill icon in a deadline field)."""

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

from django import forms
from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from leg_platform import management
from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.community.tests.helpers import staff_user
from leg_platform.management.deadline_forms import DeadlinesForm
from leg_platform.management.form_fields import (
    PASSWORD_MANAGER_IGNORE,
    PasswordManagerIgnoreMixin,
    ignored_by_password_managers,
)
from leg_platform.management.person_forms import PersonCreateForm

MARKUP = ('data-lpignore="true"', "data-1p-ignore", "data-bwignore")


def management_forms():
    """Every form class defined in a module of the management area (tests excluded)."""
    found = set()
    for info in pkgutil.walk_packages(management.__path__, management.__name__ + "."):
        if ".tests" in info.name or ".migrations" in info.name:
            continue
        module = importlib.import_module(info.name)
        for _name, value in inspect.getmembers(module, inspect.isclass):
            if issubclass(value, forms.BaseForm) and value.__module__ == module.__name__:
                found.add(value)
    return found


class TheWidgetsCarryTheIgnoreAttributesTests(SimpleTestCase):
    def test_every_form_of_the_management_area_has_the_mixin(self):
        classes = management_forms()
        self.assertGreater(len(classes), 20)
        missing = sorted(cls.__qualname__ for cls in classes if not issubclass(cls, PasswordManagerIgnoreMixin))
        self.assertEqual(missing, [])

    def test_a_deadline_field_is_drawn_with_them(self):
        html = str(DeadlinesForm()["reminder_after_days"])
        for markup in MARKUP:
            self.assertIn(markup, html)
        self.assertIn('autocomplete="off"', html)

    def test_a_widget_replaced_in_the_init_and_a_select_get_them_too(self):
        form = PersonCreateForm(data={"country": "DE"})
        for name in ("postal_code", "country", "first_name"):
            with self.subTest(field=name):
                html = str(form[name])
                for markup in MARKUP:
                    self.assertIn(markup, html)

    def test_only_fields_one_types_into_or_chooses_from(self):
        for widget in (forms.TextInput(), forms.EmailInput(), forms.NumberInput(), forms.DateInput(), forms.Textarea(), forms.Select()):
            self.assertTrue(ignored_by_password_managers(widget), widget)
        for widget in (forms.PasswordInput(), forms.HiddenInput(), forms.CheckboxInput(), forms.RadioSelect(), forms.FileInput()):
            self.assertFalse(ignored_by_password_managers(widget), widget)

    def test_the_attributes_are_the_documented_ones(self):
        self.assertEqual(PASSWORD_MANAGER_IGNORE, {"data-lpignore": "true", "data-1p-ignore": True, "data-bwignore": True})


@tag("mariadb")
class ThePagesCarryThemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = staff_user("password.managers@example.org")

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.staff)

    def test_the_settings_page_and_the_list_search(self):
        settings_page = self.client.get(reverse("management:settings")).content.decode()
        listing = self.client.get(reverse("management:locations")).content.decode()
        for page, name in ((settings_page, "reminder_after_days"), (listing, "q")):
            tag_html = re.search(rf'<input[^>]*name="{name}"[^>]*>', page).group(0)
            for markup in MARKUP:
                self.assertIn(markup, tag_html)

    def test_the_login_page_keeps_the_password_managers(self):
        page = VerifiedClient().get(reverse("accounts:login")).content.decode()
        for markup in MARKUP:
            self.assertNotIn(markup, page)


class TheCircuitIconIsReadyTests(SimpleTestCase):
    def test_the_sprite_has_the_symbol_of_the_coming_menu(self):
        sprite = (Path(settings.BASE_DIR) / "leg_platform" / "templates" / "_frame_icons.html").read_text(encoding="utf-8")
        self.assertIn('<symbol id="i-circuit" viewBox="0 0 24 24">', sprite)
