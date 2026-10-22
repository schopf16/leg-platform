"""The greeting by name in the security notifications (D-068, R-13)."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone, translation

from leg_platform.accounts import (
    email_change,
    email_texts,
    emails,
    greeting,
    mail_queue,
    password_reset,
    second_factor_reset,
)
from leg_platform.accounts.models import (
    GREETING_NAME_MAX_LENGTH,
    EmailText,
    MailJob,
    PendingEmailChange,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import (
    PASSWORD,
    VerifiedClient,
    create_active_user,
    reset_request_data,
    run_mail_queue,
)
from leg_platform.accounts.tests.test_second_factor_reset import (
    administrator,
    with_second_factor,
)
from leg_platform.accounts.tests.test_translations import PROJECT_CATALOGUE, read_catalogue

Key = EmailText.Key
SECOND = "second.address@example.org"


def rename(user, first_name="", last_name="", organisation_name=""):
    """Set the names around the form rules (e.g. only one name), like data that was stored once."""
    User.objects.filter(pk=user.pk).update(
        first_name=first_name, last_name=last_name, organisation_name=organisation_name
    )
    user.refresh_from_db()
    return user


class CleanNameTests(SimpleTestCase):
    """The fallback matrix of the name guard (no database)."""

    def test_a_person_with_both_names_is_greeted_by_name(self):
        self.assertEqual(greeting.clean_name("Maria", "Muster"), "Maria Muster")

    def test_names_with_the_usual_characters_pass(self):
        for first, last, expected in (
            ("Anne-Marie", "O'Brien", "Anne-Marie O'Brien"),
            ("Anne", "O\u2019Brien", "Anne O\u2019Brien"),
            ("Jean", "de la Fontaine", "Jean de la Fontaine"),
            ("M.", "Muster", "M. Muster"),
            ("Zoë", "Müller", "Zoë Müller"),
            ("José", "Núñez", "José Núñez"),
            ("Çağlar", "Şahin", "Çağlar Şahin"),
            ("Łukasz", "Kowalski", "Łukasz Kowalski"),
            ("Иван", "Петров", "Иван Петров"),
            ("Zoe\u0308", "Muller", "Zoë Muller"),
        ):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), expected)

    def test_only_one_name_gives_the_neutral_greeting(self):
        self.assertEqual(greeting.clean_name("Maria", ""), "")
        self.assertEqual(greeting.clean_name("", "Muster"), "")
        self.assertEqual(greeting.clean_name("Maria", "   "), "")
        self.assertEqual(greeting.clean_name("", ""), "")
        self.assertEqual(greeting.clean_name(None, None), "")

    def test_a_part_without_a_letter_gives_the_neutral_greeting(self):
        for first, last in (("-", "Muster"), ("Maria", "."), ("'", "'"), ("\u0308", "Muster")):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_a_character_that_a_name_does_not_have_gives_the_neutral_greeting(self):
        for first, last in (
            ("Maria3", "Muster"),
            ("Maria", "Muster 2"),
            ("Maria", "Muster!"),
            ("Maria", "Muster, Dr."),
            ("<b>Maria</b>", "Muster"),
            ("Maria", "Muster;"),
            ("Maria", "{greeting}"),
            ("Maria", "%(name)s"),
            ("Maria", "Muster:"),
            ("Maria", "Mu/ster"),
            ("Maria", "Mu\\ster"),
            ("Maria", "Muster\U0001f600"),
            ("Maria", "Muster\u00a9"),
            ("Maria", "Muster_"),
            ("Maria", "Muster+"),
            ("Ma@ria", "Muster"),
            ('Maria "Mia"', "Muster"),
        ):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_a_name_that_looks_like_an_address_gives_the_neutral_greeting(self):
        for first, last in (
            ("www.example", "org"),
            ("Visit", "example.org"),
            ("bit.ly", "Muster"),
            ("Maria", "M.Muster"),
            ("Maria", "example.com"),
        ):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_a_scheme_or_email_address_is_not_a_name_even_in_look_alike_forms(self):
        for first, last in (
            ("\uff48\uff54\uff54\uff50\uff53\uff1a", "Muster"),  # full-width "https:"
            ("a@b", "Muster"),
            ("Maria", "\uff4d\u3002\uff43\uff4f\uff4d"),  # full-width "m.com"
        ):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_control_format_and_line_break_characters_are_removed(self):
        self.assertEqual(greeting.clean_name("Ma\u200bria", "Mus\u202eter"), "Maria Muster")
        self.assertEqual(greeting.clean_name("Maria\n", "\r\nMuster"), "Maria Muster")
        self.assertEqual(greeting.clean_name("Ma\x00ria", "Mu\x1fster"), "Maria Muster")
        self.assertEqual(greeting.clean_name("Maria\u2028", "Muster\u2029"), "Maria Muster")
        self.assertEqual(greeting.clean_name("\ufeffMaria", "Muster"), "Maria Muster")

    def test_removed_line_breaks_cannot_smuggle_a_second_line_or_a_header(self):
        for first, last in (
            ("Maria\nBcc: attacker@example.org", "Muster"),
            ("Maria\r\nSubject: x", "Muster"),
            ("Maria", "Muster\n\nVisit our site"),
        ):
            with self.subTest(first=first, last=last):
                name = greeting.clean_name(first, last)
                self.assertNotIn("\n", name)
                self.assertNotIn("\r", name)
                self.assertNotIn(":", name)
                self.assertNotIn("@", name)

    def test_whitespace_is_collapsed_and_normalised(self):
        self.assertEqual(greeting.clean_name("  Maria \u00a0 Anna ", "\u3000Muster\u2009"), "Maria Anna Muster")

    def test_compatibility_forms_are_normalised(self):
        # NFKC: full-width letters become plain letters.
        self.assertEqual(greeting.clean_name("\uff2d\uff41\uff52\uff49\uff41", "Muster"), "Maria Muster")

    def test_a_very_long_name_gives_the_neutral_greeting(self):
        self.assertEqual(greeting.clean_name("A" * 30, "B" * 30), "A" * 30 + " " + "B" * 30)
        # The limit counts the cleaned name: 64 characters pass, 65 do not.
        self.assertEqual(GREETING_NAME_MAX_LENGTH, 64)
        self.assertEqual(len(greeting.clean_name("A" * 31, "B" * 32)), 64)
        self.assertEqual(greeting.clean_name("A" * 32, "B" * 32), "")
        self.assertEqual(greeting.clean_name("A" * 40, "B" * 40), "")
        self.assertEqual(greeting.clean_name("A" * 10000, "B"), "")

    def test_an_expanding_character_cannot_exceed_the_limit(self):
        # U+FDFA becomes 18 characters under NFKC: four of them are longer than the limit.
        self.assertEqual(greeting.clean_name("\ufdfa" * 4, "Muster"), "")

    def test_a_combining_mark_must_follow_a_letter_or_another_mark(self):
        for first, last in (
            ("Maria", "Mu \u0301ster"),  # after a space
            ("Maria", "-\u0301Muster"),  # after a hyphen at the start
            ("Maria", "Muster-\u0301"),  # after a hyphen
            ("Maria", "O'\u0301Brien"),  # after an apostrophe
            ("Maria", "M.\u0301Muster"),  # after a period
            ("\u0301Maria", "Muster"),  # at the start
        ):
            with self.subTest(first=first, last=last):
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_a_mark_after_a_letter_or_a_mark_is_allowed(self):
        self.assertEqual(greeting.clean_name("Zoe\u0308", "Muller"), "Zoë Muller")
        self.assertEqual(
            greeting.clean_name("Max\u0300\u0301\u0302", "Muster"), "Max\u0300\u0301\u0302 Muster"
        )

    def test_at_most_three_marks_in_a_row_are_allowed(self):
        self.assertNotEqual(greeting.clean_name("Max" + "\u0300" * 3, "Muster"), "")
        for count in (4, 5, 60):
            with self.subTest(count=count):
                self.assertEqual(greeting.clean_name("Max" + "\u0300\u0301" * count, "Muster"), "")
                self.assertEqual(greeting.clean_name("Maria", "Muster" + "\u0334" * count), "")

    def test_the_count_of_marks_starts_again_after_a_letter(self):
        three = "\u0300\u0301\u0302"
        self.assertEqual(
            greeting.clean_name(f"x{three}q{three}z", "Muster"),
            f"x{three}q{three}z Muster",
        )

    def test_invisible_marks_are_removed(self):
        for invisible in ("\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef", "\u034f"):
            with self.subTest(invisible=hex(ord(invisible))):
                self.assertEqual(
                    greeting.clean_name(f"Ma{invisible}ria", f"{invisible}Muster{invisible}"),
                    "Maria Muster",
                )
                # After a space it would attach to the space: it is removed, not kept.
                self.assertEqual(greeting.clean_name(f"Maria {invisible}", "Muster"), "Maria Muster")
        # A part of invisible marks only has no letter.
        self.assertEqual(greeting.clean_name("\ufe0f\u034f", "Muster"), "")

    def test_the_hangul_fillers_are_removed_and_a_part_of_fillers_has_no_letter(self):
        for filler in ("\u115f", "\u1160", "\u3164", "\uffa0"):
            with self.subTest(filler=hex(ord(filler))):
                self.assertEqual(
                    greeting.clean_name(f"Ma{filler}ria", f"{filler}Muster"), "Maria Muster"
                )
                self.assertEqual(greeting.clean_name(filler * 3, "Muster"), "")
                self.assertEqual(greeting.clean_name("Maria", filler), "")
                self.assertFalse(greeting.is_clean(f"Maria {filler}Muster"))
        self.assertEqual(greeting.clean_name("\u115f\u1160\u3164\uffa0", "Muster"), "")

    def test_the_result_is_normalised_again_after_the_removal(self):
        # "a", a zero-width space and an acute accent: after the removal the accent composes.
        cleaned = greeting.clean_name("a\u200b\u0301", "Muster")
        self.assertEqual(cleaned, "\u00e1 Muster")
        self.assertTrue(greeting.is_clean(cleaned))
        for first in ("a\u200b\u0301", "a\u034f\u0301", "e\u3164\u0308", "A \u200b b"):
            with self.subTest(first=first):
                cleaned = greeting.clean_name(first, "Muster")
                self.assertTrue(cleaned)
                self.assertTrue(greeting.is_clean(cleaned))

    def test_a_mark_cannot_hide_an_address(self):
        for first, last in (
            ("Maria", "example.c\u0334om"),
            ("example.c\u0334om", "Muster"),
            ("Maria", "ex\u0301ample.co\u0334m"),
            ("Maria", "exa\u0334mple.or\u0334g"),
            ("Maria", "www.exa\u0334mple.org"),
        ):
            with self.subTest(first=first, last=last):
                # The plain check misses it; the check without marks finds it.
                self.assertEqual(greeting.clean_name(first, last), "")

    def test_the_address_check_without_marks_does_not_refuse_a_normal_name(self):
        self.assertEqual(greeting.clean_name("Zoë", "Müller"), "Zoë Müller")
        self.assertEqual(greeting.clean_name("M.", "Muster"), "M. Muster")

    def test_a_company_name_is_never_used(self):
        user = User(first_name="", last_name="", organisation_name="Muster AG")
        self.assertEqual(greeting.name_for(user), "")

    def test_a_person_name_next_to_a_company_name_wins(self):
        user = User(first_name="Maria", last_name="Muster", organisation_name="Muster AG")
        self.assertEqual(greeting.name_for(user), "Maria Muster")

    def test_the_greeting_has_no_comma_and_comes_from_the_catalogue(self):
        with translation.override("de-ch"):
            self.assertEqual(greeting.render("Maria Muster"), "Guten Tag Maria Muster")
            self.assertEqual(greeting.render(""), "Guten Tag")
        with translation.override("en"):
            self.assertEqual(greeting.render("Maria Muster"), "Hello Maria Muster")
            self.assertEqual(greeting.render(""), "Hello")

    def test_a_percent_sign_in_a_stored_name_is_not_interpreted(self):
        # The guard refuses the name; either way it is a value, never a format string.
        with translation.override("en"):
            self.assertEqual(greeting.render("100%(name)s"), "Hello")
            self.assertEqual(greeting.render("Maria %(name)s"), "Hello")
    def test_a_stored_name_that_is_not_clean_gives_the_neutral_greeting(self):
        with translation.override("en"):
            for stored in (
                "Maria\nMuster",
                "Maria\r\nBcc: x@example.org",
                "Maria Muster\u2028Visit",
                "Maria Muster\u200b",
                "Maria  Muster",
                " Maria Muster",
                "Maria Mus3ter",
                "<b>Maria</b> Muster",
                "{greeting} Muster",
                "Maria example.c\u0334om",
                "Maria Mu \u0301ster",
                "Maria Max" + "\u0300" * 4,
                "Maria Muster\ufe0f",
                "A" * 33 + " " + "B" * 32,
                "Maria",
                None,
            ):
                with self.subTest(stored=stored):
                    self.assertEqual(greeting.render(stored), "Hello")
            # A clean name is still used.
            self.assertEqual(greeting.render("Zoë Müller"), "Hello Zoë Müller")

    def test_the_names_that_the_guard_returns_are_clean(self):
        for first, last in (
            ("Maria", "Muster"),
            ("Anne-Marie", "O'Brien"),
            ("Jean", "de la Fontaine"),
            ("Zoe\u0308", "Muller"),
            ("Иван", "Петров"),
        ):
            with self.subTest(first=first, last=last):
                self.assertTrue(greeting.is_clean(greeting.clean_name(first, last)))


class PlaceholderOfferTests(SimpleTestCase):
    def test_the_placeholder_is_offered_exactly_for_the_notification_kinds(self):
        offered = {
            key
            for key in Key.values
            if any(
                placeholder.name == email_texts.GREETING.name
                for placeholder in email_texts.placeholders_of(key)
            )
        }
        self.assertEqual(offered, {str(kind) for kind in MailJob.NOTIFICATION_KINDS})
        self.assertEqual(len(offered), len(MailJob.NOTIFICATION_KINDS))  # the notifications to the addresses of an account

    def test_the_deleted_account_notice_offers_the_neutral_variant_of_the_placeholder(self):
        offered = email_texts.placeholders_of(Key.ACCOUNT_DELETED)

        self.assertIn(email_texts.GREETING_NEUTRAL, offered)
        self.assertNotIn(email_texts.GREETING, offered)
        self.assertEqual(email_texts.GREETING_NEUTRAL.name, email_texts.GREETING.name)
        self.assertEqual(
            sum(
                email_texts.GREETING in email_texts.placeholders_of(kind)
                for kind in MailJob.NOTIFICATION_KINDS
            ),
            len(MailJob.NOTIFICATION_KINDS) - 1,  # every notification but the deleted-account notice
        )

    def test_the_placeholder_is_refused_for_every_other_email(self):
        for key in Key.values:
            if key in {str(kind) for kind in MailJob.NOTIFICATION_KINDS}:
                continue
            with self.subTest(key=key):
                body = "{greeting}\n" + "\n".join(
                    "{%s}" % placeholder.name for placeholder in email_texts.required_placeholders(key)
                )
                with self.assertRaises(ValidationError) as caught:
                    email_texts.validate_body(key, body)
                self.assertIn("{greeting}", "\n".join(caught.exception.messages))

    def test_the_placeholder_is_refused_in_a_subject(self):
        with self.assertRaises(ValidationError):
            email_texts.validate_subject(Key.PASSWORD_CHANGED, "{greeting} at {instance_name}")

    def test_the_placeholder_is_accepted_for_the_notifications(self):
        for kind in MailJob.NOTIFICATION_KINDS:
            with self.subTest(kind=kind):
                body = "{greeting}\n\n" + "\n".join(
                    "{%s}" % placeholder.name for placeholder in email_texts.required_placeholders(kind)
                )
                email_texts.validate_body(kind, body)

    def test_the_default_of_every_notification_starts_with_the_placeholder(self):
        for kind in MailJob.NOTIFICATION_KINDS:
            with self.subTest(kind=kind):
                _subject, body = email_texts.default_texts(kind)
                self.assertTrue(body.startswith("{greeting}\n\n"), body[:40])
                self.assertEqual(body.count("{greeting}"), 1)

    def test_only_the_notification_templates_have_the_greeting_line(self):
        notifications = {str(kind) for kind in MailJob.NOTIFICATION_KINDS}
        templates = Path(settings.BASE_DIR) / "leg_platform" / "accounts" / "templates"
        for key, definition in email_texts.DEFINITIONS.items():
            text = (templates / definition.template).read_text(encoding="utf-8")
            with self.subTest(key=key):
                if key in notifications:
                    self.assertTrue(
                        text.replace("\r\n", "\n").startswith(
                            "{% load i18n %}{% autoescape off %}{{ greeting }}\n\n"
                        ),
                        key,
                    )
                    # The catalogue holds "Hello" only as the two short greeting texts.
                    self.assertNotIn("Hello", text)
                else:
                    self.assertNotIn("{{ greeting }}", text)

    def test_the_catalogue_has_the_two_greeting_texts_and_no_notification_starts_with_hello(self):
        catalogue = read_catalogue(PROJECT_CATALOGUE)
        self.assertEqual(catalogue["Hello"], "Guten Tag")
        self.assertEqual(catalogue["Hello %(name)s"], "Guten Tag %(name)s")
        # The remaining texts that start with "Hello" are the emails to unverified or foreign
        # addresses (registration, existing account, password reset, new address, second address).
        self.assertEqual(
            sorted(msgid.split("\n")[2][:30] for msgid in catalogue if msgid.startswith("Hello\n\n")),
            sorted(
                [
                    "A new password was requested f",
                    "An account was created for you",
                    "Please confirm your email addr",
                    "Someone asked at %(instance_na",
                    "Someone requested a change to ",
                    "Someone tried to change an acc",
                    "There is already an account fo",
                ]
            ),
        )

    def test_the_default_of_every_other_email_has_no_placeholder(self):
        for key in Key.values:
            if key in {str(kind) for kind in MailJob.NOTIFICATION_KINDS}:
                continue
            with self.subTest(key=key):
                _subject, body = email_texts.default_texts(key)
                self.assertNotIn("{greeting}", body)


@tag("mariadb")
class RenderedGreetingTests(TestCase):
    def test_the_value_exists_only_for_the_notification_emails(self):
        with translation.override("de-ch"):
            for key in Key.values:
                values = emails._values({}, key, "Maria Muster")
                with self.subTest(key=key):
                    if key == Key.ACCOUNT_DELETED:
                        # Never a name for a deleted account (D-068).
                        self.assertEqual(values["greeting"], "Guten Tag")
                    elif key in {str(kind) for kind in MailJob.NOTIFICATION_KINDS}:
                        self.assertEqual(values["greeting"], "Guten Tag Maria Muster")
                    else:
                        self.assertNotIn("greeting", values)

    def test_the_rest_of_the_default_text_is_unchanged_for_the_neutral_greeting(self):
        values = emails._values({}, Key.PASSWORD_CHANGED, "")
        with translation.override("de-ch"):
            subject, body = email_texts.compose(Key.PASSWORD_CHANGED, "", "", values)
        self.assertTrue(
            body.startswith(
                "Guten Tag\n\nDas Passwort Ihres Kontos bei LEG Platform wurde geändert. "
                "Alle anderen Sitzungen wurden beendet.\n\n"
            ),
            body,
        )

    def test_the_description_is_translated(self):
        with translation.override("de-ch"):
            self.assertEqual(
                str(email_texts.GREETING.description),
                "Anrede mit dem Namen der Person, der das Konto gehört, oder eine neutrale Anrede, "
                "wenn kein passender Name bekannt ist. Am besten auf einer eigenen Zeile am Anfang.",
            )
            self.assertEqual(
                str(email_texts.GREETING_NEUTRAL.description),
                "Neutrale Anrede ohne Namen: Der Name eines gelöschten Kontos wird nicht "
                "aufbewahrt. Die Vorschau und die Test-E-Mail zeigen dasselbe. Am besten auf "
                "einer eigenen Zeile am Anfang.",
            )


@tag("mariadb")
class NotificationGreetingTests(TestCase):
    """Name captured at queue time, used only for addresses of the account."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("person@example.org", first_name="Maria", last_name="Muster")

    def send_password_changed(self):
        password_reset.notify_password_changed(self.user)
        run_mail_queue()
        return mail.outbox[-1].body

    def test_a_person_is_greeted_by_name(self):
        body = self.send_password_changed()

        self.assertTrue(
            body.startswith("Guten Tag Maria Muster\n\nDas Passwort Ihres Kontos bei"), body
        )

    def test_every_notification_kind_greets_by_name_and_keeps_the_rest(self):
        senders = {
            MailJob.Kind.PASSWORD_CHANGED: "Das Passwort Ihres Kontos",
            MailJob.Kind.EMAIL_CHANGED: "Die E-Mail-Adresse Ihres Kontos",
            MailJob.Kind.SECONDARY_EMAIL_CHANGED: "Die zweite E-Mail-Adresse für Ihr Konto",
            MailJob.Kind.SESSION_ENDED_WRONG_CODES: "In einer Sitzung Ihres Kontos",
            MailJob.Kind.ALL_SESSIONS_ENDED: "In den Bestätigungen Ihres Kontos",
            MailJob.Kind.TWO_FACTOR_ENABLED: "Für Ihr Konto bei",
            MailJob.Kind.TWO_FACTOR_DISABLED: "Für Ihr Konto bei",
            MailJob.Kind.RECOVERY_CODES_RENEWED: "Für Ihr Konto bei",
            MailJob.Kind.RECOVERY_CODE_USED: "Zum Einloggen in Ihr Konto",
            MailJob.Kind.CODE_ATTEMPTS_WARNING: "Für Ihr Konto bei",
            MailJob.Kind.TWO_FACTOR_RESET: "Die Wiederherstellung der Anmeldung für Ihr Konto",
        }
        for kind, start in senders.items():
            with self.subTest(kind=kind):
                mail.outbox.clear()
                mail_queue.enqueue_for_account(kind, self.user)
                run_mail_queue()
                self.assertTrue(
                    mail.outbox[0].body.startswith("Guten Tag Maria Muster\n\n" + start),
                    mail.outbox[0].body,
                )

    def test_the_two_notifications_with_a_personal_link_greet_by_name(self):
        PendingEmailChange.objects.create(
            user=self.user,
            kind=PendingEmailChange.Kind.LOGIN,
            address="new.address@example.org",
            expires_at=timezone.now() + email_change.EMAIL_CHANGE_LIFETIME,
        )
        mail_queue.enqueue_for_account(MailJob.Kind.EMAIL_CHANGE_NOTICE, self.user)
        run_mail_queue()
        self.assertTrue(
            mail.outbox[0].body.startswith("Guten Tag Maria Muster\n\nFür Ihr Konto bei"),
            mail.outbox[0].body,
        )

        mail.outbox.clear()
        administrator_user = administrator("admin.link@example.org")
        with_second_factor(self.user)
        second_factor_reset.request_link(administrator_user, self.user)
        run_mail_queue()
        self.assertEqual(mail.outbox[0].to, ["person@example.org"])
        self.assertTrue(
            mail.outbox[0].body.startswith("Guten Tag Maria Muster\n\nDer Betreiber hat"),
            mail.outbox[0].body,
        )

    def test_the_name_is_stored_in_the_job_and_nothing_else_is_added(self):
        password_reset.notify_password_changed(self.user)

        job = MailJob.objects.get()
        self.assertEqual(job.greeting_name, "Maria Muster")
        self.assertEqual(job.address, "person@example.org")

    def test_a_company_only_account_gets_the_neutral_greeting(self):
        rename(self.user, organisation_name="Muster AG")

        body = self.send_password_changed()

        self.assertTrue(body.startswith("Guten Tag\n\nDas Passwort"), body)
        self.assertNotIn("Muster AG", body)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_stored_name_of_a_company_account_is_empty(self):
        rename(self.user, organisation_name="Muster AG")

        password_reset.notify_password_changed(self.user)

        self.assertEqual(MailJob.objects.get().greeting_name, "")

    def test_only_one_name_gives_the_neutral_greeting(self):
        rename(self.user, first_name="Maria")

        self.assertTrue(self.send_password_changed().startswith("Guten Tag\n\nDas Passwort"))

    def test_a_person_name_next_to_a_company_name_wins(self):
        rename(self.user, "Maria", "Muster", "Muster AG")

        self.assertTrue(self.send_password_changed().startswith("Guten Tag Maria Muster\n\n"))

    def test_an_unsuitable_name_gives_the_neutral_greeting(self):
        for first, last in (
            ("Maria3", "Muster"),
            ("Maria", "<script>"),
            ("www.example", "org"),
            ("Maria", "Mu@ster"),
        ):
            with self.subTest(first=first, last=last):
                rename(self.user, first, last)
                mail.outbox.clear()
                self.assertTrue(self.send_password_changed().startswith("Guten Tag\n\nDas Passwort"))

    def test_control_characters_and_newlines_never_reach_the_mail(self):
        rename(self.user, "Maria\nBcc: x", "Mu\x00ster")

        body = self.send_password_changed()

        self.assertTrue(body.startswith("Guten Tag\n\nDas Passwort"), body)
        self.assertNotIn("Bcc", body)

        rename(self.user, "Ma\u200bria\r\n", "Mu\x00ster")
        mail.outbox.clear()
        self.assertTrue(self.send_password_changed().startswith("Guten Tag Maria Muster\n\nDas"))

    def test_the_name_is_captured_when_the_job_is_queued(self):
        password_reset.notify_password_changed(self.user)
        rename(self.user, "Anna", "Beispiel")

        run_mail_queue()

        self.assertTrue(mail.outbox[0].body.startswith("Guten Tag Maria Muster\n\n"))

    def test_a_rename_before_queueing_is_used(self):
        rename(self.user, "Anna", "Beispiel")

        self.assertTrue(self.send_password_changed().startswith("Guten Tag Anna Beispiel\n\n"))

    def test_a_job_that_was_queued_before_the_account_was_deleted_loses_the_name(self):
        password_reset.notify_password_changed(self.user)
        self.user.delete()

        self.assertEqual(run_mail_queue(), 1)
        # The notice is still sent, with the neutral greeting (D-068, security review M1).
        self.assertTrue(mail.outbox[0].body.startswith("Guten Tag\n\nDas Passwort"), mail.outbox[0].body)
        self.assertNotIn("Maria", mail.outbox[0].body)
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_name_goes_with_the_job_when_it_is_purged(self):
        password_reset.notify_password_changed(self.user)
        MailJob.objects.update(created_at=timezone.now() - timedelta(hours=25))

        self.assertEqual(mail_queue.delete_stale_jobs(), 1)

        self.assertFalse(MailJob.objects.filter(greeting_name__gt="").exists())
        self.assertEqual(MailJob.objects.count(), 0)

    def test_the_name_goes_with_the_job_when_it_is_given_up(self):
        password_reset.notify_password_changed(self.user)

        with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("x")):
            with self.assertLogs("leg_platform.accounts.mail_queue", "ERROR") as logs:
                for _attempt in range(mail_queue.MAX_ATTEMPTS + 1):
                    MailJob.objects.update(next_attempt_at=timezone.now())
                    mail_queue.process_pending()

        self.assertEqual(MailJob.objects.count(), 0)
        self.assertNotIn("Maria", "\n".join(logs.output))
        self.assertNotIn("Muster", "\n".join(logs.output))

    def test_the_account_deleted_notice_is_always_neutral(self):
        addresses = self.user.notification_addresses()

        mail_queue.enqueue_for_account(
            MailJob.Kind.ACCOUNT_DELETED, self.user, addresses=addresses
        )

        self.assertEqual(MailJob.objects.get().greeting_name, "")
        run_mail_queue()
        body = mail.outbox[0].body
        self.assertTrue(body.startswith("Guten Tag\n\nIhr Konto bei"), body)
        self.assertNotIn("Maria", body)

    def test_enqueue_drops_a_name_for_a_request_kind(self):
        # A request has no account: no caller can store a name for it (security review L1).
        for kind in (
            MailJob.Kind.PASSWORD_RESET,
            MailJob.Kind.REGISTRATION,
            MailJob.Kind.EMAIL_CHANGE_REQUEST,
            MailJob.Kind.SECONDARY_EMAIL_REQUEST,
        ):
            with self.subTest(kind=kind):
                mail_queue.enqueue(kind, "person@example.org", greeting_name="Maria Muster")
        self.assertEqual(MailJob.objects.count(), 4)
        self.assertFalse(MailJob.objects.exclude(greeting_name="").exists())

    def test_enqueue_drops_a_name_for_the_account_deleted_notice(self):
        mail_queue.enqueue(
            MailJob.Kind.ACCOUNT_DELETED, "person@example.org", greeting_name="Maria Muster"
        )

        self.assertEqual(MailJob.objects.get().greeting_name, "")

    def test_enqueue_keeps_a_name_for_another_notification(self):
        mail_queue.enqueue(
            MailJob.Kind.PASSWORD_CHANGED, "person@example.org", greeting_name="Maria Muster"
        )

        self.assertEqual(MailJob.objects.get().greeting_name, "Maria Muster")

    def test_a_stored_name_that_is_not_clean_gives_the_neutral_greeting_in_the_mail(self):
        for stored in ("Maria\nBcc: x@example.org", "Maria Muster\r\nVisit", "Maria example.c\u0334om"):
            with self.subTest(stored=stored):
                mail.outbox.clear()
                # Written around the queue function (a data fix, a future caller).
                MailJob.objects.create(
                    kind=MailJob.Kind.PASSWORD_CHANGED,
                    address="person@example.org",
                    greeting_name=stored,
                    next_attempt_at=timezone.now(),
                )

                run_mail_queue()

                body = mail.outbox[0].body
                self.assertTrue(body.startswith("Guten Tag\n\nDas Passwort"), body)
                self.assertNotIn("Maria", body)

    def test_the_account_deleted_notice_is_neutral_even_if_a_job_holds_a_name(self):
        # A job written around the queue function: the sending function ignores the name.
        MailJob.objects.create(
            kind=MailJob.Kind.ACCOUNT_DELETED,
            address="person@example.org",
            greeting_name="Maria Muster",
            next_attempt_at=timezone.now(),
        )

        run_mail_queue()

        self.assertTrue(mail.outbox[0].body.startswith("Guten Tag\n\nIhr Konto bei"))

    def test_the_second_address_is_greeted_by_name_too(self):
        self.user.set_secondary_email(SECOND)

        password_reset.notify_password_changed(self.user)
        run_mail_queue()

        self.assertEqual({m.to[0] for m in mail.outbox}, {"person@example.org", SECOND})
        for message in mail.outbox:
            self.assertTrue(message.body.startswith("Guten Tag Maria Muster\n\n"), message.to)

    def test_a_removed_second_address_gets_the_neutral_greeting(self):
        self.user.set_secondary_email(SECOND)

        email_change.request_changes(None, self.user, remove_secondary=True)
        run_mail_queue()

        bodies = {message.to[0]: message.body for message in mail.outbox}
        self.assertEqual(set(bodies), {"person@example.org", SECOND})
        self.assertTrue(bodies["person@example.org"].startswith("Guten Tag Maria Muster\n\n"))
        self.assertTrue(bodies[SECOND].startswith("Guten Tag\n\nDie zweite E-Mail-Adresse"))
        self.assertNotIn("Maria", bodies[SECOND])

    def test_a_replaced_second_address_gets_the_neutral_greeting_and_the_login_address_the_name(self):
        self.user.set_secondary_email(SECOND)
        PendingEmailChange.objects.create(
            user=self.user,
            kind=PendingEmailChange.Kind.SECONDARY,
            address="newer.second@example.org",
            expires_at=timezone.now() + email_change.EMAIL_CHANGE_LIFETIME,
        )
        token = VerificationToken.objects.issue(
            self.user,
            VerificationToken.Purpose.SECONDARY_EMAIL,
            email_change.EMAIL_CHANGE_LIFETIME,
            target_email="newer.second@example.org",
        )
        stored = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.SECONDARY_EMAIL)

        email_change.confirm_change(stored)
        run_mail_queue()

        bodies = {message.to[0]: message.body for message in mail.outbox}
        self.assertTrue(bodies["person@example.org"].startswith("Guten Tag Maria Muster\n\n"))
        self.assertTrue(bodies[SECOND].startswith("Guten Tag\n\n"))

    def test_the_former_login_address_gets_the_neutral_greeting(self):
        self.user.set_secondary_email(SECOND)
        PendingEmailChange.objects.create(
            user=self.user,
            kind=PendingEmailChange.Kind.LOGIN,
            address="new.address@example.org",
            expires_at=timezone.now() + email_change.EMAIL_CHANGE_LIFETIME,
        )
        token = VerificationToken.objects.issue(
            self.user,
            VerificationToken.Purpose.EMAIL_CHANGE,
            email_change.EMAIL_CHANGE_LIFETIME,
            target_email="new.address@example.org",
        )
        stored = VerificationToken.objects.find_valid(token, VerificationToken.Purpose.EMAIL_CHANGE)

        self.assertIsNotNone(email_change.confirm_change(stored))
        run_mail_queue()

        bodies = {message.to[0]: message.body for message in mail.outbox}
        self.assertEqual(set(bodies), {"person@example.org", SECOND})
        self.assertTrue(
            bodies["person@example.org"].startswith("Guten Tag\n\nDie E-Mail-Adresse Ihres Kontos"),
            bodies["person@example.org"],
        )
        self.assertTrue(bodies[SECOND].startswith("Guten Tag Maria Muster\n\nDie E-Mail-Adresse"))

    def test_an_address_of_another_person_never_gets_the_name(self):
        mail_queue.enqueue_for_account(
            MailJob.Kind.PASSWORD_CHANGED, self.user, addresses=["someone.else@example.org"]
        )

        self.assertEqual(MailJob.objects.get().greeting_name, "")
        run_mail_queue()
        self.assertTrue(mail.outbox[0].body.startswith("Guten Tag\n\n"))

    def test_the_address_comparison_ignores_the_case(self):
        mail_queue.enqueue_for_account(
            MailJob.Kind.PASSWORD_CHANGED, self.user, addresses=["Person@Example.org"]
        )

        self.assertEqual(MailJob.objects.get().greeting_name, "Maria Muster")

    def test_the_requests_of_persons_without_an_account_never_get_a_name(self):
        rename(self.user, "Maria", "Muster")
        emails.send_registration_existing_account("person@example.org")
        emails.send_registration_verification("person@example.org", "T" * 43, timedelta(hours=24))
        emails.send_password_reset("person@example.org", "T" * 43, timedelta(hours=24))
        emails.send_email_change_existing_account("person@example.org")
        emails.send_email_change_verification("person@example.org", "T" * 43, timedelta(hours=24))
        emails.send_secondary_email_verification("person@example.org", "T" * 43, timedelta(hours=24))

        self.assertEqual(len(mail.outbox), 6)
        for message in mail.outbox:
            with self.subTest(subject=message.subject):
                self.assertNotIn("Maria", message.body)
                self.assertNotIn("Muster", message.body)

    def test_a_reset_request_through_the_queue_has_no_name(self):
        Client().post(
            reverse("accounts:password_reset"),
            reset_request_data("person@example.org"),
            REMOTE_ADDR="10.77.0.1",
        )
        self.assertEqual(MailJob.objects.get().greeting_name, "")

        run_mail_queue()

        self.assertNotIn("Maria", mail.outbox[0].body)

    def test_a_saved_text_without_the_placeholder_keeps_working_unchanged(self):
        EmailText.objects.update_or_create(
            key=Key.PASSWORD_CHANGED,
            defaults={"subject": "", "body": "Hallo\n\nIhr Passwort wurde geändert."},
        )

        body = self.send_password_changed()

        self.assertEqual(body, "Hallo\n\nIhr Passwort wurde geändert.\n")

    def test_a_saved_text_with_the_placeholder_is_filled_in(self):
        EmailText.objects.update_or_create(
            key=Key.PASSWORD_CHANGED,
            defaults={"subject": "", "body": "{greeting}\n\nIhr Passwort wurde geändert."},
        )

        self.assertEqual(
            self.send_password_changed(), "Guten Tag Maria Muster\n\nIhr Passwort wurde geändert.\n"
        )

    def test_the_placeholder_in_a_saved_text_of_another_email_is_not_filled_in(self):
        # Written around the validation: the saved text is refused at sending time.
        EmailText.objects.update_or_create(
            key=Key.REGISTRATION_EXISTING_ACCOUNT,
            defaults={"subject": "", "body": "{greeting}\n\nEs gibt schon ein Konto."},
        )

        with self.assertLogs("leg_platform.accounts.email_texts", "WARNING"):
            emails.send_registration_existing_account("person@example.org")

        self.assertNotIn("{greeting}", mail.outbox[0].body)
        self.assertNotIn("Maria", mail.outbox[0].body)
        self.assertNotIn("Es gibt schon ein Konto.", mail.outbox[0].body)

    def test_nothing_is_logged_about_a_successful_notification(self):
        with self.assertNoLogs(level="DEBUG"):
            self.send_password_changed()

    def test_a_failing_job_does_not_log_the_name_or_the_greeting(self):
        password_reset.notify_password_changed(self.user)

        with patch("leg_platform.accounts.emails.EmailMessage.send", side_effect=OSError("Maria Muster")):
            with self.assertLogs(level="DEBUG") as logs:
                mail_queue.process_pending()

        output = "\n".join(logs.output)
        self.assertNotIn("Maria", output)
        self.assertNotIn("Muster", output)
        self.assertNotIn("Guten Tag", output)

    def test_queueing_logs_nothing(self):
        with self.assertNoLogs(level="DEBUG"):
            password_reset.notify_password_changed(self.user)


