"""Template context of the instance configuration (docs/DESIGN.md, section 3.2)."""

from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from leg_platform import render_as
from leg_platform.accounts.login_protection import LOGIN_COOLOFF
from leg_platform.accounts.portal_settings import portal_values
from leg_platform.accounts.second_factor import is_second_factor_complete, requires_second_factor
from leg_platform.environment import DEFAULT_INSTANCE_NAME


PROFILE_VIEWS = frozenset(
    {
        "accounts:profile",
        "accounts:profile_edit",
        "accounts:account_delete",
        "accounts:account_end",
        "accounts:password_change",
        "accounts:two_factor",
        "accounts:login_method",
        "accounts:two_factor_setup",
        "accounts:two_factor_confirm",
        "accounts:recovery_codes",
        "accounts:two_factor_disable",
        "accounts:two_factor_lost_phone",
        "accounts:email_change_request",
        "accounts:email_change_cancel",
        "accounts:recovery_codes_done",
        "accounts:passkeys",
        "accounts:passkey_setup",
    }
)


def frame_restricted(request):
    """Whether the frame shows only the header and "Log out" (D-057).

    For an account that must replace its password and for an account whose mandatory second
    factor is not complete in this session: both can do nothing else, so the menu would only
    offer links that lead back here (security review of phase 2, N-2).
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if user.password_change_required or user.authentication_method == "recovery":
        return True
    return requires_second_factor(user) and not is_second_factor_complete(user)


STAFF_NAMESPACES = ("management:", "admin:")


def frame_workspace(request):
    """The workspace the page belongs to: "staff" (the staff area: the management area and the system administration,
    D-113) or "participant" ("My area": everything else). Derived from the resolved view name only; the staff
    workspace is shown only to an account that may use the management area, so a page that is refused (a 404 for a
    customer under ``/management/``) never wears the staff look. The menu and the header show a place, they do not
    authorise anything."""
    from leg_platform.management.access import is_management_user

    user = getattr(request, "user", None)
    name = getattr(getattr(request, "resolver_match", None), "view_name", "") or ""
    if user is not None and name.startswith(STAFF_NAMESPACES) and is_management_user(user):
        return "staff"
    return "participant"


def instance(request):
    # The values of the portal settings (name, addresses, logos) are read from the database only
    # when a template uses them: an error page, which does not show the footer, does not need
    # them for them (D-054, D-056). One query per request, shared with the other users of the
    # accessor (D-073). The frame state below reads the session of a logged-in account; the 500
    # page is rendered without this processor, and if it fails for a 403 or 404 page, Django
    # answers with the 500 page.
    values = SimpleLazyObject(lambda: portal_values(request))
    in_view_as = render_as.is_active(request)
    return {
        # The support view "View as" (D-131): ``frame_subject`` is the person whose pages are shown (the logged-in
        # account otherwise); the header, the banner and the menu use it, ``user`` stays the staff member.
        "render_as": in_view_as,
        "frame_subject": render_as.subject(request),
        "render_as_paths": render_as.allowed_paths() if in_view_as else "",
        "frame_restricted": frame_restricted(request),
        "frame_workspace": frame_workspace(request),
        # The "Profile" entry of the menu is active on the profile pages only (review N-/E-4).
        "frame_profile_active": getattr(
            getattr(request, "resolver_match", None), "view_name", None
        )
        in PROFILE_VIEWS,
        "instance_name": SimpleLazyObject(lambda: values.name),
        # The built-in name stands for "not set": the welcome text then spells out the community instead.
        "instance_name_is_default": SimpleLazyObject(lambda: values.name == DEFAULT_INSTANCE_NAME),
        # The addresses of the footer: maintained in the portal settings (the privacy address
        # falls back to the instance configuration), D-056, D-073.
        "instance_privacy_url": SimpleLazyObject(lambda: values.privacy_url),
        "instance_imprint_url": SimpleLazyObject(lambda: values.imprint_url),
        "instance_website_url": SimpleLazyObject(lambda: values.website_url),
        # Only the host is shown as the text of the link (validated https URL, so it has one).
        "instance_website_host": SimpleLazyObject(lambda: values.website_host),
        # The addresses of the logos (uploaded or static files of the server; "" without a logo).
        "instance_logo_light": SimpleLazyObject(lambda: values.logo_light),
        "instance_logo_dark": SimpleLazyObject(lambda: values.logo_dark),
        # The uploaded favicon ("" = the built-in one) and its content type.
        "instance_favicon": SimpleLazyObject(lambda: values.favicon),
        "instance_favicon_type": SimpleLazyObject(lambda: values.favicon_type),
        "print_date": timezone.localdate(),
        # The duration of a temporary lockout, for the lockout page (the same value the abuse
        # protection uses, docs/SECURITY.md, section 3).
        "login_cooloff_minutes": int(LOGIN_COOLOFF.total_seconds() // 60),
    }
