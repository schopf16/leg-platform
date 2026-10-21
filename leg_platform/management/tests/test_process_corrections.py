"""The staff's corrections on the page of an application and the display of metering point numbers and plant details
(D-109; owner walkthrough 3).

What is proven: the page offers "Edit" for the address of a new building and for every metering point exactly while the
LEG works on the application (not for a draft, a paused draft, a completed application, nor for a stored building, whose
address is the location's); the two dialogs are prefilled and checked on the server like the customer's wizard (directory,
EGID, duplicates, number rule, plant figures) and say why they refuse; an address that the directory does not know is shown
with the suggestion and saved as entered only after a second, explicit save; a stale dialog (the customer changed the data
or another staff member corrected it) changes nothing; a forged field is not taken (no mass assignment); the token is
required; a customer, an anonymous visitor and an unknown id get nothing; the history names the fields and no value. The
number is on one line with the grid operator's block and blocks of four on the staff's pages and the customer's pages, and
the plant details are labelled lines, never a sentence."""

from decimal import Decimal

from django.test import tag
from django.urls import reverse

from leg_platform.community import application_corrections, applications, history, numbers
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Application,
    ApplicationBuilding,
    ApplicationMeteringPoint,
    Direction,
)
from leg_platform.community.tests.helpers import make_location, staff_user
from leg_platform.community.tests.onboarding import (
    ADDRESS,
    TODAY,
    candidate,
    customer,
    directory,
    full_number,
    publish_contract,
    ready_draft,
)
from leg_platform.management.tests.test_process_pages import ProcessBase

State = Application.State
NBSP = "\u00a0"


def shown(number):
    return numbers.format_metering_point_number(full_number(number))


@tag("mariadb")
class CorrectionBase(ProcessBase):
    # The dialog of an address asks the address directory for its suggestion (the notice "not known").
    databases = {"default", "addresses"}

    def setUp(self):
        super().setUp()
        self.application = self.submitted(self.anna, feed_in=True)
        self.building = self.application.buildings.get()
        self.draw = self.building.metering_points.get(direction=Direction.CONSUMPTION)
        self.feed_in = self.building.metering_points.get(direction=Direction.FEED_IN)

    def building_url(self, application=None, building=None):
        return reverse(
            "management:processes_application_building",
            args=[(application or self.application).pk, (building or self.building).pk],
        )

    def point_url(self, application=None, point=None):
        return reverse(
            "management:processes_application_point", args=[(application or self.application).pk, (point or self.draw).pk]
        )

    def page_url(self, application=None):
        return reverse("management:processes_application", args=[(application or self.application).pk])

    def refreshed(self, obj):
        return type(obj).objects.get(pk=obj.pk)

    def building_data(self, **values):
        row = self.refreshed(self.building)
        return {
            "street": row.street, "house_number": row.house_number, "postal_code": row.postal_code, "town": row.town,
            "egid": "", "expected": application_corrections.building_token(row), **values,
        }

    def point_data(self, point=None, **values):
        row = self.refreshed(point or self.draw)
        return {
            "number_digits": row.number[13:], "direction": row.direction,
            "pv_power_kwp": "" if row.pv_power_kwp is None else str(row.pv_power_kwp),
            "battery_kwh": "" if row.battery_kwh is None else str(row.battery_kwh),
            "wallbox_kw": "" if row.wallbox_kw is None else str(row.wallbox_kw),
            "expected": application_corrections.point_token(row), **values,
        }

    def set_state(self, state):
        Application.objects.filter(pk=self.application.pk).update(state=state)


