"""Recovery of a lost sign-in method by email (D-106): the link for an account with nothing entered, the instructions
for every other account. An address without an active account gets nothing, and the page answers the same way."""

from django.core import mail
from django.test import Client, TestCase, tag
from django.urls import reverse

from leg_platform.accounts import second_factor_reset
from leg_platform.accounts.models import (
    AuthenticatorDevice,
    MailJob,
    Passkey,
    SecurityEvent,
    User,
    VerificationToken,
)
from leg_platform.accounts.tests.helpers import PASSWORD, create_active_user, reset_request_data, run_mail_queue
from leg_platform.accounts.tests.test_second_factor_reset import administrator, link_in, with_second_factor
from leg_platform.community import own_iban
from leg_platform.community.models import CustomerBuilding
from leg_platform.community.tests.helpers import assign_person, make_location, make_metering_point
from leg_platform.community.tests.onboarding import GOOD_IBAN, start_with_building

RECOVERY_URL = reverse("accounts:recovery")
RESET_URL = reverse("accounts:password_reset")
SENT_URL = reverse("accounts:recovery_sent")
Purpose = VerificationToken.Purpose

_address = iter(range(1, 250))
_number = iter(range(1, 10_000))


def ask(url, email):
    """The form of the page, posted from its own client address (the limits are per address)."""
    return Client().post(url, reset_request_data(email), REMOTE_ADDR=f"10.140.0.{next(_address)}")


def passkey_account(email):
    """An account that signs in with a passkey: an active credential, no password."""
    user = create_active_user(email)
    Passkey.objects.create(
        user=user, credential_digest=f"{user.pk:064d}", credential_id=f"{user.pk}".encode(), public_key=b"k",
        sign_count=0, name="Key", active=True, backup_eligible=False, backed_up=False,
    )
    user.set_unusable_password()
    user._change(authentication_method=User.AuthenticationMethod.PASSKEY, password=user.password)
    return user


def app_account(email):
    user = create_active_user(email)
    with_second_factor(user)
    return user


def accounts():
    """One account per sign-in method, without anything entered."""
    return {
        "password only": create_active_user("password.person@example.org"),
        "password and app": app_account("app.person@example.org"),
        "passkey": passkey_account("passkey.person@example.org"),
    }


def add_building_draft(user):
    start_with_building(user)


def add_building_label(user):
    CustomerBuilding.objects.create(person=user, location=make_location(house_number=str(next(_number))))


def add_iban(user):
    own_iban.set_own_iban(user, GOOD_IBAN)


def add_assignment(user):
    number = next(_number)
    assign_person(make_metering_point(number, location=make_location(house_number=str(number))), user)


ENTRIES = {
    "a draft with a building": add_building_draft,
    "a name for a building": add_building_label,
    "an IBAN": add_iban,
    "a metering point of the LEG": add_assignment,
}


def link_mails():
    return [message for message in mail.outbox if "/two-factor-reset/verify/" in message.body]


