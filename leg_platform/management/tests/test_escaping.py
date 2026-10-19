"""Output escaping on the staff's pages of the onboarding (docs/SECURITY.md, section 6; the pages review): what a customer typed
(names, descriptions, addresses, their own name) and what a staff member typed (their name in the history of a contract) is shown
as text, in the content and in the attribute contexts (``title``, ``aria-label``, ``value``): the notices, the list and the page
of a process, the page of a deregistration, the pages of the person, the location and the metering point, the list of contracts.
The hostile values are written directly to the database."""

from datetime import date, timedelta

from django.test import tag
from django.urls import reverse
from django.utils.html import escape

from leg_platform.accounts.models import User
from leg_platform.community import application_notices, applications, contracts, deregistrations
from leg_platform.community.models import ApplicationBuilding, ApplicationMeteringPoint, ApplicationNotice, Direction
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point, staff_user
from leg_platform.community.tests.onboarding import TODAY, customer, pdf, ready_draft
from leg_platform.management.tests.test_process_pages import ProcessBase

HOSTILE = '"><img src=x onerror=alert(1)>'
SCRIPT = "<script>alert(1)</script>"
LONG_AGO = date.today() - timedelta(days=400)


def assert_escaped(test, html, where):
    for raw in (HOSTILE, "<img src=x", '"><img src=x', SCRIPT):
        test.assertNotIn(raw, html, f"{raw!r} is not escaped on {where}")
    test.assertNotRegex(html, r"(?i)<script(?![^>]*\ssrc=\")")


@tag("mariadb")
class StaffPagesEscapeTests(ProcessBase):
    databases = {"default", "addresses"}
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.person = customer("escape.person@example.org", first_name=HOSTILE, last_name=SCRIPT, street=HOSTILE)
        cls.location = make_location(HOSTILE, SCRIPT, "3063", "Ittigen", egid=90007701, remark=HOSTILE)
        cls.draw = make_metering_point(
            1, Direction.CONSUMPTION, cls.location, name=HOSTILE, registered_with_operator_on=LONG_AGO,
            confirmed_on=LONG_AGO, start_date=LONG_AGO,
        )
        assign_person(cls.draw, cls.person, valid_from=LONG_AGO)

    def read(self, url, expect=None):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        html = response.content.decode()
        assert_escaped(self, html, url)
        if expect is not None:
            self.assertIn(escape(expect), html, url)
        return html

    def submitted_with_hostile_words(self):
        application, building = ready_draft(self.person, egid=90007702, number=40)
        ApplicationBuilding.objects.filter(pk=building.pk).update(name=HOSTILE, description=SCRIPT, street=HOSTILE)
        ApplicationMeteringPoint.objects.filter(building=building).update(name=HOSTILE)
        applications.submit(self.person, application, today=TODAY)
        application.refresh_from_db()
        return application

    def test_the_list_the_page_and_the_notices_of_the_processes_show_the_words_as_text(self):
        application = self.submitted_with_hostile_words()
        application_notices.record(self.person, ApplicationNotice.Kind.NUMBER_UNAVAILABLE, egid=90007702, numbers=["CH1"])

        listing = self.read(reverse("management:processes"))
        self.assertIn(escape(HOSTILE), listing)
        page = self.read(reverse("management:processes_application", args=[application.pk]))
        self.assertIn(escape(HOSTILE), page)
        self.assertIn(escape(SCRIPT), page)
        notices = self.read(reverse("management:processes_notices"))
        self.assertIn(escape(HOSTILE), notices)

    def test_the_page_of_a_deregistration_shows_the_person_and_the_points_as_text(self):
        dereg = deregistrations.request_deregistration(self.person, metering_point_ids=[self.draw.pk])

        page = self.read(reverse("management:processes_deregistration", args=[dereg.pk]))
        self.assertIn(escape(HOSTILE), page)
        listing = self.read(reverse("management:processes"))
        self.assertIn(escape(HOSTILE), listing)
        self.read(reverse("management:processes") + "?q=" + HOSTILE)

    def test_the_pages_of_the_person_the_location_and_the_metering_point_show_them_as_text(self):
        for url in (
            reverse("management:persons_detail", args=[self.person.pk]),
            reverse("management:locations_detail", args=[self.location.pk]),
            reverse("management:metering_points_detail", args=[self.draw.pk]),
            reverse("management:persons"), reverse("management:locations"), reverse("management:metering_points"),
        ):
            with self.subTest(url=url):
                html = self.read(url)
                self.assertTrue(escape(HOSTILE) in html or escape(SCRIPT) in html, url)
        # A search text typed by the staff comes back as text too.
        for url in (reverse("management:persons"), reverse("management:locations"), reverse("management:metering_points")):
            self.read(url + "?q=" + HOSTILE)

    def test_the_list_of_contracts_shows_the_name_of_the_uploader_as_text(self):
        uploader = staff_user("escape.uploader@example.org")
        User.objects.filter(pk=uploader.pk).update(first_name=HOSTILE, last_name=SCRIPT)
        uploader.refresh_from_db()
        version = contracts.upload_version(uploader, pdf(b"another file"), date(2026, 10, 8))
        contracts.make_current(uploader, version)

        html = self.read(reverse("management:contracts"))

        self.assertIn("LEG", html)
