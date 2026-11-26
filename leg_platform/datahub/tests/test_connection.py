"""The connection test against a fake FTPES client (D-139): no network access in tests."""

import ftplib
import socket
import ssl
import threading
from unittest.mock import patch

from django.test import SimpleTestCase

from leg_platform.datahub import connection
from leg_platform.datahub.models import DatahubConfiguration

Outcome = DatahubConfiguration.TestOutcome
USERNAME = "leg-user-4711"
PASSWORD = "Secret-Password-9"


class FakeClient:
    """Records the commands; ``fail`` maps a method name to an exception; ``listing`` are the MLSD lines."""

    instances = []
    fail = {}
    listing = ()
    block = None

    def __init__(self, budget, context):
        self.budget = budget
        self.context = context
        self.calls = []
        type(self).instances.append(self)

    def _step(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]
        if self.block is not None and name == "retrlines":
            self.block.wait(5)

    def connect(self, host, port):
        self._step("connect", host, port)

    def auth(self):
        self._step("auth")

    def login(self, user, passwd):
        self._step("login", user, passwd)

    def prot_p(self):
        self._step("prot_p")

    def set_pasv(self, value):
        self._step("set_pasv", value)

    def cwd(self, directory):
        self._step("cwd", directory)

    def retrlines(self, command, callback):
        self._step("retrlines", command)
        for line in self.listing:
            callback(line)

    def quit(self):
        self._step("quit")

    def close(self):
        self.calls.append(("close",))


def configuration(**values):
    return DatahubConfiguration(host="datahub.example.org", port=21, username=USERNAME, **values)


class ConnectionTestTests(SimpleTestCase):
    def setUp(self):
        FakeClient.instances = []
        FakeClient.fail = {}
        FakeClient.block = None
        FakeClient.listing = (
            "type=cdir; .",
            "type=pdir; ..",
            "type=file;size=100; a.xml",
            "type=file;size=200; b.xml.gz",
            "type=dir; sub",
            "size=5; c.xml",
        )
        patcher = patch.object(connection, "_Client", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_test(self, **values):
        return connection.check_connection(configuration(**values), PASSWORD)

    def test_success_counts_the_files_and_only_lists(self):
        result = self.run_test()
        self.assertEqual(result, connection.TestResult(Outcome.SUCCEEDED, 3, ""))
        names = [call[0] for call in FakeClient.instances[0].calls]
        # The order of the manual (AUTH TLS, USER, PASS, PBSZ, PROT), passive, then only a listing.
        self.assertEqual(names, ["connect", "auth", "login", "prot_p", "set_pasv", "retrlines", "quit", "close"])
        self.assertIn(("set_pasv", True), FakeClient.instances[0].calls)
        self.assertIn(("retrlines", "MLSD"), FakeClient.instances[0].calls)

    def test_the_directory_is_changed_when_set(self):
        self.run_test(directory="/")
        self.assertIn(("cwd", "/"), FakeClient.instances[0].calls)

    def test_failures_map_to_fixed_outcomes(self):
        cases = (
            ("connect", socket.gaierror("name"), Outcome.CONNECTION_FAILED),
            ("connect", ConnectionRefusedError(), Outcome.CONNECTION_FAILED),
            ("auth", ssl.SSLError("handshake"), Outcome.TLS_FAILED),
            ("auth", ftplib.error_perm("534 Policy requires TLS"), Outcome.TLS_FAILED),
            ("login", ftplib.error_perm("530 Login incorrect"), Outcome.LOGIN_FAILED),
            ("prot_p", ftplib.error_perm("521 data connection cannot be opened"), Outcome.LISTING_FAILED),
            ("cwd", ftplib.error_perm("550 Requested action not taken"), Outcome.DIRECTORY_FAILED),
            ("retrlines", ftplib.error_temp("425 Cannot open data connection"), Outcome.LISTING_FAILED),
            ("retrlines", TimeoutError(), Outcome.TIMED_OUT),
            ("login", RuntimeError("unexpected"), Outcome.FAILED),
        )
        for method, error, outcome in cases:
            with self.subTest(method=method, error=repr(error)):
                FakeClient.fail = {method: error}
                result = self.run_test(directory="/")
                self.assertEqual(result.outcome, outcome)
                self.assertIsNone(result.file_count)
                self.assertEqual(FakeClient.instances[-1].calls[-1], ("close",))

    def test_a_certificate_error_is_a_tls_failure(self):
        error = ssl.SSLCertVerificationError("certificate verify failed")
        error.verify_message = "unable to get local issuer certificate"
        FakeClient.fail = {"auth": error}
        result = self.run_test()
        self.assertEqual(result.outcome, Outcome.TLS_FAILED)
        self.assertEqual(result.server_text, "unable to get local issuer certificate")

    def test_the_server_text_is_shortened_and_free_of_credentials(self):
        FakeClient.fail = {
            "login": ftplib.error_perm(f"530 User {USERNAME.upper()} with {PASSWORD}\r\nUSER x\x00" + "y" * 500)
        }
        result = self.run_test()
        self.assertEqual(result.outcome, Outcome.LOGIN_FAILED)
        self.assertNotIn(USERNAME.lower(), result.server_text.lower())
        self.assertNotIn(PASSWORD, result.server_text)
        self.assertNotIn("\r", result.server_text)
        self.assertNotIn("\n", result.server_text)
        self.assertNotIn("\x00", result.server_text)
        self.assertLessEqual(len(result.server_text), connection.SERVER_TEXT_LIMIT)
        self.assertTrue(result.server_text.startswith("530 User *** with ***"))

    def test_the_total_deadline_ends_a_hanging_test(self):
        FakeClient.block = threading.Event()
        try:
            with patch.object(connection, "TOTAL_SECONDS", 0.2):
                result = self.run_test()
        finally:
            FakeClient.block.set()
        self.assertEqual(result, connection.TestResult(Outcome.TIMED_OUT))

    def test_the_tls_context_verifies_the_certificate(self):
        context = connection.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


class ClientTests(SimpleTestCase):
    """The real client class: its sockets register with the budget, and nothing reaches the network."""

    def test_the_client_uses_the_verifying_context_and_short_timeouts(self):
        budget = connection.Budget()
        client = connection._Client(budget, connection.tls_context())
        self.assertEqual(client.timeout, connection.STEP_SECONDS)
        self.assertTrue(client.context.check_hostname)
        self.assertTrue(client.passiveserver)

    def test_the_client_never_trusts_the_address_of_the_pasv_reply(self):
        self.assertIs(connection._Client.trust_server_pasv_ipv4_address, False)
        client = connection._Client(connection.Budget(), connection.tls_context())
        self.assertIs(client.trust_server_pasv_ipv4_address, False)

    def test_the_data_channel_reuses_the_tls_session(self):
        budget = connection.Budget()
        client = connection._Client(budget, connection.tls_context())
        client.host = "datahub.example.org"
        client._prot_p = True
        client.sock = type("Sock", (), {"session": "the-session"})()
        raw = object()
        with (
            patch.object(ftplib.FTP, "ntransfercmd", return_value=(raw, None)),
            patch.object(client.context, "wrap_socket", return_value="wrapped") as wrap,
            patch.object(budget, "add") as add,
        ):
            conn, _size = client.ntransfercmd("MLSD")
        self.assertEqual(conn, "wrapped")
        wrap.assert_called_once_with(raw, server_hostname="datahub.example.org", session="the-session")
        self.assertEqual([call.args[0] for call in add.call_args_list], [raw, "wrapped"])
