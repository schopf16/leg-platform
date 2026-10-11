"""Email backends of LEG-Platform.

Delivery via Microsoft Graph (EMAIL_MODE allowlist and graph; docs/ARCHITECTURE.md, "Email")
is not implemented yet. Until it is, these modes use ``GraphNotImplementedEmailBackend``,
which refuses to send. Email is therefore never dropped silently and never falls back to
Django's default SMTP backend or to the console, where security links could end up in logs.
"""

from django.core.mail.backends.base import BaseEmailBackend


class GraphNotImplementedEmailBackend(BaseEmailBackend):
    """Placeholder that fails closed: every send attempt raises an error.

    ``fail_silently`` is deliberately not supported, so that a missing implementation can
    never go unnoticed.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        raise NotImplementedError(
            "Email delivery via Microsoft Graph is not implemented yet; no email was sent."
        )
