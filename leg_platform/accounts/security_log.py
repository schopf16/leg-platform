"""Writing security events (docs/SECURITY.md, section 10; D-018, D-041).

Every event is stored in the append-only security log and also written to the logger
``leg_platform.security``. Both name the account only by its internal number.
"""

import logging

from leg_platform.accounts.models import SecurityEvent

logger = logging.getLogger("leg_platform.security")

Event = SecurityEvent.Event

MESSAGES = {
    Event.PASSWORD_CHANGED: "The password was changed",
    Event.PASSWORD_RESET: "The password was reset by link",
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
}


def record(event, *, account=None, actor=None, long_retention=False):
    """Store the event and log it; call it in the transaction of the change it records."""
    SecurityEvent.objects.record(
        event, account=account, actor=actor, long_retention=long_retention
    )
    account_id = getattr(account, "pk", account)
    if account_id is None:
        logger.warning("%s.", MESSAGES[event])
    else:
        logger.warning("%s (account %d).", MESSAGES[event], account_id)