class ThePageOffersTheCorrectionOnlyWhileTheLegWorksOnTheApplicationTests(CorrectionBase):
    def test_there_is_an_edit_button_for_the_address_and_for_each_metering_point(self):
        page = self.client.get(self.page_url())

        self.assertContains(page, f'href="{self.building_url()}?next=', count=1)
        self.assertContains(page, f'href="{self.point_url()}?next=', count=1)
        self.assertContains(page, f'href="{self.point_url(point=self.feed_in)}?next=', count=1)
        # The accessible name starts with the visible word and names what it edits.
        self.assertContains(page, "Adresse bearbeiten<span class=\"visually-hidden\"> Ittigenstrasse 51</span>")
        self.assertContains(page, f'Bearbeiten<span class="visually-hidden"> {shown(1)}</span>')

    def test_the_buttons_are_there_in_every_state_in_which_the_leg_works_and_in_no_other(self):
        for state in (State.SUBMITTED, State.IN_REVIEW, State.OPERATOR_STARTED, State.OPERATOR_CONFIRMED):
            with self.subTest(state=state):
                self.set_state(state)
                page = self.client.get(self.page_url())
                self.assertContains(page, self.building_url())
                self.assertContains(page, self.point_url())
        for state in (State.DRAFT, State.PAUSED, State.COMPLETED):
            with self.subTest(state=state):
                self.set_state(state)
                page = self.client.get(self.page_url())
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, self.building_url())
                self.assertNotContains(page, self.point_url())

    def test_a_building_that_is_stored_already_has_no_address_button_but_says_where_it_is_changed(self):
        make_location("Ittigenstrasse", "99", "3063", "Ittigen", egid=90004299)
        other = customer("stored.customer@example.org", first_name="Sara", last_name="Gespeichert")
        application, building = ready_draft(other, egid=90004299, number=40, address={"house_number": "99"})
        applications.submit(other, application, today=TODAY)

        page = self.client.get(self.page_url(application))

        self.assertIsNotNone(self.refreshed(building).location_id)
        self.assertNotContains(page, self.building_url(application, building))
        self.assertContains(page, "Die Adresse eines gespeicherten Gebäudes ändern Sie auf der Seite des Standorts.")
        self.assertContains(page, self.point_url(application, building.metering_points.get()))


class TheNumberAndThePlantDetailsAreReadableTests(CorrectionBase):
    def test_the_number_is_one_element_with_the_grid_operators_block_and_blocks_of_four(self):
        page = self.client.get(self.page_url())

        self.assertContains(page, f'<span class="mp-number">{shown(1)}</span>')
        self.assertContains(page, f'<span class="mp-number">{shown(2)}</span>')
        self.assertContains(page, f"CH1A2B3C4D5E6{NBSP}0000{NBSP}0000{NBSP}0000{NBSP}0000{NBSP}0001")
        self.assertNotContains(page, "<wbr>")
        self.assertNotContains(page, full_number(1))  # never the 33 characters in one piece

    def test_the_plant_details_are_one_labelled_line_per_figure_not_a_sentence(self):
        ApplicationMeteringPoint.objects.filter(pk=self.feed_in.pk).update(
            pv_power_kwp=Decimal("9.5"), battery_kwh=Decimal("10"), wallbox_kw=Decimal("11.25")
        )
        html = self.client.get(self.page_url()).content.decode()

        block = html[html.index('<dl class="mgmt-plant">'):]
        block = block[: block.index("</dl>")]
        self.assertEqual(block.count("<dt>"), 3)
        for label, value in (("PV-Leistung", "9.50 kWp"), ("Batterie", "10.00 kWh"), ("Wallbox", "11.25 kW")):
            self.assertIn(f"<dt>{label}</dt><dd class=\"tnum\">{value}</dd>", block)
        # No sentence that squeezes them together.
        self.assertNotIn("PV 9.50 kWp Batterie", html)
        self.assertNotIn("kWp Batterie", html)

    def test_only_the_figures_that_were_entered_get_a_line_and_a_draw_has_no_plant_row(self):
        ApplicationMeteringPoint.objects.filter(pk=self.feed_in.pk).update(pv_power_kwp=Decimal("4"))
        html = self.client.get(self.page_url()).content.decode()

        self.assertEqual(html.count('<dl class="mgmt-plant">'), 1)
        block = html[html.index('<dl class="mgmt-plant">'):]
        block = block[: block.index("</dl>")]
        self.assertEqual(block.count("<dt>"), 1)
        self.assertNotIn("Batterie", block)
        # A feed-in without any figure shows a grey dash, a draw shows no "Plant details" row at all.
        ApplicationMeteringPoint.objects.filter(pk=self.feed_in.pk).update(pv_power_kwp=None)
        html = self.client.get(self.page_url()).content.decode()
        self.assertEqual(html.count('<dl class="mgmt-plant">'), 0)
        self.assertEqual(html.count("Anlagedetails"), 1)

    def test_the_list_of_metering_points_and_the_page_of_a_person_show_the_same_grouping(self):
        from leg_platform.community.tests.helpers import assign_person, make_metering_point

        place = make_location("Listenweg", "1", "3063", "Ittigen")
        point = make_metering_point(5, location=place)
        assign_person(point, self.anna)

        listing = self.client.get(reverse("management:metering_points")).content.decode()
        person = self.client.get(reverse("management:persons_detail", args=[self.anna.pk])).content.decode()
        location = self.client.get(reverse("management:locations_detail", args=[place.pk])).content.decode()

        for page in (listing, person, location):
            self.assertIn(f'<span class="mp-number">{shown(5)}</span>', page)
            self.assertNotIn("<wbr>", page)
            self.assertNotIn(full_number(5), page)

    def test_the_customers_pages_show_the_number_the_same_way(self):
        page = self.login(self.anna).get(reverse("customer:buildings")).content.decode()

        self.assertIn(f'<span class="mp-number">{shown(1)}</span>', page)
        self.assertNotIn(full_number(1), page)


