"""Customer numbers: the display format and the table of every number ever issued (D-021)."""

import logging
import secrets

from django.db import IntegrityError, models, transaction
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts.models.common import (
    EXACT_COLLATION,
    AccountIntegrityError,
    CustomerNumberUnavailableError,
)

logger = logging.getLogger(__name__)

CUSTOMER_NUMBER_DIGITS = 6
CUSTOMER_NUMBER_SPACE = 10**CUSTOMER_NUMBER_DIGITS
# With half of all numbers issued, 100 collisions in a row have a probability of 2**-100.
CUSTOMER_NUMBER_ATTEMPTS = 100
# From this share of issued numbers on, issuing logs a warning and the administration a notice.
CUSTOMER_NUMBER_WARNING_PERCENT = 90


def format_customer_number(number):
    """Return the display format of a customer number in two blocks of three digits."""
    if not number:
        return ""
    if len(number) != CUSTOMER_NUMBER_DIGITS or not number.isascii() or not number.isdigit():
        raise ValueError("A customer number consists of six digits.")
    return f"{number[:3]} {number[3:]}"


def customer_numbers_nearly_exhausted(issued):
    """Return whether the share of issued customer numbers reaches the warning level."""
    return issued * 100 >= CUSTOMER_NUMBER_SPACE * CUSTOMER_NUMBER_WARNING_PERCENT


class IssuedCustomerNumberQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AccountIntegrityError("Issued customer numbers cannot be changed.")

    def delete(self):
        raise AccountIntegrityError("Issued customer numbers are never deleted.")

    def bulk_create(self, objs, *args, **kwargs):
        raise AccountIntegrityError("Customer numbers are issued only with issue().")


class IssuedCustomerNumberManager(models.Manager.from_queryset(IssuedCustomerNumberQuerySet)):
    def issue(self):
        """Record and return a new random, never issued customer number; run it in the
        transaction of the assignment to an account."""
        issued = self.count()
        if issued >= CUSTOMER_NUMBER_SPACE:
            raise CustomerNumberUnavailableError("All customer numbers have been issued.")
        if customer_numbers_nearly_exhausted(issued):
            logger.warning(
                "%d of %d customer numbers have been issued (%d%%).",
                issued,
                CUSTOMER_NUMBER_SPACE,
                issued * 100 // CUSTOMER_NUMBER_SPACE,
            )
        for _attempt in range(CUSTOMER_NUMBER_ATTEMPTS):
            candidate = f"{secrets.randbelow(CUSTOMER_NUMBER_SPACE):0{CUSTOMER_NUMBER_DIGITS}d}"
            try:
                with transaction.atomic(using=self.db):
                    self.create(number=candidate)
            except IntegrityError:
                continue
            return candidate
        raise CustomerNumberUnavailableError(
            f"No unused customer number found in {CUSTOMER_NUMBER_ATTEMPTS} attempts."
        )


class IssuedCustomerNumber(models.Model):
    """Every customer number ever issued, without any reference to a person (D-021)."""

    number = models.CharField(
        _("customer number"),
        max_length=CUSTOMER_NUMBER_DIGITS,
        primary_key=True,
        db_collation=EXACT_COLLATION,
        editable=False,
    )

    objects = IssuedCustomerNumberManager()

    class Meta:
        # Related-object access and _base_manager use the protecting queryset as well.
        base_manager_name = "objects"
        verbose_name = _("issued customer number")
        verbose_name_plural = _("issued customer numbers")

    def __str__(self):
        # Never the number itself: it could reach logs or error output (D-021, D-036).
        return str(self._meta.verbose_name)

    def save(self, **kwargs):
        if (
            not self._state.adding
            or kwargs.get("force_update")
            or kwargs.get("update_fields") is not None
        ):
            raise AccountIntegrityError("Issued customer numbers cannot be changed.")
        # Without force_insert, Django would accept an existing number as an update.
        kwargs["force_insert"] = True
        super().save(**kwargs)

    def delete(self, *args, **kwargs):
        raise AccountIntegrityError("Issued customer numbers are never deleted.")
