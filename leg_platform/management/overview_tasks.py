"""The work queues on the overview of the management area (D-103; KONZEPT 5.6): what the staff has to do with the
onboarding, from ``community.application_queue``. Drafts never appear. A row has a count that equals the rows of the list
it links to (``process_list``), the oldest few entries and a link into the filtered list; a queue without entries has no
row. The notices and the missing contract have their own row.
"""

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from leg_platform.community import application_queue, grid_operator, process_list
from leg_platform.community.models import Application
from leg_platform.management.metering_point_views import format_date

PREVIEW = 5


def _points_text(count):
    return ngettext("%(count)d metering point", "%(count)d metering points", count) % {"count": count}


def _application_entries(found):
    zone = timezone.get_current_timezone()
    entries = []
    for row in found[:PREVIEW]:
        parts = [_points_text(row.metering_points)]
        if row.submitted_at is not None:
            parts.insert(0, _("submitted %(date)s") % {"date": format_date(row.submitted_at.astimezone(zone))})
        entries.append(
            {
                "label": row.person_label,
                "sub": " · ".join(parts),
                "url": reverse("management:processes_application", args=[row.application_id]),
            }
        )
    return entries


def _deregistration_entries(found):
    return [
        {
            "label": row.person_label,
            "sub": " · ".join(
                [_("wanted for %(date)s") % {"date": format_date(row.desired_date)}, _points_text(row.metering_points)]
            ),
            "url": reverse("management:processes_deregistration", args=[row.deregistration_id]),
        }
        for row in found[:PREVIEW]
    ]


def _list_url(state):
    return f"{reverse('management:processes')}?state={state}"


def task_rows(actor):
    """The rows of the card "To do" for an active staff account, in the order of the way an application goes."""
    counts = application_queue.queue_counts()
    operator = grid_operator.name()
    State = Application.State
    rows = []

    def add(count, text, url, label, entries=()):
        if count:
            rows.append({"count": count, "text": text, "url": url, "label": label, "entries": list(entries)})

    count = counts["applications_submitted"]
    add(
        count,
        ngettext(
            "%(count)d application waiting for review", "%(count)d applications waiting for review", count
        ) % {"count": count},
        _list_url(State.SUBMITTED.value), _("Go to the applications waiting for review"),
        _application_entries(application_queue.pending_applications(actor, (State.SUBMITTED,))),
    )
    count = counts["applications_in_review"]
    add(
        count,
        ngettext("%(count)d application in review", "%(count)d applications in review", count) % {"count": count},
        _list_url(State.IN_REVIEW.value), _("Go to the applications in review"),
        _application_entries(application_queue.pending_applications(actor, (State.IN_REVIEW,))),
    )
    count = counts["applications_at_operator"]
    add(
        count,
        ngettext(
            "%(count)d application at the %(operator)s", "%(count)d applications at the %(operator)s", count
        ) % {"count": count, "operator": operator},
        _list_url(process_list.STATE_AT_OPERATOR), _("Go to the applications at the grid operator"),
        _application_entries(application_queue.applications_at_operator(actor)),
    )
    count = counts["deregistrations_waiting"]
    add(
        count,
        ngettext(
            "%(count)d deregistration waiting for its date", "%(count)d deregistrations waiting for their date", count
        ) % {"count": count},
        _list_url(process_list.STATE_DEREGISTRATION_SUBMITTED), _("Go to the deregistrations waiting for their date"),
        _deregistration_entries(application_queue.waiting_deregistrations(actor)),
    )
    count = counts["problems"]
    add(
        count,
        ngettext("%(count)d notice for the staff", "%(count)d notices for the staff", count) % {"count": count},
        reverse("management:processes_notices"), _("Go to the notices"),
    )
    if counts["contract_missing"]:
        rows.append(
            {
                "count": None,
                "text": _("No contract version is current. Customers cannot accept the contract."),
                "url": reverse("management:contracts"), "label": _("Go to the contracts"), "entries": [],
            }
        )
    return rows
