"""Writing security events (docs/SECURITY.md, section 10; D-018): the append-only log plus the ``leg_platform.security`` logger, naming accounts by internal number only."""

import functools
import logging

from django.db import transaction

from leg_platform.accounts.models import SecurityEvent

logger = logging.getLogger("leg_platform.security")

Event = SecurityEvent.Event

MESSAGES = {
    Event.PASSKEYS_CHANGED: "The passkeys were changed",
    Event.SIGN_IN_METHOD_CHANGED: "The sign-in method was changed",
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
    Event.PORTAL_UPLOAD_REJECTED: "An upload in the portal settings was rejected",
    Event.EMAIL_CHANGE_CANCELLED: "A requested email address change was cancelled by the account",
    Event.EMAIL_CHANGE_STOPPED: "A requested email address change was stopped with the notice link",
    Event.SECONDARY_EMAIL_CHANGED: "The second email address was changed",
    Event.REGISTRATION_COMPLETED: "A registration was completed",
    Event.SESSION_ENDED_WRONG_CODES: "A session was ended after wrong codes in a confirmation",
    Event.ALL_SESSIONS_ENDED: "All sessions of an account were ended after wrong entries in confirmations",
    Event.TWO_FACTOR_REMOVED_PHONE_LOST: (
        "Two-factor authentication was removed with a recovery code (phone lost)"
    ),
    Event.ADDRESS_IMPORT_REQUESTED: "An address directory import was requested",
    Event.ADDRESS_IMPORT_SUCCEEDED: "An address directory import completed",
    Event.ADDRESS_IMPORT_FAILED: "An address directory import failed",
    Event.PV_IMPORT_SUCCEEDED: "A plant register import completed",
    Event.PV_IMPORT_FAILED: "A plant register import failed",
    Event.TRANSFORMER_SCAN_SUCCEEDED: "A transformer station scan completed",
    Event.TRANSFORMER_SCAN_FAILED: "A transformer station scan failed",
    Event.SERVICE_AREA_CHANGED: "The postal codes of the service area were changed",
    Event.LOG_VIEWED: "A log file was viewed",
    Event.LOG_DOWNLOADED: "A log file was downloaded",
    Event.PERSISTENT_SESSION_STARTED: "A long session (stay signed in) was started for a privileged account",
    Event.ACCOUNT_SESSIONS_ENDED_BY_ADMIN: "All sessions of an account were ended by a system administrator",
    Event.RETENTION_OBLIGATION_SET: "The retention obligation of an account was set",
    Event.LIST_PRINTED: "A list of the management area was printed as PDF",
    Event.PERSON_INVITED: "A person was created and invited by staff",
    Event.INVITATION_RESENT: "An invitation was sent again by staff",
    Event.ACCOUNT_DATA_CHANGED_BY_STAFF: "The data of an account was changed by staff",
    Event.GRID_OPERATOR_CHANGED: "The grid operator of the instance was changed",
    Event.MANAGEMENT_DATA_EDITED: "Management data was edited in the administration",
    Event.IBAN_CHANGED: "The IBAN of an account was changed by the account",
    Event.CONTRACT_VERSION_CHANGED: "A contract version was uploaded, made current or deleted",
}


def record(event, *, account=None, actor=None, long_retention=False, detail=""):
    """Store the event and log it; call it in the transaction of the change it records.

    The log line is written on commit, so a rolled-back change leaves neither (D-018). ``detail``
    goes to the log line only and must never contain personal data (e.g. portal setting names,
    hosts, colours, a logo's type/size/digest start, a rejected upload's fixed reason code).
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