class TheDialogOfABuildingTests(CorrectionBase):
    def test_it_is_prefilled_with_what_the_customer_entered_and_carries_the_token_the_page_showed(self):
        page = self.client.get(self.building_url(), {"next": self.page_url()})

        self.assertContains(page, "Adresse bearbeiten")
        self.assertContains(page, 'value="Ittigenstrasse"')
        self.assertContains(page, 'value="51"')
        self.assertContains(page, 'value="3063"')
        self.assertContains(page, 'value="Ittigen"')
        self.assertContains(page, 'name="expected"')
        self.assertContains(page, application_corrections.building_token(self.refreshed(self.building)))
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertContains(page, f'href="{self.page_url()}"')  # "Cancel" goes back to the page
        self.assertEqual(self.refreshed(self.building).house_number, "51")  # reading changes nothing

    def test_saving_corrects_the_address_with_the_directorys_egid_and_goes_back(self):
        with directory(candidate(90004270, house_number="52")):
            response = self.client.post(
                self.building_url(), {**self.building_data(house_number="52"), "next": self.page_url()}
            )

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        row = self.refreshed(self.building)
        self.assertEqual((row.house_number, row.egid), ("52", 90004270))
        page = self.client.get(self.page_url())
        self.assertContains(page, "Ittigenstrasse 52")
        self.assertContains(page, "EGID 90004270")
        self.assertContains(page, "Gebäude durch die LEG korrigiert")
        entry = history.entries_for(self.application).get(event=HistoryEvent.APPLICATION_BUILDING_CORRECTED)
        self.assertEqual(entry.actor_id, self.staff.pk)
        self.assertEqual(entry.detail, {"building_id": self.building.pk, "fields": ["house_number", "egid"]})

    def test_an_address_the_directory_does_not_know_is_shown_with_the_notice_and_kept_only_after_a_second_save(self):
        with directory():
            first = self.client.post(self.building_url(), self.building_data(street="Phantasiegasse"))

            self.assertEqual(first.status_code, 200)
            self.assertContains(first, "Das Adressverzeichnis kennt diese Adresse nicht.")
            self.assertContains(first, "Wie eingegeben speichern")
            self.assertContains(first, 'name="confirm_unlisted" value="1"')
            self.assertEqual(self.refreshed(self.building).street, "Ittigenstrasse")

            second = self.client.post(
                self.building_url(), self.building_data(street="Phantasiegasse", confirm_unlisted="1")
            )

        self.assertEqual(second.status_code, 302)
        row = self.refreshed(self.building)
        self.assertEqual((row.street, row.egid), ("Phantasiegasse", None))

    def test_several_buildings_ask_for_the_egid_a_wrong_one_and_a_bad_address_are_refused_on_the_field(self):
        with directory(candidate(90004270, house_number="52"), candidate(90004271, house_number="52")):
            ask = self.client.post(self.building_url(), self.building_data(house_number="52"))
            self.assertContains(ask, "Mehrere Gebäude haben diese Adresse. Geben Sie die EGID eines davon ein: 90004270, 90004271.")
            wrong = self.client.post(self.building_url(), self.building_data(house_number="52", egid="90004999"))
            self.assertContains(wrong, "Die EGID gehört nicht zu dieser Adresse.")
            chosen = self.client.post(self.building_url(), self.building_data(house_number="52", egid="90004271"))
        self.assertEqual(chosen.status_code, 302)
        self.assertEqual(self.refreshed(self.building).egid, 90004271)
        bad = self.client.post(self.building_url(), self.building_data(postal_code="30"))
        self.assertContains(bad, "Geben Sie eine Postleitzahl mit 4 Ziffern von 1000 bis 9999 ein.")
        text = self.client.post(self.building_url(), self.building_data(egid="12x"))
        self.assertContains(text, "Geben Sie die EGID als Zahl ein.")

    def test_a_stale_dialog_changes_nothing_and_says_why(self):
        stale = self.building_data()
        with directory(candidate(90004270, house_number="52")):
            self.client.post(self.building_url(), self.building_data(house_number="52"))

        with directory(candidate(90004271, house_number="53")):
            response = self.client.post(self.building_url(), {**stale, "house_number": "53"})

        self.assertContains(response, "Dieser Eintrag wurde inzwischen geändert, zum Beispiel vom Kunden.")
        self.assertEqual(self.refreshed(self.building).house_number, "52")

    def test_after_the_customer_took_the_application_back_the_dialog_is_refused_with_the_state_message(self):
        data = self.building_data(house_number="52")
        applications.reopen_for_change(self.anna, self.application)

        get = self.client.get(self.building_url(), follow=True)
        with directory(candidate(90004270, house_number="52")):
            post = self.client.post(self.building_url(), data, follow=True)

        for response in (get, post):
            self.assertContains(response, "Dieser Schritt ist im aktuellen Zustand nicht möglich")
        self.assertEqual(self.refreshed(self.building).house_number, "51")

    def test_a_stored_building_is_not_edited_here(self):
        make_location("Ittigenstrasse", "99", "3063", "Ittigen", egid=90004299)
        other = customer("stored.dialog@example.org")
        application, building = ready_draft(other, egid=90004299, number=41, address={"house_number": "99"})
        applications.submit(other, application, today=TODAY)

        get = self.client.get(self.building_url(application, building), follow=True)
        with directory(candidate(90004299, house_number="98")):
            post = self.client.post(
                self.building_url(application, building),
                {**ADDRESS, "house_number": "98", "egid": "", "expected": application_corrections.building_token(building)},
                follow=True,
            )

        for response in (get, post):
            self.assertContains(response, "Dieses Gebäude ist bereits gespeichert.")
        self.assertIsNotNone(self.refreshed(building).location_id)

    def test_no_forged_field_is_taken(self):
        other = customer("forge.customer@example.org")
        other_application, other_building = ready_draft(other, egid=90004280, number=50, address={"house_number": "80"})
        applications.submit(other, other_application, today=TODAY)
        forged = {
            "state": "completed", "application": other_application.pk, "building": other_building.pk,
            "person": other.pk, "location": 1, "preselected": "on", "name": "Forged", "description": "Forged",
            "is_staff": "on", "id": other_building.pk, "pk": other_building.pk,
        }

        with directory(candidate(90004270, house_number="52")):
            response = self.client.post(self.building_url(), {**self.building_data(house_number="52"), **forged})

        self.assertEqual(response.status_code, 302)
        row = self.refreshed(self.building)
        self.assertEqual((row.application_id, row.house_number, row.name, row.description, row.preselected, row.location_id),
                         (self.application.pk, "52", "", "", False, None))
        self.assertEqual(self.state(self.application), State.SUBMITTED)
        self.assertEqual(self.refreshed(other_building).house_number, "80")
        self.assertEqual(self.state(other_application), State.SUBMITTED)

    def test_an_id_of_another_application_or_a_missing_one_is_a_404_and_changes_nothing(self):
        other = customer("foreign.customer@example.org")
        other_application, other_building = ready_draft(other, egid=90004281, number=51, address={"house_number": "81"})
        applications.submit(other, other_application, today=TODAY)
        data = self.building_data(house_number="52")
        urls = (
            self.building_url(self.application, other_building),  # a building of another application
            reverse("management:processes_application_building", args=[self.application.pk, 999999]),
            reverse("management:processes_application_building", args=[999999, self.building.pk]),
        )

        with directory(candidate(90004270, house_number="52")):
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)
                    self.assertEqual(self.client.post(url, data).status_code, 404)

        self.assertEqual(self.refreshed(other_building).house_number, "81")
        self.assertEqual(self.refreshed(self.building).house_number, "51")


