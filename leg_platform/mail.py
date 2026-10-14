"""Email backends of LEG-Platform (D-006, docs/ARCHITECTURE.md, "Email").

``GraphEmailBackend`` sends through Microsoft Graph (``POST /v1.0/users/{sender}/sendMail``). The
application authenticates with a certificate (client credentials, MSAL). It is used for the
EMAIL_MODE values ``allowlist`` and ``graph``. It fails closed: when it is not configured or a
message cannot be sent, ``GraphEmailError`` is raised, and email is never sent by another route
(not Django's SMTP backend, not the console, where security links could end up in logs).

``fail_silently`` is deliberately not supported, so that a failed delivery never goes unnoticed.
Messages are never logged; log entries name no address, token or key.
"""

import json
import logging
from email.utils import parseaddr
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

import msal
import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.views.decorators.debug import sensitive_variables
from requests.adapters import HTTPAdapter

from leg_platform.accounts.encryption import DecryptionError
from leg_platform.accounts.models import GraphConfiguration
from leg_platform.environment import EMAIL_MODE_ALLOWLIST

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
LOGIN_AUTHORITY_URL = "https://login.microsoftonline.com/{tenant_id}"
# Applies to the token request (MSAL) and to each sendMail request.
GRAPH_TIMEOUT_SECONDS = 30
MAX_ERROR_BODY_BYTES = 4096
# Generic text for a token request that fails. The cause is never passed on, because it can contain
# tenant details or URLs.
TOKEN_UNAVAILABLE_MESSAGE = "Microsoft did not answer the token request; no email was sent."


class GraphEmailError(Exception):
    """An email could not be sent through Microsoft Graph. The text contains no secret."""


class _RefuseRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# A redirect answer raises HTTPError instead of repeating the request with the bearer token.
urlopen = build_opener(_RefuseRedirects).open


class _TokenSession(requests.Session):
    """HTTP session for the MSAL token request.

    MSAL's default session follows redirects and would send the client assertion to the
    redirect target. Here a redirect is returned unchanged, and every request has the timeout.
    """

    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        # Assigned, not set by default: an explicit timeout=None from the caller would disable it.
        kwargs["timeout"] = GRAPH_TIMEOUT_SECONDS
        return super().request(method, url, **kwargs)


class GraphEmailBackend(BaseEmailBackend):
    """Sends email through Microsoft Graph with the certificate of ``GraphConfiguration``."""

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        configuration = GraphConfiguration.load()
        if not configuration.is_configured:
            raise GraphEmailError("Microsoft Graph is not configured; no email was sent.")
        # All messages are checked before the first one is sent.
        payloads = []
        for message in email_messages:
            if parseaddr(message.from_email)[1].lower() != configuration.sender_address.lower():
                raise GraphEmailError(
                    "The sender address differs from the configured Microsoft Graph sender; "
                    "no email was sent."
                )
            payload = _payload(message)
            if payload is None:
                logger.info("Email discarded: no To recipient is on the allowlist.")
            else:
                payloads.append(payload)
        if not payloads:
            return 0
        token = _access_token(configuration)
        for payload in payloads:
            _send_mail(configuration.sender_address, token, payload)
        return len(payloads)


