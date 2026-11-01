"""The pages of the locations (D-078, D-086): list, detail and dialogs, the state that is remembered, the returns to the
place a dialog was opened from (also hostile ``next`` values) and the flows of the dialogs without JavaScript. Every
staff account sees and may do everything (D-086); an archived location is read-only. The rules themselves are tested at
service level (``community/tests/test_locations.py``); here only what the views add."""

from datetime import date
from unittest import mock

from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import history, locations, preferences
from leg_platform.community.addressing import AddressSuggestion
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import (
    Direction,
    Location,
    ManagementSettings,
    MeteringPoint,
    TransformerCircuit,
)
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

HOSTILE = (
    "https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", "evil", "/ok\r\nSet-Cookie: a=b",
)


@tag("mariadb")
class LocationPageTests(TestCase):
    databases = {"default", "addresses"}

    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.staff = staff_user("staff.person@example.org")
        cls.administrator = staff_user("administrator.person@example.org", superuser=True)
        cls.circuit = make_circuit("TRA21359", "Ittigen Nord")
        cls.leg = make_leg("LEG Quartier")
        cls.musterweg = make_location(
            "Musterweg", "12", "3063", "Ittigen", transformer_circuit=cls.circuit, remark="Hintereingang"
        )
        cls.point = make_metering_point(1, location=cls.musterweg)
        assign_leg(cls.point, cls.leg)
        cls.ada = create_active_user("ada.person@example.org", first_name="Ada", last_name="Mueller")
        assign_person(cls.point, cls.ada, valid_from=date(2026, 1, 1))
        cls.other = make_location("Zielweg", "3", "3063", "Ittigen")
        cls.fresh = make_location("Neuweg", "1", "3000", "Bern")
        cls.customer = make_location("Kundenweg", "5", "3000", "Bern", needs_confirmation=True)
        cls.old = make_location("Altweg", "9", "3000", "Bern", is_archived=True)  # no history: deletable
        settings_row = ManagementSettings.load()
        settings_row.country_code, settings_row.identifier = "CH", "1A2B3C4D5E6"
        settings_row.save()

    def setUp(self):
        patcher = mock.patch("leg_platform.community.addressing.suggest_correction", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.login(self.staff)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def detail(self, location=None):
        return reverse("management:locations_detail", args=[(location or self.musterweg).pk])

    # List ---------------------------------------------------------------------------------------

    def test_the_list_shows_the_locations_the_names_of_the_persons_and_the_buttons(self):
        url = reverse("management:locations")
        page = self.client.get(url)
        self.assertContains(page, "Musterweg 12")
        self.assertContains(page, "TRA21359")
        self.assertContains(page, "Ada Mueller")
        self.assertContains(page, "Standort anlegen")
        # The button "Drucken" (the printing block, D-082): a desktop only, left of the create button.
        self.assertContains(page, "Drucken")
        # The names of the persons are searched too.
        self.assertContains(self.client.get(url, {"q": "mueller"}), "Musterweg 12")

    def test_search_filters_and_sort_come_from_the_address_and_wrong_values_do_no_harm(self):
        client = self.client
        url = reverse("management:locations")
        page = client.get(url, {"q": "zielweg"})
        self.assertContains(page, "Zielweg 3")
        self.assertNotContains(page, "Musterweg 12")
        self.assertNotContains(client.get(url, {"q": "Hintereingang"}), "Musterweg 12")  # never the remark
        self.assertContains(client.get(url, {"circuit": "TRA21359"}), "Musterweg 12")
        self.assertNotContains(client.get(url, {"circuit": "TRA21359"}), "Zielweg 3")
        self.assertNotContains(client.get(url, {"occupancy": "empty"}), "Musterweg 12")
        for params in (
            {"sort": "x; DROP", "dir": "up", "per": "-1", "page": "zz", "occupancy": "?", "circuit": "<b>", "q": "%" * 300},
            {"sort": "occupancy", "dir": "desc", "per": "all", "page": "99"},
        ):
            self.assertEqual(client.get(url, params).status_code, 200)

    def test_the_problem_button_is_there_only_when_a_problem_exists(self):
        url = reverse("management:locations")
        client = self.client
        self.assertNotContains(client.get(url), "Probleme filtern")
        Location.objects.filter(pk=self.other.pk).update(address_correction_open=True)
        self.assertContains(client.get(url), "Probleme filtern (1)")
        page = client.get(url, {"problems": "1"})
        self.assertContains(page, "Zielweg 3")
        self.assertNotContains(page, "Musterweg 12")

    def test_the_new_mark_is_shown_in_the_list_and_the_detail(self):
        self.assertContains(self.client.get(reverse("management:locations")), 'class="mgmt-new"')
        page = self.client.get(self.detail(self.customer))
        self.assertContains(page, "Neu vom Kunden angelegt, noch nicht bestätigt.")
        self.assertContains(page, "Adresse bestätigen")

    def test_the_expanded_rows_are_remembered_per_account(self):
        client = self.client
        expanded = reverse("management:locations_expanded")
        response = client.post(expanded, {"toggle": self.musterweg.pk, "next": reverse("management:locations")})
        self.assertRedirects(response, reverse("management:locations"), fetch_redirect_response=False)
        self.assertEqual(preferences.get_preference(self.staff, "locations").expanded_locations, [self.musterweg.pk])
        self.assertContains(client.get(reverse("management:locations")), 'aria-expanded="true"')
        client.post(expanded, {"toggle": self.musterweg.pk})
        self.assertEqual(preferences.get_preference(self.staff, "locations").expanded_locations, [])
        # The script posts JSON; unknown ids are forgotten, wrong input is refused.
        ok = client.post(
            expanded, '{"ids": [%d, 99999], "open": true}' % self.musterweg.pk, content_type="application/json"
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(preferences.get_preference(self.staff, "locations").expanded_locations, [self.musterweg.pk])
        for body in (
            '{"ids": "x", "open": true}', '{"ids": [0], "open": true}', '{"ids": [true], "open": true}',
            '{"ids": [1], "open": true', "{}",
        ):
            self.assertEqual(client.post(expanded, body, content_type="application/json").status_code, 400, body)
        self.assertEqual(client.post(expanded, {"toggle": "abc"}).status_code, 400)
        # What one account remembers is not remembered for another.
        self.assertEqual(preferences.get_preference(self.administrator, "locations").expanded_locations, [])

    def test_the_whole_row_opens_and_only_the_link_icon_leads_to_the_detail_page(self):
        # D-110: the address is plain text, the row is the target of the click, the arrow is the keyboard button.
        page = self.client.get(reverse("management:locations")).content.decode()
        detail = self.detail()
        self.assertIn('<span class="mgmt-addr-name">Musterweg 12</span>', page)
        self.assertNotIn(f'class="mgmt-row-link" href="{detail}"', page)
        self.assertIn(f'href="{detail}"', page)  # the link icon at the right stays (table and card)
        self.assertIn("Zur Detailseite des Standorts", page)
        self.assertIn('class="mgmt-site is-expandable"', page)
        self.assertIn('class="mgmt-place is-expandable"', page)
        pk = self.musterweg.pk
        for kind in ("table", "card"):
            self.assertIn(f'aria-controls="points-{kind}-{pk}"', page)
            self.assertIn(f'id="points-{kind}-{pk}"', page)
        self.assertEqual(page.count('data-expand-toggle'), 2)
        # A location without metering points cannot open: no arrow button, no expandable row.
        empty = self.client.get(reverse("management:locations"), {"q": "Zielweg"}).content.decode()
        self.assertIn("Zielweg 3", empty)
        self.assertNotIn("is-expandable", empty)
        self.assertNotIn("data-expand-toggle", empty)
        # The state the server remembers is on the button too.
        self.client.post(reverse("management:locations_expanded"), {"toggle": pk})
        opened = self.client.get(reverse("management:locations")).content.decode()
        self.assertEqual(opened.count('aria-expanded="true"'), 4)  # row, card and the two buttons

    def test_a_hostile_next_value_never_leaves_the_site(self):
        client = self.client
        for value in HOSTILE:
            with self.subTest(next=value):
                response = client.post(
                    reverse("management:locations_expanded"), {"toggle": self.musterweg.pk, "next": value}
                )
                self.assertRedirects(response, reverse("management:locations"), fetch_redirect_response=False)
        # The same for a dialog and for the button of the detail page.
        for value in HOSTILE:
            with self.subTest(next=value, view="remark"):
                response = client.post(
                    reverse("management:locations_remark", args=[self.fresh.pk]), {"remark": "x", "next": value}
                )
                self.assertRedirects(response, self.detail(self.fresh), fetch_redirect_response=False)
            with self.subTest(next=value, view="confirm"):
                response = client.post(reverse("management:locations_confirm", args=[self.customer.pk]), {"next": value})
                self.assertRedirects(response, self.detail(self.customer), fetch_redirect_response=False)

    def test_a_safe_next_value_is_followed(self):
        response = self.client.post(
            reverse("management:locations_remark", args=[self.fresh.pk]),
            {"remark": "x", "next": "/management/locations/?q=a"},
        )
        self.assertRedirects(response, "/management/locations/?q=a", fetch_redirect_response=False)

    # The detail page ------------------------------------------------------------------------------

    def test_a_normal_account_gets_nothing_and_an_unknown_location_is_not_found(self):
        self.assertEqual(self.login(self.plain).get(self.detail()).status_code, 404)
        self.assertEqual(self.client.get(reverse("management:locations_detail", args=[999999])).status_code, 404)

    def test_the_detail_page_offers_the_buttons_and_names_the_persons(self):
        page = self.client.get(self.detail()).content.decode()
        for text in ("Bearbeiten", "Messpunkt anlegen", "Standort archivieren", "Trafokreis bearbeiten", "Ada Mueller"):
            self.assertIn(text, page)
        self.assertNotIn("Standort löschen", page)  # it has metering points
        # Only without a history: the button "delete" instead of "archive".
        fresh = self.client.get(self.detail(self.fresh)).content.decode()
        self.assertIn("Standort löschen", fresh)
        self.assertNotIn("Standort archivieren", fresh)

    def test_an_archived_location_offers_only_the_restore(self):
        # "Altweg 9" is archived and has no history: by the history alone it would be deletable (D-086).
        page = self.client.get(self.detail(self.old)).content.decode()
        self.assertIn("Standort wiederherstellen", page)
        for text in ("Standort löschen", "Standort archivieren", "Messpunkt anlegen"):
            self.assertNotIn(text, page)
        self.assertNotIn(reverse("management:locations_delete", args=[self.old.pk]), page)

    def test_the_responses_of_the_pages_are_not_cached(self):
        self.assertIn("no-store", self.client.get(self.detail())["Cache-Control"])

    # Dialogs without JavaScript ------------------------------------------------------------------

    def test_a_dialog_is_a_page_with_a_form_and_a_way_back(self):
        client = self.client
        page = client.get(reverse("management:locations_new"), {"next": "/management/locations/?q=x"})
        self.assertContains(page, "<form")
        self.assertContains(page, 'href="/management/locations/?q=x"')
        self.assertContains(page, 'name="csrfmiddlewaretoken"')
        # A hostile way back is not used: the cancel link goes to the default (the link to the circuit
        # dialog carries the value as a parameter of this site, where it is checked again when used).
        page = client.get(reverse("management:locations_new"), {"next": "https://evil.example/"})
        self.assertNotContains(page, 'href="https://evil.example')
        self.assertContains(page, f'href="{reverse("management:locations")}"')

    def test_the_post_of_a_dialog_without_the_csrf_token_is_refused(self):
        client = self.login(self.staff, enforce_csrf_checks=True)
        response = client.post(reverse("management:locations_remark", args=[self.fresh.pk]), {"remark": "x"})
        self.assertEqual(response.status_code, 403)
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.remark, "")

    def test_creating_a_location(self):
        client = self.client
        url = reverse("management:locations_new")
        data = {
            "street": "Bachweg", "house_number": "7", "address_addition": "Hinterhaus", "postal_code": "3000",
            "town": "Bern", "transformer_circuit": str(self.circuit.pk), "remark": "intern",
        }
        response = client.post(url, {**data, "next": reverse("management:locations")})
        created = Location.objects.get(street="Bachweg")
        # The page of the new location opens (with the next steps), and its way back is the list (D-095).
        self.assertRedirects(
            response, f"{self.detail(created)}?new=1&next=%2Fmanagement%2Flocations%2F", fetch_redirect_response=False
        )
        self.assertEqual(
            (created.transformer_circuit, created.remark, created.address_addition),
            (self.circuit, "intern", "Hinterhaus"),
        )
        self.assertEqual(history.entries_for(created).get().actor, self.staff)
        page = client.get(self.detail(created), {"new": "1"})
        self.assertContains(page, "Standort angelegt. Weiter: Messpunkte dieses Standorts erfassen.")
        self.assertContains(page, reverse("management:locations_metering_point_assign", args=[created.pk]))
        # The same address again: the hint, with the way to the existing location, and nothing is created.
        again = client.post(url, data)
        self.assertContains(again, "Diese Adresse gibt es schon:")
        self.assertContains(again, reverse("management:locations_detail", args=[created.pk]))
        self.assertEqual(Location.objects.filter(street="Bachweg").count(), 1)
        for bad in ({"postal_code": "30"}, {"street": ""}, {"transformer_circuit": "999999"}):
            self.assertEqual(client.post(url, {**data, "street": "Anderer Weg", **bad}).status_code, 200)
            self.assertFalse(Location.objects.filter(street="Anderer Weg").exists())

    def test_a_new_circuit_is_created_with_the_location_in_one_step(self):
        url = reverse("management:locations_new")
        data = {
            "street": "Bachweg", "house_number": "7", "postal_code": "3000", "town": "Bern",
            "transformer_circuit": "__new__", "new_circuit_number": "tra777", "new_circuit_name": "Neu",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        created = Location.objects.get(street="Bachweg")
        self.assertEqual((created.transformer_circuit.grid_number, created.transformer_circuit.name), ("TRA777", "Neu"))
        # Everything typed is kept when something is wrong, and nothing is created, not even the circuit.
        circuits = TransformerCircuit.objects.count()
        for bad, message in (
            ({"street": "Anderer Weg", "postal_code": "30"}, None),
            ({"street": "Anderer Weg", "new_circuit_number": "555"}, "Geben Sie die Netzbetreiber-Nummer ein: TRA und Ziffern."),
            ({"street": "Anderer Weg", "new_circuit_number": "TRA21359"}, "Dieser Trafokreis existiert bereits."),
            ({"new_circuit_number": "TRA888"}, "Diese Adresse gibt es schon"),  # the address exists: no circuit
        ):
            with self.subTest(bad=bad):
                response = self.client.post(url, {**data, **bad})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'value="Anderer Weg"' if "street" in bad else 'value="Bachweg"')
                if message:
                    self.assertContains(response, message)
        self.assertEqual(TransformerCircuit.objects.count(), circuits)
        self.assertFalse(Location.objects.filter(street="Anderer Weg").exists())
        # A chosen circuit together with a typed number is refused, a number without a choice creates a circuit.
        both = {**data, "street": "Zweiter Weg", "transformer_circuit": str(self.circuit.pk)}
        self.assertContains(self.client.post(url, both), "nicht beides")
        self.assertFalse(Location.objects.filter(street="Zweiter Weg").exists())
        only_number = {**data, "street": "Dritter Weg", "transformer_circuit": "", "new_circuit_number": "TRA999"}
        self.assertEqual(self.client.post(url, only_number).status_code, 302)
        self.assertEqual(Location.objects.get(street="Dritter Weg").transformer_circuit.grid_number, "TRA999")

    def test_a_location_created_for_the_metering_point_form_returns_to_it_with_the_location_chosen(self):
        form = reverse("management:metering_points_new")
        response = self.client.post(
            reverse("management:locations_new"),
            {"street": "Bachweg", "house_number": "7", "postal_code": "3000", "town": "Bern",
             "next": f"{form}?next=%2Fmanagement%2Fmetering-points%2F"},
        )
        created = Location.objects.get(street="Bachweg")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"], f"{form}?next=%2Fmanagement%2Fmetering-points%2F&location={created.pk}"
        )
        page = self.client.get(response["Location"])
        self.assertContains(page, f'<option value="{created.pk}" selected>')

    def test_changing_the_address_and_the_merge_with_its_confirmation_step(self):
        url = reverse("management:locations_address", args=[self.fresh.pk])
        data = {"street": "Neuweg", "house_number": "2", "address_addition": "", "postal_code": "3000", "town": "Bern"}
        client = self.client
        self.assertRedirects(client.post(url, data), self.detail(self.fresh), fetch_redirect_response=False)
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.house_number, "2")
        # The address of another location: the page warns and nothing happens yet.
        merge = {"street": "Zielweg", "house_number": "3", "address_addition": "", "postal_code": "3063", "town": "Ittigen"}
        warning = client.post(url, merge)
        self.assertContains(warning, "Übertragen und speichern")
        self.assertContains(warning, "Zielweg 3, 3063 Ittigen")
        self.assertTrue(Location.objects.filter(pk=self.fresh.pk).exists())
        # The second step is the explicit confirmation, bound to the location that was shown.
        done = client.post(url, {**merge, "confirm_merge": "1", "merge_target": str(self.other.pk)})
        self.assertRedirects(done, self.detail(self.other), fetch_redirect_response=False)
        self.assertFalse(Location.objects.filter(pk=self.fresh.pk).exists())
        self.assertTrue(history.entries_for(self.other).filter(event=HistoryEvent.LOCATIONS_MERGED).exists())

    def test_a_merge_confirmation_cannot_be_forged_and_an_archived_target_is_refused(self):
        url = reverse("management:locations_address", args=[self.fresh.pk])
        merge = {"street": "Zielweg", "house_number": "3", "address_addition": "", "postal_code": "3063", "town": "Ittigen"}
        client = self.client
        # A confirmation for another target than the one that was shown (or none): the page asks again.
        for target in ("", "999999", "abc", str(self.fresh.pk)):
            again = client.post(url, {**merge, "confirm_merge": "1", "merge_target": target})
            self.assertContains(again, "Übertragen und speichern")
            self.assertTrue(Location.objects.filter(pk=self.fresh.pk).exists())
        # The address of an archived location: nothing is moved to a location that is out of sight.
        archived = {"street": "Altweg", "house_number": "9", "address_addition": "", "postal_code": "3000", "town": "Bern"}
        refused = client.post(url, {**archived, "confirm_merge": "1", "merge_target": str(self.old.pk)})
        self.assertContains(refused, "Diese Adresse gibt es schon.")
        self.assertTrue(Location.objects.filter(pk=self.fresh.pk).exists())

    def test_the_remark_is_changed(self):
        url = reverse("management:locations_remark", args=[self.fresh.pk])
        self.client.post(url, {"remark": "  neu  "})
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.remark, "neu")

    def test_the_circuit_is_chosen_in_the_dialog_and_an_unknown_one_is_refused(self):
        url = reverse("management:locations_circuit", args=[self.fresh.pk])
        client = self.client
        page = client.get(url)
        self.assertContains(page, "Kein Trafokreis")
        self.assertContains(page, "+ Neuen Trafokreis anlegen")
        client.post(url, {"transformer_circuit": str(self.circuit.pk)})
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.transformer_circuit, self.circuit)
        client.post(url, {"transformer_circuit": "999999"})
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.transformer_circuit, self.circuit)
        client.post(url, {"transformer_circuit": ""})
        self.fresh.refresh_from_db()
        self.assertIsNone(self.fresh.transformer_circuit)

    def test_a_new_circuit_is_created_and_assigned_by_the_circuit_dialog(self):
        client = self.client
        url = reverse("management:locations_circuit", args=[self.fresh.pk])
        # A number that exists: the message at the number, nothing is created or assigned.
        taken = client.post(url, {"transformer_circuit": "__new__", "new_circuit_number": "tra21359", "new_circuit_name": "Anders"})
        self.assertContains(taken, "Dieser Trafokreis existiert bereits. Wählen Sie ihn in der Liste.")
        self.assertEqual(TransformerCircuit.objects.count(), 1)
        self.fresh.refresh_from_db()
        self.assertIsNone(self.fresh.transformer_circuit)
        self.circuit.refresh_from_db()
        self.assertEqual(self.circuit.name, "Ittigen Nord")  # the existing one is not changed
        # A new number is created and assigned, in one step.
        client.post(url, {"transformer_circuit": "__new__", "new_circuit_number": "tra555", "new_circuit_name": "Neu"})
        self.fresh.refresh_from_db()
        self.assertEqual(self.fresh.transformer_circuit.grid_number, "TRA555")
        self.assertEqual(self.fresh.transformer_circuit.name, "Neu")
        # A wrong number: the dialog again, nothing changes.
        bad = client.post(url, {"transformer_circuit": "__new__", "new_circuit_number": "555"})
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(TransformerCircuit.objects.count(), 2)
        self.assertEqual(history.entries_for(self.fresh).filter(event=HistoryEvent.TRANSFORMER_CIRCUIT_CHANGED).count(), 1)
    def test_changing_a_circuit_says_how_many_locations_it_reaches(self):
        url = reverse("management:circuits_edit", args=[self.circuit.pk])
        client = self.client
        self.assertContains(client.get(url), "Die Änderung betrifft 1 Standort.")
        client.post(url, {"grid_number": "tra21359", "name": "Ittigen Süd"})
        self.circuit.refresh_from_db()
        self.assertEqual(self.circuit.name, "Ittigen Süd")
        other = make_circuit("TRA5")
        taken = client.post(reverse("management:circuits_edit", args=[other.pk]), {"grid_number": "TRA21359", "name": ""})
        self.assertContains(taken, "Diesen Trafokreis gibt es schon.")

    def test_creating_a_metering_point_at_the_location(self):
        client = self.client
        url = reverse("management:locations_metering_point_new", args=[self.fresh.pk])
        # The location is locked (a fact row, no field); direction and number are required, LEG and the rest optional.
        page = client.get(url)
        self.assertContains(page, "Neuweg 1, 3000 Bern")
        self.assertNotContains(page, 'name="location"')
        self.assertContains(page, 'name="leg"')
        self.assertEqual(client.post(url, {"number_digits": "5"}).status_code, 200)  # no direction
        self.assertContains(client.post(url, {"direction": Direction.CONSUMPTION}), "Geben Sie die letzten Zeichen")
        self.assertRedirects(
            client.post(url, {"direction": Direction.FEED_IN, "number_digits": "77", "leg": self.leg.pk}),
            self.detail(self.fresh), fetch_redirect_response=False,
        )
        point = MeteringPoint.objects.get(location=self.fresh)
        self.assertEqual(point.number, "CH1A2B3C4D5E6" + "77".zfill(20))
        self.assertEqual(point.leg_assignments.get().leg, self.leg)
        self.assertFalse(point.person_assignments.exists())
        # The number exists: the page tells, nothing is created.
        again = client.post(url, {"direction": Direction.CONSUMPTION, "number_digits": "77"})
        self.assertContains(again, "Diese Messpunkt-Nummer gibt es schon.")
        self.assertEqual(MeteringPoint.objects.filter(location=self.fresh).count(), 1)

    def test_the_locked_location_comes_from_the_page_and_never_from_the_post(self):
        url = reverse("management:locations_metering_point_new", args=[self.fresh.pk])
        before = MeteringPoint.objects.count()
        for forged in (str(self.other.pk), str(self.old.pk), "999999", "abc", ""):
            with self.subTest(forged=forged):
                response = self.client.post(
                    url, {"direction": Direction.CONSUMPTION, "number_digits": "9", "location": forged}
                )
                self.assertEqual(response.status_code, 302)
                created = MeteringPoint.objects.get(number="CH1A2B3C4D5E6" + "9".zfill(20))
                self.assertEqual(created.location, self.fresh)
                created.delete()
        self.assertEqual(MeteringPoint.objects.count(), before)
        # An archived location is refused before anything is read or written.
        gone = self.client.post(
            reverse("management:locations_metering_point_new", args=[self.old.pk]),
            {"direction": Direction.CONSUMPTION, "number_digits": "8"},
            follow=True,
        )
        self.assertContains(gone, "Dieser Standort ist archiviert.")
        self.assertFalse(MeteringPoint.objects.filter(location=self.old).exists())

    def test_an_existing_metering_point_is_assigned_to_the_location(self):
        loose = make_metering_point(2)  # no location
        elsewhere = make_metering_point(3, location=self.other)
        archived_point = make_metering_point(4, is_archived=True)
        url = reverse("management:locations_metering_point_assign", args=[self.fresh.pk])
        page = self.client.get(url)
        self.assertContains(page, "Ohne Standort")
        self.assertContains(page, "An einem anderen Standort")
        self.assertContains(page, f'<option value="{loose.pk}">')
        self.assertContains(page, f'<option value="{elsewhere.pk}">')
        self.assertNotContains(page, f'<option value="{archived_point.pk}">')  # archived: not offered
        response = self.client.post(url, {"metering_point": str(loose.pk)})
        self.assertRedirects(response, self.detail(self.fresh), fetch_redirect_response=False)
        loose.refresh_from_db()
        self.assertEqual(loose.location, self.fresh)
        entry = history.entries_for(loose).get(event=HistoryEvent.METERING_POINT_LOCATION_CHANGED)
        self.assertEqual(entry.detail, {"old_location_id": None, "new_location_id": self.fresh.pk})
        # A point of another location moves; its new circuit is the one of this location.
        self.client.post(url, {"metering_point": str(elsewhere.pk)})
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.location, self.fresh)

    def test_assigning_refuses_what_is_not_offered(self):
        url = reverse("management:locations_metering_point_assign", args=[self.fresh.pk])
        archived_point = make_metering_point(4, is_archived=True)
        here = make_metering_point(5, location=self.fresh)
        before = list(MeteringPoint.objects.order_by("pk").values_list("pk", "location_id"))
        for forged in (str(archived_point.pk), str(here.pk), "999999", "-1", "abc", "9" * 40, ""):
            with self.subTest(forged=forged[:12]):
                self.assertEqual(self.client.post(url, {"metering_point": forged}).status_code, 200)
        self.assertEqual(list(MeteringPoint.objects.order_by("pk").values_list("pk", "location_id")), before)
        # An archived or unknown location takes none.
        for pk in (self.old.pk, 999999):
            self.client.post(
                reverse("management:locations_metering_point_assign", args=[pk]), {"metering_point": str(self.point.pk)}
            )
        self.point.refresh_from_db()
        self.assertEqual(self.point.location, self.musterweg)

    def test_the_assign_dialog_says_so_when_there_is_nothing_to_assign(self):
        MeteringPoint.objects.exclude(location=self.fresh).update(is_archived=True)
        page = self.client.get(reverse("management:locations_metering_point_assign", args=[self.fresh.pk]))
        self.assertContains(page, "Alle Messpunkte haben bereits einen Standort.")
        self.assertNotContains(page, "Messpunkt zuordnen</button>")

    def test_a_metering_point_needs_the_identifier_from_the_settings_for_a_number(self):
        ManagementSettings.objects.update(identifier="")
        response = self.client.post(
            reverse("management:locations_metering_point_new", args=[self.fresh.pk]),
            {"direction": Direction.CONSUMPTION, "number_digits": "5"},
        )
        self.assertContains(response, "Erfassen Sie zuerst das Land und die Kennung in den Einstellungen.")

    # Buttons of the detail page -------------------------------------------------------------------

    def test_archiving_and_confirming(self):
        client = self.client
        client.post(reverse("management:locations_archive", args=[self.fresh.pk]))
        self.fresh.refresh_from_db()
        self.assertTrue(self.fresh.is_archived)
        self.assertContains(client.get(self.detail(self.fresh)), "Standort wiederherstellen")
        client.post(reverse("management:locations_archive", args=[self.fresh.pk]), {"archive": "0"})
        self.fresh.refresh_from_db()
        self.assertFalse(self.fresh.is_archived)
        client.post(reverse("management:locations_confirm", args=[self.customer.pk]))
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.needs_confirmation)

    def test_the_banner_of_the_address_correction_and_its_two_buttons(self):
        suggestion = AddressSuggestion("Musterweg", "12", "3063", "Ittigen")
        wrong = make_location("Musterwg", "12", "3063", "Ittigen")
        with mock.patch("leg_platform.community.addressing.suggest_correction", return_value=suggestion):
            locations.refresh_suggestion(wrong)
        url = reverse("management:locations_suggestion", args=[wrong.pk])
        client = self.client
        page = client.get(self.detail(wrong))
        self.assertContains(page, "Musterweg 12, 3063 Ittigen")
        self.assertContains(page, "Nicht übernehmen")
        # An unknown action is refused; the correct ones decide.
        self.assertEqual(client.post(url, {"action": "other"}).status_code, 400)
        # The suggestion is the address of another location: refused with a message, address unchanged.
        client.post(url, {"action": "accept"})
        wrong.refresh_from_db()
        self.assertEqual(wrong.street, "Musterwg")
        self.assertTrue(wrong.address_correction_open)
        client.post(url, {"action": "dismiss"})
        wrong.refresh_from_db()
        self.assertFalse(wrong.address_correction_open)
        entry = history.entries_for(wrong).get(event=HistoryEvent.ADDRESS_CORRECTION_DISMISSED)
        self.assertEqual(entry.actor, self.staff)

    def test_deleting_a_location_asks_first_and_only_works_without_a_history(self):
        client = self.client
        url = reverse("management:locations_delete", args=[self.fresh.pk])
        self.assertContains(client.get(url), "Standort löschen")
        self.assertTrue(Location.objects.filter(pk=self.fresh.pk).exists())
        self.assertRedirects(client.post(url), reverse("management:locations"), fetch_redirect_response=False)
        self.assertFalse(Location.objects.filter(pk=self.fresh.pk).exists())
        # A location with metering points cannot be deleted, by neither request.
        self.assertEqual(client.get(reverse("management:locations_delete", args=[self.musterweg.pk])).status_code, 404)
        client.post(reverse("management:locations_delete", args=[self.musterweg.pk]))
        self.assertTrue(Location.objects.filter(pk=self.musterweg.pk).exists())

    def test_an_archived_location_is_never_deleted_not_even_without_a_history(self):
        # "Altweg 9" is archived and has no history, so by the history alone it would be deletable (D-086).
        url = reverse("management:locations_delete", args=[self.old.pk])
        for method in (self.client.get, self.client.post):
            response = method(url, follow=True)
            self.assertEqual(response.redirect_chain, [(self.detail(self.old), 302)])
            self.assertContains(response, "Dieser Standort ist archiviert.")
            self.assertTrue(Location.objects.filter(pk=self.old.pk).exists())
