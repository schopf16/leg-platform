"""The address suggestions in the change forms of the administration (D-111, owner specification of 2026-10-28, masks 9 to 11):
the user, the location and the management settings (the sender address). The add form of the location is not one of them: the raw
table adds a row in its list, the add view stays refused (a test says so).

What is proven: each of the three change forms (and the add form of the location) carries the widget under the street, the notice
after the town, the script (deferred) and the stylesheet in its media; no field is added, removed or reordered and no permission
changes (a staff account without the right still gets no form, an anonymous visitor is sent to the login); the editable raw lists
keep plain inputs (an overlay does not belong into a table row); nothing is inline (CSP); the sentence about an empty directory is
given there (everyone in the administration is staff); the stylesheet of the suggestions is loaded by the administration."""

import re

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import RequestFactory, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.admin.users import UserAdmin
from leg_platform.accounts.models import User
from leg_platform.accounts.tests.helpers import ProvingClient, create_active_user
from leg_platform.addresses.admin_widgets import (
    ADDRESS_PARTS,
    AddressStreetInput,
    AddressSuggestionsMixin,
    AddressTownInput,
)
from leg_platform.community.admin import LocationAdmin, ManagementSettingsAdmin
from leg_platform.community.models import Location, ManagementSettings
from leg_platform.community.tests.helpers import make_location

SCRIPT = '<script src="/static/js/address-directory.js" defer></script>'
STYLE = '<link href="/static/css/address-suggest.css" media="all" rel="stylesheet">'


def widget_of(html):
    start = html.index('<div class="address-assist"')
    marker = "data-address-announce></p>"
    return html[start:html.index(marker, start) + len(marker)]


