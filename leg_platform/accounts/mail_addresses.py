"""The sender and the contact address of the emails of the instance (D-116).

Both are stored in ``GraphConfiguration``, the one row of the E-Mail chapter of the administration. The sender is the
Microsoft Graph mailbox: the Graph backend refuses any other sender, so one value serves both. Every email reads the
sender here, in every mode, so console mode sends from the same address as Microsoft Graph delivery.
"""

from django.core.exceptions import ImproperlyConfigured

from leg_platform.accounts.models import GraphConfiguration


def sender():
    """The sender address of every email; ``ImproperlyConfigured`` while it is not set (nothing is sent)."""
    address = GraphConfiguration.load().sender_address
    if not address:
        raise ImproperlyConfigured(
            "The sender address is not set in the E-Mail chapter of the administration; no email is sent."
        )
    return address


def contact_recipient():
    """The LEG's address for the contact form, or an empty string when it is not set."""
    return GraphConfiguration.load().contact_address
