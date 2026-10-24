"""The contract (D-103): versions uploaded by the staff, the one current version, and the acceptance of a customer.

* ``ContractVersion`` keeps the PDF in the database (like the logos): checked by content (``contract_pdf``), SHA-256 and
  size stored, the file name never stored. Exactly one version is current (``current_marker``, unique index);
  a version can be deleted only if it never was current and nobody accepted it.
* ``ContractAcceptance`` is append-only: person, version, time, SHA-256 of the accepted file, and the second tick
  (data exchange with the grid operator) as its own value. Both ticks are required. Once per person and version. An
  acceptance sets the one-way retention obligation of the account (``set_retention_obligation``, like an assignment), so
  the evidence survives an account deletion (the account is deactivated instead).
* A customer may download the CURRENT version and every earlier version the account itself accepted
  (``customer_download``); the staff may download every version.
"""

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models import Count, F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from leg_platform.accounts import security_log
from leg_platform.accounts.models import SecurityEvent
from leg_platform.community import contract_pdf, history
from leg_platform.community.application_common import ApplicationError, locked_customer, staff_actor
from leg_platform.community.history import HistoryEvent
from leg_platform.community.models import ContractAcceptance, ContractVersion, HistoryEntry

DUPLICATE_FILE = _("This file is already stored.")


def current_version():
    """The current version without its file, or None while none is current."""
    return ContractVersion.objects.defer("pdf").filter(current_marker=True).first()


def versions():
    """Every version, newest first, without the files and with ``acceptance_count`` (for the staff's list)."""
    return ContractVersion.objects.defer("pdf").annotate(acceptance_count=Count("acceptances"))


def file_name(version_date):
    """The file name for a version date (a date or its ISO text): ``contract-2026-10-07.pdf``."""
    text = version_date if isinstance(version_date, str) else version_date.isoformat()
    return f"contract-{text}.pdf"


def download_name(version):
    """The name of the downloaded file: built from the version date, never from the uploaded name."""
    return file_name(version.version_date)


def can_delete(version):
    """A version that never was current and that nobody accepted."""
    return version.made_current_at is None and not version.acceptances.exists()


def upload_version(actor, data, version_date):
    """Store a new version (not current yet); returns it. ``ValidationError`` for a file that fails the checks, for a
    date that is not a date and for a file that is already stored."""
    staff = staff_actor(actor)
    if version_date is None:
        raise ValidationError(_("This field is required."), code="required")
    contract_pdf.validate_pdf(data)
    data = bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with transaction.atomic():
        if ContractVersion.objects.filter(sha256=digest).exists():
            raise ValidationError(DUPLICATE_FILE, code="duplicate")
        try:
            version = ContractVersion.objects.create(
                version_date=version_date, pdf=data, sha256=digest, size=len(data), uploaded_by=staff
            )
        except IntegrityError:
            raise ValidationError(DUPLICATE_FILE, code="duplicate") from None
        history.record(version, HistoryEvent.CONTRACT_UPLOADED, staff, {"version_date": version_date.isoformat()})
        security_log.record(SecurityEvent.Event.CONTRACT_VERSION_CHANGED, actor=staff, detail="uploaded")
    return version


def make_current(actor, version):
    """Make the version the current one; the previous current version stays as evidence. Returns the version."""
    staff = staff_actor(actor)
    with transaction.atomic():
        rows = {
            row.pk: row
            for row in ContractVersion.objects.select_for_update().defer("pdf").order_by("pk")
            if row.pk == version.pk or row.current_marker is True
        }
        chosen = rows.get(version.pk)
        if chosen is None:
            raise ApplicationError("not_found")
        if chosen.is_current:
            return chosen
        for row in rows.values():
            if row.is_current:
                row.current_marker = None
                row.save(update_fields=["current_marker"])
        chosen.current_marker = True
        chosen.made_current_at = timezone.now()
        chosen.save(update_fields=["current_marker", "made_current_at"])
        history.record(chosen, HistoryEvent.CONTRACT_MADE_CURRENT, staff)
        security_log.record(SecurityEvent.Event.CONTRACT_VERSION_CHANGED, actor=staff, detail="made current")
    return chosen


def delete_version(actor, version):
    """Delete a version that never was current and was never accepted; ``ApplicationError("wrong_state")`` otherwise."""
    staff = staff_actor(actor)
    with transaction.atomic():
        try:
            fresh = ContractVersion.objects.select_for_update().defer("pdf").get(pk=version.pk)
        except ContractVersion.DoesNotExist:
            raise ApplicationError("not_found") from None
        if fresh.is_current or not can_delete(fresh):
            raise ApplicationError("wrong_state")
        history.record(fresh, HistoryEvent.CONTRACT_DELETED, staff)
        security_log.record(SecurityEvent.Event.CONTRACT_VERSION_CHANGED, actor=staff, detail="deleted")
        fresh.delete()


