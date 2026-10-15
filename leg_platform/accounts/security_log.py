"""Writing security events (docs/SECURITY.md, section 10; D-018, D-041).

Every event is stored in the append-only security log and also written to the logger
``leg_platform.security``. Both name the account only by its internal number.
"""

import functools
import logging

from django.db import transaction

from leg_platform.accounts.models import SecurityEvent

logger = logging.getLogger("leg_platform.security")

Event = SecurityEvent.Event

MESSAGES = {
    Event.PASSWORD_CHANGED: "The password was changed",
    Event.PASSWORD_RESET: "The password was reset by link",
    Event.EMAIL_CHANGE_REQUESTED: "An email address change was requested",
    Event.EMAIL_CHANGED: "The email address was changed",
    Event.ACCOUNT_BLOCKED: "The account was blocked",
    Event.ACCOUNT_UNBLOCKED: "The account was unblocked",
    Event.ACCOUNT_DEACTIVATED: "The account was deleted and is kept under the retention obligation",
    Event.ACCOUNT_REACTIVATED: "The account was restored",
    Event.ACCOUNT_DELETED: "The account was deleted",
    Event.TWO_FACTOR_ENABLED: "Two-factor authentication was set up",
    Event.TWO_FACTOR_DISABLED: "Two-factor authentication was removed",
    Event.TWO_FACTOR_RESET: "Two-factor authentication was reset",
    Event.TWO_FACTOR_RESET_REQUESTED: "The removal of two-factor authentication was requested",
    Event.RECOVERY_CODES_RENEWED: "The recovery codes were renewed",
    Event.RECOVERY_CODE_USED: "A recovery code was used",
    Event.CODE_ATTEMPTS_WARNING: "Many wrong codes were entered",
    Event.CODE_ATTEMPTS_PAUSED: "Code entry is paused",
    Event.LOGIN_LOCKOUT: "An account was locked after failed logins",
    Event.PRIVILEGES_CHANGED: "The access to the administration was changed",
    Event.ACCESS_RELATIONS_CHANGED: "Roles or permissions were changed",
    Event.INITIAL_ADMINISTRATOR_CREATED: "The initial system administrator was created",
    Event.EMERGENCY_TWO_FACTOR_RESET: "Two-factor authentication was reset by the emergency command",
    Event.EMERGENCY_ACCOUNT_UNBLOCKED: "An account was unblocked by the emergency command",
    Event.EMERGENCY_KEYS_REENCRYPTED: "Second factor keys were encrypted again by the emergency command",
    Event.GRAPH_CONFIGURATION_CHANGED: "The Microsoft Graph configuration was changed",
    Event.GRAPH_CERTIFICATE_UPLOADED: "A Microsoft Graph certificate was uploaded",
    Event.GRAPH_TEST_EMAIL_REQUESTED: "A Microsoft Graph test email was requested",
    Event.PORTAL_SETTINGS_CHANGED: "The portal settings were changed",
    Event.EMAIL_CHANGE_CANCELLED: "A requested email address change was cancelled by the account",
    Event.EMAIL_CHANGE_STOPPED: "A requested email address change was stopped with the notice link",
    Event.SECONDARY_EMAIL_CHANGED: "The second email address was changed",
    Event.REGISTRATION_COMPLETED: "A registration was completed",
}


def record(event, *, account=None, actor=None, long_retention=False, detail=""):
    """Store the event and log it; call it in the transaction of the change it records.

    The log line is written only when the transaction commits, so a rolled-back change leaves
    neither a stored event nor a log line (D-018). ``detail`` is added to the log line only; it
    must never contain personal data (the portal settings give the host names of public
    addresses, D-056).
    """
    SecurityEvent.objects.record(
        event, account=account, actor=actor, long_retention=long_retention
    )
    account_id = getattr(account, "pk", account)
    actor_id = getattr(actor, "pk", actor)
    # Internal numbers only (D-018): no name, address or other personal data.
    references = [
        f"{label} {value}"
        for label, value in (("account", account_id), ("actor", actor_id))
        if value is not None
    ]
    if detail:
        references.append(detail)
    if references:
        transaction.on_commit(
            functools.partial(
                logger.warning, "%s (%s).", MESSAGES[event], ", ".join(references)
            )
        )
    else:
        transaction.on_commit(functools.partial(logger.warning, "%s.", MESSAGES[event]))
