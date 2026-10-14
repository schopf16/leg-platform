"""Tests of the Microsoft Graph email backend and its certificate configuration (D-006).

No test contacts Microsoft: MSAL and the HTTP request are replaced by mocks. Addresses use the
reserved domain example.com (RFC 2606).
"""

import io
import json
import threading
import warnings
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import requests
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.contrib.messages import get_messages
from django.contrib.messages.constants import ERROR
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.translation import gettext, override

from leg_platform.accounts.encryption import DecryptionError, is_current
from leg_platform.accounts.models import GRAPH_CERTIFICATE_KEY_PURPOSE, GraphConfiguration
from leg_platform.accounts.tests.helpers import VerifiedClient, create_active_user
from leg_platform.mail import (
    GRAPH_TIMEOUT_SECONDS,
    GraphEmailBackend,
    GraphEmailError,
    _TokenSession,
    urlopen,
)

TENANT_ID = "6f1f6b5e-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
CLIENT_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SENDER = "sender@example.com"
TOKEN = "test-access-token-value"


def make_credentials(*, key_size=2048, valid_from_days=-1, valid_to_days=365):
    """Return (private key PEM, certificate PEM) as bytes for a self-signed test certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LEG-Platform test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=valid_from_days))
        .not_valid_after(now + timedelta(days=valid_to_days))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private_pem, certificate.public_bytes(serialization.Encoding.PEM)


def german(message_id):
    """Return the Swiss German text of an English message ID, as the users see it (de-ch)."""
    with override("de-ch"):
        return gettext(message_id)


class GraphConfigurationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_pem, cls.certificate_pem = make_credentials()

    def test_certificate_is_stored_with_encrypted_private_key(self):
        configuration = GraphConfiguration.load()
        configuration.set_certificate(self.private_pem, self.certificate_pem)
        stored = GraphConfiguration.objects.get(id=1)
        self.assertNotIn("PRIVATE KEY", stored.encrypted_private_key)
        self.assertEqual(stored.certificate_pem, self.certificate_pem.decode("ascii"))
        self.assertIsNotNone(stored.certificate_expires_on)
        self.assertEqual(stored.private_key_pem(), self.private_pem)

    def test_key_must_belong_to_certificate(self):
        other_private_pem, _other_certificate = make_credentials()
        with self.assertRaisesMessage(
            ValidationError, german("The private key does not belong to the certificate.")
        ):
            GraphConfiguration.load().set_certificate(other_private_pem, self.certificate_pem)
        self.assertEqual(GraphConfiguration.objects.get(id=1).certificate_pem, "")

    def test_expired_certificate_is_refused(self):
        private_pem, certificate_pem = make_credentials(valid_from_days=-10, valid_to_days=-5)
        with self.assertRaisesMessage(ValidationError, german("The certificate has expired.")):
            GraphConfiguration.load().set_certificate(private_pem, certificate_pem)

    def test_weak_key_is_refused(self):
        private_pem, certificate_pem = make_credentials(key_size=1024)
        with self.assertRaisesMessage(
            ValidationError,
            german("The private key must be an RSA key with at least 2048 bits."),
        ):
            GraphConfiguration.load().set_certificate(private_pem, certificate_pem)

    def test_unreadable_pem_is_refused(self):
        with self.assertRaisesMessage(
            ValidationError,
            german("The certificate or the private key cannot be read (PEM, no password)."),
        ):
            GraphConfiguration.load().set_certificate(b"not a key", self.certificate_pem)

    def test_unsupported_key_algorithm_is_refused_with_the_generic_message(self):
        with mock.patch(
            "leg_platform.accounts.models.serialization.load_pem_private_key",
            side_effect=UnsupportedAlgorithm("test"),
        ):
            with self.assertRaisesMessage(
                ValidationError,
                german("The certificate or the private key cannot be read (PEM, no password)."),
            ):
                GraphConfiguration.check_certificate(self.private_pem, self.certificate_pem)

    def test_non_ascii_certificate_is_refused_and_not_stored(self):
        with self.assertRaisesMessage(
            ValidationError,
            german("The certificate or the private key cannot be read (PEM, no password)."),
        ):
            GraphConfiguration.load().set_certificate(
                self.private_pem, b"\xc3\xa9" + self.certificate_pem
            )
        self.assertEqual(GraphConfiguration.objects.get(id=1).certificate_pem, "")

    def test_non_ascii_private_key_is_refused_and_not_stored(self):
        with self.assertRaisesMessage(
            ValidationError,
            german("The certificate or the private key cannot be read (PEM, no password)."),
        ):
            GraphConfiguration.load().set_certificate(
                b"\xc3\xa9\n" + self.private_pem, self.certificate_pem
            )
        self.assertEqual(GraphConfiguration.objects.get(id=1).encrypted_private_key, "")

    def test_key_encrypted_with_earlier_secret_key_is_encrypted_again(self):
        old_secret = "old-secret-key-for-the-test-only-0123456789abcdef"
        with override_settings(SECRET_KEY=old_secret, SECRET_KEY_FALLBACKS=[]):
            GraphConfiguration.load().set_certificate(self.private_pem, self.certificate_pem)
            previous = GraphConfiguration.objects.get(id=1).encrypted_private_key
        with override_settings(SECRET_KEY="new-" + old_secret, SECRET_KEY_FALLBACKS=[old_secret]):
            self.assertFalse(is_current(previous, GRAPH_CERTIFICATE_KEY_PURPOSE))
            self.assertEqual(GraphConfiguration.objects.get(id=1).private_key_pem(), self.private_pem)
            reloaded = GraphConfiguration.objects.get(id=1)
            self.assertNotEqual(reloaded.encrypted_private_key, previous)
            self.assertTrue(is_current(reloaded.encrypted_private_key, GRAPH_CERTIFICATE_KEY_PURPOSE))

    def test_undecryptable_key_raises_decryption_error(self):
        configuration = GraphConfiguration.load()
        configuration.set_certificate(self.private_pem, self.certificate_pem)
        with override_settings(SECRET_KEY="another-secret-key-for-the-test-only-0123456789"):
            with self.assertRaises(DecryptionError):
                GraphConfiguration.objects.get(id=1).private_key_pem()


@override_settings(EMAIL_MODE="graph", EMAIL_ALLOWED_RECIPIENTS=[], DEFAULT_FROM_EMAIL=SENDER)
class GraphEmailBackendTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_pem, cls.certificate_pem = make_credentials()

    def setUp(self):
        configuration = GraphConfiguration.load()
        configuration.tenant_id = TENANT_ID
        configuration.client_id = CLIENT_ID
        configuration.sender_address = SENDER
        configuration.save()
        configuration.set_certificate(self.private_pem, self.certificate_pem)

        self.application = mock.Mock()
        self.application.acquire_token_for_client.return_value = {"access_token": TOKEN}
        patcher = mock.patch(
            "leg_platform.mail.msal.ConfidentialClientApplication",
            return_value=self.application,
        )
        self.msal = patcher.start()
        self.addCleanup(patcher.stop)

        self.response = mock.MagicMock()
        self.response.status = 202
        patcher = mock.patch("leg_platform.mail.urlopen")
        self.urlopen = patcher.start()
        self.urlopen.return_value.__enter__.return_value = self.response
        self.addCleanup(patcher.stop)

    def send(self, *messages, **options):
        # fail_silently is passed only where a test needs it: Django 6.1 warns about unknown
        # keyword arguments of a backend (RemovedInDjango2028Warning), which the test runs treat
        # as errors; the warning is irrelevant to what the test checks, and Django 6.0 has none.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            backend = GraphEmailBackend(**options)
        return backend.send_messages(list(messages))

    def message(self, to=("recipient@example.com",), **kwargs):
        return mail.EmailMessage("Subject", "Body text", None, list(to), **kwargs)

    def sent_payload(self):
        request = self.urlopen.call_args.args[0]
        return request, json.loads(request.data.decode("utf-8"))

    def test_sends_with_certificate_token_and_graph_payload(self):
        self.assertEqual(self.send(self.message()), 1)

        kwargs = self.msal.call_args.kwargs
        self.assertEqual(self.msal.call_args.args, (CLIENT_ID,))
        self.assertEqual(kwargs["authority"], f"https://login.microsoftonline.com/{TENANT_ID}")
        self.assertEqual(kwargs["client_credential"]["public_certificate"], self.certificate_pem.decode())
        self.assertTrue(kwargs["client_credential"]["private_key"].startswith("-----BEGIN PRIVATE KEY-----"))
        self.application.acquire_token_for_client.assert_called_once_with(
            scopes=["https://graph.microsoft.com/.default"]
        )

        request, payload = self.sent_payload()
        self.assertEqual(request.full_url, "https://graph.microsoft.com/v1.0/users/sender%40example.com/sendMail")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(self.urlopen.call_args.kwargs["timeout"], GRAPH_TIMEOUT_SECONDS)
        self.assertIs(payload["saveToSentItems"], False)
        self.assertEqual(payload["message"]["subject"], "Subject")
        self.assertEqual(payload["message"]["body"], {"contentType": "Text", "content": "Body text"})
        self.assertEqual(
            payload["message"]["toRecipients"],
            [{"emailAddress": {"address": "recipient@example.com"}}],
        )
        self.assertEqual(payload["message"]["ccRecipients"], [])
        self.assertEqual(payload["message"]["bccRecipients"], [])

    def test_html_alternative_is_sent_as_html(self):
        message = mail.EmailMultiAlternatives("Subject", "Text part", None, ["recipient@example.com"])
        message.attach_alternative("<p>HTML part</p>", "text/html")
        self.send(message)
        _request, payload = self.sent_payload()
        self.assertEqual(payload["message"]["body"], {"contentType": "HTML", "content": "<p>HTML part</p>"})

    def test_empty_list_sends_nothing(self):
        self.assertEqual(self.send(), 0)
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    def test_not_configured_refuses_to_send(self):
        GraphConfiguration.objects.all().delete()
        with self.assertRaisesMessage(GraphEmailError, "not configured"):
            self.send(self.message())
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    def test_fail_silently_is_not_supported(self):
        GraphConfiguration.objects.all().delete()
        with self.assertRaises(GraphEmailError):
            self.send(self.message(), fail_silently=True)

    def test_graph_refusal_is_raised_without_token_or_body(self):
        body = json.dumps({"error": {"code": "ErrorAccessDenied", "message": "Denied for sender"}})
        self.urlopen.side_effect = HTTPError(
            "https://graph.microsoft.com", 403, "Forbidden", {}, io.BytesIO(body.encode())
        )
        with self.assertRaises(GraphEmailError) as context:
            self.send(self.message())
        text = str(context.exception)
        self.assertIn("HTTP 403", text)
        self.assertIn("ErrorAccessDenied", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("Denied for sender", text)

    def test_refusal_without_error_body_is_raised(self):
        self.urlopen.side_effect = HTTPError(
            "https://graph.microsoft.com", 500, "Server Error", {}, io.BytesIO(b"<html>")
        )
        with self.assertRaisesMessage(GraphEmailError, "error: unknown"):
            self.send(self.message())

    def test_network_error_is_raised(self):
        self.urlopen.side_effect = URLError("no route to host")
        with self.assertRaisesMessage(GraphEmailError, "could not be reached"):
            self.send(self.message())

    def test_status_other_than_202_is_rejected(self):
        self.response.status = 200
        with self.assertRaisesMessage(GraphEmailError, "instead of 202"):
            self.send(self.message())

    def test_token_failure_raises_error_code_only(self):
        self.application.acquire_token_for_client.return_value = {
            "error": "invalid_client",
            "error_description": "AADSTS700027 tenant internal detail",
        }
        with self.assertRaises(GraphEmailError) as context:
            self.send(self.message())
        self.assertIn("invalid_client", str(context.exception))
        self.assertNotIn("AADSTS700027", str(context.exception))
        self.urlopen.assert_not_called()

    @override_settings(EMAIL_MODE="allowlist", EMAIL_ALLOWED_RECIPIENTS=["allowed@example.com"])
    def test_allowlist_discards_message_without_allowed_to_recipient(self):
        self.assertEqual(self.send(self.message(to=["other@example.com"])), 0)
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    @override_settings(EMAIL_MODE="allowlist", EMAIL_ALLOWED_RECIPIENTS=["allowed@example.com"])
    def test_allowlist_keeps_only_allowed_recipients(self):
        message = self.message(
            to=["ALLOWED@example.com", "other@example.com"],
            cc=["other@example.com"],
            bcc=["allowed@example.com", "third@example.com"],
        )
        self.assertEqual(self.send(message), 1)
        _request, payload = self.sent_payload()
        self.assertEqual(
            payload["message"]["toRecipients"],
            [{"emailAddress": {"address": "ALLOWED@example.com"}}],
        )
        self.assertEqual(payload["message"]["ccRecipients"], [])
        self.assertEqual(
            payload["message"]["bccRecipients"],
            [{"emailAddress": {"address": "allowed@example.com"}}],
        )

    def test_attachments_are_refused_before_anything_is_sent(self):
        valid = self.message()
        with_attachment = self.message()
        with_attachment.attach("notes.txt", "content", "text/plain")
        with self.assertRaisesMessage(GraphEmailError, "Attachments"):
            self.send(valid, with_attachment)
        self.urlopen.assert_not_called()

    def test_reply_to_is_refused(self):
        with self.assertRaisesMessage(GraphEmailError, "Reply-To"):
            self.send(self.message(reply_to=["reply@example.com"]))
        self.urlopen.assert_not_called()

    def test_message_without_to_recipient_is_refused(self):
        with self.assertRaisesMessage(GraphEmailError, "To recipient"):
            self.send(self.message(to=[], bcc=["recipient@example.com"]))
        self.urlopen.assert_not_called()

    @override_settings(DEFAULT_FROM_EMAIL="other@example.com")
    def test_sender_differing_from_configuration_is_refused(self):
        with self.assertRaisesMessage(GraphEmailError, "sender address differs"):
            self.send(self.message())
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    def test_auto_submitted_header_is_not_sent_and_is_logged(self):
        for name in ("Auto-Submitted", "auto-submitted"):
            with self.subTest(name=name):
                self.urlopen.reset_mock()
                message = self.message(headers={name: "auto-generated"})
                with self.assertLogs("leg_platform.mail", level="WARNING") as logs:
                    self.send(message)
                _request, payload = self.sent_payload()
                self.assertNotIn("internetMessageHeaders", payload["message"])
                self.assertIn("Auto-Submitted", logs.output[0])

    def test_token_request_uses_session_that_refuses_redirects(self):
        self.send(self.message())

        session = self.msal.call_args.kwargs["http_client"]
        self.assertIsInstance(session, _TokenSession)
        adapter = session.get_adapter("https://login.microsoftonline.com/")
        self.assertEqual(adapter.max_retries.total, 1)
        self.assertNotIn("timeout", self.msal.call_args.kwargs)

    def test_token_request_failure_is_raised_without_details(self):
        for error in (ValueError("tenant detail in response"), OSError("no route")):
            with self.subTest(error=type(error).__name__):
                self.application.acquire_token_for_client.side_effect = error
                with self.assertRaises(GraphEmailError) as context:
                    self.send(self.message())
                self.assertIn("did not answer the token request", str(context.exception))
                self.assertNotIn("tenant detail", str(context.exception))
                self.urlopen.assert_not_called()

    def test_non_ascii_private_key_is_refused_without_token_request(self):
        # The key is ASCII PEM by definition; a non-ASCII key must not reach the MSAL call.
        with mock.patch.object(GraphConfiguration, "private_key_pem", return_value=b"\xc3\xa9"):
            with self.assertRaises(GraphEmailError) as context:
                self.send(self.message())
        self.assertEqual(
            str(context.exception),
            "The Microsoft Graph private key cannot be read; no email was sent.",
        )
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    def test_discovery_failure_in_constructor_is_raised_without_tenant_or_url(self):
        # MSAL runs the tenant discovery in its constructor; its error text can contain the URL.
        self.msal.side_effect = OSError(
            f"could not reach https://login.microsoftonline.com/{TENANT_ID}"
        )
        with self.assertRaises(GraphEmailError) as context:
            self.send(self.message())
        text = str(context.exception)
        self.assertEqual(
            text, "Microsoft did not answer the token request; no email was sent."
        )
        self.assertNotIn(TENANT_ID, text)
        self.assertNotIn("login.microsoftonline.com", text)
        self.urlopen.assert_not_called()

    def test_other_error_in_constructor_is_raised_without_tenant_or_url(self):
        # Not OSError or ValueError: msal/authority.py line 305 raises RuntimeError.
        self.msal.side_effect = RuntimeError(
            f"Unable to complete OIDC Discovery at https://login.microsoftonline.com/{TENANT_ID}"
        )
        with self.assertRaises(GraphEmailError) as context:
            self.send(self.message())
        text = str(context.exception)
        self.assertEqual(
            text, "Microsoft did not answer the token request; no email was sent."
        )
        self.assertNotIn(TENANT_ID, text)
        self.assertNotIn("login.microsoftonline.com", text)
        self.assertTrue(context.exception.__suppress_context__)
        self.urlopen.assert_not_called()

    def test_error_response_is_closed_after_the_error_code_is_read(self):
        body = io.BytesIO(json.dumps({"error": {"code": "ErrorAccessDenied"}}).encode("utf-8"))
        self.urlopen.side_effect = HTTPError(
            "https://graph.microsoft.com/v1.0/users/sender/sendMail", 403, "Forbidden", {}, body
        )
        with self.assertRaisesMessage(GraphEmailError, "ErrorAccessDenied"):
            self.send(self.message())
        self.assertTrue(body.closed)


class TokenSessionTests(SimpleTestCase):
    def test_every_request_gets_the_graph_timeout_and_no_redirects(self):
        with mock.patch.object(requests.Session, "request") as parent:
            _TokenSession().post("https://login.microsoftonline.com/example", data=b"{}")
        self.assertIs(parent.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(parent.call_args.kwargs["timeout"], GRAPH_TIMEOUT_SECONDS)

    def test_explicit_timeout_none_is_replaced_by_the_graph_timeout(self):
        with mock.patch.object(requests.Session, "request") as parent:
            _TokenSession().post(
                "https://login.microsoftonline.com/example", data=b"{}", timeout=None
            )
        self.assertEqual(parent.call_args.kwargs["timeout"], GRAPH_TIMEOUT_SECONDS)

    def test_redirect_of_the_token_request_is_returned_and_not_followed(self):
        requested = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                # An unread request body makes Windows reset the connection before the response.
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requested.append(self.path)
                self.send_response(302 if self.path == "/start" else 200)
                self.send_header("Location", "/second")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/start"

        response = _TokenSession().post(url, data=b"client_assertion=test")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(requested, ["/start"])


@override_settings(EMAIL_MODE="console", EMAIL_ALLOWED_RECIPIENTS=[], DEFAULT_FROM_EMAIL=SENDER)
class GraphAdminTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_pem, cls.certificate_pem = make_credentials()

    def setUp(self):
        configuration = GraphConfiguration.load()
        configuration.tenant_id = TENANT_ID
        configuration.client_id = CLIENT_ID
        configuration.sender_address = SENDER
        configuration.save()
        configuration.set_certificate(self.private_pem, self.certificate_pem)

        self.superuser = create_active_user("system.admin@example.org")
        self.superuser.set_privileges(is_staff=True, is_superuser=True)
        self.staff = create_active_user("staff.user@example.org")
        self.staff.set_privileges(is_staff=True, is_superuser=False)

        patcher = mock.patch("leg_platform.mail.msal.ConfidentialClientApplication")
        self.msal = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("leg_platform.mail.urlopen")
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_staff_user_without_permission_gets_403_and_no_row_is_created(self):
        GraphConfiguration.objects.all().delete()
        client = VerifiedClient()
        client.force_login(self.staff)

        response = client.get(reverse("admin:accounts_graphconfiguration_change", args=(1,)))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(GraphConfiguration.objects.exists())

    def test_console_mode_refuses_the_test_email_without_contacting_microsoft(self):
        client = VerifiedClient()
        client.force_login(self.superuser)

        response = client.get(reverse("admin:accounts_graphconfiguration_send_test"))

        self.assertEqual(
            response.context["refusal"],
            german("In console mode, no test email is sent, so the Microsoft Graph delivery is not tested."),
        )
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()

    def test_post_to_send_test_in_console_mode_is_refused_without_contacting_microsoft(self):
        client = VerifiedClient()
        client.force_login(self.superuser)

        with mock.patch("leg_platform.accounts.admin.GraphEmailBackend") as backend:
            response = client.post(reverse("admin:accounts_graphconfiguration_send_test"))

        self.assertRedirects(
            response,
            reverse("admin:accounts_graphconfiguration_change", args=(1,)),
            fetch_redirect_response=False,
        )
        backend.assert_not_called()
        self.msal.assert_not_called()
        self.urlopen.assert_not_called()
        stored = [(m.level, str(m)) for m in get_messages(response.wsgi_request)]
        self.assertEqual(
            stored,
            [
                (
                    ERROR,
                    german(
                        "In console mode, no test email is sent, so the Microsoft Graph "
                        "delivery is not tested."
                    ),
                )
            ],
        )


class GraphRedirectTests(SimpleTestCase):
    def test_redirect_is_refused_and_not_followed(self):
        requested = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                # An unread request body makes Windows reset the connection before the response.
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requested.append(self.path)
                self.send_response(302 if self.path == "/start" else 202)
                self.send_header("Location", "/second")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/start"

        with self.assertRaises(HTTPError) as context:
            urlopen(Request(url, data=b"{}", method="POST"), timeout=5)
        self.assertEqual(context.exception.code, 302)
        self.assertEqual(requested, ["/start"])
