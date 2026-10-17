"""Views of the management area (D-077). Every view uses ``leg_platform.management.access``."""

from dataclasses import dataclass

from django.contrib import messages
from django.core.exceptions import SuspiciousOperation
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View
from django.views.generic import TemplateView

from leg_platform.community import addressing, problems, settings_service
from leg_platform.community.models import LEGAL_MINIMUM_QUOTA_PERCENT, ManagementSettings
from leg_platform.community.settings_service import EDITABLE_FIELDS
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.forms import ADDRESS_FIELDS, ManagementSettingsForm
from leg_platform.management.navigation import AREA_VIEW_PERMISSIONS

VIEW_SETTINGS = "community.view_managementsettings"
CHANGE_SETTINGS = "community.change_managementsettings"


@dataclass(frozen=True)
class ProblemList:
    """A list that can have problems: the key of ``problems.problem_counts``, the right to view it, the URL
    name and the two texts. The order of ``PROBLEM_LISTS`` is the order of the menu."""

    key: str
    permission: str
    url_name: str
    name: object
    text: object


PROBLEM_LISTS = (
    ProblemList(
        "locations", "community.view_location", "management:locations", lambda: _("Locations"),
        lambda count: ngettext(
            "%(count)d location with a problem", "%(count)d locations with a problem", count
        ) % {"count": count},
    ),
    ProblemList(
        "legs", "community.view_leg", "management:legs", lambda: _("LEGs"),
        lambda count: ngettext("%(count)d LEG with a problem", "%(count)d LEGs with a problem", count)
        % {"count": count},
    ),
    ProblemList(
        "metering_points", "community.view_meteringpoint", "management:metering_points",
        lambda: _("Metering points"),
        lambda count: ngettext(
            "%(count)d metering point with a problem", "%(count)d metering points with a problem", count
        ) % {"count": count},
    ),
)


class OverviewView(ManagementAccessMixin, TemplateView):
    """The start page of the management area (``uebersicht/UMSETZUNG.md``). Whoever may open one of the areas
    may open it; it shows only what the account may see: a row for each list with a problem, for the lists the
    account may view only (the counts of another list are not even computed), and the button "Filter problems"
    with the sum of those counts. The button opens the first of the affected lists (in the order of the menu)
    with the problem filter on; from there each list has its own switch."""

    template_name = "management/overview.html"
    any_permission_required = AREA_VIEW_PERMISSIONS

    def get_context_data(self, **kwargs):
        user = self.request.user
        allowed = [entry for entry in PROBLEM_LISTS if user.has_perm(entry.permission)]
        counts = problems.problem_counts(only=[entry.key for entry in allowed])
        rows = []
        for entry in allowed:
            count = counts[entry.key]
            if count:
                url = reverse(entry.url_name) + "?problems=1"
                rows.append(
                    {
                        "name": entry.name(),
                        "text": entry.text(count),
                        "count": count,
                        "url": url,
                        "label": _("Go to the list %(name)s") % {"name": entry.name()},
                    }
                )
        return super().get_context_data(
            page_title=_("Overview"),
            problem_lists=rows,
            problem_count=sum(row["count"] for row in rows),
            filter_url=rows[0]["url"] if rows else "",
            **kwargs,
        )


class SettingsFieldCheckView(ManagementAccessMixin, View):
    """The check of one field of the settings page on leaving the field (the script
    ``settings-validate.js`` asks; without a script the server checks everything when saving): the answer is
    the message of the form itself, so the text is the one of the server (D-084). Nothing is saved."""

    permission_required = (VIEW_SETTINGS,)
    http_method_names = ["get"]

    def get(self, request):
        name = request.GET.get("field", "")
        if name not in EDITABLE_FIELDS:
            raise Http404
        value = request.GET.get("value", "")[:200]
        form = ManagementSettingsForm({name: value})
        form.full_clean()
        errors = form.errors.get(name)
        return JsonResponse({"message": str(errors[0]) if errors else ""})


class SettingsView(ManagementAccessMixin, View):
    """The settings page (``einstellungen/``): showing needs the right to view, saving the right to
    change. One button saves all sections. The directory check of the sender address is made on the
    server (when the page is shown), not while typing."""

    template_name = "management/settings.html"
    http_method_names = ["get", "head", "options", "post"]

    def get_required_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return (VIEW_SETTINGS,)
        return (CHANGE_SETTINGS,)

    def get(self, request):
        row = ManagementSettings.load()
        form = ManagementSettingsForm(instance=row)
        return self.show(request, form, self.address_check(row.sender_address(), row.sender_suggestion_dismissed_for))

    def post(self, request):
        action = request.POST.get("action", "save")
        if action not in ("save", "accept_suggestion", "dismiss_suggestion"):
            raise SuspiciousOperation("Unknown action.")
        row = ManagementSettings.load()
        form = ManagementSettingsForm(request.POST, instance=row)
        valid = form.is_valid()
        if action == "save":
            if valid:
                settings_service.update(form.settings_values(), request.user)
                messages.success(request, _("Saved."))
                return redirect("management:settings")
            return self.show(request, form, {})
        return self.decide_suggestion(request, form, action)

    def decide_suggestion(self, request, form, action):
        """"Accept" puts the suggestion into the fields (it is saved only with the button "Save");
        "Do not accept" remembers the address as it is. Nothing else is saved."""
        initial = {name: form.data.get(name, "") for name in form.fields}
        suggestion = None
        if form.sender_fields_are_valid():
            values = tuple(form.cleaned_data[name] for name in ADDRESS_FIELDS)
            suggestion = addressing.suggest_correction(*values)
            if suggestion is not None and action == "accept_suggestion":
                initial.update(zip(ADDRESS_FIELDS, suggestion.as_tuple(), strict=True))
                settings_service.accept_sender_suggestion(suggestion, request.user)
            elif suggestion is not None:
                settings_service.dismiss_sender_suggestion(addressing.address_signature(*values), request.user)
        row = ManagementSettings.load()
        shown = tuple(initial[name].strip() for name in ADDRESS_FIELDS)
        return self.show(
            request,
            ManagementSettingsForm(initial=initial),
            self.address_check(shown, row.sender_suggestion_dismissed_for),
        )

    @staticmethod
    def address_check(address, dismissed_for):
        """The banner or the notice about the sender address: ``{"suggestion": text, "notice": text}``
        (at most one of them). Nothing for an incomplete address."""
        if not all(address):
            return {}
        check = addressing.check_address(*address)
        if check.suggestion is not None:
            if addressing.address_signature(*address) == dismissed_for:
                return {}
            return {"suggestion": addressing.suggestion_banner_text(check.suggestion)}
        if check.not_found_notice:
            return {"notice": check.not_found_notice}
        return {}

    def show(self, request, form, check):
        can_change = request.user.has_perm(CHANGE_SETTINGS)
        if not can_change:
            # The right to view only: the values are shown, nothing can be typed or saved.
            for field in form.fields.values():
                field.disabled = True
        return render(
            request,
            self.template_name,
            {
                "page_title": _("Settings"),
                "form": form,
                "suggestion_text": check.get("suggestion", "") if can_change else "",
                "notice_text": check.get("notice", ""),
                "can_change": can_change,
                # The legal minimum is a constant of the law, shown as a fixed note.
                "quota_hint": _("Applies to all LEGs. The law requires at least %(minimum)d %%.")
                % {"minimum": LEGAL_MINIMUM_QUOTA_PERCENT},
            },
        )
