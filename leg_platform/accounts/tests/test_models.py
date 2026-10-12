"""Database tests of the user model (D-016, D-021, D-031, D-034); they need MariaDB (tag "mariadb")."""

import threading
from unittest import mock

import phonenumbers
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, tag
from django.utils import translation

from leg_platform.accounts import models as account_models
from leg_platform.accounts.models import (
    AccountIntegrityError,
    CustomerNumberUnavailableError,
    IssuedCustomerNumber,
    User,
)

PASSWORD = "copper kettle hums beside quiet orchard"
# 209 characters; each ligature U+FB00 becomes "ff" under NFKC, so the normalised value has 409.
LENGTHENED_EMAIL = "a@" + ".".join(["\ufb00" * 50] * 4) + ".org"


def activate_account(user):
    """Activate an unverified account. An active account needs a usable password, so both are saved in one write."""
    user.set_password(PASSWORD)
    with user._account_change("status"):
        user.status = User.Status.ACTIVE
        user.save()
    return user


def create_active_user(email, **extra_fields):
    """Create an account the way the verification flow completes a registration."""
    user = activate_account(User.objects.create_user(email, **extra_fields))
    user.assign_customer_number()
    return user


def create_system_administrator(email):
    user = create_active_user(email)
    user.set_privileges(is_staff=True, is_superuser=True)
    return user


