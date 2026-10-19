"""Output escaping on the customer's pages (docs/SECURITY.md, section 6; the pages review): a name, a description, an address or
a person's own name that holds markup is shown as text, in the content and in the attribute contexts (``aria-label``, ``title``,
``value``). The hostile value is stored directly in the database, as a staff member or an older version could have stored it,
and typed into forms, so that every place that shows it is read."""

from datetime import date, timedelta

from django.test import TestCase, tag
from django.urls import reverse
from django.utils.html import escape

from leg_platform.community import applications, contracts, deregistrations, own_iban
from leg_platform.community.models import ApplicationBuilding, ApplicationMeteringPoint, CustomerBuilding, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.customer.tests.helpers import (
    ADDRESS,
    BUILDINGS,
    GOOD_IBAN,
    START,
    candidate,
    configure_numbers,
    customer,
    directory,
    draft_with_points,
    login,
    publish_contract,
    step_url,
)

HOSTILE = '"><img src=x onerror=alert(1)>'
SCRIPT = "<script>alert(1)</script>"
LONG_AGO = date.today() - timedelta(days=400)


def assert_escaped(test, html, where):
    """No hostile markup survives: neither the tag nor the quote that closes an attribute before it."""
    for raw in (HOSTILE, "<img src=x", '"><img src=x', SCRIPT):
        test.assertNotIn(raw, html, f"{raw!r} is not escaped on {where}")
    test.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")


