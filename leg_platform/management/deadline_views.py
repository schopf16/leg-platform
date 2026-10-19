"""Saving the chapter "Reminders and deadlines" of the settings page (KONZEPT 5.8, D-103).

The chapter has its own form and button (the other sections of the settings page are one form with address checks). A
change that would roll back drafts or applications at the grid operator is shown first with the two numbers from
``deadline_settings.affected_counts`` and needs a second, explicit step; the service recomputes the numbers after the
lock of the settings row, and if they differ from the ones the person saw (``CountChanged``) the question is shown again
with the new numbers. The deadlines and the run time are saved in one transaction. The history (field names only) and
the whole-number rule are in the service.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from leg_platform.community import deadline_settings, grid_operator
from leg_platform.community.models import ManagementSettings
from leg_platform.management.deadline_forms import DeadlinesForm
from leg_platform.management.forms import ManagementSettingsForm
from leg_platform.management.views import SettingsView


class DeadlinesView(SettingsView):
    """POST: check the chapter, ask once when drafts would be deleted, then save. A GET leads back to the page."""

    http_method_names = ["get", "head", "options", "post"]

    def get(self, request):
        return redirect("management:settings")

    def post(self, request):
        form = DeadlinesForm(request.POST)
        if not form.is_valid():
            row = ManagementSettings.load()
            return self.show(
                request, ManagementSettingsForm(instance=row),
                self.address_check(row.sender_address(), row.sender_suggestion_dismissed_for), deadline_form=form,
            )
        values = form.deadline_values()
        confirmed = request.POST.get("confirmed_count", "")
        try:
            # The numbers are recomputed in the service after the row lock; the deadlines and the run time are one
            # transaction.
            deadline_settings.update(
                values, request.user, expected=confirmed, run_time=form.cleaned_data["run_time"]
            )
        except deadline_settings.CountChanged as changed:
            return self.question(request, form, changed.affected)
        messages.success(request, _("Saved."))
        return redirect("management:settings")

    def question(self, request, form, affected):
        """The warning with the two numbers and the second step; the posted values travel as hidden fields and are
        checked again, whatever they say."""
        hidden = [(name, form.data.get(name, "")) for name in (*deadline_settings.FIELDS, "run_time")]
        hidden.append(("confirmed_count", affected.token))
        sentences = []
        if affected.drafts:
            sentences.append(
                ngettext(
                    "This change deletes %(count)d draft. The next run of the scheduled tasks rolls it back and "
                    "the customer receives an email.",
                    "This change deletes %(count)d drafts. The next run of the scheduled tasks rolls them back and "
                    "the customers receive an email.",
                    affected.drafts,
                ) % {"count": affected.drafts}
            )
        if affected.at_operator:
            sentences.append(
                ngettext(
                    "%(count)d application that is already at the %(operator)s is rolled back. The staff must "
                    "withdraw its registration there.",
                    "%(count)d applications that are already at the %(operator)s are rolled back. The staff must "
                    "withdraw their registrations there.",
                    affected.at_operator,
                ) % {"count": affected.at_operator, "operator": grid_operator.name()}
            )
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Confirm the change"),
                "dialog_lead": " ".join(sentences),
                "submit_label": _("Save the change"),
                "back_url": reverse("management:settings"),
                "form": None,
                "danger": True,
                "hidden_values": hidden,
            },
        )
