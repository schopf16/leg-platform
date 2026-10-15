"""Swiss German translations of the account texts (AGENTS.md, section 2; D-010, D-034).

The checks read the translation file directly; CI checks that the committed compiled
catalogue (.mo) matches it (D-034).
"""

import ast
import re
from pathlib import Path

import django
import django.contrib.auth
import django_countries
import phonenumber_field
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import translation
from django_countries import countries
from django_countries.fields import CountryField

from leg_platform.accounts import email_texts
from leg_platform.accounts.admin import EmailTextForm, UserAdminForm
from leg_platform.accounts.models import EmailText, IssuedCustomerNumber, User
from leg_platform.accounts.tests.helpers import VerifiedClient

SHARP_S = "\u00df"
PROJECT_CATALOGUE = Path(settings.BASE_DIR) / "locale" / "de_CH" / "LC_MESSAGES" / "django.po"
# German catalogues Django falls back to for de-ch texts of the account fields.
DJANGO_CATALOGUES = [
    Path(package.__file__).parent / relative / "de" / "LC_MESSAGES" / "django.po"
    for package, relative in ((django, "conf/locale"), (django.contrib.auth, "locale"))
]
# Fields of AbstractBaseUser and PermissionsMixin; their texts come from Django.
DJANGO_FIELDS = {"id", "password", "last_login", "is_superuser", "groups", "user_permissions"}

