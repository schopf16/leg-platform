"""Tests of the user model that need no database (D-021, D-031, D-032, D-034)."""

from unittest.mock import patch

import phonenumbers
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import translation

from leg_platform.accounts.models import PortalSettings
from leg_platform.accounts.backends import EmailModelBackend
from leg_platform.accounts.models import (
    EXACT_COLLATION,
    IssuedCustomerNumber,
    User,
    format_customer_number,
)
from leg_platform.accounts.normalization import normalize_email_address
from leg_platform.accounts.login_protection import axes_username, client_ip_digest


def valid_profile(**overrides):
    values = {
        "email": "person@example.org",
        "first_name": "Anna",
        "last_name": "Example",
        "street": "Examplestrasse",
        "house_number": "12a",
        "town": "Exampletown",
        "swiss_postal_code": 3000,
    }
    values.update(overrides)
    return User(**values)


def clean_errors(user):
    try:
        user.clean()
    except ValidationError as error:
        return error.message_dict
    return {}


def field_errors(user, exclude=()):
    """Return the errors of clean_fields(), which checks the postal code rules (D-036)."""
    try:
        # The password hash is set by the account logic, not by profile forms.
        user.clean_fields(exclude={"password", *exclude})
    except ValidationError as error:
        return error.message_dict
    return {}


