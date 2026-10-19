"""Saving the chapter "Reminders and deadlines" of the settings page (KONZEPT 5.8, D-103) and the small block "Address
directory" (the time of the daily run).

The chapter has its own form and button (the other sections of the settings page are one form with address checks). A
change that would roll back drafts or applications at the grid operator is shown first with the old and the new value and
the two numbers from ``deadline_settings.affected_counts`` (no names: the service gives numbers only), and needs a second,
explicit step with a red button that says what it deletes; the service recomputes the numbers after the lock of the
settings row, and if they differ from the ones the person saw (``CountChanged``) the question is shown again with the new
numbers. The history (field names only) and the whole-number rule are in the service.
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from leg_platform.community import deadline_settings, grid_operator
from leg_platform.community.models import ManagementSettings
from leg_platform.management.deadline_forms import DEADLINES, DeadlinesForm, RunTimeForm
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
            # The numbers are recomputed in the service after the row lock.
            deadline_settings.update(values, request.user, expected=confirmed)
        except deadline_settings.CountChanged as changed:
            return self.question(request, form, values, changed.affected)
        messages.success(request, _("Saved."))
        return redirect("management:settings")

    @staticmethod
    def change_sentences(values):
        """"Deadline X: you change it from 14 to 5 days." for every changed deadline (the old value is the stored one)."""
        current = deadline_settings.current_values()
        sentences = []
        for deadline in DEADLINES:
            old, new = current[deadline.field], values.get(deadline.field)
            if new is not None and new != old:
                sentences.append(
                    ngettext(
                        "Deadline “%(name)s”: you change it from %(old)d to %(new)d day.",
                        "Deadline “%(name)s”: you change it from %(old)d to %(new)d days.",
                        new,
                    ) % {"name": deadline.name, "old": old, "new": new}
                )
        return sentences

    @staticmethod
    def consequence_sentences(affected):
        """What the change deletes or rolls back, in words, and that it cannot be undone."""
        sentences = []
        if affected.drafts:
            sentences.append(
                ngettext(
                    "This permanently deletes %(count)d draft. The customer receives an email.",
                    "This permanently deletes %(count)d drafts. The customers receive an email.",
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
        sentences.append(_("This cannot be undone."))
        return sentences

    @staticmethod
    def button_label(affected):
        """The red button names what it does: "Save and delete 1 draft" (and roll back N applications)."""
        parts = []
        if affected.drafts:
            parts.append(
                ngettext("delete %(count)d draft", "delete %(count)d drafts", affected.drafts)
                % {"count": affected.drafts}
            )
        if affected.at_operator:
            parts.append(
                ngettext(
                    "roll back %(count)d application", "roll back %(count)d applications", affected.at_operator
                ) % {"count": affected.at_operator}
            )
        actions = parts[0] if len(parts) == 1 else _("%(first)s and %(second)s") % {"first": parts[0], "second": parts[1]}
        return _("Save and %(actions)s") % {"actions": actions}

    def question(self, request, form, values, affected):
        """The warning and the second step; the posted values travel as hidden fields and are checked again, whatever
        they say."""
        hidden = [(name, form.data.get(name, "")) for name in deadline_settings.FIELDS]
        hidden.append(("confirmed_count", affected.token))
        return render(
            request,
            "management/dialog_form.html",
            {
                "page_title": _("Confirm the change"),
                "dialog_lead": " ".join([*self.change_sentences(values), *self.consequence_sentences(affected)]),
                "submit_label": self.button_label(affected),
                "back_url": reverse("management:settings"),
                "form": None,
                "danger": True,
                "hidden_values": hidden,
            },
        )


class RunTimeView(SettingsView):
    """POST: the time of the daily address directory run (its own small block of the settings page). A GET leads back."""

    http_method_names = ["get", "head", "options", "post"]

    def get(self, request):
        return redirect("management:settings")

    def post(self, request):
        form = RunTimeForm(request.POST)
        if not form.is_valid():
            row = ManagementSettings.load()
            return self.show(
                request, ManagementSettingsForm(instance=row),
                self.address_check(row.sender_address(), row.sender_suggestion_dismissed_for), run_time_form=form,
            )
        deadline_settings.set_address_run_time(form.cleaned_data["run_time"], request.user)
        messages.success(request, _("Saved."))
        return redirect("management:settings")