_KEYWORD = re.compile(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(".*")$')


def read_catalogue(path):
    """Return {msgid: msgstr} of a .po file (header excluded).

    Entries with a context are keyed "context\\x04msgid" as in gettext; plural forms are joined.
    """
    entries = {}
    entry = {}
    current = None

    def finish():
        if entry.get("msgid"):
            key = entry["msgid"]
            if "msgctxt" in entry:
                key = f"{entry['msgctxt']}\x04{key}"
            entries[key] = "\n".join(
                value for name, value in entry.items() if name.startswith("msgstr")
            )
        entry.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        match = _KEYWORD.match(line)
        if match:
            keyword, text = match.groups()
            if keyword == "msgctxt" or (keyword == "msgid" and current != "msgctxt"):
                finish()
            entry[keyword] = ast.literal_eval(text)
            current = keyword
        elif line.startswith('"') and current:
            entry[current] += ast.literal_eval(line)
        elif not line or line.startswith("#"):
            current = None
    finish()
    return entries


def account_texts():
    """Return the English source texts of the account model shown to users."""
    with translation.override("en"):
        texts = {
            str(model._meta.verbose_name) for model in (User, IssuedCustomerNumber)
        } | {str(model._meta.verbose_name_plural) for model in (User, IssuedCustomerNumber)}
        for field in User._meta.get_fields():
            if field.name in DJANGO_FIELDS or not hasattr(field, "verbose_name"):
                continue
            texts.add(str(field.verbose_name))
            # Country names come from django-countries (PackageCatalogueTests).
            if not isinstance(field, CountryField):
                texts.update(str(label) for value, label in field.flatchoices if value)
            # Messages given explicitly to validators of this model (not Django's defaults).
            for validator in getattr(field, "_validators", []):
                if "message" in vars(validator):
                    texts.add(str(validator.message))
        texts.add(str(IssuedCustomerNumber._meta.get_field("number").verbose_name))
        # The editable email texts (D-039): model, fields, choices, placeholders and the form.
        texts.update(
            {str(EmailText._meta.verbose_name), str(EmailText._meta.verbose_name_plural)}
        )
        for name in ("key", "subject", "body", "updated_at"):
            texts.add(str(EmailText._meta.get_field(name).verbose_name))
        texts.update(str(label) for value, label in EmailText._meta.get_field("key").flatchoices)
        for key in EmailText.Key.values:
            texts.update(str(p.description) for p in email_texts.placeholders_of(key))
        for name in ("reset_to_default",):
            field = EmailTextForm.base_fields[name]
            texts.update({str(field.label), str(field.help_text)})
        # Help texts and unique messages of the account fields.
        for field in User._meta.get_fields():
            if field.name in DJANGO_FIELDS or not hasattr(field, "verbose_name"):
                continue
            if field.help_text:
                texts.add(str(field.help_text))
        texts.add(str(User._meta.get_field("normalized_email").error_messages["unique"]))
        # String representations (User.__str__), admin titles and admin form messages.
        texts.update(
            {
                "Account %(customer_number)s",
                "Unconfirmed account",
                "Account with internal number %(number)s",
                "internal number",
                "Administration",
                "A system administrator also needs access to the administration.",
                "Only active accounts can receive access to the administration.",
            }
        )
        # The postal code field of the admin form (D-036).
        texts.add(str(UserAdminForm.base_fields["postal_code"].label))
        for profile in (
            User(country=""),
            User(country="DE", swiss_postal_code=3000),
            User(country="CH", foreign_postal_code="10115"),
            User(email="a@" + "b" * 260 + ".org"),
        ):
            try:
                profile.clean()
            except ValidationError as error:
                texts.update(message for messages in error.message_dict.values() for message in messages)
            texts.update(str(message) for message in profile.postal_code_errors().values())
    return texts


def field_texts():
    """Return the English texts Django can show for the account fields, including its defaults."""
    texts = set()
    with translation.override("en"):
        for model in (User, IssuedCustomerNumber):
            for field in model._meta.get_fields():
                if not hasattr(field, "verbose_name"):
                    continue
                texts.update(str(text) for text in (field.verbose_name, field.help_text) if text)
                texts.update(str(message) for message in field.error_messages.values())
                # Plural messages (e.g. maximum length) render as "" without a number; function
                # validators (phone number) are covered by PackageCatalogueTests.
                texts.update(
                    str(getattr(validator, "message", "")) for validator in field.validators
                )
    texts.discard("")
    return texts


class ProjectCatalogueTests(SimpleTestCase):
    def setUp(self):
        self.catalogue = read_catalogue(PROJECT_CATALOGUE)

    def test_every_account_text_is_translated(self):
        missing = sorted(text for text in account_texts() if not self.catalogue.get(text))
        self.assertEqual(missing, [])

    def test_no_translation_contains_sharp_s(self):
        self.assertEqual([msgid for msgid, msgstr in self.catalogue.items() if SHARP_S in msgstr], [])

    def test_translations_keep_every_placeholder(self):
        # GNU msgfmt --check (CI) rejects a translation that omits or adds a placeholder.
        placeholder = re.compile(r"%(?:\((\w+)\))?[#0\- +]*\d*(?:\.\d+)?([diouxXeEfFgGcrsa])")
        mismatched = [
            msgid
            for msgid, msgstr in self.catalogue.items()
            if sorted(set(placeholder.findall(msgid))) != sorted(set(placeholder.findall(msgstr)))
        ]
        self.assertEqual(mismatched, [])

    def test_every_entry_is_translated(self):
        self.assertEqual([msgid for msgid, msgstr in self.catalogue.items() if not msgstr], [])

    def test_django_texts_with_sharp_s_are_overridden(self):
        django_de = {}
        for path in DJANGO_CATALOGUES:
            django_de.update(read_catalogue(path))
        affected = sorted(text for text in field_texts() if SHARP_S in django_de.get(text, ""))
        # Known case (D-010): the minimum of positive integer fields.
        self.assertIn("Ensure this value is greater than or equal to %(limit_value)s.", affected)
        not_overridden = [
            text for text in affected if SHARP_S in self.catalogue.get(text, SHARP_S)
        ]
        self.assertEqual(not_overridden, [])


class PackageCatalogueTests(SimpleTestCase):
    """German texts of the new packages that contain a sharp s are overridden (D-010)."""

    def test_sharp_s_texts_of_packages_are_overridden(self):
        project = read_catalogue(PROJECT_CATALOGUE)
        for package in (django_countries, phonenumber_field):
            path = Path(package.__file__).parent / "locale" / "de" / "LC_MESSAGES" / "django.po"
            with self.subTest(package=package.__name__):
                self.assertTrue(path.is_file())
                affected = [msgid for msgid, msgstr in read_catalogue(path).items() if SHARP_S in msgstr]
                for msgid in affected:
                    self.assertIn(msgid, project)
                    self.assertNotIn(SHARP_S, project[msgid])

    def test_displayed_country_names_contain_no_sharp_s(self):
        with translation.override("de-ch"):
            names = [str(name) for _code, name in countries]
        self.assertGreater(len(names), 200)
        self.assertEqual([name for name in names if SHARP_S in name], [])


@tag("mariadb")
class AdminPageTranslationTests(TestCase):
    client_class = VerifiedClient

    def setUp(self):
        self.administrator = User.objects.create_user(
            "admin@example.org",
            first_name="Anna",
            last_name="Example",
            street="Examplestrasse",
            house_number="12a",
            town="Bern",
            swiss_postal_code=3000,
        )
        self.administrator.set_password("copper kettle hums beside quiet orchard")
        with self.administrator._account_change("status"):
            self.administrator.status = User.Status.ACTIVE
            self.administrator.save()
        self.administrator.set_privileges(is_staff=True, is_superuser=True)
        self.member = User.objects.create_user(
            "member@example.org",
            first_name="Anna",
            last_name="Example",
            street="Examplestrasse",
            house_number="12a",
            town="Bern",
            swiss_postal_code=3000,
        )

    def test_admin_login_user_and_group_pages_use_swiss_german_without_sharp_s(self):
        with translation.override("de-ch"):
            self.client.force_login(self.administrator)
            pages = {
                "user list": reverse("admin:accounts_user_changelist"),
                "user form": reverse("admin:accounts_user_change", args=(self.member.pk,)),
                "group list": reverse("admin:auth_group_changelist"),
                "group form": reverse("admin:auth_group_add"),
            }
            rendered = {}
            for page_name, url in pages.items():
                with self.subTest(page=page_name):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    rendered[page_name] = response.content.decode()
                    self.assertNotIn(SHARP_S, rendered[page_name])

        self.assertIn("E-Mail-Adresse", rendered["user form"])
        self.assertIn("Kundennummer", rendered["user form"])
        self.assertIn("E-Mail-Adresse", rendered["user list"])
        self.assertIn("Kundennummer", rendered["user list"])
        for page_name, content in rendered.items():
            with self.subTest(page=page_name):
                self.assertNotIn("Normalisiert", content)
                self.assertNotIn("Django", content)
                # The admin shows the name of the instance (WL-23), not a fixed product name.
                self.assertIn(settings.INSTANCE_NAME, content)
        for text in (
            "Firmenname",
            "Adresszusatz",
            "Zum Beispiel eine zweite Person oder c/o.",
            "Postfach",
            "Postleitzahl",
            "Zugang zur Verwaltung",
            "Systemadministrator",
            "Die Gruppen, zu denen dieses Konto gehört.",
            "Berechtigungen, die diesem Konto direkt zugewiesen sind.",
        ):
            self.assertIn(text, rendered["user form"])
        if django.VERSION >= (6, 1):
            # Empty choice label introduced in Django 6.1; Django 6.0 renders "---------".
            self.assertIn("- Bitte auswählen -", rendered["user form"])
        # "Benutzer" is the name of the navigation group (WL-23), so it is no longer a leftover.
        for text in ("Administrator-Status", "Mitarbeiter", "Select an option"):
            self.assertNotIn(text, rendered["user form"])
        # eCH-0010 fields that are kept but not shown (D-036).
        for text in (
            "Titel",
            "Namenszusatz",
            "Adresszeile",
            "Wohnungsnummer",
            "Postfachtext",
            "Ortschaft",
            "Schweizer Postleitzahl",
            "Ausländische Postleitzahl",
        ):
            self.assertNotIn(text, rendered["user form"])
        self.assertIn("Konten durchsuchen", rendered["user list"])
        self.assertIn("Seitennavigation Konten", rendered["user list"])
        self.assertNotIn("Search ", rendered["user list"])
        self.assertNotIn("Pagination ", rendered["user list"])
        # The "ID" column was replaced by the customer number (D-036).
        self.assertNotIn('class="column-id', rendered["user list"])
        self.assertIn("column-customer_number_column", rendered["user list"])
        self.assertIn("Gruppe zur Änderung auswählen", rendered["group list"])
        self.assertIn("Berechtigungen", rendered["group form"])



class ChangeMessageLabelTests(SimpleTestCase):
    """Labels stored in admin change messages are shown in Swiss German (D-036).

    Django stores the English form label of each changed field and translates it with gettext
    when the history is shown (LogEntry.get_change_message()).
    """

    class SystemAdministrator:
        is_active = True
        is_staff = True
        is_superuser = True
        pk = -1

        def has_perm(self, perm, obj=None):
            return True

    def test_every_editable_label_is_translated(self):
        request = RequestFactory().get("/")
        request.user = self.SystemAdministrator()
        untranslated = []
        for model, obj in ((User, User(pk=1)), (Group, Group(pk=1))):
            form = admin.site._registry[model].get_form(request, obj, change=True)
            with translation.override(None):
                labels = [str(field.label or name) for name, field in form.base_fields.items()]
            with translation.override("de-ch"):
                for label in labels:
                    german = translation.gettext(label)
                    if german == label and label != "Name":
                        untranslated.append(label)
                    self.assertNotIn(SHARP_S, german)
        self.assertEqual(untranslated, [])
        with translation.override("de-ch"):
            self.assertEqual(translation.gettext("First name"), "Vorname")
