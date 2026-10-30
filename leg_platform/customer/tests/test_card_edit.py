"""The edit mode of a building card on the page Buildings (``?edit=<card id>``, ``customer.card_edit``), the direct edit pages
of a submitted application (``customer.application_edit``) and the new fields of the overview. One "Ändern" button per card,
in-place inputs, no pencils, the server decides (owner, state, CSRF). The negative authorization tests come first in
meaning: another account's application, point, building and card ids, a wrong state, a missing CSRF token."""

import re
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase, tag
from django.urls import reverse

from leg_platform.accounts.tests.helpers import VerifiedClient
from leg_platform.addresses.models import TransformerStation
from leg_platform.community import application_staff, applications, contracts, own_iban
from leg_platform.community.history import HistoryEvent, entries_for
from leg_platform.community.models import Application, ApplicationBuilding, ApplicationMeteringPoint, Direction, MeteringPoint
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)
from leg_platform.customer import overview
from leg_platform.customer.tests.helpers import (
    BUILDINGS,
    GOOD_IBAN,
    OTHER_IBAN,
    TODAY,
    candidate,
    configure_numbers,
    customer,
    directory,
    draft_with_points,
    full_number,
    login,
    publish_contract,
)

LONG_AGO = date.today() - timedelta(days=400)


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def typed(last):
    """The typed end of a metering point number: all 20 characters (a shorter entry is an error, D-128)."""
    return str(last).zfill(20)


def edit_ids(html):
    return re.findall(r'href="/participation/buildings/\?edit=([^"]+)"', html)


def post_data(state, **overrides):
    """The values of every form of an edit state as a browser would send them, with the overrides (prefixed names)."""
    data = {}
    for form in state.forms:
        for name, field in form.fields.items():
            value = form[name].value()
            if value is None or value is False:
                continue
            data[form.add_prefix(name)] = value
    data.update(overrides)
    return data


def stored_card(client):
    return client.get(BUILDINGS).context["overview"].cards[0]


