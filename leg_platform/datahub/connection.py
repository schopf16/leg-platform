"""The connection test of the Datahub access (D-139): log in over FTPES and count the files. Nothing else.

Rules of the Datahub manual (Swisseldex, "Benutzerhandbuch Datahub", as of 2026-09-28, chapters 3.4.2 and 4):
explicit TLS on the control channel before anything else (``AUTH TLS``, else 534), the login sequence
``AUTH TLS, USER, PASS, PBSZ, PROT``, ``PROT P`` before any transfer (else 521), always passive mode, a flat
directory (``CWD`` only to ``/`` or ``.``), ``MLSD`` supported, ``FEAT`` not, a disconnect after 10 seconds
without activity, and repeated logins within 60 seconds refused (the caller keeps that distance).

The test only lists: no ``RETR``, ``DELE``, ``STOR`` or rename. It runs in a helper thread with a hard total
deadline (``http_budget.Budget``, as the building search): when the deadline passes, every socket of the test is
shut down. The helper thread does not touch the database.
"""

import ftplib
import re
import ssl
import threading
from dataclasses import dataclass

from django.views.decorators.debug import sensitive_variables

from leg_platform.addresses.http_budget import Budget

from .models import CONTROL, DatahubConfiguration

Outcome = DatahubConfiguration.TestOutcome

TOTAL_SECONDS = 20  # below the Gunicorn timeout of 30 s (docs/DEPLOYMENT.md, 8.1)
STEP_SECONDS = 8  # one wait of one socket; the server itself disconnects after 10 s without activity
MAX_LISTED_LINES = 1_000_000
SERVER_TEXT_LIMIT = 160
NOT_FILES = frozenset({"dir", "cdir", "pdir"})


@dataclass(frozen=True)
class TestResult:
    outcome: str
    file_count: int | None = None
    # The reply of the server, shortened and cleaned of the credentials; only for the page, never for a log.
    server_text: str = ""


class _DeadlineReached(Exception):
    pass


class _Client(ftplib.FTP_TLS):
    """FTP_TLS whose sockets register with the budget; the data channel reuses the TLS session of the control
    channel (servers that require it refuse the data connection otherwise)."""

    # The data connection goes to the host of the control channel, never to an address the server names in its
    # PASV reply (the default of ftplib, stated here so that it is checked by a test).
    trust_server_pasv_ipv4_address = False

    def __init__(self, budget, context):
        self._budget = budget
        super().__init__(context=context, timeout=STEP_SECONDS)

    def connect(self, *args, **kwargs):
        welcome = super().connect(*args, **kwargs)
        self._budget.add(self.sock)
        return welcome

    def auth(self):
        response = super().auth()
        self._budget.add(self.sock)
        return response

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        self._budget.add(conn)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
            self._budget.add(conn)
        return conn, size


def tls_context():
    """Certificate and host name checked against the system's trust store; TLS 1.2 or newer."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@sensitive_variables("password", "secrets")
def clean_server_text(text, secrets):
    """Shorten a server reply and remove control characters and every credential it repeats."""
    text = CONTROL.sub(" ", str(text))
    for secret in secrets:
        if secret:
            text = re.sub(re.escape(secret), "***", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    if len(text) > SERVER_TEXT_LIMIT:
        text = text[: SERVER_TEXT_LIMIT - 1] + "…"
    return text


def _is_file(line):
    facts, _sep, _name = line.partition(" ")
    for fact in facts.split(";"):
        key, _eq, value = fact.partition("=")
        if key.strip().lower() == "type":
            return value.strip().lower() not in NOT_FILES
    return True


@sensitive_variables()
def _run(host, port, username, password, directory, budget, context):
    """The steps of the test; returns (outcome, file_count, server_text). The step names the outcome of a failure."""
    step = Outcome.CONNECTION_FAILED
    client = _Client(budget, context)
    secrets = (username, password)
    try:
        client.connect(host, port)
        step = Outcome.TLS_FAILED
        client.auth()
        step = Outcome.LOGIN_FAILED
        client.login(username, password)
        # PROT P protects the data channel: a refusal (for example 521) concerns the listing, not the login.
        step = Outcome.LISTING_FAILED
        client.prot_p()
        client.set_pasv(True)
        if directory:
            step = Outcome.DIRECTORY_FAILED
            client.cwd(directory)
        step = Outcome.LISTING_FAILED
        count = 0
        lines = 0

        def take(line):
            nonlocal count, lines
            lines += 1
            if lines > MAX_LISTED_LINES or budget.expired:
                raise _DeadlineReached
            if _is_file(line):
                count += 1

        client.retrlines("MLSD", take)
        try:
            client.quit()
        except (OSError, EOFError, ftplib.Error):
            pass
        return Outcome.SUCCEEDED, count, ""
    except _DeadlineReached:
        return Outcome.TIMED_OUT, None, ""
    except ssl.SSLCertVerificationError as error:
        return Outcome.TLS_FAILED, None, clean_server_text(error.verify_message or "", secrets)
    except ssl.SSLError:
        return Outcome.TLS_FAILED, None, ""
    except TimeoutError:
        return Outcome.TIMED_OUT, None, ""
    except ftplib.Error as error:
        # A reply of the server (4xx, 5xx or an unexpected one): the only text of the server that is shown.
        return step, None, clean_server_text(error, secrets)
    except (OSError, EOFError):
        return (Outcome.TIMED_OUT if budget.expired else step), None, ""
    finally:
        client.close()


@sensitive_variables("password")
def check_connection(configuration, password):
    """Log in with the stored data and count the files of the directory; never raises for a network failure."""
    budget = Budget()
    context = tls_context()
    outcome = {}

    def work():
        try:
            outcome["result"] = _run(
                configuration.host,
                configuration.port,
                configuration.username,
                password,
                configuration.directory,
                budget,
                context,
            )
        except Exception:  # noqa: BLE001 - nothing of it may reach a log (it could hold a credential)
            outcome["result"] = (Outcome.FAILED, None, "")

    thread = threading.Thread(target=work, name="datahub-connection-test", daemon=True)
    thread.start()
    thread.join(TOTAL_SECONDS)
    if thread.is_alive():
        budget.expire()
        thread.join(1)
        return TestResult(Outcome.TIMED_OUT)
    result_outcome, file_count, server_text = outcome["result"]
    return TestResult(str(result_outcome), file_count, server_text)

