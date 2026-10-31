"""The "Print list" dialog (GET) and the PDF (POST) per list (D-082).

The list state is re-parsed from the dialog address and queried with the page's ``build_list``. The bounds
(``areas.MAX_*``) refuse oversized printouts. The PDF is made in memory, sent ``no-store``; nothing of it is logged.
"""

import logging

from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View

from leg_platform.accounts import security_log
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.printing import areas, pdf

logger = logging.getLogger(__name__)

SCOPE_LIST = "list"
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
SCOPES = (SCOPE_LIST, SCOPE_ALL, SCOPE_SELECTED)
MAX_POSTED_IDS = areas.MAX_SELECTABLE


class PrintView(ManagementAccessMixin, View):
    """The dialog and the PDF of one list; a subclass names the area (``areas.AREAS``)."""

    area_key = ""
    http_method_names = ["get", "head", "options", "post"]

    @property
    def area(self):
        return areas.AREAS[self.area_key]

    def get(self, request):
        prepared = self.area.prepare(request.GET)
        return self.show(request, prepared, selected_ids=set(), scope=SCOPE_LIST)

    def post(self, request):
        scope = request.POST.get("scope", "")
        ids = request.POST.getlist("ids")
        if scope not in SCOPES or len(ids) > MAX_POSTED_IDS:
            raise SuspiciousOperation("Invalid print request.")
        prepared = self.area.prepare(request.GET)
        selected_ids = {value for value in ids if value.isascii() and value.isdigit() and len(value) < 12}
        error = self.check(prepared, scope)
        if error:
            return self.show(request, prepared, selected_ids, scope, error)
        today = timezone.localdate()
        items = prepared.load()
        if scope == SCOPE_LIST:
            chosen = []
        elif scope == SCOPE_ALL:
            chosen = items
        else:
            # Ids are matched against this list's entries; foreign ids are ignored.
            chosen = [item for item in items if str(item.pk) in selected_ids]
            if not chosen:
                return self.show(request, prepared, selected_ids, scope, _("Select at least one entry."))
            if len(chosen) > areas.MAX_DETAIL_PAGES:
                return self.show(request, prepared, selected_ids, scope, self.too_many_details())
        document = self.area.document(prepared, items, chosen, today)
        if document.row_count() > areas.MAX_ROWS:
            return self.show(request, prepared, selected_ids, scope, _("The printout is too large. Choose fewer entries."))
        try:
            data = pdf.render(document)
        except pdf.PdfError as error:
            # Only the kind of the error, never document text.
            logger.error("The PDF of a list could not be created (%s).", error)
            return self.show(request, prepared, selected_ids, scope, _("The PDF could not be created."))
        response = HttpResponse(data, content_type="application/pdf")
        # Security log: who printed which list; no content. Person details hold the IBAN, so say so.
        with_iban = ", with IBAN" if self.area.key == "persons" and chosen else ""
        security_log.record(
            security_log.Event.LIST_PRINTED, actor=request.user,
            detail=f"list {self.area.key}, scope {scope}, {len(items)} entries, {len(chosen)} detail pages{with_iban}",
        )
        response["Content-Disposition"] = f'attachment; filename="{self.area.filename}"'
        patch_cache_control(response, no_store=True, private=True)
        return response

    @staticmethod
    def too_many_details():
        return _("At most %(max)d detail pages can be printed at once. Narrow the list or choose fewer entries.") % {
            "max": areas.MAX_DETAIL_PAGES
        }

    def check(self, prepared, scope):
        """The refusal text for a request that is beyond the bounds, else ""."""
        if prepared.total > areas.MAX_LIST_ENTRIES:
            return self.too_long()
        if scope == SCOPE_ALL and prepared.total > areas.MAX_DETAIL_PAGES:
            return self.too_many_details()
        if scope == SCOPE_SELECTED and prepared.total > areas.MAX_SELECTABLE:
            return self.too_many_to_select()
        return ""

    @staticmethod
    def too_many_to_select():
        return _("There are too many entries to select from. Narrow the list with the search or the filters.")

    @staticmethod
    def too_long():
        return _("The list is too long to print. It has more than %(max)d entries. Narrow it with the search or the filters.") % {
            "max": areas.MAX_LIST_ENTRIES
        }

    def show(self, request, prepared, selected_ids, scope, error=""):
        too_long = prepared.total > areas.MAX_LIST_ENTRIES
        selectable = prepared.total <= areas.MAX_SELECTABLE
        items = [] if too_long or not selectable else prepared.load()
        notice = error or (self.too_long() if too_long else "")
        context = {
            "page_title": _("Print list"),
            "dialog_lead": self.lead(prepared.total),
            "submit_label": _("Create PDF"),
            "back_url": prepared.back_url,
            "scope": scope,
            "scope_list": SCOPE_LIST,
            "scope_all": SCOPE_ALL,
            "scope_selected": SCOPE_SELECTED,
            "all_allowed": prepared.total <= areas.MAX_DETAIL_PAGES,
            "all_hint": "" if prepared.total <= areas.MAX_DETAIL_PAGES else self.too_many_details(),
            "selectable": selectable,
            "select_hint": "" if selectable else self.too_many_to_select(),
            "can_print": not too_long,
            "no_submit": too_long,
            "notice": notice,
            "choices": [{"pk": item.pk, "label": item.label, "checked": str(item.pk) in selected_ids} for item in items],
            "max_details": areas.MAX_DETAIL_PAGES,
        }
        return render(request, "management/print_dialog.html", context)

    @staticmethod
    def lead(total):
        return ngettext(
            "The list is printed as it is filtered and sorted. %(count)d entry will be printed.",
            "The list is printed as it is filtered and sorted. %(count)d entries will be printed.",
            total,
        ) % {"count": total}


class PersonPrintView(PrintView):
    area_key = "persons"


class LocationPrintView(PrintView):
    area_key = "locations"


class LegPrintView(PrintView):
    area_key = "legs"


class MeteringPointPrintView(PrintView):
    area_key = "metering_points"
