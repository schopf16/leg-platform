"""The form of the contract page: upload of a new version (D-103; owner ``anmeldungprozess/SCHRITT-4-VERTRAG.md``,
section 3a). The form only reads the upload; the checks of the file (size, signature, no active content) are in
``community.contract_pdf`` and run again in ``contracts.upload_version``. The uploaded file name is never used."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from leg_platform.community import contract_pdf
from leg_platform.management.form_fields import date_field
from leg_platform.management.metering_point_forms import DialogForm


class ContractUploadForm(DialogForm):
    """The PDF and the date of the version. ``cleaned_data["file"]`` is the bytes (read once, at most the size limit
    plus one byte, so that a file that is too large is refused without reading all of it)."""

    file = forms.FileField(
        label=_("PDF file"),
        widget=forms.FileInput(attrs={"class": "form-control", "accept": ".pdf,application/pdf"}),
        error_messages={
            "required": _("Choose a PDF file."),
            "missing": _("Choose a PDF file."),
            "empty": _("The file is empty."),
            "invalid": _("The file could not be read."),
        },
    )
    version_date = date_field(_("Version date"), initial="today")
    layout = [("single", ["file"]), ("single", ["version_date"])]
    hint = _(
        "A new version is not current yet: customers see only the version that is set as current. "
        "PDF only, at most 10 MiB."
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > contract_pdf.MAX_SIZE:
            raise ValidationError(contract_pdf.TOO_LARGE, code="too_large")
        return upload.read(contract_pdf.MAX_SIZE + 1)
