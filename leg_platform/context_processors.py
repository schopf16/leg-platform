"""Template context of the instance configuration (docs/DESIGN.md, section 3.2)."""

from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject

from leg_platform.accounts.login_protection import LOGIN_COOLOFF
from leg_platform.accounts.portal_settings import legal_links
from leg_platform.accounts.second_factor import is_second_factor_complete, requires_second_factor


PROFILE_VIEWS = frozenset(
    {
        "accounts:profile",
        "accounts:profile_edit",
        "accounts:account_delete",
        "accounts:password_change",
        "accounts:two_factor",
        "accounts:two_factor_setup",
        "accounts:two_factor_confirm",
        "accounts:recovery_codes",
        "accounts:two_factor_disable",
        "accounts:email_change_request",
        "accounts:email_change_cancel",
        "accounts:recovery_codes_done",
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
    if user.password_change_required:
        return True
    return requires_second_factor(user) and not is_second_factor_complete(user)


def instance(request):
    # The addresses of the footer are read from the database only when a template uses them: an
    # error page, which does not show the footer, does not need it for them (D-054, D-056). The
    # frame state below reads the session of a logged-in account; the 500 page is rendered without
    # this processor, and if it fails for a 403 or 404 page, Django answers with the 500 page.
    links = SimpleLazyObject(lambda: legal_links(request))
    return {
        "frame_restricted": frame_restricted(request),
        # The "Profile" entry of the menu is active on the profile pages only (review N-/E-4).
        "frame_profile_active": getattr(
            getattr(request, "resolver_match", None), "view_name", None
        )
        in PROFILE_VIEWS,
        "instance_name": settings.INSTANCE_NAME,
        # The addresses of the footer: maintained in the portal settings (the privacy address
        # falls back to the instance configuration), D-056.
        "instance_privacy_url": SimpleLazyObject(lambda: links.privacy_url),
        "instance_imprint_url": SimpleLazyObject(lambda: links.imprint_url),
        "instance_website_url": settings.INSTANCE_WEBSITE_URL,
        # Only the host is shown as the text of the link (validated https URL, so it has one).
        "instance_website_host": urlsplit(settings.INSTANCE_WEBSITE_URL).hostname or "",
        "instance_logo_light": settings.INSTANCE_BRANDING.logo_light,
        "instance_logo_dark": settings.INSTANCE_BRANDING.logo_dark,
        "print_date": timezone.localdate(),
        # The duration of a temporary lockout, for the lockout page (the same value the abuse
        # protection uses, docs/SECURITY.md, section 3).
        "login_cooloff_minutes": int(LOGIN_COOLOFF.total_seconds() // 60),
    }