class TheDialogOfAMeteringPointTests(CorrectionBase):
    def test_it_is_prefilled_with_the_number_the_direction_and_the_plant_details(self):
        ApplicationMeteringPoint.objects.filter(pk=self.feed_in.pk).update(
            pv_power_kwp=Decimal("9.5"), battery_kwh=Decimal("10")
        )

        page = self.client.get(self.point_url(point=self.feed_in), {"next": self.page_url()})

        self.assertContains(page, "Messpunkt bearbeiten")
        self.assertContains(page, "CH1A2B3C4D5E6")  # the prefix of the settings stands in front
        self.assertContains(page, 'value="00000000000000000002"')  # the last 20 characters
        self.assertContains(page, "Anlagedetails")
        self.assertContains(page, 'value="9.50"')
        self.assertContains(page, 'value="10.00"')
        self.assertContains(page, 'name="expected"')
        self.assertContains(page, "Wurden der Bezug und die Einspeisung vertauscht eingegeben")  # two points: the hint
        self.assertContains(page, reverse("management:metering_points_check_number"))  # the check of the entry

    def test_saving_corrects_the_number_the_figures_and_records_the_fields_not_the_values(self):
        response = self.client.post(
            self.point_url(point=self.feed_in),
            {**self.point_data(self.feed_in, number_digits="77", pv_power_kwp="9,5"), "next": self.page_url()},
        )

        self.assertRedirects(response, self.page_url(), fetch_redirect_response=False)
        row = self.refreshed(self.feed_in)
        self.assertEqual((row.number, row.pv_power_kwp), (full_number(77), Decimal("9.50")))
        page = self.client.get(self.page_url())
        self.assertContains(page, f'<span class="mp-number">{shown(77)}</span>')
        self.assertContains(page, "Messpunkt durch die LEG korrigiert")
        entry = history.entries_for(self.application).get(event=HistoryEvent.APPLICATION_POINT_CORRECTED)
        self.assertEqual(entry.detail, {"point_id": self.feed_in.pk, "fields": ["number", "pv_power_kwp"]})

    def test_a_pasted_number_with_the_blocks_of_the_display_is_taken(self):
        pasted = shown(88)  # the blocks are separated by no-break spaces, as copied from a page

        self.client.post(self.point_url(), self.point_data(number_digits=pasted))

        self.assertEqual(self.refreshed(self.draw).number, full_number(88))

    def test_a_refused_number_is_shown_at_the_field_with_its_reason(self):
        for typed, text in (
            (full_number(2)[13:], "Diese Nummer wird bereits von einem anderen Messpunkt dieser Anmeldung verwendet."),
            ("1" * 21, "Die Messpunktnummer ist ungültig: Sie ist zu lang."),
            ("a!", "Erlaubt sind nur die Buchstaben A–Z"),
        ):
            with self.subTest(typed=typed):
                page = self.client.post(self.point_url(), self.point_data(number_digits=typed))
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, text)
        self.assertEqual(self.refreshed(self.draw).number, full_number(1))

    def test_a_number_of_a_stored_point_or_another_customers_application_is_refused_without_a_notice(self):
        from leg_platform.community.models import ApplicationNotice
        from leg_platform.community.tests.helpers import make_metering_point

        make_metering_point(5)
        other = customer("number.other@example.org")
        other_application, _building = ready_draft(other, egid=90004282, number=6, address={"house_number": "82"})
        applications.submit(other, other_application, today=TODAY)
        notices = ApplicationNotice.objects.count()

        for last in ("5", "6"):
            with self.subTest(last=last):
                page = self.client.post(self.point_url(), self.point_data(number_digits=last))
                self.assertContains(page, "Diese Nummer gehört zu einem gespeicherten Messpunkt oder zu einer anderen Anmeldung.")
        self.assertEqual(ApplicationNotice.objects.count(), notices)
        self.assertEqual(self.refreshed(self.draw).number, full_number(1))

    def test_plant_details_for_a_draw_and_a_wrong_figure_are_refused(self):
        draw = self.client.post(self.point_url(), self.point_data(pv_power_kwp="3"))
        self.assertContains(draw, "Anlagedetails gelten nur für eine Einspeisung.")
        for typed in ("-1", "abc", "1.234", "9999999"):
            with self.subTest(typed=typed):
                page = self.client.post(self.point_url(point=self.feed_in), self.point_data(self.feed_in, pv_power_kwp=typed))
                self.assertEqual(page.status_code, 200)
        self.assertIsNone(self.refreshed(self.feed_in).pv_power_kwp)

    def test_an_unusual_figure_saves_and_warns(self):
        response = self.client.post(
            self.point_url(point=self.feed_in), self.point_data(self.feed_in, wallbox_kw="5000"), follow=True
        )

        self.assertContains(response, "Die Angabe zu Wallbox ist ungewöhnlich gross.")
        self.assertEqual(self.refreshed(self.feed_in).wallbox_kw, Decimal("5000.00"))

    def test_the_directions_are_exchanged_when_the_customer_entered_them_the_wrong_way_round(self):
        draw_number, feed_in_number = self.draw.number, self.feed_in.number

        response = self.client.post(self.point_url(), self.point_data(direction="feed_in"))

        self.assertEqual(response.status_code, 302)
        draw, feed_in = self.refreshed(self.draw), self.refreshed(self.feed_in)
        self.assertEqual((draw.direction, draw.number, feed_in.direction, feed_in.number),
                         ("feed_in", draw_number, "consumption", feed_in_number))

    def test_a_direction_that_leaves_the_building_without_a_draw_is_refused(self):
        self.feed_in.delete()

        page = self.client.post(self.point_url(), self.point_data(direction="feed_in"))

        self.assertContains(page, "Ein Gebäude braucht einen Messpunkt für den Bezug.")
        self.assertEqual(self.refreshed(self.draw).direction, "consumption")

    def test_a_stale_dialog_and_a_dialog_after_the_customer_took_the_application_back_change_nothing(self):
        stale = self.point_data(number_digits="99")
        self.client.post(self.point_url(), self.point_data(number_digits="12"))

        response = self.client.post(self.point_url(), stale)

        self.assertContains(response, "Dieser Eintrag wurde inzwischen geändert, zum Beispiel vom Kunden.")
        self.assertEqual(self.refreshed(self.draw).number, full_number(12))
        data = self.point_data(number_digits="13")
        applications.reopen_for_change(self.anna, self.application)
        for response in (self.client.get(self.point_url(), follow=True), self.client.post(self.point_url(), data, follow=True)):
            self.assertContains(response, "Dieser Schritt ist im aktuellen Zustand nicht möglich")
        self.assertEqual(self.refreshed(self.draw).number, full_number(12))

    def test_no_forged_field_is_taken(self):
        other = customer("forge.point@example.org")
        other_application, other_building = ready_draft(other, egid=90004283, number=60, address={"house_number": "83"})
        applications.submit(other, other_application, today=TODAY)
        other_point = other_building.metering_points.get()
        forged = {
            "building": other_building.pk, "name": "Forged", "metering_point": 1, "id": other_point.pk,
            "state": "completed", "person": other.pk, "application": other_application.pk,
        }

        response = self.client.post(self.point_url(), {**self.point_data(number_digits="14"), **forged})

        self.assertEqual(response.status_code, 302)
        row = self.refreshed(self.draw)
        self.assertEqual((row.number, row.building_id, row.name, row.metering_point_id),
                         (full_number(14), self.building.pk, "", None))
        self.assertEqual(self.refreshed(other_point).number, full_number(60))
        self.assertEqual(self.state(self.application), State.SUBMITTED)

    def test_the_point_of_another_application_or_a_missing_one_is_a_404(self):
        other = customer("foreign.point@example.org")
        other_application, other_building = ready_draft(other, egid=90004284, number=70, address={"house_number": "84"})
        applications.submit(other, other_application, today=TODAY)
        foreign = other_building.metering_points.get()

        for url in (
            self.point_url(self.application, foreign),
            reverse("management:processes_application_point", args=[self.application.pk, 999999]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, self.point_data(number_digits="15")).status_code, 404)
        self.assertEqual(self.refreshed(foreign).number, full_number(70))