@sensitive_variables("private_key")
def _access_token(configuration):
    """Request an application token with the certificate (client credentials, MSAL)."""
    try:
        private_key = configuration.private_key_pem().decode("ascii")
    except DecryptionError:
        raise GraphEmailError(
            "The Microsoft Graph private key cannot be decrypted; no email was sent."
        ) from None
    except UnicodeDecodeError:
        # A key that is not ASCII PEM is refused; its content is not part of the message.
        raise GraphEmailError(
            "The Microsoft Graph private key cannot be read; no email was sent."
        ) from None
    session = _TokenSession()
    # The retry policy of MSAL's default session (msal/application.py: HTTPAdapter(max_retries=1)).
    adapter = HTTPAdapter(max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        application = msal.ConfidentialClientApplication(
            str(configuration.client_id),
            authority=LOGIN_AUTHORITY_URL.format(tenant_id=configuration.tenant_id),
            client_credential={
                "private_key": private_key,
                "public_certificate": configuration.certificate_pem,
            },
            # MSAL ignores its own timeout argument when http_client is given, so the session
            # applies GRAPH_TIMEOUT_SECONDS.
            http_client=session,
        )
    except OSError:
        # MSAL runs the tenant discovery in its constructor, so a connection error can occur here.
        raise GraphEmailError(TOKEN_UNAVAILABLE_MESSAGE) from None
    except ValueError:
        raise GraphEmailError(
            "The Microsoft Graph certificate cannot be used; no email was sent."
        ) from None
    except Exception:
        # Other discovery errors (RuntimeError, msal/authority.py line 305) can carry the URL.
        raise GraphEmailError(TOKEN_UNAVAILABLE_MESSAGE) from None
    try:
        result = application.acquire_token_for_client(scopes=[GRAPH_SCOPE])
    except (OSError, ValueError):
        # ValueError: MSAL cannot read the token response, for example a refused redirect.
        # OSError: no connection. The cause is not passed on, because it can contain tenant details.
        raise GraphEmailError(TOKEN_UNAVAILABLE_MESSAGE) from None
    token = result.get("access_token")
    if not token:
        # The error description can contain tenant details; only the error code is kept.
        code = str(result.get("error", "unknown"))[:64]
        raise GraphEmailError(
            f"Microsoft did not issue an access token (error: {code}); no email was sent."
        )
    return token


def _allowed(addresses):
    """Keep the addresses that may receive email in the current EMAIL_MODE."""
    if settings.EMAIL_MODE != EMAIL_MODE_ALLOWLIST:
        return list(addresses)
    allowed = set(settings.EMAIL_ALLOWED_RECIPIENTS)
    return [address for address in addresses if address.lower() in allowed]


def _recipients(addresses):
    return [{"emailAddress": {"address": address}} for address in addresses]


def _payload(message):
    """Return the sendMail JSON body for a message, or None when no To recipient is allowed."""
    if message.attachments:
        raise GraphEmailError(
            "Attachments are not supported by the Microsoft Graph backend; no email was sent."
        )
    if message.reply_to:
        raise GraphEmailError(
            "Reply-To is not supported by the Microsoft Graph backend; no email was sent."
        )
    if not message.to:
        raise GraphEmailError("A message needs a To recipient; no email was sent.")
    to = _allowed(message.to)
    if not to:
        return None
    if any(name.lower() == "auto-submitted" for name in message.extra_headers):
        logger.warning("Header Auto-Submitted is not sent by the Microsoft Graph backend.")
    html = next(
        (
            content
            for content, mimetype in getattr(message, "alternatives", [])
            if mimetype == "text/html"
        ),
        None,
    )
    if html is not None:
        body = {"contentType": "HTML", "content": html}
    elif message.content_subtype == "html":
        body = {"contentType": "HTML", "content": message.body}
    else:
        body = {"contentType": "Text", "content": message.body}
    return {
        "message": {
            "subject": message.subject,
            "body": body,
            "toRecipients": _recipients(to),
            "ccRecipients": _recipients(_allowed(message.cc)),
            "bccRecipients": _recipients(_allowed(message.bcc)),
        },
        # Not kept in the Sent Items of the sender mailbox (sendMail, saveToSentItems).
        "saveToSentItems": False,
    }


def _error_code(error):
    try:
        body = json.loads(error.read(MAX_ERROR_BODY_BYTES).decode("utf-8"))
        return str(body["error"]["code"])[:64]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"
    finally:
        # Releases the connection of the HTTP error response.
        error.close()


@sensitive_variables("token")
def _send_mail(sender, token, payload):
    """POST the message. Graph answers 202 Accepted when it has accepted the message."""
    request = Request(
        GRAPH_SEND_MAIL_URL.format(sender=quote(sender, safe="")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=GRAPH_TIMEOUT_SECONDS) as response:
            status = response.status
    except HTTPError as error:
        raise GraphEmailError(
            f"Microsoft Graph refused the email (HTTP {error.code}, error: {_error_code(error)});"
            " no email was sent."
        ) from None
    except OSError:
        raise GraphEmailError("Microsoft Graph could not be reached; no email was sent.") from None
    if status != 202:
        raise GraphEmailError(
            f"Microsoft Graph answered with HTTP {status} instead of 202; no email was sent."
        )