@tag("mariadb")
class CustomerPagesEscapeTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("escape.mine@example.org", first_name=HOSTILE, last_name=SCRIPT, street=HOSTILE)
        cls.location = make_location(HOSTILE, SCRIPT, "3063", "Ittigen", egid=90007777)
        cls.draw = make_metering_point(
            1, Direction.CONSUMPTION, cls.location, name=HOSTILE, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        cls.feed_in = make_metering_point(
            2, Direction.FEED_IN, cls.location, name=SCRIPT, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(cls.draw, cls.mine, valid_from=LONG_AGO)
        assign_person(cls.feed_in, cls.mine, valid_from=LONG_AGO)
        CustomerBuilding.objects.create(person=cls.mine, location=cls.location, name=HOSTILE, description=SCRIPT)

    def setUp(self):
        self.client = login(self.mine)

    def read(self, url, where=None, **kwargs):
        response = self.client.get(url, follow=True, **kwargs)
        self.assertEqual(response.status_code, 200, url)
        html = response.content.decode()
        assert_escaped(self, html, where or url)
        return html

    def test_the_page_buildings_shows_names_addresses_and_descriptions_as_text(self):
        html = self.read(BUILDINGS)
        # The title is the own name; it stands in the content and in the aria-label of the list.
        self.assertIn(escape(HOSTILE), html)
        self.assertIn(escape(SCRIPT), html)
        self.assertRegex(html, r'aria-label="[^"]*&quot;&gt;&lt;img src=x onerror=alert\(1\)&gt;')

    def test_the_pencil_pages_show_the_own_words_as_text_also_after_an_error(self):
        details = reverse("customer:building_details", args=[self.location.pk])
        name = reverse("customer:metering_point_name", args=[self.draw.pk])
        self.assertIn(f'value="{escape(HOSTILE)}"', self.read(details))
        self.assertIn(f'value="{escape(HOSTILE)}"', self.read(name))
        # A refused entry is shown again with what was typed.
        for url, data in ((details, {"name": HOSTILE + "\x07", "description": SCRIPT}), (name, {"name": HOSTILE + "\x07"})):
            response = self.client.post(url, data)
            self.assertEqual(response.status_code, 200)
            assert_escaped(self, response.content.decode(), url)

    def test_the_deregistration_pages_show_the_points_and_the_building_as_text(self):
        html = self.read(reverse("customer:deregistration_start"))
        self.assertIn(escape(HOSTILE), html)
        self.assertIn(escape(SCRIPT), html)
        wrong = self.client.post(
            reverse("customer:deregistration_start"),
            {"metering_points": [str(self.draw.pk)], "termination": "date", "desired_date": HOSTILE,
             "current_password": "wrong " + HOSTILE},
        )
        self.assertEqual(wrong.status_code, 200)
        assert_escaped(self, wrong.content.decode(), "the deregistration after an error")
        self.assertNotIn("wrong " + HOSTILE, wrong.content.decode())
        own_iban.set_own_iban(self.mine, GOOD_IBAN)
        done = deregistrations.request_deregistration(self.mine, metering_point_ids=[self.draw.pk, self.feed_in.pk])
        html = self.read(reverse("customer:deregistration_done", args=[done.pk]))
        self.assertIn(escape(SCRIPT), html)
        self.assertIn(escape(HOSTILE), self.read(BUILDINGS))

    def test_the_profile_shows_the_name_and_the_address_as_text(self):
        html = self.read(reverse("accounts:profile"))
        self.assertIn(escape(HOSTILE), html)
        for query in ("?edit=personal", "?edit=bank", "?edit=email", "?edit=delete"):
            self.read(reverse("accounts:profile") + query)
        self.read(reverse("accounts:profile") + "?edit=bank&next=" + HOSTILE)
        response = self.client.post(reverse("customer:bank_connection"), {"iban": HOSTILE, "next": HOSTILE})
        self.assertEqual(response.status_code, 200)
        assert_escaped(self, response.content.decode(), "the bank card after an error")

    def test_the_start_page_the_home_page_and_the_contract_page_show_the_name_as_text(self):
        publish_contract()
        for url in (reverse("home"), reverse("customer:contract"), START):
            self.read(url)


@tag("mariadb")
class WizardPagesEscapeTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.mine = customer("escape.wizard@example.org", first_name=HOSTILE)

    def setUp(self):
        configure_numbers()
        self.version = publish_contract()
        self.client = login(self.mine)

    def test_every_step_and_the_pages_around_it_show_stored_and_typed_values_as_text(self):
        application, building = draft_with_points(self.mine, feed_in=True)
        ApplicationBuilding.objects.filter(pk=building.pk).update(
            name=HOSTILE, description=SCRIPT, street=HOSTILE, house_number=SCRIPT
        )
        ApplicationMeteringPoint.objects.filter(building=building).update(name=HOSTILE)
        applications.save_bank_connection(self.mine, application, GOOD_IBAN)
        applications.accept_contract_step(
            self.mine, application, contracts.current_version().pk, agrees_contract=True, agrees_data_exchange=True
        )
        pages = [START, BUILDINGS, reverse("customer:application_cancel", args=[application.pk])]
        pages += [step_url(application, name) for name in ("building", "metering_points", "bank_connection", "contract", "start_date")]
        pages += [step_url(application, name) + "?cancel=1" for name in ("building", "metering_points")]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                assert_escaped(self, response.content.decode(), url)
        self.assertIn(escape(HOSTILE), self.client.get(BUILDINGS).content.decode())

    def test_typed_values_come_back_as_text_in_the_forms_of_the_steps(self):
        application = applications.start_draft(self.mine)
        url = step_url(application, "building")
        with directory():
            response = self.client.post(url, {"street": HOSTILE, "house_number": SCRIPT, "postal_code": "3063", "town": HOSTILE})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        assert_escaped(self, html, "the building step")
        self.assertIn(f'value="{escape(HOSTILE)}"', html)
        # The suggestions of the local directory come from the directory, shown as text too.
        hostile_candidate = {**ADDRESS, "street": HOSTILE, "egid": 90007778, "entrance_id": None}
        with directory(suggestions=[hostile_candidate]):
            response = self.client.post(url, {**ADDRESS, "street": "Ittigenstrase", "house_number": "51", "postal_code": "3063", "town": "Ittigen"})
        assert_escaped(self, response.content.decode(), "the suggestions")
        # The metering point step with a refused entry.
        with directory(candidate(90007779)):
            self.client.post(url, {**ADDRESS})
        points = step_url(application, "metering_points")
        response = self.client.post(points, {"draw_number": HOSTILE, "draw_name": SCRIPT, "feed_in": "yes", "feed_in_number": SCRIPT})
        assert_escaped(self, response.content.decode(), "the metering point step")
        bank = step_url(application, "bank_connection")
        response = self.client.post(bank, {"iban": HOSTILE})
        assert_escaped(self, response.content.decode(), "the bank step")

    def test_the_name_of_the_person_in_the_frame_is_text(self):
        # The header and the greeting show the person's own name.
        html = self.client.get(reverse("home")).content.decode()
        assert_escaped(self, html, "the frame")