@tag("mariadb")
class SelfRecoveryTests(TestCase):
    databases = {"default", "addresses"}

    def test_an_account_with_nothing_entered_gets_the_link_whatever_its_method(self):
        for name, user in accounts().items():
            with self.subTest(method=name):
                mail.outbox.clear()
                response = ask(RECOVERY_URL, user.email)
                self.assertRedirects(response, SENT_URL, fetch_redirect_response=False)
                run_mail_queue()

                (message,) = link_mails()
                self.assertEqual(message.to, [user.email])
                path = link_in(message)
                token = VerificationToken.objects.get(user=user)
                self.assertEqual(token.purpose, Purpose.SELF_RECOVERY)
                # The request changes nothing: the methods stay until the link is confirmed.
                user.refresh_from_db()
                self.assertNotEqual(user.authentication_method, User.AuthenticationMethod.RECOVERY)
                entry = SecurityEvent.objects.filter(account_id=user.pk, event="two_factor_reset_requested").get()
                self.assertIsNone(entry.actor_id)

                owner = Client()
                opened = owner.get(path)
                self.assertEqual(opened.status_code, 302)
                self.assertEqual(owner.get(opened["Location"]).status_code, 200)
                done = owner.post(opened["Location"])

                self.assertRedirects(done, reverse("accounts:login_method"), fetch_redirect_response=False)
                user.refresh_from_db()
                self.assertEqual(user.authentication_method, User.AuthenticationMethod.RECOVERY)
                self.assertFalse(user.passkeys.exists())
                self.assertFalse(AuthenticatorDevice.objects.filter(user=user).exists())
                self.assertFalse(user.has_usable_password())
                self.assertFalse(VerificationToken.objects.filter(user=user).exists())
                # Every address of the account is told (the notice is a job of its own, D-106).
                self.assertTrue(MailJob.objects.filter(kind=MailJob.Kind.TWO_FACTOR_RESET, address=user.email).exists())
                run_mail_queue()
                self.assertTrue(SecurityEvent.objects.filter(account_id=user.pk, event="two_factor_reset").exists())

    def test_the_password_reset_page_gives_a_passkey_account_the_same_answer(self):
        user = passkey_account("passkey.person@example.org")

        response = ask(RESET_URL, user.email)
        self.assertRedirects(response, reverse("accounts:password_reset_sent"), fetch_redirect_response=False)
        run_mail_queue()

        (message,) = mail.outbox
        self.assertEqual(VerificationToken.objects.get(user=user).purpose, Purpose.SELF_RECOVERY)
        self.assertTrue(link_in(message))

    def test_the_password_reset_of_a_password_account_is_unchanged(self):
        user = create_active_user("password.person@example.org")

        ask(RESET_URL, user.email)
        run_mail_queue()

        (message,) = mail.outbox
        self.assertIn("/password-reset/verify/", message.body)
        self.assertEqual(VerificationToken.objects.get(user=user).purpose, Purpose.PASSWORD_RESET)

    def test_an_account_with_an_entry_gets_the_instructions_and_no_link(self):
        for entry, add in ENTRIES.items():
            for url in (RECOVERY_URL, RESET_URL):
                with self.subTest(entry=entry, page=url):
                    user = passkey_account(f"entry.{next(_number)}@example.org")
                    add(user)
                    mail.outbox.clear()

                    ask(url, user.email)
                    run_mail_queue()

                    (message,) = mail.outbox
                    self.assertNotIn("/verify/", message.body)
                    self.assertIn("Ausweis", message.body)
                    self.assertFalse(VerificationToken.objects.filter(user=user).exists())

    def test_staff_and_administrators_get_the_instructions_even_with_nothing_entered(self):
        staff = create_active_user("staff.person@example.org")
        with_second_factor(staff)
        staff.set_privileges(is_staff=True, is_superuser=False)
        for user in (staff, administrator("admin.person@example.org")):
            with self.subTest(user=user.email):
                mail.outbox.clear()
                ask(RECOVERY_URL, user.email)
                run_mail_queue()

                (message,) = mail.outbox
                self.assertNotIn("/two-factor-reset/verify/", message.body)
                self.assertFalse(VerificationToken.objects.filter(user=user).exists())
                self.assertFalse(second_factor_reset.may_recover_by_mail(user))

    def test_an_address_without_an_active_account_gets_nothing_and_the_page_does_not_tell(self):
        blocked = create_active_user("blocked.person@example.org")
        blocked.set_status(User.Status.BLOCKED)
        known = create_active_user("known.person@example.org")

        answers = {}
        for name, email in {
            "unknown": "nobody@example.org", "blocked": blocked.email, "known": known.email,
        }.items():
            for url in (RECOVERY_URL, RESET_URL):
                response = ask(url, email)
                answers[(name, url)] = (response.status_code, response["Location"])
        run_mail_queue()

        for url in (RECOVERY_URL, RESET_URL):
            self.assertEqual(len({answers[(name, url)] for name in ("unknown", "blocked", "known")}), 1)
        # Only the known account got mail (one per page).
        self.assertEqual({message.to[0] for message in mail.outbox}, {known.email})

    def test_the_link_ends_as_soon_as_something_is_entered(self):
        user = passkey_account("passkey.person@example.org")
        ask(RECOVERY_URL, user.email)
        run_mail_queue()
        path = link_in(mail.outbox[0])
        owner = Client()
        opened = owner.get(path)
        self.assertEqual(owner.get(opened["Location"]).status_code, 200)

        # The account is used in another session while the link is open.
        add_iban(user)

        confirm = owner.post(opened["Location"])
        self.assertRedirects(confirm, reverse("accounts:two_factor_reset_invalid"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.authentication_method, User.AuthenticationMethod.PASSKEY)
        self.assertTrue(user.passkeys.filter(active=True).exists())
        self.assertRedirects(
            Client().get(path), reverse("accounts:two_factor_reset_invalid"), fetch_redirect_response=False
        )

    def test_a_link_is_not_sent_when_something_was_entered_after_the_request(self):
        user = passkey_account("passkey.person@example.org")
        ask(RECOVERY_URL, user.email)
        add_iban(user)

        run_mail_queue()

        self.assertEqual(link_mails(), [])
        self.assertFalse(VerificationToken.objects.filter(user=user).exists())

    def test_a_newer_link_replaces_the_older_one(self):
        user = passkey_account("passkey.person@example.org")
        ask(RECOVERY_URL, user.email)
        run_mail_queue()
        ask(RESET_URL, user.email)
        run_mail_queue()

        first, second = (link_in(message) for message in link_mails())

        self.assertEqual(VerificationToken.objects.filter(user=user).count(), 1)
        self.assertRedirects(Client().get(first), reverse("accounts:two_factor_reset_invalid"), fetch_redirect_response=False)
        self.assertEqual(Client().get(second).status_code, 302)

    def test_the_link_of_the_staff_still_works_for_an_account_with_entries(self):
        user = passkey_account("passkey.person@example.org")
        add_iban(user)
        second_factor_reset.request_link(administrator("admin.person@example.org"), user)
        run_mail_queue()

        (message,) = mail.outbox
        token = VerificationToken.objects.get(user=user)
        owner = Client()
        opened = owner.get(link_in(message))
        done = owner.post(opened["Location"])

        self.assertEqual(token.purpose, Purpose.TWO_FACTOR_RESET)
        self.assertRedirects(done, reverse("accounts:login_method"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.authentication_method, User.AuthenticationMethod.RECOVERY)

    def test_the_session_after_the_link_is_restricted(self):
        user = create_active_user("password.person@example.org")
        ask(RECOVERY_URL, user.email)
        run_mail_queue()
        owner = Client()
        opened = owner.get(link_in(mail.outbox[0]))
        owner.post(opened["Location"])

        # Only the choice of a new method; nothing else of the account opens (D-106).
        self.assertEqual(owner.get(reverse("accounts:profile")).status_code, 302)
        self.assertEqual(Client().post(reverse("accounts:login"), {"username": user.email, "password": PASSWORD}).status_code, 200)
