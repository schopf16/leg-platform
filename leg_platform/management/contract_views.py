"""The pages of the contract versions (D-103; owner ``verwaltung/vertraege``): the list with the actions, the upload
dialog, the download, "set as current" and "delete". Every view uses ``management.access`` (every active staff
account, D-086); the rules (one current version, delete only a version that never was current and that nobody accepted,
the checks of the file, history and security events) live in ``community.contracts`` and ``community.contract_pdf``.

The download is served as an attachment by ``leg_platform.downloads.pdf_attachment`` (``nosniff``, a sandboxing
``Content-Security-Policy``, ``Cross-Origin-Resource-Policy`` and ``Referrer-Policy``; the file is never shown in the page), the
name is built from the version date and never from the uploaded name, and each download is written to the application log
(account and version number only).
"""

import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from leg_platform.community import contracts
from leg_platform.community.application_common import ApplicationError
from leg_platform.community.models import ContractVersion
from leg_platform.downloads import pdf_attachment
from leg_platform.management import lists
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.contract_forms import ContractUploadForm
from leg_platform.management.location_views import DialogView, event_label
from leg_platform.management.metering_point_views import format_date
from leg_platform.management.returns import return_url

logger = logging.getLogger(__name__)

STATE_TONES = {
    ContractVersion.State.CURRENT: "ok",
    ContractVersion.State.UPLOADED: "accent",
    ContractVersion.State.REPLACED: "muted",
}
STATE_RANK = {ContractVersion.State.CURRENT: 0, ContractVersion.State.UPLOADED: 1, ContractVersion.State.REPLACED: 2}


def state_word(state):
    """The words of the owner's design: Current, Uploaded, Older."""
    return {
        ContractVersion.State.CURRENT: _("Current"),
        ContractVersion.State.UPLOADED: _("Uploaded"),
        ContractVersion.State.REPLACED: _("Older"),
    }[state]


def list_columns():
    # Start widths that fit the card of a 1280 px screen (about 967 px): the file name and the actions never shrink (the
    # three buttons of 44 px with their gaps need 148 px and the cell's padding).
    return (
        lists.Column("file", gettext_lazy("File name"), width=260, keep=True),
        lists.Column("state", gettext_lazy("Status"), width=150),
        lists.Column("uploaded", gettext_lazy("Uploaded"), width=160),
        lists.Column("acceptances", gettext_lazy("Acceptances"), width=150),
        lists.Column("actions", gettext_lazy("Actions"), sortable=False, width=172, resizable=False, keep=True),
    )


def sort_versions(rows, key, descending):
    """Sort the rows by a column (the newest upload for an unknown key); the id breaks ties."""
    pick = {
        "file": lambda row: row["file_name"].lower(),
        "state": lambda row: STATE_RANK[row["version"].state],
        "uploaded": lambda row: row["version"].uploaded_at,
        "acceptances": lambda row: row["acceptances"],
    }
    return sorted(
        rows, key=lambda row: (pick.get(key, pick["uploaded"])(row), row["version"].pk), reverse=descending
    )


class ContractListView(ManagementAccessMixin, View):
    """One row per uploaded file, newest first; the actions as icon buttons that appear only where they are allowed."""

    def get(self, request):
        columns = list_columns()
        state = lists.parse(
            request.GET, path=reverse("management:contracts"), columns=columns, default_sort="uploaded",
            default_descending=True,
        )
        zone = timezone.get_current_timezone()
        rows = []
        for version in contracts.versions():
            rows.append(
                {
                    "version": version,
                    "file_name": contracts.download_name(version),
                    "state_label": state_word(version.state),
                    "state_tone": STATE_TONES[version.state],
                    "uploaded": format_date(version.uploaded_at.astimezone(zone)),
                    "acceptances": version.acceptance_count,
                    "can_make_current": not version.is_current,
                    "can_delete": not version.is_current and contracts.can_delete(version),
                    "download_url": reverse("management:contracts_download", args=[version.pk]),
                    "current_url": reverse("management:contracts_current", args=[version.pk]),
                    "delete_url": reverse("management:contracts_delete", args=[version.pk]),
                }
            )
        rows = lists.ListRows(sort_versions(rows, state.sort, state.descending))
        page = lists.paginate(rows, state)
        context = lists.list_context(
            request, "contracts", columns, state, page,
            count_text=_("%(first)d–%(last)d of %(total)d versions") % {
                "first": page.first, "last": page.last, "total": page.total,
            },
            widths_url=reverse("management:list_widths", args=["contracts"]),
        )
        context.update(
            page_title=_("Contracts"),
            page_subtitle=_("The contract files of the LEG. Customers accept the version that is current."),
            entries=page.items,
            empty_text=_("No contract version has been uploaded yet."),
            no_current=contracts.current_version() is None,
            history=[
                {
                    "time": entry.created_at, "label": event_label(entry.event), "file_name": file_name,
                    "actor": entry.actor.display_name if entry.actor_id else "",
                }
                for entry, file_name in contracts.history_rows(request.user)
            ],
            upload_url=reverse("management:contracts_upload"),
            no_link_column=True,
        )
        return render(request, "management/contracts/list.html", context)


