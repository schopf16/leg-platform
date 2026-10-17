"""The pages of the LEGs (D-080): the list (columns, triangles, filters, sort), the detail page in each state (banner,
cards, the table with its last column, the rights that shape it), the dialogs (create, quota, master data) with their
refusals, archive, restore and delete, the returns to the place a dialog was opened from and the flows without
JavaScript. The rules themselves are tested at service level (``community/tests/test_leg_rules.py``); here only what
the views add. The negative authorization of every URL for anonymous, non-staff, rights-less and second-factor-less
accounts is in ``test_access.py`` (the route walk)."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone

from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.community import history
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import Direction, Leg
from leg_platform.community.tests.helpers import (
    assign_leg,
    assign_person,
    make_circuit,
    make_leg,
    make_location,
    make_metering_point,
    staff_user,
)

TODAY = timezone.localdate()
EARLIER = TODAY - timedelta(days=30)
LIST = reverse("management:legs")
HOSTILE = ("https://evil.example/x", "//evil.example/x", "/\\evil.example", "javascript:alert(1)")


@tag("mariadb")
class LegPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plain = create_active_user("plain.person@example.org")
        cls.none = staff_user("staff.none@example.org")
        cls.viewer = staff_user("staff.view@example.org", "view_leg")
        cls.creator = staff_user("staff.add@example.org", "view_leg", "add_leg")
        cls.changer = staff_user("staff.change@example.org", "view_leg", "change_leg")
        cls.deleter = staff_user("staff.delete@example.org", "view_leg", "delete_leg")
        cls.bare = staff_user("staff.bare@example.org", "view_leg", "view_meteringpoint")
        cls.full = staff_user("staff.all@example.org")
        cls.full.user_permissions.set(Permission.objects.filter(content_type__app_label="community"))

        cls.circuit_a = make_circuit("TRA21359")
        cls.circuit_b = make_circuit("TRA21361")
        cls.place_a = make_location("Musterweg", "12", transformer_circuit=cls.circuit_a)
        cls.place_b = make_location("Lindenweg", "7", transformer_circuit=cls.circuit_b)
        cls.anna = create_active_user("anna.person@example.org", first_name="Anna", last_name="Muster")

        # Musterquartier: two circuits, 3 : 1, a good quota that was read today (no problem).
        cls.muster = make_leg(
            "LEG Musterquartier", production_quota_percent=Decimal("37.6"), quota_checked_on=TODAY,
            remark="Pilot Ittigen Nord",
        )
        points = {}
        for index, direction, place in (
            (1, Direction.CONSUMPTION, cls.place_a), (2, Direction.FEED_IN, cls.place_a),
            (3, Direction.CONSUMPTION, cls.place_a), (4, Direction.CONSUMPTION, cls.place_b),
        ):
            points[index] = make_metering_point(index, direction, place)
            assign_leg(points[index], cls.muster, valid_from=EARLIER)
        assign_person(points[1], cls.anna, EARLIER)
        cls.point_one = points[1]
        # Bahnhof: one circuit, consumers only and a low quota: two problems.
        cls.bahnhof = make_leg("LEG Bahnhof", production_quota_percent=Decimal("8.2"), quota_checked_on=TODAY)
        for index in (5, 6):
            assign_leg(make_metering_point(index, Direction.CONSUMPTION, cls.place_b), cls.bahnhof, valid_from=EARLIER)
        cls.neuland = make_leg("LEG Neuland")  # nothing yet: deletable
        cls.old = make_leg("LEG Alt", is_archived=True)

    def login(self, user, **kwargs):
        client = VerifiedClient(**kwargs)
        client.force_login(user)
        return client

    def detail(self, leg):
        return reverse("management:legs_detail", args=[leg.pk])

    def url(self, name, leg):
        return reverse(f"management:legs_{name}", args=[leg.pk])

    # The list -----------------------------------------------------------------------------------

    def test_the_list_shows_the_columns_the_triangles_and_the_discount_tiers(self):
        page = self.login(self.viewer).get(LIST)
        for text in (
            "LEG Musterquartier", "37.6 %", "3 : 1", "Basis-Rabatt", "Preisoptimiert", "8.2 %", "2 : 0",
            "Produktionsquote", "Teilnehmer", "Rabattstufe", "Messpunkte", "Probleme filtern (1)",
            "Problem: Produktionsquote unter 10 %", "Problem: kein Produzent in der LEG",
            "Verhältnis von Konsumenten zu Produzenten.", "Mindestens 5 % sind gesetzlich nötig.",
            "Die BKW bestimmt den tatsächlichen Rabatt.",
        ):
            self.assertContains(page, text)
        # A LEG without metering points: "–", no tier and no triangle; archived ones are not in this view.
        self.assertNotContains(page, "LEG Alt")
        self.assertContains(page, "LEG Neuland")
        # Neither the remark nor a create button without the right; the button "Print" is in the head (D-082).
        for text in ("Pilot Ittigen Nord", "LEG anlegen", "Nur Verbraucher", "knapp"):
            self.assertNotContains(page, text)
        self.assertContains(page, ">Drucken</a>")
        # The name is plain text, the icon at the right is the link to the page; the columns have their handle.
        self.assertContains(page, f'href="{self.detail(self.muster)}"')
        self.assertContains(page, 'class="mgmt-grip"')
        self.assertContains(self.login(self.creator).get(LIST), "LEG anlegen")

    def test_the_default_order_is_the_name_and_each_column_sorts(self):
        client = self.login(self.viewer)

        def order(**params):
            content = client.get(LIST, params).content.decode()
            return sorted(
                ("LEG Bahnhof", "LEG Musterquartier", "LEG Neuland"),
                key=lambda name: content.index(f'title="{name}"'),
            )

        self.assertEqual(order(), ["LEG Bahnhof", "LEG Musterquartier", "LEG Neuland"])
        self.assertEqual(order(sort="name", dir="desc")[0], "LEG Neuland")
        self.assertEqual(order(sort="quota")[:2], ["LEG Bahnhof", "LEG Musterquartier"])
        self.assertEqual(order(sort="quota", dir="desc")[:2], ["LEG Musterquartier", "LEG Bahnhof"])
        self.assertEqual(order(sort="points", dir="desc")[0], "LEG Musterquartier")
        self.assertEqual(order(sort="tier")[-1], "LEG Neuland")  # no tier: last
        self.assertEqual(order(sort="nonsense")[0], "LEG Bahnhof")  # an unknown column is the default
        # The pages of the list are cut from the sorted rows; a page beyond the end shows the last one.
        for params in ({"per": "30", "page": "9"}, {"per": "all"}, {"per": "x", "page": "-1"}):
            self.assertContains(client.get(LIST, params), "LEG Musterquartier")
        self.assertContains(client.get(LIST, {"per": "all"}), "1–3 von 3 LEGs")

    def test_search_display_and_the_problem_filter_come_from_the_address(self):
        client = self.login(self.viewer)
        names = ("LEG Bahnhof", "LEG Musterquartier", "LEG Neuland", "LEG Alt")

        def shown(**params):
            content = client.get(LIST, params).content.decode()
            return [name for name in names if f'title="{name}"' in content]

        self.assertEqual(shown(q="tra21361"), ["LEG Bahnhof", "LEG Musterquartier"])
        self.assertEqual(shown(q="Pilot"), [])  # the remark is never searched
        self.assertEqual(shown(display="archived"), ["LEG Alt"])
        self.assertEqual(shown(display="x"), ["LEG Bahnhof", "LEG Musterquartier", "LEG Neuland"])
        self.assertEqual(shown(problems="1"), ["LEG Bahnhof"])
        # The button counts the current view: with a search that finds no problem it is gone.
        self.assertNotContains(client.get(LIST, {"q": "Muster"}), "Probleme filtern")
        self.assertContains(client.get(LIST, {"q": "no such leg"}), "Noch keine LEGs. Legen Sie die erste LEG an.")

    def test_the_menu_has_the_entry_and_the_other_pages_link_to_the_pages_of_the_leg(self):
        client = self.login(self.full)
        page = client.get(LIST)
        self.assertContains(page, 'class="frame-item is-active" href="/management/legs/" aria-current="page"')
        # The list of the metering points and the page of a location link to the page of the LEG.
        self.assertContains(client.get(reverse("management:metering_points")), f'href="{self.detail(self.muster)}"')
        self.assertContains(client.get(reverse("management:locations_detail", args=[self.place_a.pk])), self.detail(self.muster))

    # The detail page ----------------------------------------------------------------------------

    def test_the_detail_page_shows_the_banner_the_cards_and_the_table_of_the_metering_points(self):
        page = self.login(self.full).get(self.detail(self.bahnhof))
        for text in (
            "← Zurück zu LEGs", "LEG Bahnhof", "2 Messpunkte", "Problem: Produktionsquote unter 10 %",
            "Problem: kein Produzent in der LEG", "Produktionsquote", "8.2 %", TODAY.strftime("%d.%m.%Y"), "Unter 10 %",
            "Die Quote kommt aus dem BKW-LEG-Portal. Mindestens 5 % sind gesetzlich nötig.", "Stammdaten", "Keine",
            "Zusammensetzung", "2 : 0", "Preisoptimiert", "TRA21361", "Messpunkte dieser LEG", "LEG archivieren",
            "keine Person", "eigenes", "Grün: schon im eigenen LEG.", "Information: Rabattstufe",
        ):
            self.assertContains(page, text)
        self.assertNotContains(page, "LEG löschen")  # it has a history
        self.assertNotContains(page, "Ausreichend")
        # Every row can change its LEG through the shared dialog, and comes back to this page.
        point = self.bahnhof.assignments.first().metering_point
        self.assertContains(page, f'href="{reverse("management:metering_points_leg", args=[point.pk])}?next=')
        self.assertContains(page, "LEG für diesen Messpunkt wechseln")

    def test_a_leg_of_several_circuits_recommends_the_leg_of_its_own_and_has_no_banner_without_a_problem(self):
        client = self.login(self.full)
        page = client.get(self.detail(self.muster))
        self.assertNotContains(page, "Problem:")  # no problem, no banner
        for text in (
            "37.6 %", "✓", "Ausreichend", "Pilot Ittigen Nord", "3 : 1", "Basis-Rabatt", "TRA21359", "TRA21361",
            "Anna Muster", "kein eigenes", "Blau: Wechsel empfohlen.", "Grau: kein eigenes LEG.",
        ):
            self.assertContains(page, text)
        # The metering point in circuit B is recommended the LEG that covers only B; the ones in A have none.
        self.assertContains(page, 'class="mgmt-action-state is-recommend" title="Blau: Wechsel empfohlen.">LEG Bahnhof<')
        self.assertContains(page, 'class="mgmt-action-state is-none"')
        self.assertNotContains(page, 'class="mgmt-action-state is-own"')
        # The table sorts by every heading; an unknown column is the default.
        content = client.get(self.detail(self.muster), {"sort": "address", "dir": "desc"}).content.decode()
        self.assertLess(content.index("Musterweg 12"), content.index("Lindenweg 7"))
        self.assertContains(client.get(self.detail(self.muster), {"sort": "x"}), self.point_one.number[:13])

    def test_the_rights_shape_the_table_and_the_links(self):
        # Only view_leg: no table of metering points at all, but the count.
        page = self.login(self.viewer).get(self.detail(self.muster))
        self.assertContains(page, "4 Messpunkte")
        self.assertNotContains(page, "Messpunkte dieser LEG")
        self.assertNotContains(page, self.point_one.number[:13])
        # With view_meteringpoint: the table, but no icon to change the LEG, no link to the location, no name
        # and no buttons of a change.
        page = self.login(self.bare).get(self.detail(self.muster))
        self.assertContains(page, "Messpunkte dieser LEG")
        self.assertContains(page, "Musterweg 12")
        for text in ("mgmt-swap", "/management/locations/", "Anna Muster", "Bearbeiten", "LEG archivieren"):
            self.assertNotContains(page, text)
        # A person exists for the first point but may not be named; the others have none.
        self.assertContains(page, "keine Person")

    def test_the_buttons_depend_on_the_history_and_the_rights(self):
        fresh = self.login(self.deleter).get(self.detail(self.neuland))
        self.assertContains(fresh, "LEG löschen")
        self.assertNotContains(fresh, "LEG archivieren")
        self.assertNotContains(self.login(self.deleter).get(self.detail(self.muster)), "LEG löschen")  # history
        # Without the right to delete a LEG without a history is archived instead.
        self.assertContains(self.login(self.changer).get(self.detail(self.neuland)), "LEG archivieren")
        archived = self.login(self.changer).get(self.detail(self.old))
        self.assertContains(archived, "LEG wiederherstellen")
        self.assertContains(archived, "archiviert")
        self.assertNotContains(self.login(self.viewer).get(self.detail(self.neuland)), "LEG archivieren")

    # The dialogs --------------------------------------------------------------------------------

    def test_creating_a_leg_checks_the_name_and_the_date_and_returns_to_the_list(self):
        client = self.login(self.creator)
        create = reverse("management:legs_new")
        page = client.get(create)
        for text in (
            "LEG anlegen", "Eine LEG ist ein virtueller Netzzusammenschluss. Messpunkte ordnen Sie der LEG danach zu.",
            "z. B. LEG Musterquartier", "(optional)", "Geprüft am", "Bemerkung", "Abbrechen", "Speichern", 'type="date"',
            "leg-quota.js", f'data-today="{TODAY.isoformat()}"',
        ):
            self.assertContains(page, text)
        self.assertNotContains(page, f'value="{TODAY.isoformat()}"')  # no quota: no date
        # A name in use (the case does not matter): the message under the field, nothing saved.
        refused = client.post(create, {"name": "leg musterquartier", "quota": "", "checked_on": ""})
        self.assertContains(refused, "Dieser Name wird bereits verwendet.")
        self.assertEqual(Leg.objects.filter(name__iexact="leg musterquartier").count(), 1)
        # A quota needs its date; the quota is a percentage; the day of the reading is not in the future.
        self.assertContains(client.post(create, {"name": "LEG X", "quota": "20"}), "Bitte füllen Sie dieses Feld aus.")
        self.assertContains(client.post(create, {"name": "LEG X", "quota": "120", "checked_on": TODAY.isoformat()}), "100")
        self.assertContains(client.post(create, {"name": "LEG X", "quota": "abc", "checked_on": TODAY.isoformat()}), "Zahl")
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        self.assertContains(
            client.post(create, {"name": "LEG X", "quota": "20", "checked_on": tomorrow}), "nicht in der Zukunft"
        )
        self.assertFalse(Leg.objects.filter(name="LEG X").exists())
        # A decimal comma is accepted; the new LEG is in the list with its quota.
        done = client.post(create, {"name": "LEG Neu", "quota": "37,6", "checked_on": EARLIER.isoformat(), "remark": "x"})
        self.assertRedirects(done, LIST, fetch_redirect_response=False)
        leg = Leg.objects.get(name="LEG Neu")
        self.assertEqual((leg.production_quota_percent, leg.quota_checked_on), (Decimal("37.60"), EARLIER))
        self.assertEqual(history.entries_for(leg).get().event, HistoryEvent.CREATED)
        self.assertContains(client.get(LIST), "37.6 %")
        # Without a quota the date is dropped, whatever the form sent.
        client.post(create, {"name": "LEG Ohne", "quota": "", "checked_on": EARLIER.isoformat()})
        self.assertIsNone(Leg.objects.get(name="LEG Ohne").quota_checked_on)

    def test_editing_the_quota_and_the_master_data(self):
        client = self.login(self.changer)
        quota = self.url("quota", self.bahnhof)
        page = client.get(quota)
        for text in (
            "Produktionsquote bearbeiten", "Die Quote steht im BKW-LEG-Portal.", 'value="8.2"',
            f'value="{TODAY.isoformat()}"', "%",
        ):
            self.assertContains(page, text)
        self.assertContains(client.post(quota, {"quota": "9", "checked_on": ""}), "Bitte füllen Sie dieses Feld aus.")
        done = client.post(quota, {"quota": "9,5", "checked_on": TODAY.isoformat(), "next": self.detail(self.bahnhof)})
        self.assertRedirects(done, self.detail(self.bahnhof), fetch_redirect_response=False)
        self.bahnhof.refresh_from_db()
        self.assertEqual(self.bahnhof.production_quota_percent, Decimal("9.50"))
        self.assertTrue(history.entries_for(self.bahnhof).filter(event=HistoryEvent.QUOTA_CHANGED).exists())
        # An empty quota removes the quota and the date.
        client.post(quota, {"quota": "", "checked_on": TODAY.isoformat()})
        self.bahnhof.refresh_from_db()
        self.assertEqual((self.bahnhof.production_quota_percent, self.bahnhof.quota_checked_on), (None, None))

        edit = self.url("edit", self.muster)
        page = client.get(edit)
        for text in ("Stammdaten bearbeiten", "Name und Bemerkung der LEG. Die Bemerkung ist nur intern.", "Pilot Ittigen Nord"):
            self.assertContains(page, text)
        self.assertContains(client.post(edit, {"name": "LEG Bahnhof"}), "Dieser Name wird bereits verwendet.")
        self.assertContains(client.post(edit, {"name": ""}), "Bitte füllen Sie dieses Feld aus.")
        client.post(edit, {"name": "LEG Musterquartier", "remark": "neu"})  # the own name is no duplicate
        self.muster.refresh_from_db()
        self.assertEqual((self.muster.name, self.muster.remark), ("LEG Musterquartier", "neu"))

    def test_a_dialog_returns_only_to_a_page_of_this_site(self):
        client = self.login(self.changer)
        for hostile in HOSTILE:
            page = client.get(self.url("quota", self.neuland), {"next": hostile})
            self.assertNotContains(page, "evil.example")
            done = client.post(self.url("edit", self.neuland), {"name": "LEG Neuland", "next": hostile})
            self.assertRedirects(done, self.detail(self.neuland), fetch_redirect_response=False)

    # Archive, restore, delete -------------------------------------------------------------------

    def test_archive_and_restore_need_the_right_to_change_and_are_logged(self):
        post = self.url("archive", self.muster)
        self.assertEqual(self.login(self.viewer).post(post).status_code, 403)
        self.assertEqual(self.login(self.deleter).post(post).status_code, 403)
        client = self.login(self.changer)
        done = client.post(post, {"next": self.detail(self.muster)})
        self.assertRedirects(done, self.detail(self.muster), fetch_redirect_response=False)
        self.muster.refresh_from_db()
        self.assertTrue(self.muster.is_archived)
        client.post(post, {"archive": "0"})
        self.muster.refresh_from_db()
        self.assertFalse(self.muster.is_archived)
        self.assertEqual(
            [e.event for e in history.entries_for(self.muster)], [HistoryEvent.ARCHIVED, HistoryEvent.UNARCHIVED]
        )
        self.assertEqual(client.get(post).status_code, 405)  # a button, never a link

    def test_delete_asks_first_and_is_only_possible_without_a_history(self):
        client = self.login(self.deleter)
        question = client.get(self.url("delete", self.neuland))
        self.assertContains(question, "Die LEG LEG Neuland wird endgültig gelöscht.")
        self.assertContains(question, "LEG löschen")
        self.assertEqual(client.get(self.url("delete", self.muster)).status_code, 404)  # a history
        refused = client.post(self.url("delete", self.muster), follow=True)
        self.assertContains(refused, "Diese LEG hat einen Verlauf und kann nicht gelöscht werden.")
        self.assertTrue(Leg.objects.filter(pk=self.muster.pk).exists())
        self.assertEqual(self.login(self.changer).post(self.url("delete", self.neuland)).status_code, 403)
        done = client.post(self.url("delete", self.neuland))
        self.assertRedirects(done, LIST, fetch_redirect_response=False)
        self.assertFalse(Leg.objects.filter(pk=self.neuland.pk).exists())
        self.assertEqual(client.get(self.url("delete", self.neuland)).status_code, 404)

    def test_every_post_is_protected_by_the_csrf_token(self):
        client = self.login(self.full, enforce_csrf_checks=True)
        for url, data in (
            (reverse("management:legs_new"), {"name": "LEG Csrf"}), (self.url("quota", self.muster), {}),
            (self.url("edit", self.muster), {"name": "LEG Csrf"}), (self.url("archive", self.muster), {}),
            (self.url("delete", self.neuland), {}),
        ):
            self.assertEqual(client.post(url, data).status_code, 403, url)
        self.assertFalse(Leg.objects.filter(name="LEG Csrf").exists())
        self.assertTrue(Leg.objects.filter(pk=self.neuland.pk).exists())

    def test_the_pages_need_the_right_of_the_action_and_a_normal_account_sees_nothing(self):
        for url in (LIST, self.detail(self.muster)):
            self.assertEqual(self.login(self.plain).get(url).status_code, 404)
            self.assertEqual(self.login(self.none).get(url).status_code, 403)
        for user, url, status in (
            (self.viewer, reverse("management:legs_new"), 403), (self.creator, self.url("quota", self.muster), 403),
            (self.changer, reverse("management:legs_new"), 403), (self.viewer, self.url("edit", self.muster), 403),
            (self.creator, self.url("delete", self.neuland), 403), (self.changer, self.url("quota", self.muster), 200),
        ):
            self.assertEqual(self.login(user).get(url).status_code, status, (user.email, url))
        self.assertEqual(self.login(self.viewer).get(self.detail(self.muster)).status_code, 200)
        self.assertEqual(self.login(self.viewer).get(reverse("management:legs_detail", args=[999999])).status_code, 404)
