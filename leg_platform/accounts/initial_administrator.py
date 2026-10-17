"""Creation of the initial system administrator, shared by ``setup_initial_administrator`` (D-005) and ``setup_instance`` (D-076).

Both go through ``User.objects.create_superuser()``, which refuses while any system administrator
exists, validates the password and serialises concurrent calls with a MariaDB named lock. The
account must change the password and set up 2FA at first login (D-041). Error messages never
contain the password.
"""

from django.core.exceptions import ValidationError
from django.utils import translation

from leg_platform.accounts import security_log
from leg_platform.accounts.models import AccountIntegrityError, SecurityEvent, User


class InitialAdministratorError(Exception):
    """The account was not created; the message is safe to show (it contains no secret)."""


class SystemAdministratorExists(InitialAdministratorError):
    """A system administrator exists, so nothing was created."""


def system_administrator_exists():
    """True while any account with is_superuser exists, also a blocked or deactivated one."""
    return User._base_manager.filter(is_superuser=True).exists()


def accounts_exist():
    """True while the account table holds any row (every status, also a deactivated account)."""
    return User._base_manager.exists()


def create_initial_administrator(email, password):
    """Create the initial system administrator and write the security log entry.

    Raises ``SystemAdministratorExists`` when one exists (also when another process created it
    first) and ``InitialAdministratorError`` for every other refusal.
    """
    try:
        with translation.override("en"):
            # English: for the operator's terminal and logs (Django's validators translate when they raise).
            user = User.objects.create_superuser(email, password, password_change_required=True)
    except ValidationError as error:
        # The validators of Django do not repeat the password; a message that did is dropped.
        messages = [message for message in error.messages if password not in message]
        raise InitialAdministratorError(
            "The password is not accepted: " + " ".join(messages)
        ) from None
    except AccountIntegrityError as error:
        if "already exists" in str(error):
            raise SystemAdministratorExists(
                "A system administrator already exists. This command never changes an "
                "existing account."
            ) from None
        raise InitialAdministratorError(str(error)) from None
    except ValueError as error:
        raise InitialAdministratorError(str(error)) from None
    security_log.record(SecurityEvent.Event.INITIAL_ADMINISTRATOR_CREATED, account=user)
    return user
