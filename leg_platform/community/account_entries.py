"""Whether an account has buildings, metering points or an IBAN entered (D-106, recovery by email).

An account without any of these entries has nothing to protect beyond its identity: a lost sign-in method
is replaced after a link to the account's address (``accounts.second_factor_reset``). As soon as one entry
exists, the recovery needs the LEG administrator. The entries count whatever the state of the application is:
a draft, a pending or a withdrawn application keeps its rows until they are deleted, and a deleted row is not
an entry any more. Name, address, customer number and email addresses are not entries: the registration
writes them.
"""


def exist(user):
    """True if a building, a metering point or an IBAN is entered for the account."""
    from leg_platform.community.models import (
        ApplicationBuilding,
        CustomerBuilding,
        PersonAssignment,
        PersonRecord,
    )

    # An application's metering points belong to its buildings, so the buildings cover them.
    return (
        ApplicationBuilding.objects.filter(application__person=user).exists()
        or CustomerBuilding.objects.filter(person=user).exists()
        or PersonAssignment.objects.filter(person=user).exists()
        or PersonRecord.objects.filter(person=user).exclude(iban_encrypted="").exists()
    )