@tag("mariadb")
class AdministrationGreetingTests(TestCase):
    client_class = VerifiedClient

    @classmethod
    def setUpTestData(cls):
        cls.administrator = create_active_user(
            "admin@example.org", first_name="Anna", last_name="Beispiel"
        )
        cls.administrator.set_privileges(is_staff=True, is_superuser=True)
        EmailText.objects.ensure_all()

    def setUp(self):
        self.client.force_login(self.administrator)

    def change_url(self, key):
        text = EmailText.objects.get(key=key)
        return reverse("admin:accounts_emailtext_change", args=(text.pk,))

    def test_the_editor_default_starts_with_the_placeholder(self):
        with translation.override("de-ch"):
            response = self.client.get(self.change_url(Key.PASSWORD_CHANGED))

        self.assertContains(response, "{greeting}")
        self.assertContains(response, "Anrede mit dem Namen der Person")

    def test_the_editor_of_another_email_does_not_offer_the_placeholder(self):
        with translation.override("de-ch"):
            response = self.client.get(self.change_url(Key.PASSWORD_RESET))

        self.assertNotContains(response, "{greeting}")
        self.assertNotContains(response, "Anrede mit dem Namen der Person")

    def test_the_placeholder_is_refused_when_saving_another_email(self):
        with translation.override("de-ch"):
            response = self.client.post(
                self.change_url(Key.REGISTRATION_VERIFICATION),
                {"subject": "", "body": "{greeting}\n\n{verification_url}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Der Platzhalter {greeting} existiert nicht")
        self.assertEqual(EmailText.objects.get(key=Key.REGISTRATION_VERIFICATION).body, "")

    def test_the_placeholder_is_saved_for_a_notification(self):
        response = self.client.post(
            self.change_url(Key.PASSWORD_CHANGED),
            {"subject": "", "body": "Hoi {greeting}\n\nGeändert."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            EmailText.objects.get(key=Key.PASSWORD_CHANGED).body, "Hoi {greeting}\n\nGeändert."
        )

    def test_the_unchanged_default_is_stored_as_empty(self):
        _subject, default = email_texts.default_texts(Key.PASSWORD_CHANGED)

        self.client.post(
            self.change_url(Key.PASSWORD_CHANGED), {"subject": "", "body": default}
        )

        self.assertEqual(EmailText.objects.get(key=Key.PASSWORD_CHANGED).body, "")

    def test_the_preview_uses_the_example_name(self):
        response = self.client.post(
            self.change_url(Key.PASSWORD_CHANGED),
            {"subject": "", "body": "{greeting}\n\nGeändert.", "_preview": "1"},
        )

        self.assertContains(response, "Guten Tag Maria Muster")
        self.assertNotContains(response, "Guten Tag Anna Beispiel")

    def test_the_preview_of_the_default_uses_the_example_name(self):
        response = self.client.post(
            self.change_url(Key.PASSWORD_CHANGED), {"subject": "", "body": "", "_preview": "1"}
        )

        self.assertContains(response, "Guten Tag Maria Muster")

    def test_the_test_email_uses_the_real_rule_on_the_own_account(self):
        self.client.post(
            self.change_url(Key.PASSWORD_CHANGED),
            {"subject": "", "body": "{greeting}\n\nGeändert.", "_send_test": "1"},
        )

        message = mail.outbox[0]
        self.assertEqual(message.to, ["admin@example.org"])
        self.assertIn("Guten Tag Anna Beispiel\n\nGeändert.", message.body)

    def test_the_test_email_is_neutral_if_the_own_name_is_unsuitable(self):
        rename(self.administrator, "Anna", "Beispiel2")

        self.client.post(
            self.change_url(Key.PASSWORD_CHANGED),
            {"subject": "", "body": "{greeting}\n\nGeändert.", "_send_test": "1"},
        )

        message = mail.outbox[0]
        self.assertIn("Guten Tag\n\nGeändert.", message.body)
        self.assertNotIn("Anna", message.body)

    def test_the_test_email_of_a_company_account_is_neutral(self):
        rename(self.administrator, organisation_name="Beispiel AG")

        self.client.post(
            self.change_url(Key.PASSWORD_CHANGED),
            {"subject": "", "body": "", "_send_test": "1"},
        )

        self.assertIn("Guten Tag\n\nDas Passwort", mail.outbox[0].body)
        self.assertNotIn("Beispiel AG", mail.outbox[0].body)

    def test_the_test_email_of_another_kind_has_no_name(self):
        self.client.post(
            self.change_url(Key.PASSWORD_RESET), {"subject": "", "body": "", "_send_test": "1"}
        )

        self.assertNotIn("Anna", mail.outbox[0].body)

    def test_the_editor_of_the_deleted_account_notice_says_that_the_greeting_is_neutral(self):
        with translation.override("de-ch"):
            response = self.client.get(self.change_url(Key.ACCOUNT_DELETED))

        self.assertContains(response, "{greeting}")
        self.assertContains(response, "Neutrale Anrede ohne Namen")
        self.assertNotContains(response, "Anrede mit dem Namen der Person")

    def test_the_preview_of_the_deleted_account_notice_is_neutral(self):
        for body in ("", "{greeting}\n\nGelöscht."):
            with self.subTest(body=body):
                response = self.client.post(
                    self.change_url(Key.ACCOUNT_DELETED),
                    {"subject": "", "body": body, "_preview": "1"},
                )

                self.assertContains(response, "Guten Tag")
                self.assertNotContains(response, "Maria Muster")
                self.assertNotContains(response, "Anna Beispiel")

    def test_the_test_email_of_the_deleted_account_notice_is_neutral(self):
        for body in ("", "{greeting}\n\nGelöscht."):
            with self.subTest(body=body):
                mail.outbox.clear()

                self.client.post(
                    self.change_url(Key.ACCOUNT_DELETED),
                    {"subject": "", "body": body, "_send_test": "1"},
                )

                message = mail.outbox[0]
                self.assertEqual(message.to, ["admin@example.org"])
                self.assertIn("Guten Tag\n\n", message.body)
                self.assertNotIn("Anna", message.body)
                self.assertNotIn("Beispiel", message.body)


@tag("mariadb")
class DeletionErasesTheNameTests(TestCase):
    """The name for the greeting does not outlive the account in a queued job (D-068, M1)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = create_active_user("person@example.org", first_name="Maria", last_name="Muster")
        cls.user.set_secondary_email(SECOND)
        cls.other = create_active_user("other.person@example.org", first_name="Otto", last_name="Anders")

    def setUp(self):
        self.client = VerifiedClient()
        self.client.force_login(self.user)
        # Notifications that were queued before the deletion: two jobs per kind for the account,
        # one for another person.
        password_reset.notify_password_changed(self.user)
        mail_queue.enqueue_for_account(MailJob.Kind.TWO_FACTOR_ENABLED, self.user)
        mail_queue.enqueue_for_account(MailJob.Kind.EMAIL_CHANGED, self.user)
        password_reset.notify_password_changed(self.other)

    def delete_account(self):
        return self.client.post(
            reverse("accounts:account_delete"), {"confirm": "on", "current_password": PASSWORD}
        )

    def test_the_queued_jobs_hold_the_names_before_the_deletion(self):
        self.assertEqual(MailJob.objects.filter(greeting_name="Maria Muster").count(), 6)
        self.assertEqual(MailJob.objects.filter(greeting_name="Otto Anders").count(), 1)

    def test_deleting_the_account_blanks_the_name_in_its_queued_jobs_only(self):
        response = self.delete_account()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(MailJob.objects.filter(greeting_name="Maria Muster").exists())
        # The jobs are kept (the notices are still sent); only the name is gone.
        own = MailJob.objects.filter(address__in=("person@example.org", SECOND))
        self.assertEqual(own.exclude(kind=MailJob.Kind.ACCOUNT_DELETED).count(), 6)
        self.assertFalse(own.exclude(greeting_name="").exists())
        # The jobs of another person are untouched.
        self.assertEqual(
            MailJob.objects.get(address="other.person@example.org").greeting_name, "Otto Anders"
        )

    def test_the_notices_are_still_sent_neutral_and_the_other_name_is_kept(self):
        self.delete_account()

        run_mail_queue()

        bodies = {}
        for message in mail.outbox:
            bodies.setdefault(message.to[0], []).append(message.body)
        self.assertEqual(set(bodies), {"person@example.org", SECOND, "other.person@example.org"})
        # Three queued notices and the notice about the deletion to each address of the account.
        self.assertEqual(len(bodies["person@example.org"]), 4)
        self.assertEqual(len(bodies[SECOND]), 4)
        for address in ("person@example.org", SECOND):
            for body in bodies[address]:
                with self.subTest(address=address):
                    self.assertTrue(body.startswith("Guten Tag\n\n"), body)
                    self.assertNotIn("Maria", body)
                    self.assertNotIn("Muster", body)
        self.assertTrue(bodies["other.person@example.org"][0].startswith("Guten Tag Otto Anders\n\n"))
        self.assertEqual(MailJob.objects.count(), 0)

    def test_an_account_that_is_kept_under_the_retention_obligation_loses_the_name_too(self):
        self.user.set_retention_obligation()

        self.delete_account()

        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.DEACTIVATED)
        self.assertFalse(MailJob.objects.filter(greeting_name="Maria Muster").exists())
        self.assertEqual(
            MailJob.objects.get(address="other.person@example.org").greeting_name, "Otto Anders"
        )

    def test_a_refused_deletion_keeps_the_names(self):
        response = self.client.post(
            reverse("accounts:account_delete"), {"confirm": "on", "current_password": "wrong"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(MailJob.objects.filter(greeting_name="Maria Muster").count(), 6)

    def test_the_administration_deletion_blanks_the_name_too(self):
        outcome = User.objects.get(pk=self.user.pk).delete_or_deactivate()

        self.assertEqual(outcome, "deleted")
        self.assertFalse(MailJob.objects.filter(greeting_name="Maria Muster").exists())
        self.assertEqual(MailJob.objects.filter(greeting_name="Otto Anders").count(), 1)

    def test_the_names_are_compared_normalised(self):
        MailJob.objects.create(
            kind=MailJob.Kind.PASSWORD_CHANGED,
            address="Person@Example.ORG",
            greeting_name="Maria Muster",
            next_attempt_at=timezone.now(),
        )

        self.user.delete_or_deactivate()

        self.assertFalse(MailJob.objects.filter(greeting_name="Maria Muster").exists())
