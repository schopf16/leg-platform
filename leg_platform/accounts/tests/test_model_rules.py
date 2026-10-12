"""Tests of the user model that need no database (D-021, D-031, D-032, D-034)."""

import phonenumbers
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from leg_platform.accounts.models import (
    EXACT_COLLATION,
    IssuedCustomerNumber,
    User,
    format_customer_number,
)
from leg_platform.accounts.normalization import normalize_email_address


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
        self.assertEqual(normalize_email_address("person@bücher.de"), "person@xn--bcher-kva.de")
        self.assertEqual(
            normalize_email_address("person@xn--bcher-kva.de"), "person@xn--bcher-kva.de"
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
        field_names = {field.name for field in User._meta.get_fields()}
        self.assertNotIn("username", field_names)

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
        self.assertEqual(registered, {"accounts.User", "auth.Group"})


class ProfileValidationTests(SimpleTestCase):
    def test_valid_person_with_swiss_address(self):
        self.assertEqual(clean_errors(valid_profile()), {})

    def test_country_defaults_to_switzerland(self):
        self.assertEqual(User().country.code, "CH")

    def test_person_needs_first_and_last_name(self):
        errors = clean_errors(valid_profile(first_name="", last_name=" "))
        self.assertEqual(set(errors), {"first_name", "last_name"})

    def test_organisation_without_person(self):
        user = valid_profile(first_name="", last_name="", organisation_name="Example AG")
        self.assertEqual(clean_errors(user), {})

    def test_street_or_post_office_box_is_required(self):
        errors = clean_errors(valid_profile(street="", house_number=""))
        self.assertEqual(set(errors), {"street"})
        user = valid_profile(street="", house_number="", post_office_box_text="Postfach")
        self.assertEqual(clean_errors(user), {})
        user.post_office_box_number = 1234
        self.assertEqual(clean_errors(user), {})

    def test_post_office_box_number_needs_the_text(self):
        errors = clean_errors(valid_profile(street="", post_office_box_number=1234))
        self.assertEqual(set(errors), {"street", "post_office_box_text"})

    def test_town_and_country_are_required(self):
        errors = clean_errors(valid_profile(town="", country=""))
        self.assertEqual(set(errors), {"town", "country"})

    def test_swiss_address_needs_a_swiss_postal_code(self):
        errors = clean_errors(valid_profile(swiss_postal_code=None, foreign_postal_code="3000"))
        self.assertEqual(set(errors), {"swiss_postal_code", "foreign_postal_code"})

    def test_foreign_address_needs_a_foreign_postal_code(self):
        user = valid_profile(country="DE", swiss_postal_code=None, foreign_postal_code="10115")
        self.assertEqual(clean_errors(user), {})
        errors = clean_errors(valid_profile(country="DE"))
        self.assertEqual(set(errors), {"swiss_postal_code", "foreign_postal_code"})

    def test_swiss_postal_code_range(self):
        field = User._meta.get_field("swiss_postal_code")
        for value in (1000, 9999):
            field.run_validators(value)
        for value in (999, 10000):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                field.run_validators(value)

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
        with self.assertRaises(ValidationError):
            box_number.run_validators(100_000_000)

    def test_salutation_choices(self):
        field = User._meta.get_field("salutation")
        self.assertTrue(field.blank)
        for value in ("mrs", "mr", "family", ""):
            field.clean(value, User())
        with self.assertRaises(ValidationError):
            field.clean("dr", User())


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