class NormalizationTests(SimpleTestCase):
    def test_surrounding_white_space_is_removed_and_case_is_lowered(self):
        self.assertEqual(normalize_email_address("  Person@Example.ORG \t"), "person@example.org")

    def test_accents_are_kept(self):
        self.assertEqual(normalize_email_address("José@Example.org"), "jos\u00e9@example.org")
        self.assertNotEqual(
            normalize_email_address("jos\u00e9@example.org"), normalize_email_address("jose@example.org")
        )

    def test_full_width_letters_are_folded(self):
        self.assertEqual(
            normalize_email_address("\uff30\uff25\uff32\uff33\uff2f\uff2e@example.org"),
            "person@example.org",
        )

    def test_decomposed_and_composed_forms_are_equal(self):
        self.assertEqual(
            normalize_email_address("jose\u0301@example.org"),
            normalize_email_address("jos\u00e9@example.org"),
        )

    def test_unicode_domain_and_punycode_domain_are_equal(self):
        self.assertEqual(normalize_email_address("person@bücher.test"), "person@xn--bcher-kva.test")
        self.assertEqual(
            normalize_email_address("person@xn--bcher-kva.test"), "person@xn--bcher-kva.test"
        )

    def test_empty_values_become_none(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(normalize_email_address(value))


class CustomerNumberFormatTests(SimpleTestCase):
    def test_two_blocks_of_three_digits(self):
        self.assertEqual(format_customer_number("482915"), "482 915")
        self.assertEqual(format_customer_number("007001"), "007 001")

    def test_no_number(self):
        self.assertEqual(format_customer_number(None), "")

    def test_invalid_numbers_are_rejected(self):
        for value in ("48291", "4829150", "48291a", "\u0664\u0668\u0662\u0669\u0661\u0665"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_customer_number(value)


class UserModelConfigurationTests(SimpleTestCase):
    def test_custom_user_model_is_active(self):
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")
        self.assertIs(get_user_model(), User)

    def test_login_identifier_is_the_normalised_email_address(self):
        self.assertEqual(User.USERNAME_FIELD, "normalized_email")
        self.assertEqual(User.EMAIL_FIELD, "email")
        self.assertEqual(User.REQUIRED_FIELDS, ["email"])
        self.assertEqual(
            settings.AUTHENTICATION_BACKENDS,
            [
                "axes.backends.AxesStandaloneBackend",
                "leg_platform.accounts.backends.EmailModelBackend",
            ],
        )
        field_names = {field.name for field in User._meta.get_fields()}
        self.assertNotIn("username", field_names)
        self.assertIsInstance(EmailModelBackend(), ModelBackend)


    def test_identifier_columns_use_the_exact_collation(self):
        for model, name in (
            (User, "normalized_email"),
            (User, "customer_number"),
            (IssuedCustomerNumber, "number"),
        ):
            with self.subTest(field=name):
                field = model._meta.get_field(name)
                # A foreign key column takes the collation of its target (ForeignKey.db_parameters).
                collation_field = field.target_field if field.is_relation else field
                self.assertEqual(collation_field.db_collation, EXACT_COLLATION)
                self.assertTrue(field.unique)
                self.assertFalse(field.editable)
        self.assertIsNone(User._meta.get_field("email").db_collation)

    def test_customer_number_references_the_issued_numbers(self):
        field = User._meta.get_field("customer_number")
        self.assertIs(field.related_model, IssuedCustomerNumber)
        self.assertEqual(field.target_field.name, "number")
        self.assertEqual(field.column, "customer_number")
        self.assertEqual(field.remote_field.on_delete.__name__, "PROTECT")

    def test_retention_flag_and_is_active_are_not_editable(self):
        self.assertFalse(User._meta.get_field("retention_obligation").editable)
        self.assertFalse(User._meta.get_field("is_active").editable)

    def test_status_keys_are_english(self):
        self.assertEqual(User.Status.values, ["unverified", "active", "blocked", "deactivated"])
        self.assertEqual(User.Salutation.values, ["mrs", "mr", "family"])

    def test_phone_number_settings(self):
        self.assertEqual(settings.PHONENUMBER_DEFAULT_REGION, "CH")
        self.assertEqual(settings.PHONENUMBER_DB_FORMAT, "E164")

    def test_user_model_is_registered_with_issued_numbers_hidden(self):
        registered = {model._meta.label for model in admin.site._registry}
        self.assertIn("accounts.User", registered)
        self.assertNotIn("accounts.IssuedCustomerNumber", registered)
        self.assertEqual(
            registered,
            {
                "accounts.User",
                "accounts.EmailText",
                "accounts.SecurityEvent",
                "accounts.GraphConfiguration",
                "accounts.PortalSettings",
                "axes.AccessAttempt",
                "axes.AccessFailureLog",
                "addresses.HousekeepingStatus",
                "addresses.ImportRequest",
                "addresses.ServicePostalCode",
                "auth.Group",
                "community.GridOperator",
                # Plain read-only tables of the management data (D-091).
                "community.Location",
                "community.MeteringPoint",
                "community.Leg",
                "community.TransformerCircuit",
                "community.PersonRecord",
                "community.PersonAssignment",
                "community.LegAssignment",
                "community.ShareChange",
                "community.HistoryEntry",
                "community.ManagementSettings",
            },
        )


class AuthenticationBackendTests(SimpleTestCase):
    def test_unknown_user_with_empty_password_returns_none(self):
        with patch.object(
            User._default_manager,
            "get_by_natural_key",
            side_effect=User.DoesNotExist,
        ) as lookup:
            result = EmailModelBackend().authenticate(
                None,
                username="unknown@example.org",
                password="",
            )

        self.assertIsNone(result)
        lookup.assert_called_once_with("unknown@example.org")


class LoginProtectionIdentifierTests(SimpleTestCase):
    def test_axes_username_is_normalised_and_not_stored_as_plaintext(self):
        first = axes_username(None, {"username": " Person@Example.ORG "})
        second = axes_username(None, {"username": "person@example.org"})

        self.assertEqual(first, second)
        self.assertNotIn("person@example.org", first)
        self.assertRegex(first, r"\A[0-9a-f]{64}\Z")

    def test_axes_username_rejects_unbounded_or_empty_values(self):
        self.assertIsNone(axes_username(None, {"username": " "}))
        self.assertIsNone(axes_username(None, {"username": "a" * 255}))

    def test_client_ip_digest_canonicalises_ipv4_mapped_ipv6(self):
        self.assertEqual(
            client_ip_digest("::ffff:192.0.2.1"),
            client_ip_digest("192.0.2.1"),
        )
        self.assertNotEqual(client_ip_digest("192.0.2.1"), client_ip_digest("192.0.2.2"))
        self.assertIsNone(client_ip_digest("not-an-ip-address"))


class ProfileValidationTests(SimpleTestCase):
    def test_valid_person_with_swiss_address(self):
        self.assertEqual(clean_errors(valid_profile()), {})

    def test_country_defaults_to_switzerland(self):
        self.assertEqual(User().country.code, "CH")

    def test_person_needs_first_and_last_name(self):
        errors = clean_errors(valid_profile(first_name="", last_name=" "))
        self.assertEqual(set(errors), {"first_name", "last_name"})

    def test_company_without_person(self):
        user = valid_profile(first_name="", last_name="", organisation_name="Example AG")
        self.assertEqual(clean_errors(user), {})

    def test_person_or_company_message(self):
        with translation.override("en"):
            errors = clean_errors(valid_profile(first_name="", last_name=""))
        self.assertEqual(errors["first_name"], ["Enter a first name or a company name."])
        self.assertEqual(errors["last_name"], ["Enter a last name or a company name."])

    def test_street_or_post_office_box_is_required(self):
        errors = clean_errors(valid_profile(street="", house_number=""))
        self.assertEqual(set(errors), {"street"})
        user = valid_profile(street="", house_number="", post_office_box_number=1234)
        self.assertEqual(clean_errors(user), {})

    def test_hidden_fields_are_never_required(self):
        # D-036: a post office box number needs no post office box text.
        user = valid_profile(street="", post_office_box_number=1234)
        self.assertEqual(clean_errors(user), {})
        self.assertEqual(field_errors(user), {})

    def test_town_and_country_are_required(self):
        errors = clean_errors(valid_profile(town="", country=""))
        self.assertEqual(set(errors), {"town", "country"})

    def test_valid_postal_codes(self):
        for values in (
            {"swiss_postal_code": 1000},
            {"swiss_postal_code": 9999},
            {"country": "DE", "swiss_postal_code": None, "foreign_postal_code": "10115"},
            {"country": "GB", "swiss_postal_code": None, "foreign_postal_code": "SW1A 1AA"},
            {"country": "LI", "swiss_postal_code": None, "foreign_postal_code": "9490"},
        ):
            with self.subTest(values=values):
                self.assertEqual(field_errors(valid_profile(**values)), {})

    def test_swiss_address_needs_a_swiss_postal_code_from_1000_to_9999(self):
        message = ["Enter a postal code with 4 digits from 1000 to 9999."]
        with translation.override("en"):
            for value in (None, 999, 10000):
                with self.subTest(value=value):
                    errors = field_errors(valid_profile(swiss_postal_code=value))
                    self.assertEqual(errors, {"swiss_postal_code": message})
            errors = field_errors(valid_profile(foreign_postal_code="3000"))
        self.assertEqual(set(errors), {"foreign_postal_code"})

    def test_foreign_address_needs_a_foreign_postal_code(self):
        for country in ("DE", "LI"):
            with self.subTest(country=country):
                errors = field_errors(valid_profile(country=country))
                self.assertEqual(set(errors), {"swiss_postal_code", "foreign_postal_code"})

    def test_postal_code_rules_skip_excluded_fields(self):
        # A form that shows one postal code field excludes both model fields and checks the
        # rules itself; errors for excluded fields would raise ValueError in the form.
        user = valid_profile(country="DE")
        self.assertEqual(
            field_errors(user, exclude={"swiss_postal_code", "foreign_postal_code"}), {}
        )

    def test_without_country_no_postal_code_rule(self):
        user = valid_profile(country="", swiss_postal_code=None)
        self.assertEqual(field_errors(user), {})
        self.assertEqual(set(clean_errors(user)), {"country"})

    def test_postal_code_display(self):
        self.assertEqual(valid_profile().postal_code_display, "3000")
        user = valid_profile(country="DE", swiss_postal_code=None, foreign_postal_code="10115")
        self.assertEqual(user.postal_code_display, "10115")
        self.assertEqual(User().postal_code_display, "")

    def test_ech_0010_fields_are_kept(self):
        field_names = {field.name for field in User._meta.get_fields()}
        self.assertLessEqual(
            {
                "title",
                "organisation_name",
                "organisation_name_add_on_1",
                "organisation_name_add_on_2",
                "address_line_1",
                "address_line_2",
                "dwelling_number",
                "post_office_box_number",
                "post_office_box_text",
                "locality",
                "swiss_postal_code",
                "foreign_postal_code",
                "phone_number",
            },
            field_names,
        )

    def test_labels(self):
        expected = {
            "organisation_name": ("company name", "Firmenname"),
            "address_line_1": ("address addition", "Adresszusatz"),
            "post_office_box_number": ("post office box", "Postfach"),
            "town": ("town", "Ort"),
            "is_staff": ("access to administration", "Zugang zur Verwaltung"),
            "is_superuser": ("superuser status", "Systemadministrator"),
        }
        for name, (english, swiss_german) in expected.items():
            field = User._meta.get_field(name)
            with self.subTest(field=name):
                with translation.override("en"):
                    self.assertEqual(str(field.verbose_name), english)
                with translation.override("de-ch"):
                    self.assertEqual(str(field.verbose_name), swiss_german)
        with translation.override("de-ch"):
            self.assertEqual(
                str(User._meta.get_field("address_line_1").help_text),
                "Zum Beispiel eine zweite Person oder c/o.",
            )
            self.assertEqual(str(User._meta.verbose_name), "Konto")
            self.assertEqual(str(User._meta.verbose_name_plural), "Konten")

    def test_maximum_lengths_follow_ech_0010(self):
        expected = {
            "title": 50,
            "first_name": 30,
            "last_name": 30,
            "organisation_name": 60,
            "organisation_name_add_on_1": 60,
            "organisation_name_add_on_2": 60,
            "address_line_1": 150,
            "address_line_2": 150,
            "street": 150,
            "house_number": 30,
            "dwelling_number": 30,
            "post_office_box_text": 15,
            "locality": 40,
            "town": 40,
            "foreign_postal_code": 15,
        }
        for name, length in expected.items():
            with self.subTest(field=name):
                self.assertEqual(User._meta.get_field(name).max_length, length)
        box_number = User._meta.get_field("post_office_box_number")
        box_number.run_validators(99_999_999)
        with self.assertRaises(ValidationError) as context:
            box_number.run_validators(100_000_000)
        with translation.override("en"):
            self.assertEqual(
                context.exception.messages, ["Enter a post office box of at most 8 digits."]
            )

    def test_salutation_choices(self):
        field = User._meta.get_field("salutation")
        self.assertTrue(field.blank)
        for value in ("mrs", "mr", "family", ""):
            field.clean(value, User())
        with self.assertRaises(ValidationError):
            field.clean("dr", User())


class StringRepresentationWithoutDatabaseTests(SimpleTestCase):
    """SimpleTestCase rejects database queries, so these tests also show that none is made."""

    def test_customer_number_is_read_from_the_row(self):
        user = User(
            pk=17, email="person@example.org", first_name="Anna", customer_number_id="686821"
        )
        with translation.override("en"):
            self.assertEqual(str(user), "Account 686 821")
        with translation.override("de-ch"):
            self.assertEqual(str(user), "Konto 686 821")

    def test_account_without_customer_number(self):
        for user in (User(), User(pk=17, email="person@example.org")):
            with self.subTest(pk=user.pk):
                with translation.override("en"):
                    self.assertEqual(str(user), "Unconfirmed account")
                with translation.override("de-ch"):
                    self.assertEqual(str(user), "Nicht bestätigtes Konto")


class PhoneNumberTests(SimpleTestCase):
    def setUp(self):
        # libphonenumber's example number for Switzerland, so that no real number is used.
        self.example = phonenumbers.example_number("CH")
        self.e164 = phonenumbers.format_number(self.example, phonenumbers.PhoneNumberFormat.E164)
        self.field = User._meta.get_field("phone_number")

    def test_national_number_is_read_as_swiss_and_stored_as_e164(self):
        national = phonenumbers.format_number(
            self.example, phonenumbers.PhoneNumberFormat.NATIONAL
        )
        value = self.field.clean(national, User())
        self.assertEqual(self.field.get_prep_value(value), self.e164)

    def test_international_format_is_accepted(self):
        international = phonenumbers.format_number(
            self.example, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        )
        value = self.field.clean(international, User())
        self.assertEqual(self.field.get_prep_value(value), self.e164)

    def test_invalid_numbers_are_rejected(self):
        for value in ("123", "+41 12", "not a number"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.field.clean(value, User())

    def test_phone_number_is_optional(self):
        self.assertTrue(self.field.blank)
        self.assertEqual(self.field.clean("", User()), "")