@tag("mariadb")
class AdminAddressWidgetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user("admin.widgets@example.org")
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        cls.customer = create_active_user("customer.widgets@example.org")
        cls.location = make_location("Gantrischweg", "7", "3063", "Ittigen")
        cls.settings_row = ManagementSettings.load()

    def setUp(self):
        self.client = ProvingClient()
        self.client.force_login(self.administrator)

    def forms(self):
        """``(name, url, prefix of the field ids, country attribute)`` of the three masks."""
        return (
            ("user", reverse("admin:accounts_user_change", args=(self.customer.pk,)), "", 'data-country-id="id_country"'),
            ("location", reverse("admin:community_location_change", args=(self.location.pk,)), "", 'data-country-value="CH"'),
            (
                "settings", reverse("admin:community_managementsettings_change", args=(self.settings_row.pk,)), "sender_",
                'data-country-value="CH"',
            ),
        )

    def test_every_change_form_has_the_widget_the_notice_the_script_and_the_style(self):
        for name, address, prefix, country in self.forms():
            with self.subTest(name):
                response = self.client.get(address)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                widget = widget_of(html)

                self.assertEqual(html.count("data-address-directory"), 1)
                self.assertEqual(html.count(SCRIPT), 1)
                self.assertEqual(html.count(STYLE), 1)
                positions = []
                for data_name, field in (
                    ("street", "street"), ("house-number", "house_number"), ("postal-code", "postal_code"), ("locality", "town"),
                ):
                    field_id = f"id_{prefix}{field}"
                    self.assertIn(f'data-{data_name}-id="{field_id}"', widget)
                    self.assertEqual(html.count(f' id="{field_id}"'), 1, field_id)
                    positions.append(html.index(f' id="{field_id}"'))
                self.assertIn(country, widget)
                # The widget follows the street input, the notice follows the town input.
                self.assertLess(positions[0], html.index("data-address-directory"))
                self.assertLess(html.index("data-address-directory"), positions[1])
                self.assertEqual(html.count("data-address-notice"), 1)
                self.assertRegex(html, r'<div class="address-notice" id="address-notice" data-address-notice role="status"></div>')
                self.assertLess(positions[3], html.index("data-address-notice"))

    def test_the_script_and_the_style_come_from_the_form_media_without_anything_inline(self):
        for name, address, _prefix, _country in self.forms():
            with self.subTest(name):
                html = self.client.get(address).content.decode()
                widget = widget_of(html)

                self.assertNotRegex(widget, r"<script|\sstyle=|\son[a-z]+=")
                self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [])
                self.assertNotIn("<style", html)
                self.assertNotRegex(html, r"""\son(?:click|change|input|load)=""")

    def test_the_sentence_about_an_empty_directory_is_given_in_the_administration(self):
        for name, address, _prefix, _country in self.forms():
            with self.subTest(name):
                self.assertIn("data-directory-missing=", widget_of(self.client.get(address).content.decode()))

    def test_the_fields_are_the_same_with_and_without_the_widgets(self):
        factory = RequestFactory()
        for admin_class, model, obj in (
            (UserAdmin, User, self.customer), (LocationAdmin, Location, self.location),
            (ManagementSettingsAdmin, ManagementSettings, self.settings_row),
        ):
            with self.subTest(admin_class.__name__):
                model_admin = admin.site._registry[model]
                self.assertIsInstance(model_admin, AddressSuggestionsMixin)
                request = factory.get("/")
                request.user = self.administrator
                with_widgets = model_admin.get_form(request, obj)
                plain = super(AddressSuggestionsMixin, model_admin).get_form(request, obj)

                self.assertEqual(list(with_widgets.base_fields), list(plain.base_fields))
                changed = {
                    name for name, field in with_widgets.base_fields.items()
                    if type(field.widget) is not type(plain.base_fields[name].widget)
                }
                prefix = model_admin.address_prefix
                self.assertEqual(changed, {f"{prefix}street", f"{prefix}town"})
                self.assertIsInstance(with_widgets.base_fields[f"{prefix}street"].widget, AddressStreetInput)
                self.assertIsInstance(with_widgets.base_fields[f"{prefix}town"].widget, AddressTownInput)
                # What the widget replaced keeps its attributes and its required state.
                for part in ("street", "town"):
                    field = with_widgets.base_fields[f"{prefix}{part}"]
                    base = plain.base_fields[f"{prefix}{part}"]
                    self.assertEqual(field.widget.is_required, base.widget.is_required)
                    self.assertEqual(field.widget.attrs, base.widget.attrs)

    def test_a_form_that_shows_fewer_than_the_four_fields_stays_as_it_is(self):
        class Narrow(AddressSuggestionsMixin, admin.ModelAdmin):
            fields = ("street", "town")

        model_admin = Narrow(Location, admin.site)
        request = RequestFactory().get("/")
        request.user = self.administrator
        form = model_admin.get_form(request, None)

        self.assertEqual(set(form.base_fields), {"street", "town"})
        self.assertNotIsInstance(form.base_fields["street"].widget, AddressStreetInput)
        self.assertEqual(ADDRESS_PARTS, ("street", "house_number", "postal_code", "town"))

    def test_a_save_works_as_before_and_the_widget_adds_no_field_to_the_post(self):
        address = reverse("admin:community_location_change", args=(self.location.pk,))
        response = self.client.get(address)
        form = response.context["adminform"].form
        data = {}
        for bound in form:
            value = bound.value()
            if value in (None, False):
                continue
            data[bound.html_name] = "on" if value is True else str(value)
        data["street"] = "Neuweg"
        data["_save"] = "Save"
        before = set(form.fields)

        saved = self.client.post(address, data)

        self.assertEqual(saved.status_code, 302)
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Neuweg")
        self.assertEqual(set(self.client.get(address).context["adminform"].form.fields), before)

    def test_no_permission_changes_a_staff_account_without_the_right_gets_no_form(self):
        staff = create_active_user("staff.widgets@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        client = ProvingClient()
        client.force_login(staff)
        for name, address, _prefix, _country in self.forms():
            with self.subTest(name):
                response = client.get(address)
                # Whatever the page is (a refusal, or the step to set up the second factor first), it is not the form.
                self.assertNotEqual((response.status_code, b'id="id_street"' in response.content), (200, True), name)
                self.assertNotIn(b"data-address-directory", response.content)
                self.assertNotIn(b"address-directory.js", response.content)

    def test_a_staff_account_with_only_the_view_right_cannot_save_and_gets_no_widget_in_the_read_only_form(self):
        staff = create_active_user("viewer.widgets@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        staff.user_permissions.set(Permission.objects.filter(codename__in=("view_location",)))
        client = ProvingClient()
        client.force_login(staff)
        address = reverse("admin:community_location_change", args=(self.location.pk,))

        response = client.get(address)
        posted = client.post(address, {"street": "Hackweg", "_save": "Save"})

        self.assertNotEqual(posted.status_code, 302)
        self.location.refresh_from_db()
        self.assertEqual(self.location.street, "Gantrischweg")
        self.assertNotIn(b"address-directory.js", response.content)

    def test_an_anonymous_visitor_is_sent_to_the_login_and_sees_no_widget(self):
        for name, address, _prefix, _country in self.forms():
            with self.subTest(name):
                response = self.client_class().get(address)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(b"data-address-directory", response.content)

    def test_the_permission_methods_are_the_ones_of_the_admin_classes_not_of_the_mixin(self):
        for name in ("has_view_permission", "has_add_permission", "has_change_permission", "has_delete_permission"):
            self.assertNotIn(name, vars(AddressSuggestionsMixin))
        request = RequestFactory().get("/")
        staff = create_active_user("rights.widgets@example.org")
        staff.set_privileges(is_staff=True, is_superuser=False)
        # Who may do what is what it was: nobody adds or deletes a user; a system administrator changes one; a staff
        # account without the right changes nothing; the raw tables are not changed by a staff account without the right.
        request.user = self.administrator
        users = admin.site._registry[User]
        self.assertEqual(
            (users.has_add_permission(request), users.has_change_permission(request), users.has_delete_permission(request)),
            (False, True, False),
        )
        request.user = staff
        self.assertEqual(
            (users.has_add_permission(request), users.has_change_permission(request), users.has_delete_permission(request)),
            (False, False, False),
        )
        for model in (Location, ManagementSettings):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                request.user = staff
                self.assertEqual(
                    [getattr(model_admin, method)(request) for method in (
                        "has_add_permission", "has_change_permission", "has_delete_permission",
                    )],
                    [False, False, False],
                )

    def test_the_add_view_of_the_location_stays_refused_and_has_no_widget(self):
        response = self.client.get(reverse("admin:community_location_add"))

        self.assertNotEqual(response.status_code, 200)
        self.assertNotIn(b"data-address-directory", response.content)

    def test_the_raw_lists_keep_plain_inputs_without_the_script(self):
        for address in (
            reverse("admin:community_location_changelist"), reverse("admin:community_managementsettings_changelist"),
        ):
            with self.subTest(address=address):
                response = self.client.get(address)
                html = response.content.decode()

                self.assertEqual(response.status_code, 200)
                self.assertNotIn("data-address-directory", html)
                self.assertNotIn("address-directory.js", html)
                self.assertNotIn("data-address-notice", html)
                self.assertRegex(html, r'<input[^>]*name="form-0-(?:sender_)?street"')
                self.assertNotIn('role="combobox"', html)

    def test_the_list_of_users_has_no_widget(self):
        html = self.client.get(reverse("admin:accounts_user_changelist")).content.decode()

        self.assertNotIn("data-address-directory", html)
        self.assertNotIn("address-directory.js", html)

    def test_the_lookup_endpoint_of_the_widget_is_the_one_of_the_portal(self):
        html = self.client.get(reverse("admin:community_location_change", args=(self.location.pk,))).content.decode()

        self.assertIn(f'data-endpoint="{reverse("addresses:lookup")}"', widget_of(html))
