"""Every place where an address can be entered has the typing suggestions of the address directory (D-111), built the same way.

One table of the pages and one contract for all of them: the one widget (``addresses/widget.html``) under the street, the
one notice after the postal code and the town, the one script, field ids that exist in the page, the country (fixed or the
field of the form), a hidden and empty panel without JavaScript, no inline script or handler, and the sentence about an
empty directory for staff accounts only. The forms: registration details, profile, customer wizard (building), management
person creation and billing address, location creation and change, the correction of an application's building, and the
settings of the LEG (the sender address). The behaviour of the script is in ``test_widget_script`` (a headless Chrome) and
the rules of its source in ``accounts.tests.test_address_notice``."""

import re

from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts.models import VerificationToken
from leg_platform.accounts.tests.helpers import PASSWORD, VerifiedClient, create_active_user, password_data
from leg_platform.accounts.tests.helpers import verification_path
from leg_platform.accounts.tests.test_registration import register
from leg_platform.community import applications
from leg_platform.community.tests.helpers import make_location, staff_user
from leg_platform.customer.tests.helpers import login as customer_login
from leg_platform.management.tests.test_process_pages import ProcessBase

SCRIPT = '<script defer src="/static/js/address-directory.js"></script>'
FIELDS = (("street", "street"), ("house-number", "house_number"), ("postal-code", "postal_code"), ("locality", "town"))


def widget_of(html):
    start = html.index('<div class="address-assist"')
    marker = "data-address-announce></p>"
    return html[start:html.index(marker, start) + len(marker)]


@tag("mariadb")
class EveryAddressFormTests(ProcessBase):
    databases = {"default", "addresses"}

    def setUp(self):
        super().setUp()
        self.location = make_location("Gantrischweg", "7", "3063", "Ittigen")
        self.application = self.submitted(self.anna)
        self.building = self.application.buildings.get()

    def registration_page(self):
        client = Client()
        register(client)
        # The mail of the registration among the notices that the fixtures of the page queued.
        paths = []
        for message in mail.outbox:
            try:
                paths.append(verification_path(message))
            except AssertionError:
                continue
        self.assertEqual(len(paths), 1)
        client.get(paths[0])
        token = VerificationToken.objects.get()
        client.post(reverse("accounts:registration_password"), password_data(PASSWORD, registration=token.pk))
        return client.get(reverse("accounts:registration_complete")).content.decode()

    def staff_page(self, name, *args, **query):
        client = VerifiedClient()
        client.force_login(self.staff)
        response = client.get(reverse(f"management:{name}", args=args), query)
        self.assertEqual(response.status_code, 200, name)
        return response.content.decode()

    def pages(self):
        """``(name, html, prefix of the field ids, whether the page is for a staff account)``."""
        profile_client = VerifiedClient()
        profile_client.force_login(create_active_user("typing.profile@example.org"))
        applicant = create_active_user("typing.customer@example.org")
        customer = customer_login(applicant)
        draft = applications.start_draft(applicant)
        return (
            ("registration details", self.registration_page(), "", False),
            ("profile", profile_client.get(reverse("accounts:profile"), {"edit": "personal"}).content.decode(), "", False),
            ("customer wizard", customer.get(reverse("customer:application_building", args=[draft.pk])).content.decode(), "", False),
            ("person creation", self.staff_page("persons_new"), "", True),
            ("person billing address", self.staff_page("persons_address", self.anna.pk), "", True),
            ("location creation", self.staff_page("locations_new"), "", True),
            ("location change", self.staff_page("locations_address", self.location.pk), "", True),
            (
                "correction of a building",
                self.staff_page("processes_application_building", self.application.pk, self.building.pk), "", True,
            ),
            ("settings of the LEG", self.staff_page("settings"), "sender_", True),
        )

    def test_every_form_with_an_address_has_the_widget_the_notice_and_the_script(self):
        for name, html, prefix, staff in self.pages():
            with self.subTest(name):
                widget = widget_of(html)
                self.assertEqual(html.count("data-address-directory"), 1)
                self.assertEqual(html.count(SCRIPT), 1)
                # The ids of the four fields exist in the page, each once, in the order street .. town, and the widget
                # names them (a prefix for the sender address of the settings).
                positions = []
                for data_name, field in FIELDS:
                    field_id = f"id_{prefix}{field}"
                    self.assertIn(f'data-{data_name}-id="{field_id}"', widget)
                    self.assertEqual(html.count(f' id="{field_id}"'), 1, field_id)
                    positions.append(html.index(f' id="{field_id}"'))
                self.assertEqual(positions[0] < positions[2] < positions[3], True)
                # The country: fixed to Switzerland, or the country field of the form.
                if "data-country-value" in widget:
                    self.assertIn('data-country-value="CH"', widget)
                    self.assertNotIn("data-country-id", widget)
                else:
                    self.assertIn('data-country-id="id_country"', widget)
                    self.assertEqual(html.count(' id="id_country"'), 1)
                # The one notice after the postal code and the town; the widget names it.
                self.assertEqual(html.count("data-address-notice"), 1)
                self.assertRegex(html, r'<div class="address-notice" id="address-notice" data-address-notice role="status"></div>')
                self.assertIn('data-notice-id="address-notice"', widget)
                self.assertLess(positions[3], html.index("data-address-notice"))
                # The widget sits under the street input, before the next field.
                self.assertLess(positions[0], html.index("data-address-directory"))
                self.assertLess(html.index("data-address-directory"), positions[1])

    def test_without_javascript_the_page_is_a_plain_form_with_an_empty_hidden_panel(self):
        for name, html, prefix, staff in self.pages():
            with self.subTest(name):
                widget = widget_of(html)
                # The panel: hidden, empty, a listbox with a label, nothing to say without a script.
                self.assertRegex(widget, r'<div class="address-suggest" data-address-suggest hidden>')
                self.assertRegex(widget, r'<ul class="address-suggest-list" id="address-suggest-list" data-address-results role="listbox" aria-labelledby="address-suggest-title"></ul>')
                self.assertRegex(widget, r'<p class="address-suggest-title" id="address-suggest-title" data-address-title></p>')
                self.assertRegex(widget, r'<p class="visually-hidden" role="status" aria-live="polite" aria-atomic="true" data-address-announce></p>')
                self.assertNotIn("address-option", widget)
                # The inputs are plain inputs until the script runs: it sets the combobox attributes, so nothing
                # announces a widget that does not work.
                for _data_name, field in FIELDS:
                    tag_of_input = re.search(rf'<input[^>]*\bid="id_{prefix}{field}"[^>]*>', html).group(0)
                    self.assertNotIn("role=", tag_of_input)
                    self.assertNotIn("aria-expanded", tag_of_input)
                self.assertIn('type="submit"', html)
                self.assertNotIn("<noscript", widget)

    def test_no_script_is_inline_and_no_handler_is_an_attribute(self):
        for name, html, prefix, staff in self.pages():
            with self.subTest(name):
                self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)", html), [])
                self.assertNotRegex(widget_of(html), r"\son[a-z]+=|\sstyle=")

    def test_the_sentence_about_an_empty_directory_is_given_to_staff_only(self):
        for name, html, prefix, staff in self.pages():
            with self.subTest(name):
                self.assertEqual("data-directory-missing=" in widget_of(html), staff)

    def test_a_read_only_page_has_neither_the_widget_nor_the_script(self):
        for html in (self.staff_page("locations_detail", self.location.pk), self.staff_page("persons_detail", self.anna.pk)):
            self.assertNotIn("data-address-directory", html)
            self.assertNotIn("address-directory.js", html)