@tag("mariadb")
class StoredCardEditTests(TestCase):
    """A card of a participating building: the names and the figures of the feed-in point change in place."""

    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("card.mine@example.org")
        self.other = customer("card.other@example.org")
        self.location = make_location("Musterstrasse", "1", "3063", "Ittigen", egid=90001234)
        kwargs = {"registered_with_operator_on": LONG_AGO, "confirmed_on": LONG_AGO, "start_date": LONG_AGO}
        self.draw = make_metering_point(1, Direction.CONSUMPTION, self.location, name="Haus", **kwargs)
        self.feed_in = make_metering_point(2, Direction.FEED_IN, self.location, name="Dach", **kwargs)
        for point in (self.draw, self.feed_in):
            assign_person(point, self.mine, valid_from=LONG_AGO)
        self.foreign = make_metering_point(3, Direction.CONSUMPTION, self.location, name="Fremd", **kwargs)
        assign_person(self.foreign, self.other, valid_from=LONG_AGO)
        self.client = login(self.mine)

    def edit_url(self, client=None):
        html = (client or self.client).get(BUILDINGS).content.decode()
        ids = edit_ids(html)
        self.assertEqual(len(ids), 1, "one Ändern button for the card")
        return f"{BUILDINGS}?edit={ids[0]}"

    def state(self, client=None):
        response = (client or self.client).get(self.edit_url(client))
        self.assertEqual(response.status_code, 200)
        return response.context["overview"].cards[0].editing

    # --- the read page -----------------------------------------------------------------------------------------

    def test_the_card_has_one_change_button_and_no_pencils(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertEqual(html.count(">Ändern<"), 1)
        self.assertNotIn("#i-pencil", html)
        self.assertNotIn('class="icon-btn"', html)
        self.assertNotIn("<form", html.split('class="building-card"', 1)[1].split("</section>", 1)[0])

    def test_the_status_stands_top_right_and_a_completed_card_has_no_progress(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertRegex(html, r'class="building-status"><span class="dotword"><span class="dot tone-ok" aria-hidden="true"></span>Teilnehmend')
        self.assertNotIn("<progress", html)

    def test_the_card_shows_leg_name_transformer_and_figures(self):
        leg = make_leg("LEG Beispiel")
        assign_leg(self.feed_in, leg, valid_from=LONG_AGO)
        TransformerStation.objects.using("addresses").create(
            egid=90001234, postal_code="3063", name="TRA1234", match="exact", scanned_at="2026-10-01T00:00:00Z"
        )
        MeteringPoint.objects.filter(pk=self.feed_in.pk).update(pv_power_kwp=Decimal("8.4"))
        html = self.client.get(BUILDINGS).content.decode()
        self.assertIn("LEG Beispiel", html)
        self.assertIn("TRA1234", html)
        self.assertIn("8.4", html)
        self.assertIn("Trafokreis", html)
        self.assertIn("LEG-Name", html)

    def test_without_a_leg_or_transformer_the_rows_say_so(self):
        html = self.client.get(BUILDINGS).content.decode()
        self.assertIn("Noch nicht zugewiesen", html)
        card = stored_card(self.client)
        self.assertEqual((card.leg_name, card.transformer), ("", ""))

    def test_another_customers_leg_name_and_figures_are_never_shown(self):
        MeteringPoint.objects.filter(pk=self.foreign.pk).update(pv_power_kwp=Decimal("77.7"), name="Fremd")
        assign_leg(self.foreign, make_leg("LEG Fremd"), valid_from=LONG_AGO)
        html = self.client.get(BUILDINGS).content.decode()
        self.assertNotIn("LEG Fremd", html)
        self.assertNotIn("77.7", html)

    # --- the edit mode -----------------------------------------------------------------------------------------

    def test_the_edit_mode_is_a_form_with_inputs_in_place_and_locked_values(self):
        response = self.client.get(self.edit_url())
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn('class="building-card is-editing"', html)
        self.assertIn("Speichern", html)
        self.assertIn("Abbrechen", html)
        # The EGID and the numbers are plain text (D-128: no lock icon, no hint), never inputs.
        self.assertNotRegex(html, r'<input[^>]*name="[^"]*(egid|number)')
        self.assertNotIn("nicht änderbar", html)
        self.assertNotIn("i-lock", html)
        self.assertRegex(html, r'<dt>EGID</dt><dd class="mono">90001234</dd>')
        # The other cards' / the own Ändern button is disabled while one card is edited.
        self.assertNotIn(f'href="{BUILDINGS}?edit=', html)

    def test_a_save_changes_the_names_and_the_figures_and_writes_the_history(self):
        state = self.state()
        feed_in_form = next(form for form in state.forms if "pv_power_kwp" in form.fields)
        draw_form = next(form for form in state.forms if "pv_power_kwp" not in form.fields and "name" in form.fields and form.prefix != "b")
        response = self.client.post(self.edit_url(), post_data(
            state, **{
                "b-name": "Mein Haus", draw_form.add_prefix("name"): "Wohnen", feed_in_form.add_prefix("name"): "Solar",
                feed_in_form.add_prefix("pv_power_kwp"): "9.8", feed_in_form.add_prefix("battery_kwh"): "5",
            },
        ))
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.feed_in.refresh_from_db()
        self.draw.refresh_from_db()
        self.assertEqual((self.feed_in.name, self.feed_in.pv_power_kwp, self.feed_in.battery_kwh), ("Solar", Decimal("9.8"), Decimal("5")))
        self.assertEqual(self.draw.name, "Wohnen")
        events = [e.event for e in entries_for(self.feed_in)]
        self.assertIn(HistoryEvent.METERING_POINT_DATA_CHANGED, events)
        self.assertIn("Gespeichert.", messages_of(self.client.get(BUILDINGS)) + messages_of(response))

    def test_an_unchanged_save_changes_nothing(self):
        state = self.state()
        before = len(list(entries_for(self.feed_in)))
        response = self.client.post(self.edit_url(), post_data(state))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(list(entries_for(self.feed_in))), before)

    def test_an_invalid_figure_shows_every_error_and_saves_nothing(self):
        state = self.state()
        feed_in_form = next(form for form in state.forms if "pv_power_kwp" in form.fields)
        response = self.client.post(self.edit_url(), post_data(
            state, **{"b-name": "Neu", feed_in_form.add_prefix("name"): "Neu", feed_in_form.add_prefix("pv_power_kwp"): "-4"},
        ))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Bitte prüfen Sie Ihre Angaben", html)
        self.assertIn("autofocus", html)
        self.feed_in.refresh_from_db()
        self.assertEqual((self.feed_in.name, self.feed_in.pv_power_kwp), ("Dach", None))
        self.assertFalse(CustomerBuildingNamed(self.mine, self.location, "Neu"))

    def test_fields_the_card_does_not_offer_are_ignored(self):
        state = self.state()
        response = self.client.post(self.edit_url(), post_data(
            state, **{"b-egid": "1", "r0-number": full_number(99), "r1-number": full_number(98), "b-street": "Hacker"},
        ))
        self.assertEqual(response.status_code, 302)
        self.draw.refresh_from_db()
        self.location.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(1))
        self.assertEqual((self.location.egid, self.location.street), (90001234, "Musterstrasse"))

    # --- negative authorization --------------------------------------------------------------------------------

    def test_an_unknown_or_malformed_edit_id_shows_the_plain_page(self):
        for value in ("p999999", "a1", "", "x" * 300, "p1;drop", "../p1", "<b>"):
            with self.subTest(value=value):
                response = self.client.get(BUILDINGS, {"edit": value})
                self.assertIn(response.status_code, (200, 302))
                if response.status_code == 200:
                    self.assertNotIn("is-editing", response.content.decode())

    def test_another_accounts_card_id_never_opens_or_saves(self):
        # The other account has a building of its own (its card id is another one than this account's).
        elsewhere = make_location("Nebenweg", "5", "3063", "Ittigen", egid=90001299)
        kwargs = {"registered_with_operator_on": LONG_AGO, "confirmed_on": LONG_AGO, "start_date": LONG_AGO}
        stranger_point = make_metering_point(9, Direction.CONSUMPTION, elsewhere, name="Fremdhaus", **kwargs)
        assign_person(stranger_point, self.other, valid_from=LONG_AGO)
        other_client = login(self.other)
        mine = edit_ids(self.client.get(BUILDINGS).content.decode())
        other_id = next(i for i in edit_ids(other_client.get(BUILDINGS).content.decode()) if i not in mine)
        response = self.client.get(BUILDINGS, {"edit": other_id})
        self.assertNotIn("is-editing", response.content.decode() if response.status_code == 200 else "")
        self.assertNotIn("Fremdhaus", response.content.decode() if response.status_code == 200 else "")
        self.client.post(
            f"{BUILDINGS}?edit={other_id}", {"b-name": "Gekapert", "r0-name": "Gekapert", "r1-name": "Gekapert"}
        )
        stranger_point.refresh_from_db()
        self.assertEqual(stranger_point.name, "Fremdhaus")
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.name, "Haus")

    def test_posting_the_foreign_point_id_through_the_own_card_changes_nothing(self):
        state = self.state()
        self.client.post(self.edit_url(), post_data(state, **{"r0-point_id": str(self.foreign.pk), "point": str(self.foreign.pk)}))
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.name, "Fremd")

    def test_a_post_without_csrf_token_is_refused(self):
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)
        ids = edit_ids(guarded.get(BUILDINGS).content.decode())
        response = guarded.post(f"{BUILDINGS}?edit={ids[0]}", {"b-name": "Ohne Token"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CustomerBuildingNamed(self.mine, self.location, "Ohne Token"))

    def test_a_post_without_an_edit_id_saves_nothing(self):
        response = self.client.post(BUILDINGS, {"b-name": "Irgendwas"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CustomerBuildingNamed(self.mine, self.location, "Irgendwas"))

    def test_an_anonymous_visitor_neither_sees_nor_saves_the_edit_mode(self):
        from django.test import Client

        anonymous = Client()
        for response in (anonymous.get(BUILDINGS, {"edit": "p1"}), anonymous.post(f"{BUILDINGS}?edit=p1", {"b-name": "x"})):
            self.assertIn(response.status_code, (302, 403, 404))
            self.assertNotIn("is-editing", response.content.decode())

    def test_a_former_holder_cannot_change_the_point(self):
        from leg_platform.community.models import PersonAssignment

        PersonAssignment.objects.filter(metering_point=self.feed_in, person=self.mine).update(
            valid_to=date.today() - timedelta(days=1)
        )
        state_page = self.client.get(BUILDINGS)
        self.assertNotIn("pv_power_kwp", state_page.content.decode())
        self.feed_in.refresh_from_db()
        self.assertEqual(self.feed_in.name, "Dach")

    def test_only_one_card_is_edited_and_the_other_buttons_are_disabled(self):
        second = make_location("Nebenweg", "5", "3063", "Ittigen", egid=90001999)
        extra = make_metering_point(
            9, Direction.CONSUMPTION, second, registered_with_operator_on=LONG_AGO, confirmed_on=LONG_AGO,
            start_date=LONG_AGO,
        )
        assign_person(extra, self.mine, valid_from=LONG_AGO)
        ids = edit_ids(self.client.get(BUILDINGS).content.decode())
        self.assertEqual(len(ids), 2)
        html = self.client.get(BUILDINGS, {"edit": ids[0]}).content.decode()
        self.assertEqual(html.count('class="building-card is-editing is-fold"'), 1)
        self.assertRegex(html, r'<button type="button" class="btn btn-secondary-action" disabled>Ändern</button>')


def CustomerBuildingNamed(person, location, name):
    from leg_platform.community.models import CustomerBuilding

    return CustomerBuilding.objects.filter(person=person, location=location, name=name).exists()


# ---------------------------------------------------------------------------------------------------------------


def submitted(person, *, feed_in=True, egid=90004269, number=10):
    """A SUBMITTED application with a draw and (optionally) a feed-in."""
    configure_numbers()
    if contracts.current_version() is None:
        publish_contract()
    application, building = draft_with_points(person, feed_in=feed_in, egid=egid, number=number)
    if feed_in:
        applications.save_bank_connection(person, application, GOOD_IBAN)
    applications.accept_contract_step(person, application, contracts.current_version().pk, agrees_contract=True)
    applications.set_start(person, application, asap=True, today=TODAY)
    applications.submit(person, application, today=TODAY)
    application.refresh_from_db()
    return application, building


@tag("mariadb")
class SubmittedCardEditTests(TestCase):
    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("sub.mine@example.org")
        self.other = customer("sub.other@example.org")
        self.application, self.building = submitted(self.mine)
        self.client = login(self.mine)

    def edit_url(self):
        ids = edit_ids(self.client.get(BUILDINGS).content.decode())
        self.assertEqual(len(ids), 1)
        return f"{BUILDINGS}?edit={ids[0]}"

    def test_the_card_offers_address_start_and_points_while_the_application_is_submitted(self):
        response = self.client.get(self.edit_url())
        html = response.content.decode()
        for name in ("b-street", "b-house_number", "b-postal_code", "b-town", "b-name", "s-start", "s-desired_date"):
            self.assertIn(f'name="{name}"', html)
        self.assertRegex(html, r'name="r\d+-pv_power_kwp"')
        card = response.context["overview"].cards[0]
        self.assertTrue(card.can_edit)
        self.assertTrue(card.edit_application.pk == self.application.pk)

    def test_the_overview_gives_edit_urls_and_figures_only_while_submitted(self):
        data = overview.build(self.mine)
        card = data.cards[0]
        self.assertEqual(set(card.edit), {"address", "start", "bank"})
        self.assertTrue(card.start_text)
        self.assertTrue(card.iban_masked == "" or "•" in card.iban_masked)
        self.assertTrue(all(row.edit_url for row in card.rows))
        self.assertIsNotNone(card.feed_in)
        self.assertIn("pv_power_kwp", card.feed_in.figures)
        application_staff.start_review(self.application, staff_user("sub.staff@example.org"))
        card = overview.build(self.mine).cards[0]
        self.assertEqual(card.edit, {})
        self.assertFalse(any(row.edit_url for row in card.rows))
        self.assertTrue(card.review_locked)

    def test_a_save_changes_address_start_and_point_names(self):
        response = self.client.get(self.edit_url())
        state = response.context["overview"].cards[0].editing
        later = (applications.earliest_start_date() + timedelta(days=12)).isoformat()
        names = {form.add_prefix("name"): "Neu" for form in state.forms if form.prefix and form.prefix.startswith("r")}
        with directory(candidate(90007777, street="Neuweg", house_number="3", postal_code="3000", town="Bern")):
            response = self.client.post(self.edit_url(), post_data(
                state, **{
                    "b-street": "Neuweg", "b-house_number": "3", "b-postal_code": "3000", "b-town": "Bern",
                    "s-start": "date", "s-desired_date": later, **names,
                },
            ))
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        row = ApplicationBuilding.objects.get(pk=self.building.pk)
        self.assertEqual(row.egid, 90007777)
        self.application.refresh_from_db()
        self.assertEqual(self.application.state, Application.State.SUBMITTED)
        self.assertEqual(self.application.desired_start_date.isoformat(), later)
        self.assertTrue(ApplicationMeteringPoint.objects.filter(building=row, name="Neu").exists())

    def test_the_card_of_an_application_in_review_cannot_be_edited(self):
        application_staff.start_review(self.application, staff_user("sub.staff2@example.org"))
        self.assertEqual(edit_ids(self.client.get(BUILDINGS).content.decode()), [])
        response = self.client.get(BUILDINGS, {"edit": "a" + str(self.application.pk)})
        self.assertNotIn("is-editing", response.content.decode())

    def test_a_save_after_the_review_started_is_refused_and_changes_nothing(self):
        url = self.edit_url()
        state = self.client.get(url).context["overview"].cards[0].editing
        application_staff.start_review(self.application, staff_user("sub.staff3@example.org"))
        before = ApplicationBuilding.objects.get(pk=self.building.pk).egid
        response = self.client.post(url, post_data(state, **{"b-street": "Neuweg", "b-name": "Zu spaet", "s-start": "asap"}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ApplicationBuilding.objects.get(pk=self.building.pk).egid, before)
        self.assertFalse(ApplicationBuilding.objects.filter(name="Zu spaet").exists())

    def test_a_race_between_the_page_and_the_service_is_refused_by_the_service(self):
        # The page still offers the edit (forced), the LEG has started the review: the services refuse under their lock.
        from unittest.mock import patch

        url = self.edit_url()
        state = self.client.get(url).context["overview"].cards[0].editing
        application_staff.start_review(self.application, staff_user("sub.staff4@example.org"))
        later = (applications.earliest_start_date() + timedelta(days=5)).isoformat()
        with patch.object(applications, "can_edit_submitted", return_value=True):
            response = self.client.post(url, post_data(state, **{"s-start": "date", "s-desired_date": later}))
        self.assertIn(response.status_code, (200, 302))
        self.application.refresh_from_db()
        self.assertEqual(self.application.start_choice, Application.StartChoice.ASAP)

    def test_the_card_of_another_accounts_application_is_not_offered(self):
        stranger = customer("sub.stranger@example.org")
        other_application, _building = submitted(stranger, egid=90004270, number=30)
        ids = edit_ids(self.client.get(BUILDINGS).content.decode())
        self.assertEqual(len(ids), 1)
        other_id = f"a{other_application.pk}"
        self.assertNotEqual(ids[0], other_id)
        self.client.post(f"{BUILDINGS}?edit={other_id}", {"s-start": "asap", "b-name": "Gekapert"})
        self.assertFalse(ApplicationBuilding.objects.filter(application=other_application, name="Gekapert").exists())


# ---------------------------------------------------------------------------------------------------------------


@tag("mariadb")
class DirectEditPagesTests(TestCase):
    """``application/<pk>/edit/...``: the four pages of a submitted application."""

    databases = {"default", "addresses"}

    def setUp(self):
        self.mine = customer("direct.mine@example.org")
        self.other = customer("direct.other@example.org")
        self.application, self.building = submitted(self.mine)
        self.draw = ApplicationMeteringPoint.objects.get(building=self.building, direction=Direction.CONSUMPTION)
        self.feed_in = ApplicationMeteringPoint.objects.get(building=self.building, direction=Direction.FEED_IN)
        self.client = login(self.mine)

    def urls(self, application=None, building=None, point=None):
        application = application or self.application
        building = building or self.building
        point = point or self.draw
        return {
            "start": reverse("customer:application_edit_start", args=[application.pk]),
            "bank": reverse("customer:application_edit_bank", args=[application.pk]),
            "point": reverse("customer:application_edit_point", args=[application.pk, point.pk]),
            "address": reverse("customer:application_edit_address", args=[application.pk, building.pk]),
        }

    # --- own, submitted ----------------------------------------------------------------------------------------

    def test_the_owner_sees_the_four_pages_while_submitted(self):
        for name, url in self.urls().items():
            with self.subTest(name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn("csrfmiddlewaretoken", response.content.decode())

    def test_the_start_page_saves_the_start(self):
        later = (applications.earliest_start_date() + timedelta(days=9)).isoformat()
        response = self.client.post(self.urls()["start"], {"start": "date", "desired_date": later})
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.application.refresh_from_db()
        self.assertEqual(self.application.desired_start_date.isoformat(), later)

    def test_a_start_before_the_earliest_day_is_shown_again_with_the_error(self):
        early = (applications.earliest_start_date() - timedelta(days=3)).isoformat()
        response = self.client.post(self.urls()["start"], {"start": "date", "desired_date": early})
        self.assertEqual(response.status_code, 200)
        self.application.refresh_from_db()
        self.assertEqual(self.application.start_choice, Application.StartChoice.ASAP)

    def test_the_bank_page_saves_the_iban_and_refuses_a_bad_one(self):
        url = self.urls()["bank"]
        self.assertEqual(self.client.post(url, {"iban": "CH00 0000"}).status_code, 200)
        response = self.client.post(url, {"iban": OTHER_IBAN})
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.assertEqual(own_iban.own_iban(self.mine), OTHER_IBAN)

    def test_the_point_page_saves_number_and_name(self):
        url = self.urls()["point"]
        response = self.client.post(url, {"draw_number": typed(20), "draw_name": "Neu"})
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.draw.refresh_from_db()
        self.assertEqual((self.draw.number, self.draw.name), (full_number(20), "Neu"))

    def test_the_point_page_refuses_a_number_of_the_same_application(self):
        response = self.client.post(self.urls()["point"], {"draw_number": typed(11), "draw_name": "X"})
        self.assertEqual(response.status_code, 200)
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))

    def test_a_too_short_number_is_an_error_and_never_padded(self):
        response = self.client.post(self.urls()["point"], {"draw_number": "20", "draw_name": "Neu"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Bitte geben Sie alle 20 Zeichen ein.", response.content.decode())
        self.draw.refresh_from_db()
        self.assertEqual((self.draw.number, self.draw.name), (full_number(10), "House"))

    def test_the_start_page_has_no_prefilled_date(self):
        html = self.client.get(self.urls()["start"]).content.decode()
        self.assertNotRegex(html, r'type="date"[^>]*value="\d')

    def test_the_feed_in_page_saves_the_figures(self):
        url = self.urls(point=self.feed_in)["point"]
        response = self.client.post(url, {"feed_in_number": typed(11), "feed_in_name": "Dach", "pv_power_kwp": "7.5"})
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.feed_in.refresh_from_db()
        self.assertEqual(self.feed_in.pv_power_kwp, Decimal("7.5"))

    def test_the_address_page_saves_a_new_address(self):
        url = self.urls()["address"]
        with directory(candidate(90008888, street="Neuweg", house_number="4", postal_code="3000", town="Bern")):
            response = self.client.post(url, {
                "name": "", "street": "Neuweg", "house_number": "4", "postal_code": "3000", "town": "Bern",
            })
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.assertEqual(ApplicationBuilding.objects.get(pk=self.building.pk).egid, 90008888)

    # --- negative: another account ----------------------------------------------------------------------------

    def test_another_account_gets_404_for_every_page_and_changes_nothing(self):
        client = login(self.other)
        for name, url in self.urls().items():
            with self.subTest(name):
                self.assertEqual(client.get(url).status_code, 404)
                self.assertEqual(client.post(url, {"iban": OTHER_IBAN, "start": "asap", "draw_number": "77", "draw_name": "X"}).status_code, 404)
        self.draw.refresh_from_db()
        self.assertEqual((self.draw.number, self.draw.name), (full_number(10), "House"))
        self.assertFalse(own_iban.has_iban(self.other))

    def test_an_anonymous_visitor_gets_nothing(self):
        from django.test import Client

        for name, url in self.urls().items():
            with self.subTest(name):
                self.assertIn(Client().get(url).status_code, (302, 403, 404))
                self.assertIn(Client().post(url, {"start": "asap"}).status_code, (302, 403, 404))

    def test_a_point_or_building_of_another_application_is_404_even_for_the_owner(self):
        second = customer("direct.second@example.org")
        foreign_application, foreign_building = submitted(second, egid=90004271, number=40)
        foreign_point = ApplicationMeteringPoint.objects.filter(building=foreign_building).first()
        # The owner's application id with the foreign point / building id.
        self.assertEqual(
            self.client.get(reverse("customer:application_edit_point", args=[self.application.pk, foreign_point.pk])).status_code, 404
        )
        self.assertEqual(
            self.client.get(reverse("customer:application_edit_address", args=[self.application.pk, foreign_building.pk])).status_code, 404
        )
        response = self.client.post(
            reverse("customer:application_edit_point", args=[self.application.pk, foreign_point.pk]),
            {"draw_number": typed(88), "draw_name": "X"},
        )
        self.assertEqual(response.status_code, 404)
        foreign_point.refresh_from_db()
        self.assertNotEqual(foreign_point.number, full_number(88))

    def test_a_missing_object_is_404(self):
        self.assertEqual(self.client.get(reverse("customer:application_edit_point", args=[self.application.pk, 99999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("customer:application_edit_start", args=[99999999])).status_code, 404)

    # --- negative: wrong state --------------------------------------------------------------------------------

    def test_in_review_every_page_leaves_with_a_message_and_a_post_changes_nothing(self):
        application_staff.start_review(self.application, staff_user("direct.staff@example.org"))
        for name, url in self.urls().items():
            with self.subTest(name):
                response = self.client.get(url)
                self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
                self.assertTrue(messages_of(response))
                response = self.client.post(url, {"iban": OTHER_IBAN, "start": "asap", "draw_number": "77", "draw_name": "X", "street": "Neuweg"})
                self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))
        self.assertFalse(own_iban.own_iban(self.mine) == OTHER_IBAN)

    def test_a_draft_cannot_be_edited_here(self):
        draft_owner = customer("direct.draft@example.org")
        draft, building = draft_with_points(draft_owner, feed_in=True, egid=90004272, number=60)
        client = login(draft_owner)
        for name, url in self.urls(draft, building).items():
            with self.subTest(name):
                self.assertRedirects(client.get(url), BUILDINGS, fetch_redirect_response=False)

    def test_a_fixed_building_keeps_its_address(self):
        ApplicationBuilding.objects.filter(pk=self.building.pk).update(preselected=True)
        self.assertRedirects(self.client.get(self.urls()["address"]), BUILDINGS, fetch_redirect_response=False)

    def test_the_bank_page_needs_a_feed_in(self):
        person = customer("direct.nofeed@example.org")
        application, _building = submitted(person, feed_in=False, egid=90004273, number=70)
        response = login(person).get(reverse("customer:application_edit_bank", args=[application.pk]))
        self.assertRedirects(response, BUILDINGS, fetch_redirect_response=False)

    # --- CSRF -------------------------------------------------------------------------------------------------

    def test_a_post_without_csrf_token_is_refused_on_every_page(self):
        guarded = VerifiedClient(enforce_csrf_checks=True)
        guarded.force_login(self.mine)
        for name, url in self.urls().items():
            with self.subTest(name):
                self.assertEqual(guarded.post(url, {"iban": OTHER_IBAN, "start": "asap"}).status_code, 403)
        self.assertFalse(own_iban.own_iban(self.mine) == OTHER_IBAN)

    # --- the request budget and the queue position -------------------------------------------------------------

    def test_every_post_counts_against_the_budget_and_changes_nothing_when_exhausted(self):
        from unittest.mock import patch

        from leg_platform.customer import limits

        values = {
            "start": {"start": "asap"}, "bank": {"iban": OTHER_IBAN},
            "point": {"draw_number": typed(20), "draw_name": "Neu"},
            "address": {"street": "Neuweg", "house_number": "1", "postal_code": "3000", "town": "Bern"},
        }
        with patch.object(limits, "allow_wizard", return_value=False):
            for name, url in self.urls().items():
                with self.subTest(name):
                    self.assertEqual(self.client.post(url, values[name]).status_code, 429)
        self.draw.refresh_from_db()
        self.assertEqual(self.draw.number, full_number(10))
        self.assertFalse(own_iban.own_iban(self.mine) == OTHER_IBAN)

    def test_a_changed_number_keeps_the_queue_position(self):
        before = Application.objects.get(pk=self.application.pk).submitted_at
        self.client.post(self.urls()["point"], {"draw_number": typed(20), "draw_name": "Neu"})
        self.assertEqual(Application.objects.get(pk=self.application.pk).submitted_at, before)