class AuthorizationTests(CorrectionBase):
    def test_the_dialogs_need_the_csrf_token(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        with directory(candidate(90004270, house_number="52")):
            building = client.post(self.building_url(), self.building_data(house_number="52"))
        point = client.post(self.point_url(), self.point_data(number_digits="9"))

        self.assertEqual((building.status_code, point.status_code), (403, 403))
        self.assertEqual(self.refreshed(self.building).house_number, "51")
        self.assertEqual(self.refreshed(self.draw).number, full_number(1))

    def test_a_customer_even_the_owner_of_the_application_and_a_visitor_get_nothing(self):
        owner = self.login(self.anna)
        visitor = self.client_class()
        with directory(candidate(90004270, house_number="52")):
            for client, expected in ((owner, 404), (visitor, 302)):
                for url, data in (
                    (self.building_url(), self.building_data(house_number="52")),
                    (self.point_url(), self.point_data(number_digits="9")),
                ):
                    with self.subTest(client=expected, url=url):
                        self.assertEqual(client.get(url).status_code, expected)
                        self.assertEqual(client.post(url, data).status_code, expected)
        self.assertEqual(self.refreshed(self.building).house_number, "51")
        self.assertEqual(self.refreshed(self.draw).number, full_number(1))

    def test_a_staff_account_without_the_second_factor_and_one_that_lost_the_staff_status_get_nothing(self):
        no_factor = staff_user("fix.nofactor@example.org")
        former = staff_user("fix.former@example.org")
        without_factor = self.client_class()
        without_factor.force_login(no_factor)  # a login without the completed second factor
        demoted = self.login(former)
        former.set_privileges(is_staff=False, is_superuser=False)  # the live session still exists
        responses = []
        with directory(candidate(90004270, house_number="52")):
            for client in (without_factor, demoted):
                for url, data in (
                    (self.building_url(), self.building_data(house_number="52")),
                    (self.point_url(), self.point_data(number_digits="9")),
                ):
                    responses.append((client.get(url).status_code, client.post(url, data).status_code))

        self.assertTrue(all(status[0] in (302, 403, 404) and status[1] in (302, 403, 404) for status in responses), responses)
        self.assertEqual(self.refreshed(self.building).house_number, "51")
        self.assertEqual(self.refreshed(self.draw).number, full_number(1))
        self.assertEqual(len(history.entries_for(self.application).filter(
            event__in=[HistoryEvent.APPLICATION_BUILDING_CORRECTED, HistoryEvent.APPLICATION_POINT_CORRECTED]
        )), 0)
