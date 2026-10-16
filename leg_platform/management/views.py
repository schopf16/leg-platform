"""Views of the management area (D-077). Every view uses ``leg_platform.management.access``."""

from django.contrib import messages
from django.core.exceptions import SuspiciousOperation
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView

from leg_platform.community import addressing, settings_service
from leg_platform.community.models import LEGAL_MINIMUM_QUOTA_PERCENT, ManagementSettings
from leg_platform.management.access import ManagementAccessMixin
from leg_platform.management.forms import ADDRESS_FIELDS, ManagementSettingsForm
from leg_platform.management.navigation import AREA_VIEW_PERMISSIONS

VIEW_SETTINGS = "community.view_managementsettings"
CHANGE_SETTINGS = "community.change_managementsettings"


class OverviewView(ManagementAccessMixin, TemplateView):
    """The start page of the management area (``uebersicht/UMSETZUNG.md``). Whoever may open one of
    the areas may open it; it shows only what the account may see. The lists with problems and the
    button "Filter problems" are added with the lists."""

    template_name = "management/overview.html"
    any_permission_required = AREA_VIEW_PERMISSIONS

    def get_context_data(self, **kwargs):
        return super().get_context_data(page_title=_("Overview"), problem_lists=[], **kwargs)


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