def history_rows(actor, limit=100):
    """The newest history entries of the contract versions (uploaded, made current, deleted; also of versions that no
    longer exist), newest first, for an active staff account: ``[(entry, file name)]``. The file name is built from the
    version date of the upload entry; no file content is read. The history never holds content (D-077)."""
    staff_actor(actor)
    entries = list(
        HistoryEntry.objects.filter(subject_type=history.CONTRACT).select_related("actor")
        .order_by("-created_at", "-pk")[:limit]
    )
    uploads = {
        entry.subject_id: entry.detail.get("version_date", "")
        for entry in HistoryEntry.objects.filter(
            subject_type=history.CONTRACT, event=HistoryEvent.CONTRACT_UPLOADED,
            subject_id__in={entry.subject_id for entry in entries},
        )
    }
    return [
        (entry, file_name(uploads[entry.subject_id]) if uploads.get(entry.subject_id) else "")
        for entry in entries
    ]


def stored_version_of(actor, data):
    """The stored version (without its file) that has exactly these bytes, or None; for an active staff account. Used to
    name the existing version when an upload is refused as a duplicate (design walkthrough W5-09). Reads nothing but the
    SHA-256 of the given bytes."""
    staff_actor(actor)
    digest = hashlib.sha256(bytes(data)).hexdigest()
    return ContractVersion.objects.defer("pdf").filter(sha256=digest).first()


def staff_download(actor, version_id):
    """``(file name, bytes)`` of any version, for an active staff account."""
    staff_actor(actor)
    try:
        version = ContractVersion.objects.get(pk=version_id)
    except ContractVersion.DoesNotExist:
        raise ApplicationError("not_found") from None
    return download_name(version), bytes(version.pdf)


def customer_download(person, version_id=None):
    """``(file name, bytes)`` for an active account: the CURRENT version, or an earlier version that THIS account accepted
    (the evidence of what it agreed to). Any other version (not accepted by the account, or never current) is "not
    found", the same answer as a missing one. ``version_id`` None means the current one."""
    user_model = get_user_model()
    if (
        getattr(person, "pk", None) is None
        or not user_model.objects.filter(pk=person.pk, status=user_model.Status.ACTIVE).exists()
    ):
        raise ApplicationError("not_allowed")
    if version_id is None:
        version = ContractVersion.objects.filter(current_marker=True).first()
        if version is None:
            raise ApplicationError("no_contract")
    else:
        try:
            version = ContractVersion.objects.filter(pk=version_id).first()
        except (TypeError, ValueError):
            version = None
        if version is None or not (
            version.is_current or ContractAcceptance.objects.filter(person=person, version=version).exists()
        ):
            raise ApplicationError("not_found")
    return download_name(version), bytes(version.pdf)


def customer_versions(person):
    """The versions the account may see, newest first, without the files: the current one and every one the account
    accepted. ``[(version, acceptance or None)]`` (``acceptance`` is the account's own, for the current one possibly
    None: not accepted yet)."""
    accepted = {row.version_id: row for row in ContractAcceptance.objects.filter(person=person)}
    rows = ContractVersion.objects.defer("pdf").filter(Q(current_marker=True) | Q(pk__in=list(accepted)))
    return [(version, accepted.get(version.pk)) for version in rows]


def latest_acceptance(person):
    """The newest acceptance of the account as ``{"accepted_at", "data_exchange_accepted", "version_date"}``, or None.
    For the staff's page of an application; no file content is read."""
    return (
        ContractAcceptance.objects.filter(person=person).order_by("-accepted_at", "-pk")
        .values(
            "accepted_at", "data_exchange_accepted", version_date=F("version__version_date"),
        )
        .first()
    )


def acceptance_of(person, version):
    """The acceptance of this version by the account, or None."""
    if version is None:
        return None
    return ContractAcceptance.objects.filter(person=person, version=version).first()


def has_accepted_current(person):
    version = current_version()
    return version is not None and ContractAcceptance.objects.filter(person=person, version=version).exists()


def accept_contract(person, version_id, *, agrees_contract):
    """The account accepts the CURRENT version with its one tick; returns the acceptance.

    The data exchange with the grid operator is part of the contract text (owner decision 2026-10-24), so every
    acceptance stores ``data_exchange_accepted=True``. ``version_id`` is the version the page showed: if another version
    became current meanwhile it is ``ApplicationError("version_changed")`` and nothing is stored. ``agreement_required``,
    ``no_contract``, ``already_accepted``. Stores no IP address, device data or password."""
    if agrees_contract is not True:
        raise ApplicationError("agreement_required")
    with transaction.atomic():
        fresh = locked_customer(person)
        version = ContractVersion.objects.select_for_update().defer("pdf").filter(current_marker=True).first()
        if version is None:
            raise ApplicationError("no_contract")
        if version.pk != version_id:
            raise ApplicationError("version_changed")
        if ContractAcceptance.objects.filter(person=fresh, version=version).exists():
            raise ApplicationError("already_accepted")
        acceptance = ContractAcceptance.objects.create(
            person=fresh, version=version, sha256=version.sha256, data_exchange_accepted=True
        )
        history.record(fresh, HistoryEvent.CONTRACT_ACCEPTED, fresh, {"version_id": version.pk})
        if not fresh.retention_obligation:
            # Evidence of the acceptance must survive an account deletion: the account is kept, as for an assignment
            # (D-016, D-086; owner decision 2026-10-19).
            fresh.set_retention_obligation()
            security_log.record(SecurityEvent.Event.RETENTION_OBLIGATION_SET, account=fresh, actor=fresh)
    return acceptance