class ContractUploadView(DialogView):
    """"Upload a new version": a dialog with the PDF and its date. The new version is not current yet."""

    title = gettext_lazy("Upload a new version")
    submit_label = gettext_lazy("Upload")
    form_class = ContractUploadForm
    default_back = ""

    def back(self, request):
        return return_url(request, reverse("management:contracts"))

    def get_form(self, data=None):
        return ContractUploadForm(data, self.request.FILES if data is not None else None)

    def render_dialog(self, request, form, **extra):
        return super().render_dialog(request, form, multipart=True, **extra)

    @staticmethod
    def duplicate_text(request, data):
        """The refusal of a file that is stored already, with the version it is (W5-09); the plain refusal if that
        version is gone meanwhile."""
        existing = contracts.stored_version_of(request.user, data)
        if existing is None:
            return contracts.DUPLICATE_FILE
        return _("This file is already stored as the version of %(date)s.") % {
            "date": format_date(existing.version_date)
        }

    def save(self, request, form):
        try:
            contracts.upload_version(request.user, form.cleaned_data["file"], form.cleaned_data["version_date"])
        except ValidationError as error:
            if error.code == "duplicate":
                form.add_error("file", self.duplicate_text(request, form.cleaned_data["file"]))
                return None
            field = "version_date" if error.code == "required" else "file"
            form.add_error(field, error.messages)
            return None
        messages.success(request, _("The version was uploaded. It is not current yet."))
        return redirect(self.back(request))


class ContractDownloadView(ManagementAccessMixin, View):
    """The file of any version as an attachment (never shown in the page); the download is logged."""

    http_method_names = ["get", "head", "options"]

    def get(self, request, pk):
        try:
            name, data = contracts.staff_download(request.user, pk)
        except ApplicationError:
            raise Http404 from None
        logger.info("A contract version was downloaded (account %s, version %s).", request.user.pk, pk)
        return pdf_attachment(data, name)


class ContractCurrentView(ManagementAccessMixin, View):
    """"Set as current": a question first (GET, like the deletion: all new customers get this version), then the step
    (POST). Only this step makes a version visible to the customers. A version that is current already has no question."""

    http_method_names = ["get", "head", "options", "post"]

    def version(self, pk):
        return get_object_or_404(ContractVersion.objects.defer("pdf"), pk=pk)

    def get(self, request, pk):
        version = self.version(pk)
        if version.is_current:
            raise Http404
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Set as current"),
                "dialog_lead": _(
                    "Set the version of %(date)s as current? New customers accept this version. "
                    "Existing acceptances are kept."
                ) % {"date": format_date(version.version_date)},
                "submit_label": _("Set as current"),
                "back_url": reverse("management:contracts"),
                "form": None,
            },
        )

    def post(self, request, pk):
        version = self.version(pk)
        try:
            contracts.make_current(request.user, version)
        except ApplicationError:
            raise Http404 from None
        messages.success(request, _("The version is now current."))
        return redirect(reverse("management:contracts"))


class ContractDeleteView(ManagementAccessMixin, View):
    """A question first, then the deletion; only a version that never was current and that nobody accepted."""

    http_method_names = ["get", "head", "options", "post"]

    def version(self, pk):
        return get_object_or_404(ContractVersion.objects.defer("pdf"), pk=pk)

    def get(self, request, pk):
        version = self.version(pk)
        if version.is_current or not contracts.can_delete(version):
            raise Http404
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Delete version"),
                "dialog_lead": _("The version %(name)s is deleted for good.") % {"name": contracts.download_name(version)},
                "submit_label": _("Delete version"),
                "back_url": reverse("management:contracts"),
                "form": None,
                "danger": True,
            },
        )

    def post(self, request, pk):
        version = self.version(pk)
        try:
            contracts.delete_version(request.user, version)
        except ApplicationError as error:
            if error.code == "not_found":
                raise Http404 from None
            messages.error(
                request, _("This version cannot be deleted: it is current, was current or was accepted.")
            )
            return redirect(reverse("management:contracts"))
        messages.success(request, _("Deleted."))
        return redirect(reverse("management:contracts"))
