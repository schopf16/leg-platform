"""The dialog "Print list" and the PDF (D-082; ``LISTEN-STANDARD.md``, section 10).

One view per list, all the same: GET shows the dialog (a normal page that works without JavaScript; "Cancel" is a
link back to the list), POST answers with the PDF. The address of the dialog carries the state of the list
(``q``, the filters, ``sort``, ``dir``, ``problems``); it is parsed again here with the list framework and
queried with the same function as the page, so the printout is what the page shows, never what a browser says
it shows. The right is the right to view the list (``management.access``), the same as for the page.

The bounds (``areas.MAX_*``) keep a printout from using up the server: a list with more entries than the bound is
refused with a message, so are more detail pages than the bound and a printout with too many rows. The PDF is made
in memory, sent with ``Cache-Control: no-store`` and a fixed file name, and nothing of it is logged.
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

from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.printing import areas, pdf

logger = logging.getLogger(__name__)

SCOPE_LIST = "list"
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
SCOPES = (SCOPE_LIST, SCOPE_ALL, SCOPE_SELECTED)
MAX_POSTED_IDS = 2 * areas.MAX_LIST_ENTRIES


class PrintView(ManagementAccessMixin, View):
    """The dialog and the PDF of one list; a subclass names the area (``areas.AREAS``)."""

    area_key = ""
    http_method_names = ["get", "head", "options", "post"]

    @property
    def area(self):
        return areas.AREAS[self.area_key]

    def get_required_permissions(self):
        return (self.area.permission,)

    def get(self, request):
        prepared = self.area.prepare(request.user, request.GET)
        return self.show(request, prepared, selected_ids=set(), scope=SCOPE_LIST)

    def post(self, request):
        scope = request.POST.get("scope", "")
        ids = request.POST.getlist("ids")
        if scope not in SCOPES or len(ids) > MAX_POSTED_IDS:
            raise SuspiciousOperation("Invalid print request.")
        prepared = self.area.prepare(request.user, request.GET)
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
            # The ids are matched against the entries of this list; an id of another entry is ignored.
            chosen = [item for item in items if str(item.pk) in selected_ids]
            if not chosen:
                return self.show(request, prepared, selected_ids, scope, _("Select at least one entry."))
            if len(chosen) > areas.MAX_DETAIL_PAGES:
                return self.show(request, prepared, selected_ids, scope, self.too_many_details())
        document = self.area.document(request.user, prepared, items, chosen, today)
        if document.row_count() > areas.MAX_ROWS:
            return self.show(request, prepared, selected_ids, scope, _("The printout is too large. Choose fewer entries."))
        try:
            data = pdf.render(document)
        except pdf.PdfError as error:
            # Only the kind of the error: never a text of the document.
            logger.error("The PDF of a list could not be created (%s).", error)
            return self.show(request, prepared, selected_ids, scope, _("The PDF could not be created."))
        response = HttpResponse(data, content_type="application/pdf")
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
        return ""

    @staticmethod
    def too_long():
        return _("The list is too long to print. It has more than %(max)d entries. Narrow it with the search or the filters.") % {
            "max": areas.MAX_LIST_ENTRIES
        }

    def show(self, request, prepared, selected_ids, scope, error=""):
        too_long = prepared.total > areas.MAX_LIST_ENTRIES
        items = [] if too_long else prepared.load()
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