@tag("mariadb")
class CreateUserTests(TestCase):
    def test_new_user_is_unverified_without_privileges(self):
        user = User.objects.create_user(" Person@Example.org ")
        user.refresh_from_db()
        self.assertEqual(user.email, "person@example.org")
        self.assertEqual(user.normalized_email, "person@example.org")
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.groups.exists())
        self.assertFalse(user.user_permissions.exists())
        self.assertEqual(user.get_all_permissions(), set())
        self.assertIsNone(user.customer_number_id)
        self.assertFalse(user.retention_obligation)
        self.assertEqual(user.country.code, "CH")

    def test_password_is_not_accepted_before_verification(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("person@example.org", password=PASSWORD)
        self.assertFalse(User.objects.exists())

    def test_controlled_fields_are_not_accepted(self):
        existing = User.objects.create_user("existing@example.org")
        for name, value in (
            ("is_staff", True),
            ("is_superuser", True),
            ("is_active", True),
            ("status", User.Status.ACTIVE),
            ("customer_number", "123456"),
            ("customer_number_id", "123456"),
            ("retention_obligation", True),
            ("id", existing.pk),
            ("pk", existing.pk),
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                User.objects.create_user("person@example.org", **{name: value})
        self.assertEqual(User.objects.count(), 1)

    def test_email_address_is_required(self):
        for value in ("", "  ", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                User.objects.create_user(value)

    def test_email_address_format_is_validated(self):
        for value in (
            "person",
            "person@",
            "@example.org",
            "person@example",
            "per son@example.org",
            '"victim"@example.org',
            "person@localhost",
            "person@[127.0.0.1]",
            "person@127.0.0.1",
            "a@b＠c.org",
            "x@exam\u00adple.org",
            "jos\u00e9@example.org",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                User.objects.create_user(value)
        self.assertFalse(User.objects.exists())

    def test_unicode_and_punycode_domain_spellings_share_one_account(self):
        user = User.objects.create_user("person@bücher.test")
        self.assertEqual(user.email, "person@xn--bcher-kva.test")
        self.assertEqual(user.normalized_email, "person@xn--bcher-kva.test")
        with self.assertRaises(IntegrityError):
            User.objects.create_user("person@xn--bcher-kva.test")

    def test_stored_address_is_the_canonical_form(self):
        user = User.objects.create_user(" Person.Name@Bücher.TEST ")
        user.refresh_from_db()
        self.assertEqual(user.email, "person.name@xn--bcher-kva.test")
        self.assertEqual(user.normalized_email, user.email)

    def test_email_address_longer_than_the_column_is_rejected(self):
        too_long = "a" * 64 + "@" + ".".join(["b" * 62] * 3) + ".org"
        self.assertGreater(len(too_long), 254)
        for value in (too_long, LENGTHENED_EMAIL):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                User.objects.create_user(value)
        self.assertFalse(User.objects.exists())

    def test_normalised_address_must_match(self):
        with self.assertRaises(ValueError):
            User.objects.create_user("person@example.org", normalized_email="other@example.org")
        user = User.objects.create_user("Person@example.org", normalized_email="person@example.org")
        self.assertEqual(user.normalized_email, "person@example.org")

    def test_unverified_account_cannot_get_a_usable_password(self):
        user = User.objects.create_user("person@example.org")
        user.set_password(PASSWORD)
        with self.assertRaises(AccountIntegrityError):
            user.save()

    def test_empty_password_is_refused_and_the_stored_password_is_kept(self):
        user = User.objects.create_user("person@example.org")
        user.refresh_from_db()
        stored = user.password
        with self.assertRaises(AccountIntegrityError):
            user.set_password("")
        user.refresh_from_db()
        self.assertEqual(user.password, stored)

    def test_no_roles_exist(self):
        # No code or migration creates roles (D-012).
        self.assertFalse(Group.objects.exists())


@tag("mariadb")
class CreateSuperuserTests(TransactionTestCase):
    # create_superuser() refuses to run inside a transaction, so TestCase cannot be used.

    def test_first_system_administrator(self):
        admin = User.objects.create_superuser("admin@example.org", password=PASSWORD)
        admin.refresh_from_db()
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.status, User.Status.ACTIVE)
        self.assertTrue(admin.check_password(PASSWORD))
        self.assertRegex(admin.customer_number_id, r"\A[0-9]{6}\Z")
        self.assertFalse(admin.groups.exists())

    def test_refused_while_a_system_administrator_exists(self):
        admin = User.objects.create_superuser("admin@example.org", password=PASSWORD)
        create_system_administrator("second@example.org")
        admin.set_status(User.Status.BLOCKED)
        with self.assertRaises(AccountIntegrityError):
            User.objects.create_superuser("third@example.org", password=PASSWORD)
        self.assertEqual(User.objects.count(), 2)

    def test_privileges_cannot_be_passed(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser("admin@example.org", password=PASSWORD, status="blocked")

    def test_password_is_required_and_validated(self):
        for value in (None, ""):
            with self.subTest(password=value), self.assertRaises(ValueError):
                User.objects.create_superuser("admin@example.org", password=value)
        for value in ("short", "admin@example.org-admin", "1234567890123456"):
            with self.subTest(password=value), self.assertRaises(ValidationError):
                User.objects.create_superuser("admin@example.org", password=value)
        self.assertFalse(User.objects.exists())

    def test_refused_inside_a_transaction(self):
        with transaction.atomic(), self.assertRaises(AccountIntegrityError):
            User.objects.create_superuser("admin@example.org", password=PASSWORD)
        self.assertFalse(User.objects.exists())

    def test_django_createsuperuser_command_is_disabled(self):
        with self.assertRaisesMessage(CommandError, "D-005"):
            call_command("createsuperuser")
        self.assertFalse(User.objects.exists())


@tag("mariadb")
class EmailUniquenessTests(TestCase):
    def test_case_and_white_space_variants_are_rejected_by_the_database(self):
        User.objects.create_user("jose@example.org")
        for variant in ("Jose@example.org", " JOSE@EXAMPLE.ORG"):
            with self.subTest(variant=variant):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    User.objects.create_user(variant)

    def test_compatibility_and_composition_variants_are_rejected_by_the_database(self):
        User.objects.create_user("person@ex\u00e4mple.org")
        # Decomposed (NFD) form of the same domain; accepted by Django's EmailValidator.
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("person@exa\u0308mple.org")
        # Full-width letters fail Django's EmailValidator in create_user(), so the model is used.
        variant = User(email="\uff50\uff45\uff52\uff53\uff4f\uff4e@ex\u00e4mple.org")
        variant.set_unusable_password()
        with self.assertRaises(IntegrityError), transaction.atomic():
            variant.save()
        self.assertEqual(User.objects.count(), 1)

    def test_addresses_differing_in_accents_are_different(self):
        User.objects.create_user("person@example.org")
        User.objects.create_user("person@ex\u00e4mple.org")
        self.assertEqual(User.objects.count(), 2)

    def test_full_clean_reports_the_duplicate(self):
        User.objects.create_user("person@example.org")
        user = User(
            email="PERSON@example.org",
            first_name="Anna",
            last_name="Example",
            street="Examplestrasse",
            town="Exampletown",
            swiss_postal_code=3000,
        )
        user.set_unusable_password()
        with self.assertRaises(ValidationError) as context:
            user.full_clean()
        self.assertEqual(set(context.exception.message_dict), {"normalized_email"})

    def test_deactivated_accounts_release_the_address(self):
        first = create_active_user("person@example.org")
        first.set_status(User.Status.DEACTIVATED)
        second = create_active_user("Person@example.org")
        second.set_status(User.Status.DEACTIVATED)
        third = create_active_user("person@example.org")
        self.assertEqual(
            User.objects.filter(normalized_email__isnull=True).count(), 2
        )
        self.assertEqual(User.objects.get_by_natural_key("PERSON@example.org"), third)

    def test_reactivation_restores_the_normalised_address(self):
        user = create_active_user("person@example.org")
        user.set_status(User.Status.DEACTIVATED)
        user.refresh_from_db()
        self.assertIsNone(user.normalized_email)
        user.set_status(User.Status.ACTIVE)
        user.refresh_from_db()
        self.assertEqual(user.normalized_email, "person@example.org")

    def test_email_change_updates_the_normalised_address(self):
        user = create_active_user("person@example.org")
        user.change_email("New@Example.org")
        user.refresh_from_db()
        self.assertEqual(user.email, "new@example.org")
        self.assertEqual(user.normalized_email, "new@example.org")

    def test_email_change_is_validated(self):
        user = create_active_user("person@example.org")
        for value in ("invalid", LENGTHENED_EMAIL, "", "jos\u00e9@example.org"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                user.change_email(value)
        user.refresh_from_db()
        self.assertEqual(user.email, "person@example.org")

    def test_lengthened_address_is_rejected_before_the_database(self):
        user = User(email=LENGTHENED_EMAIL)
        user.set_unusable_password()
        with self.assertRaises(AccountIntegrityError):
            user.save()
        with self.assertRaises(ValidationError) as context:
            user.full_clean()
        self.assertIn("email", context.exception.message_dict)
        self.assertFalse(User.objects.exists())

    def test_columns_use_the_exact_collation(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND ("
                "(TABLE_NAME = 'accounts_user' AND COLUMN_NAME IN "
                "('email', 'normalized_email', 'customer_number')) OR "
                "(TABLE_NAME = 'accounts_issuedcustomernumber' AND COLUMN_NAME = 'number'))"
            )
            collations = {(table, column): collation for table, column, collation in cursor}
        self.assertEqual(
            collations,
            {
                ("accounts_user", "email"): "utf8mb4_uca1400_ai_ci",
                ("accounts_user", "normalized_email"): "utf8mb4_bin",
                ("accounts_user", "customer_number"): "utf8mb4_bin",
                ("accounts_issuedcustomernumber", "number"): "utf8mb4_bin",
            },
        )


@tag("mariadb")
class NaturalKeyTests(TestCase):
    def test_lookup_uses_the_normalised_address(self):
        user = create_active_user("person@example.org")
        self.assertEqual(User.objects.get_by_natural_key("  PERSON@example.org "), user)
        self.assertEqual(
            User.objects.get_by_natural_key("\uff30\uff25\uff32\uff33\uff2f\uff2e@example.org"), user
        )

    def test_deactivated_accounts_are_not_found(self):
        user = create_active_user("person@example.org")
        user.set_status(User.Status.DEACTIVATED)
        for value in ("person@example.org", "", "  ", None):
            with self.subTest(value=value), self.assertRaises(User.DoesNotExist):
                User.objects.get_by_natural_key(value)

    def test_accents_are_not_folded(self):
        create_active_user("jose@example.org")
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key("jos\u00e9@example.org")

    def test_overlong_input_is_not_looked_up(self):
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key(LENGTHENED_EMAIL)

    def test_raw_input_longer_than_the_column_is_not_looked_up(self):
        # "a" and a combining diaeresis form one character under NFKC: 256 raw characters
        # normalise to 134. The raw length is checked first, so no query is made.
        overlong = "a\u0308" * 122 + "@example.org"
        self.assertGreater(len(overlong), account_models.EMAIL_MAX_LENGTH)
        with self.assertNumQueries(0), self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key(overlong)


@tag("mariadb")
class StatusTests(TestCase):
    def test_only_active_accounts_are_active(self):
        user = create_active_user("person@example.org")
        create_system_administrator("admin@example.org")
        expected = {
            User.Status.BLOCKED: False,
            User.Status.ACTIVE: True,
            User.Status.DEACTIVATED: False,
        }
        for status, is_active in expected.items():
            with self.subTest(status=status):
                user.set_status(status)
                user.refresh_from_db()
                self.assertIs(user.is_active, is_active)

    def test_unknown_status_is_rejected(self):
        user = create_active_user("person@example.org")
        with self.assertRaises(AccountIntegrityError):
            user.set_status("suspended")
        self.assertEqual(user.status, User.Status.ACTIVE)

    def test_verified_accounts_cannot_become_unverified(self):
        user = create_active_user("person@example.org")
        with self.assertRaises(AccountIntegrityError):
            user.set_status(User.Status.UNVERIFIED)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)

    def test_active_accounts_need_a_usable_password(self):
        user = User.objects.create_user("person@example.org")
        with self.assertRaises(AccountIntegrityError):
            user.set_status(User.Status.ACTIVE)
        user.set_password(PASSWORD)
        with self.assertRaises(AccountIntegrityError):
            user.set_status(User.Status.ACTIVE)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        self.assertFalse(user.has_usable_password())

    def test_an_empty_password_is_not_a_usable_password(self):
        """An empty password is never stored; refused saves leave the stored password unchanged."""
        user = User.objects.create_user("person@example.org")
        user.password = ""
        with self.assertRaises(AccountIntegrityError):
            user.set_status(User.Status.ACTIVE)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.UNVERIFIED)
        stored = user.password
        user.password = ""
        with self.assertRaises(AccountIntegrityError):
            user.save()
        user.refresh_from_db()
        self.assertEqual(user.password, stored)
        user.password = ""
        with self.assertRaises(AccountIntegrityError):
            user.save(update_fields=["password"])
        user.refresh_from_db()
        self.assertEqual(user.password, stored)


@tag("mariadb")
class ControlledFieldTests(TestCase):
    """Controlled fields change only through the account logic (review findings M-1 to M-3)."""

    def test_direct_changes_are_rejected(self):
        user = create_active_user("person@example.org")
        for name, value in (
            ("status", User.Status.BLOCKED),
            ("email", "other@example.org"),
            ("is_staff", True),
            ("is_superuser", True),
        ):
            with self.subTest(field=name):
                setattr(user, name, value)
                with self.assertRaises(AccountIntegrityError):
                    user.save()
                with self.assertRaises(AccountIntegrityError):
                    user.save(update_fields=[name])
                user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertFalse(user.is_staff)

    def test_derived_fields_cannot_be_written_alone(self):
        user = create_active_user("person@example.org")
        create_system_administrator("admin@example.org")
        user.set_status(User.Status.BLOCKED)
        user.is_active = True
        user.save(update_fields=["is_active"])
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        user.email = "other@example.org"
        with self.assertRaises(AccountIntegrityError):
            user.save(update_fields=["normalized_email"])
        user.refresh_from_db()
        self.assertEqual(user.normalized_email, "person@example.org")

    def test_new_instance_with_an_existing_pk_does_not_overwrite_the_account(self):
        existing = create_active_user("person@example.org")
        intruder = User(pk=existing.pk, email="other@example.org")
        intruder.set_unusable_password()
        with self.assertRaises(IntegrityError), transaction.atomic():
            intruder.save()
        existing.refresh_from_db()
        self.assertEqual(existing.email, "person@example.org")
        self.assertEqual(existing.status, User.Status.ACTIVE)

    def test_primary_key_of_a_loaded_account_cannot_change(self):
        existing = create_active_user("person@example.org")
        other = User.objects.create_user("other@example.org")
        other.pk = existing.pk
        with self.assertRaises(AccountIntegrityError):
            other.save()
        existing.refresh_from_db()
        self.assertEqual(existing.email, "person@example.org")

    def test_stale_instance_cannot_undo_a_promotion_or_a_block(self):
        user = create_active_user("person@example.org")
        create_system_administrator("admin@example.org")
        stale = User.objects.get(pk=user.pk)
        user.set_privileges(is_staff=True, is_superuser=False)
        stale.first_name = "Changed"
        with self.assertRaises(AccountIntegrityError):
            stale.save()
        user.set_status(User.Status.BLOCKED)
        stale = User.objects.get(pk=user.pk)
        user.set_status(User.Status.ACTIVE)
        with self.assertRaises(AccountIntegrityError):
            stale.save()
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(user.first_name, "")

    def test_saving_a_deleted_account_does_not_recreate_it(self):
        user = create_active_user("person@example.org")
        stale = User.objects.get(pk=user.pk)
        user.delete()
        with self.assertRaises(AccountIntegrityError):
            stale.save()
        self.assertFalse(User.objects.exists())

    def test_privileges(self):
        user = create_active_user("person@example.org")
        user.set_privileges(is_staff=True, is_superuser=False)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        with self.assertRaises(AccountIntegrityError):
            user.set_privileges(is_staff=False, is_superuser=True)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_privileges_are_granted_only_to_active_accounts(self):
        unverified = User.objects.create_user("unverified@example.org")
        blocked = create_active_user("blocked@example.org")
        blocked.set_status(User.Status.BLOCKED)
        deactivated = create_active_user("deactivated@example.org")
        deactivated.set_status(User.Status.DEACTIVATED)
        for user in (unverified, blocked, deactivated):
            for is_superuser in (False, True):
                with self.subTest(status=user.status, is_superuser=is_superuser):
                    with self.assertRaises(AccountIntegrityError):
                        user.set_privileges(is_staff=True, is_superuser=is_superuser)
                    user.refresh_from_db()
                    self.assertFalse(user.is_staff)
                    self.assertFalse(user.is_superuser)

    def test_privileges_of_inactive_accounts_can_be_removed(self):
        create_system_administrator("admin@example.org")
        second = create_system_administrator("second@example.org")
        second.set_status(User.Status.BLOCKED)
        second.set_privileges(is_staff=True, is_superuser=False)
        second.set_privileges(is_staff=False, is_superuser=False)
        second.refresh_from_db()
        self.assertFalse(second.is_staff)
        self.assertFalse(second.is_superuser)

    def test_new_accounts_cannot_be_created_with_controlled_values(self):
        for name, value in (
            ("status", User.Status.ACTIVE),
            ("is_staff", True),
            ("is_superuser", True),
            ("retention_obligation", True),
        ):
            with self.subTest(field=name):
                user = User(email="person@example.org", **{name: value})
                user.set_unusable_password()
                with self.assertRaises(AccountIntegrityError):
                    user.save()
        self.assertFalse(User.objects.exists())


@tag("mariadb")
class SystemAdministratorTests(TestCase):
    """At least one active system administrator always remains (D-016)."""

    def setUp(self):
        self.admin = create_system_administrator("admin@example.org")

    def test_last_system_administrator_cannot_be_blocked_deactivated_demoted_or_deleted(self):
        for change in (
            lambda: self.admin.set_status(User.Status.BLOCKED),
            lambda: self.admin.set_status(User.Status.DEACTIVATED),
            lambda: self.admin.set_privileges(is_staff=False, is_superuser=False),
            self.admin.delete,
        ):
            with self.assertRaises(AccountIntegrityError):
                change()
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.status, User.Status.ACTIVE)
        self.assertTrue(self.admin.is_superuser)

    def test_blocked_system_administrator_does_not_count(self):
        second = create_system_administrator("second@example.org")
        second.set_status(User.Status.BLOCKED)
        with self.assertRaises(AccountIntegrityError):
            self.admin.set_status(User.Status.BLOCKED)

    def test_possible_while_another_active_system_administrator_exists(self):
        create_system_administrator("second@example.org")
        self.admin.set_status(User.Status.BLOCKED)
        self.admin.set_status(User.Status.ACTIVE)
        self.admin.set_privileges(is_staff=False, is_superuser=False)
        self.admin.set_privileges(is_staff=True, is_superuser=True)
        self.admin.delete()
        self.assertEqual(User.objects.count(), 1)


@tag("mariadb")
class DeletionTests(TestCase):
    def test_account_with_retention_obligation_is_not_deleted(self):
        user = create_active_user("person@example.org")
        user.set_retention_obligation()
        stale = User.objects.get(pk=user.pk)
        stale.retention_obligation = False
        for instance in (user, stale):
            with self.assertRaises(AccountIntegrityError):
                instance.delete()
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_account_without_retention_obligation_is_deleted(self):
        user = create_active_user("person@example.org")
        user.delete()
        self.assertFalse(User.objects.exists())


@tag("mariadb")
class StringRepresentationTests(TestCase):
    def test_no_email_address(self):
        with mock.patch.object(account_models.secrets, "randbelow", side_effect=[482915]):
            user = create_active_user("person@example.org")
        unverified = User.objects.create_user("other@example.org")
        with translation.override("en"):
            self.assertEqual(str(user), f"Account {user.pk}")
            self.assertEqual(str(unverified), f"Account {unverified.pk}")
        for instance in (user, unverified):
            self.assertNotIn("example.org", str(instance))

    def test_customer_number_is_not_in_its_string_representation(self):
        number = IssuedCustomerNumber.objects.issue()
        with translation.override("en"):
            self.assertNotIn(number, str(IssuedCustomerNumber.objects.get(pk=number)))


class AddressRuleEnforcementTests(TestCase):
    """The address rules also apply to save() and clean(), not only to create_user() and change_email()."""

    def test_save_rejects_rules_that_bypass_create_user(self):
        for value in (
            '"victim"@example.org',
            "person@localhost",
            "person@[127.0.0.1]",
            "p\u0131bc@example.org",
            "person@exa\ufe0fmple.org",
            "person@exa\u20d1mple.org",
        ):
            with self.subTest(value=value):
                user = User(email=value)
                with self.assertRaises(AccountIntegrityError):
                    user.save()
                self.assertFalse(User.objects.exists())

    def test_clean_rejects_rules_that_bypass_create_user(self):
        for value in ('"victim"@example.org', "person@localhost", "person@[127.0.0.1]"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as context:
                    User(email=value, country="CH").clean()
                self.assertIn("email", context.exception.message_dict)

    def test_combining_marks_in_domain_are_rejected(self):
        with self.assertRaises(ValueError):
            account_models.validate_account_email("x@exa\u20d1mple.org")

    def test_non_ascii_local_part_is_rejected(self):
        with self.assertRaises(ValueError):
            account_models.validate_account_email("j\u00f6se@example.org")


@tag("mariadb")
class CustomerNumberTests(TestCase):
    def test_number_is_issued_when_the_registration_is_completed(self):
        user = create_active_user("person@example.org")
        user.refresh_from_db()
        number = user.customer_number_id
        self.assertRegex(number, r"\A[0-9]{6}\Z")
        self.assertEqual(user.customer_number, IssuedCustomerNumber.objects.get(number=number))
        self.assertEqual(user.customer_number_display, f"{number[:3]} {number[3:]}")

    def test_unverified_accounts_get_no_number(self):
        user = User.objects.create_user("person@example.org")
        with self.assertRaises(AccountIntegrityError):
            user.assign_customer_number()
        self.assertFalse(IssuedCustomerNumber.objects.exists())

    def test_unsaved_status_change_does_not_count(self):
        user = User.objects.create_user("person@example.org")
        user.status = User.Status.ACTIVE
        with self.assertRaises(AccountIntegrityError):
            user.assign_customer_number()
        self.assertIsNone(user.customer_number_id)

    def test_number_is_assigned_only_once(self):
        user = create_active_user("person@example.org")
        with self.assertRaises(AccountIntegrityError):
            user.assign_customer_number()
        self.assertEqual(IssuedCustomerNumber.objects.count(), 1)

    def test_number_cannot_be_set_changed_or_removed_directly(self):
        user = create_active_user("person@example.org")
        original = user.customer_number_id
        replacement = IssuedCustomerNumber.objects.create(
            number="000001" if original != "000001" else "000002"
        )
        for value in (replacement.number, None):
            with self.subTest(value=value):
                user.customer_number_id = value
                with self.assertRaises(AccountIntegrityError):
                    user.save()
                with self.assertRaises(AccountIntegrityError):
                    user.save(update_fields=["customer_number"])
                user.customer_number_id = original
        user.customer_number = replacement
        with self.assertRaises(AccountIntegrityError):
            user.save()
        third = activate_account(User.objects.create_user("third@example.org"))
        third.customer_number_id = "123456"
        with self.assertRaises(AccountIntegrityError):
            third.save()
        user.refresh_from_db()
        self.assertEqual(user.customer_number_id, original)

    def test_database_accepts_only_issued_numbers(self):
        user = create_active_user("person@example.org")
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE accounts_user SET customer_number = %s WHERE id = %s", ["999999", user.pk]
            )

    def test_numbers_of_deleted_accounts_are_never_reused(self):
        with mock.patch.object(account_models.secrets, "randbelow", side_effect=[482915]):
            user = create_active_user("person@example.org")
        user.delete()
        self.assertTrue(IssuedCustomerNumber.objects.filter(number="482915").exists())
        with mock.patch.object(account_models.secrets, "randbelow", side_effect=[482915, 7001]):
            second = create_active_user("person@example.org")
        self.assertEqual(second.customer_number_id, "007001")

    def test_collision_is_retried(self):
        IssuedCustomerNumber.objects.create(number="000005")
        user = activate_account(User.objects.create_user("person@example.org"))
        with mock.patch.object(
            account_models.secrets, "randbelow", side_effect=[5, 5, 7]
        ) as randbelow:
            self.assertEqual(user.assign_customer_number(), "000007")
        self.assertEqual(randbelow.call_count, 3)

    def test_attempts_are_bounded(self):
        IssuedCustomerNumber.objects.create(number="000005")
        user = create_active_user("person@example.org")
        other = activate_account(User.objects.create_user("other@example.org"))
        with mock.patch.object(account_models.secrets, "randbelow", return_value=5) as randbelow:
            with self.assertRaises(CustomerNumberUnavailableError):
                other.assign_customer_number()
        self.assertEqual(randbelow.call_count, account_models.CUSTOMER_NUMBER_ATTEMPTS)
        other.refresh_from_db()
        self.assertIsNone(other.customer_number_id)
        self.assertIsNotNone(user.customer_number_id)

    def test_exhaustion_fails_without_searching(self):
        with mock.patch.object(account_models, "CUSTOMER_NUMBER_SPACE", 3):
            for index in range(3):
                create_active_user(f"person{index}@example.org")
            user = activate_account(User.objects.create_user("last@example.org"))
            with mock.patch.object(account_models.secrets, "randbelow") as randbelow:
                with self.assertRaises(CustomerNumberUnavailableError):
                    user.assign_customer_number()
            randbelow.assert_not_called()
        self.assertEqual(
            sorted(IssuedCustomerNumber.objects.values_list("number", flat=True)),
            ["000000", "000001", "000002"],
        )

    def test_issued_numbers_cannot_be_changed_or_deleted(self):
        issued = IssuedCustomerNumber.objects.create(number="123456")
        with self.assertRaises(AccountIntegrityError):
            issued.delete()
        with self.assertRaises(AccountIntegrityError):
            IssuedCustomerNumber.objects.all().delete()
        with self.assertRaises(AccountIntegrityError):
            IssuedCustomerNumber.objects.update(number="654321")
        with self.assertRaises(AccountIntegrityError):
            issued.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            IssuedCustomerNumber(number="123456").save()
        with self.assertRaises(AccountIntegrityError):
            IssuedCustomerNumber.objects.bulk_create([IssuedCustomerNumber(number="654321")])
        self.assertEqual(
            list(IssuedCustomerNumber.objects.values_list("number", flat=True)), ["123456"]
        )

    def test_only_active_accounts_get_a_number(self):
        create_system_administrator("admin@example.org")
        for status in (User.Status.BLOCKED, User.Status.DEACTIVATED):
            with self.subTest(status=status):
                user = User.objects.create_user(f"{status}@example.org")
                user.set_status(status)
                with self.assertRaises(AccountIntegrityError):
                    user.assign_customer_number()
                user.refresh_from_db()
                self.assertIsNone(user.customer_number_id)
        self.assertEqual(IssuedCustomerNumber.objects.count(), 1)


@tag("mariadb")
class RetentionObligationTests(TestCase):
    def test_flag_can_be_set(self):
        user = create_active_user("person@example.org")
        user.set_retention_obligation()
        user.refresh_from_db()
        self.assertTrue(user.retention_obligation)

    def test_flag_cannot_be_unset(self):
        user = create_active_user("person@example.org")
        user.set_retention_obligation()
        user.retention_obligation = False
        with self.assertRaises(AccountIntegrityError):
            user.save()
        with self.assertRaises(AccountIntegrityError):
            user.save(update_fields=["retention_obligation"])
        user.refresh_from_db()
        self.assertTrue(user.retention_obligation)

    def test_stale_instance_cannot_unset_the_flag(self):
        user = create_active_user("person@example.org")
        stale = User.objects.get(pk=user.pk)
        user.set_retention_obligation()
        stale.first_name = "Changed"
        with self.assertRaises(AccountIntegrityError):
            stale.save()
        stale.save(update_fields=["first_name"])
        user.refresh_from_db()
        self.assertTrue(user.retention_obligation)
        self.assertEqual(user.first_name, "Changed")

    def test_flag_is_not_set_at_creation(self):
        with self.assertRaises(AccountIntegrityError):
            User(email="person@example.org", retention_obligation=True).save()


@tag("mariadb")
class BulkChangeTests(TestCase):
    def setUp(self):
        self.user = create_active_user("person@example.org")

    def test_bulk_update_of_protected_fields_is_rejected(self):
        values = {
            "status": User.Status.BLOCKED,
            "is_active": False,
            "email": "other@example.org",
            "normalized_email": "other@example.org",
            "retention_obligation": False,
            "is_staff": True,
            "is_superuser": True,
            "customer_number": None,
            "customer_number_id": None,
            "password": "!",
        }
        for name, value in values.items():
            with self.subTest(field=name), self.assertRaises(AccountIntegrityError):
                User.objects.filter(pk=self.user.pk).update(**{name: value})
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertFalse(self.user.is_staff)

    def test_bulk_update_through_bulk_update_is_rejected(self):
        self.user.is_staff = True
        with self.assertRaises(AccountIntegrityError):
            User.objects.bulk_update([self.user], ["is_staff"])

    def test_bulk_update_of_profile_fields_is_allowed(self):
        self.assertEqual(User.objects.filter(pk=self.user.pk).update(first_name="Anna"), 1)

    def test_bulk_delete_is_rejected(self):
        with self.assertRaises(AccountIntegrityError):
            User.objects.all().delete()
        with self.assertRaises(AccountIntegrityError):
            User.objects.filter(pk=self.user.pk).delete()
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_bulk_create_is_rejected(self):
        with self.assertRaises(AccountIntegrityError):
            User.objects.bulk_create([User(email="other@example.org")])

    def test_related_managers_of_groups_do_not_bypass_the_protection(self):
        group = Group.objects.create(name="Example group")
        self.user.groups.add(group)
        with self.assertRaises(AccountIntegrityError):
            group.user_set.update(is_superuser=True)
        with self.assertRaises(AccountIntegrityError):
            group.user_set.all().delete()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_single_account_can_be_deleted(self):
        self.user.delete()
        self.assertFalse(User.objects.exists())

    def test_base_manager_does_not_bypass_the_protection(self):
        with self.assertRaises(AccountIntegrityError):
            User._base_manager.filter(pk=self.user.pk).update(is_staff=True)
        with self.assertRaises(AccountIntegrityError):
            User._base_manager.filter(pk=self.user.pk).delete()
        with self.assertRaises(AccountIntegrityError):
            IssuedCustomerNumber._base_manager.all().delete()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_related_object_access_uses_the_base_manager(self):
        entry = LogEntry.objects.create(
            user=self.user, object_repr="example", action_flag=CHANGE
        )
        self.assertEqual(LogEntry.objects.get(pk=entry.pk).user, self.user)
        self.assertEqual(
            list(LogEntry.objects.filter(user__normalized_email="person@example.org")), [entry]
        )
        self.user.delete()
        self.assertFalse(LogEntry.objects.exists())


@tag("mariadb")
class ProfileStorageTests(TestCase):
    def test_phone_number_is_stored_as_e164(self):
        example = phonenumbers.example_number("CH")
        national = phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.NATIONAL)
        e164 = phonenumbers.format_number(example, phonenumbers.PhoneNumberFormat.E164)
        user = create_active_user("person@example.org", phone_number=national)
        with connection.cursor() as cursor:
            cursor.execute("SELECT phone_number, country FROM accounts_user WHERE id = %s", [user.pk])
            self.assertEqual(cursor.fetchone(), (e164, "CH"))

    def test_permissions_of_new_users_are_empty(self):
        user = create_active_user("person@example.org")
        self.assertFalse(user.has_perm("accounts.change_user"))
        self.assertTrue(Permission.objects.filter(codename="change_user").exists())


def run_concurrently(*functions):
    """Run the functions in parallel threads, each with its own connection; return results."""
    barrier = threading.Barrier(len(functions))
    results = [None] * len(functions)

    def run(index, function):
        try:
            barrier.wait(timeout=10)
            results[index] = function()
        except Exception as error:
            results[index] = error
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=(index, function))
        for index, function in enumerate(functions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@tag("mariadb")
class ConcurrencyTests(TransactionTestCase):
    """Parallel requests on separate connections (review finding L-11)."""

    def test_same_normalised_address_is_registered_once(self):
        results = run_concurrently(
            lambda: User.objects.create_user("person@example.org"),
            lambda: User.objects.create_user(" PERSON@example.org"),
        )
        self.assertEqual(sum(isinstance(result, User) for result in results), 1)
        self.assertEqual(sum(isinstance(result, IntegrityError) for result in results), 1)
        self.assertEqual(User.objects.count(), 1)

    def test_parallel_assignment_issues_one_number(self):
        user = activate_account(User.objects.create_user("person@example.org"))
        first, second = User.objects.get(pk=user.pk), User.objects.get(pk=user.pk)
        results = run_concurrently(first.assign_customer_number, second.assign_customer_number)
        numbers = [result for result in results if isinstance(result, str)]
        self.assertEqual(len(numbers), 1)
        self.assertEqual(sum(isinstance(result, AccountIntegrityError) for result in results), 1)
        user.refresh_from_db()
        self.assertEqual(user.customer_number_id, numbers[0])
        self.assertEqual(IssuedCustomerNumber.objects.count(), 1)

    def test_parallel_first_system_administrators(self):
        results = run_concurrently(
            lambda: User.objects.create_superuser("admin@example.org", password=PASSWORD),
            lambda: User.objects.create_superuser("other@example.org", password=PASSWORD),
        )
        self.assertEqual(sum(isinstance(result, User) for result in results), 1)
        self.assertEqual(sum(isinstance(result, AccountIntegrityError) for result in results), 1)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)
