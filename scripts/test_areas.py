"""Test areas: which tests belong to which part of the source (D-071).

This is the ONE place that maps source paths to test labels. ``scripts/test.ps1`` uses it to run
only the tests of the area a change touches instead of the whole suite. The test
``leg_platform/tests/test_areas.py`` keeps the mapping from rotting: every test module belongs to
an area, every label and path exists and every file of the repository is mapped (or is a core
file that runs the full suite).

Rules of the resolution (``plan``):

* A changed file selects every area whose ``paths`` match it and that does not ``exclude`` it.
* A changed test module selects that module (and nothing else).
* A core file (``CORE_PATHS``) or a file that no area maps selects the FULL suite: when in doubt
  the broader run is the safe one.
* ``ALWAYS`` labels (cheap repository checks) are added to every scoped run.
* ``fresh_db`` areas never use ``--keepdb``: the run creates the test database from the
  migrations, so that a migration problem cannot be masked by an old database.

Patterns in ``paths``: a pattern ending with "/" is a directory (everything below it); any other
pattern is a ``fnmatch`` pattern over the whole repository-relative path with "/" separators
(``*`` also matches "/"). Usage: ``python scripts/test_areas.py --help``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A test label derived from a changed file name is passed to ``manage.py test`` only if it is a
# plain dotted module name (D-072, L-2); anything else selects the full suite.
TEST_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

T = "leg_platform.tests."
A = "leg_platform.accounts.tests."
D = "leg_platform.addresses.tests."
C = "leg_platform.community.tests."
M = "leg_platform.management.tests."


def _labels(prefix, *names):
    return tuple(prefix + name for name in names)


@dataclass(frozen=True)
class Area:
    description: str
    paths: tuple[str, ...]
    tests: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    fresh_db: bool = False
    checks: tuple[str, ...] = ()


# Cheap checks added to every scoped run: they scan the tracked files (no database).
ALWAYS = (T + "test_repository",)

# Extra commands an area can request after its tests (run with the same interpreter).
CHECK_COMMANDS = {
    "makemigrations": ["manage.py", "makemigrations", "--check", "--dry-run"],
    "system-check": ["manage.py", "check", "--fail-level", "WARNING"],
}

# Files whose change can affect everything: the full suite runs (the broader set is the safe one).
CORE_PATHS = (
    "manage.py",
    "pyproject.toml",
    "requirements/",
    "leg_platform/__init__.py",
    "leg_platform/settings.py",
    # Builds every security setting, the email backend, the cookie names and the base URL of the
    # email links: a change reaches the mail, login and session tests as well, so the full suite
    # runs (D-072, L-5).
    "leg_platform/environment.py",
    "leg_platform/test_settings.py",
    "leg_platform/urls.py",
    "leg_platform/middleware.py",
    "leg_platform/accounts/__init__.py",
    "leg_platform/accounts/apps.py",
    "leg_platform/accounts/models/",
    "leg_platform/accounts/backends.py",
    "leg_platform/accounts/urls.py",
    "leg_platform/addresses/__init__.py",
    "leg_platform/addresses/apps.py",
    # Shared test infrastructure of many modules.
    "leg_platform/accounts/tests/__init__.py",
    "leg_platform/accounts/tests/helpers.py",
    "leg_platform/tests/__init__.py",
    "leg_platform/tests/urls.py",
)

# The reviews of the earlier phases: cross-cutting regression modules of the user-facing flows.
_FLOW_REVIEWS = _labels(
    A,
    "test_acceptance_final",
    "test_acceptance_negative_authorization",
    "test_acceptance_owner_decisions",
    "test_final_review",
)

AREAS: dict[str, Area] = {
    "passkeys": Area(
        "D-106 WebAuthn ceremonies and method transitions.",
        paths=(
            "leg_platform/accounts/passkeys.py", "leg_platform/accounts/passkey_methods.py",
            "leg_platform/accounts/passkey_views.py", "leg_platform/accounts/tests/test_passkeys.py",
            "leg_platform/accounts/method_changes.py", "leg_platform/accounts/method_views.py",
            "leg_platform/accounts/tests/test_method_changes.py",
            "leg_platform/accounts/tests/test_passkey_script.py",
            "leg_platform/accounts/tests/test_passkey_wizard.py",
            "leg_platform/accounts/tests/test_proof_entry_points.py",
            "leg_platform/accounts/templates/accounts/method*.html",
            "leg_platform/accounts/templates/accounts/recovery*.html",
            "leg_platform/accounts/templates/accounts/login_help.html",
            "leg_platform/accounts/templates/accounts/_recovery_clarification.html",
            "leg_platform/accounts/templates/accounts/passkeys.html",
            "leg_platform/static/js/passkeys.js", "leg_platform/static/js/passkey-setup.js",
            "leg_platform/static/vendor/simplewebauthn/",
            "leg_platform/accounts/templates/accounts/passkey_setup.html",
            "leg_platform/accounts/templates/accounts/widgets/",
        ),
        tests=_labels(
            A, "test_passkeys", "test_passkey_script", "test_passkey_wizard", "test_proof_entry_points",
            "test_method_changes", "test_login_methods",
        ),
        checks=("makemigrations", "system-check"),
    ),
    "docs": Area(
        "Documentation and agent instructions only: nothing but the repository checks run.",
        paths=(
            "docs/", "README.md", "AGENTS.md", "CLAUDE.md", "CONDUCT.md", "LICENSE", ".gitignore",
            ".claude/", ".github/prompts/", ".github/dependabot.yml", ".env.example",
        ),
        tests=(T + "test_repository",),
    ),
    "packaging": Area(
        "Continuous integration and the minimum version check. The workflow is also covered by "
        "the environment area (the stage deployment check, D-072).",
        paths=(".github/workflows/", "scripts/check_minimum_constraints.py"),
        tests=(T + "test_minimum_constraints", T + "test_production_settings"),
    ),
    "testing": Area(
        "The test infrastructure itself: the areas and the runner.",
        paths=("scripts/test_areas.py", "scripts/test.ps1"),
        tests=(T + "test_areas",),
    ),
    "local-scripts": Area(
        "The local setup scripts (not covered by Django tests; run "
        "scripts/local/test-setup-dev-windows.ps1 by hand after a change).",
        paths=("scripts/local/",),
        tests=(),
    ),
    "translations": Area(
        "Catalogues (Swiss German, French, Italian): translation, wording, placeholders and the ban "
        "of the letter that Swiss German does not use.",
        paths=("locale/",),
        tests=(
            _labels(A, "test_translations", "test_usability", "test_acceptance_final",
                    "test_acceptance_owner_decisions", "test_admin_acceptance",
                    "test_portal_branding", "test_portal_favicon")
            + _labels(T, "test_error_pages", "test_presentation", "test_language")
            + ("leg_platform.customer.tests.test_translations", A + "test_workspaces")
        ),
    ),
    "language": Area(
        "The language of the pages (D-056): the language cookie, the middleware, the view, the "
        "English fallback, the language menu and its script.",
        paths=(
            "leg_platform/language.py", "leg_platform/accounts/templatetags/language_menu.py",
            "leg_platform/templates/_corner_controls.html", "leg_platform/static/js/corner-controls.js",
        ),
        tests=(
            _labels(T, "test_language", "test_settings", "test_request_body_limit", "test_design_assets",
                    "test_error_pages")
            + _labels(A, "test_entry_shell", "test_translations", "test_frame", "test_second_factor",
                      "test_invalid_link_redirect")
        ),
    ),
    "templates": Area(
        "Page templates and template tags: the rendered pages of the entry area, the frame, "
        "the profile and the administration.",
        paths=("leg_platform/templates/", "leg_platform/*/templates/", "leg_platform/*/templatetags/"),
        exclude=("leg_platform/accounts/templates/accounts/email/",),
        tests=(
            _labels(A, "test_entry_shell", "test_entry_pages", "test_frame", "test_usability",
                    "test_translations", "test_token_page_forms", "test_portal_views",
                    "test_profile", "test_profile_acceptance", "test_address_notice",
                    "test_admin_acceptance", "test_admin_spotcheck", "test_registration",
                    "test_password_reset", "test_phase4_review", "test_portal_branding",
                    "test_portal_favicon", "test_workspaces")
            + _FLOW_REVIEWS
            + _labels(T, "test_error_pages", "test_presentation", "test_sizes", "test_design_assets")
            + _labels(M, "test_access", "test_settings_page", "test_components", "test_onboarding_assets")
            + ("leg_platform.customer.tests",)
        ),
    ),
    "static": Area(
        "Style sheets, scripts, images and vendor files.",
        paths=("leg_platform/static/", "leg_platform/*/static/"),
        tests=(
            _labels(T, "test_design_assets", "test_sizes", "test_presentation", "test_error_pages")
            + _labels(A, "test_entry_shell", "test_frame", "test_usability", "test_address_notice",
                      "test_portal_branding", "test_portal_favicon", "test_workspaces")
            + _labels(M, "test_components", "test_onboarding_assets")
            + ("leg_platform.customer.tests.test_templates",)
        ),
    ),
    "branding": Area(
        "Instance presentation: branding, colour mode, instance settings, the uploaded logos and "
        "favicon and the portal legal links.",
        paths=(
            "leg_platform/branding.py", "leg_platform/presentation.py",
            "leg_platform/context_processors.py", "leg_platform/accounts/portal_settings.py",
            "leg_platform/accounts/portal_settings_form.py", "leg_platform/accounts/logo_images.py",
            "leg_platform/accounts/templatetags/instance_branding.py",
        ),
        tests=(
            _labels(T, "test_presentation", "test_instance_configuration", "test_design_assets",
                    "test_sizes", "test_request_body_limit")
            + _labels(A, "test_entry_shell", "test_portal_settings", "test_frame", "test_workspaces",
                      "test_logo_images", "test_portal_branding", "test_portal_favicon")
        ),
    ),
    "environment": Area(
        "Environment variables, settings, security settings, logging filters and error pages "
        "(leg_platform/environment.py itself is a core file: the full suite runs).",
        paths=(
            "leg_platform/debug.py", "leg_platform/log_filters.py", "leg_platform/logfiles.py",
            "leg_platform/log_views.py",
            "leg_platform/wsgi.py", "leg_platform/asgi.py", ".env.example",
        ),
        tests=_labels(
            T, "test_environment", "test_settings", "test_production_settings", "test_responses",
            "test_instance_configuration", "test_request_body_limit", "test_database",
            "test_error_pages", "test_logs",
        ),
    ),
    "address-directory": Area(
        "The address directory, its import jobs and the address suggestions.",
        paths=("leg_platform/addresses/", "leg_platform/static/css/admin-portal.css",
               "leg_platform/templates/admin/base_site.html"),
        exclude=("leg_platform/addresses/__init__.py", "leg_platform/addresses/apps.py"),
        tests=("leg_platform.addresses.tests", D + "test_plants", A + "test_address_notice", C + "test_address_check"),
    ),
    "community": Area(
        "The domain of the community (D-077): models, rules, services, the address check, the rights "
        "of the management area, and the pages built on them (the migrations have their own area).",
        paths=("leg_platform/community/", "leg_platform/accounts/permission_names.py"),
        exclude=("leg_platform/community/migrations/",),
        tests=("leg_platform.community.tests", "leg_platform.management.tests"),
    ),
    "management": Area(
        "The management area for staff users (D-077): the access check and the route walk, the menu, "
        "the pages, the shared building blocks, the stylesheet and the script.",
        paths=("leg_platform/management/", "leg_platform/downloads.py"),
        tests=("leg_platform.management.tests", C + "test_rights", T + "test_downloads"),
    ),
    "customer": Area(
        "The customer's participation (D-103): the pages Buildings and the registration wizard, the contract download, "
        "the card on the start page and the customer menu; the route walk of the access check.",
        paths=("leg_platform/customer/", "leg_platform/accounts/tasks.py", "leg_platform/downloads.py"),
        tests=(
            "leg_platform.customer.tests",
            A + "test_frame",
            A + "test_workspaces",
            C + "test_contracts",
            C + "test_building_review",
            T + "test_downloads",
        ),
    ),
    "registration": Area(
        "Registration, verification links, entry pages and the abuse protection of the forms.",
        paths=(
            "leg_platform/accounts/registration.py", "leg_platform/accounts/resend.py",
            "leg_platform/accounts/invitations.py",
            "leg_platform/accounts/bot_check.py", "leg_platform/accounts/normalization.py",
            "leg_platform/accounts/views.py", "leg_platform/accounts/forms.py",
            "leg_platform/accounts/registration_methods.py", "leg_platform/accounts/registration_views.py",
            "leg_platform/accounts/management/commands/delete_expired_registrations.py",
        ),
        tests=(
            _labels(A, "test_registration", "test_registration_hardening", "test_entry_pages",
                    "test_invalid_link_redirect", "test_token_page_forms", "test_browser_origin",
                    "test_invitations", "test_registration_methods")
            + _FLOW_REVIEWS
        ),
    ),
    "login": Area(
        "Login, lockout, sessions, session timeouts, stay signed in, password reset and the "
        "security log.",
        paths=(
            "leg_platform/accounts/login_protection.py", "leg_platform/accounts/axes_handler.py",
            "leg_platform/accounts/session_timeout.py", "leg_platform/accounts/password_reset.py",
            "leg_platform/accounts/security_log.py", "leg_platform/accounts/views.py",
            "leg_platform/accounts/forms.py",
            "leg_platform/accounts/management/commands/cleanup_login_ip_attempts.py",
            "leg_platform/accounts/management/commands/cleanup_security_log.py",
        ),
        tests=(
            _labels(A, "test_portal_views", "test_login_protection", "test_session_timeout",
                    "test_stay_signed_in", "test_password_reset", "test_security_log",
                    "test_browser_origin")
            + _FLOW_REVIEWS
        ),
    ),
    "second-factor": Area(
        "Two-factor authentication, recovery codes, lost phone, one proof per confirmation, "
        "emergency access and the initial administrator.",
        paths=(
            "leg_platform/accounts/second_factor.py", "leg_platform/accounts/second_factor_reset.py",
            "leg_platform/accounts/recovery.py",
            "leg_platform/accounts/totp.py", "leg_platform/accounts/encryption.py",
            "leg_platform/accounts/digests.py", "leg_platform/accounts/views.py",
            "leg_platform/accounts/forms.py",
            "leg_platform/accounts/management/commands/emergency_access.py",
            "leg_platform/accounts/management/commands/setup_initial_administrator.py",
            "leg_platform/accounts/management/commands/setup_instance.py",
            "leg_platform/accounts/initial_administrator.py",
            "leg_platform/accounts/management/commands/seed_design_accounts.py",
            "leg_platform/accounts/management/commands/cleanup_confirmation_failures.py",
        ),
        tests=(
            _labels(A, "test_second_factor", "test_second_factor_reset", "test_self_recovery", "test_lost_phone",
                    "test_one_proof", "test_one_proof_review", "test_recent_authentication",
                    "test_session_code_limit", "test_session_limit_review", "test_design_accounts",
                    "test_setup_instance", "test_proof_entry_points")
            + _FLOW_REVIEWS
        ),
    ),
    "profile": Area(
        "The profile, the email change, the second address and the account lifecycle.",
        paths=(
            "leg_platform/accounts/profile_views.py", "leg_platform/accounts/email_change.py",
            "leg_platform/accounts/views.py", "leg_platform/accounts/forms.py",
            "leg_platform/accounts/login_methods.py", "leg_platform/accounts/sign_in_change.py",
            "leg_platform/accounts/tests/test_sign_in_change.py",
            "leg_platform/accounts/templates/accounts/sign_in_change.html",
            "leg_platform/accounts/templates/accounts/_flow_progress.html",
        ),
        tests=(
            _labels(A, "test_profile", "test_profile_acceptance", "test_account_lifecycle",
                    "test_phase4_review", "test_address_notice", "test_recent_authentication",
                    "test_login_methods", "test_sign_in_change", "test_proof_entry_points")
            + _FLOW_REVIEWS
            + ("leg_platform.customer.tests.test_profile_cards", C + "test_profile_address")
        ),
    ),
    "logs-menu": Area(
        "The log menu (D-120): the page for system administrators only, its redacted downloads, and the e-mail notice of "
        "log events.",
        paths=("leg_platform/log_menu.py", "leg_platform/accounts/models/log_notice.py",
               "leg_platform/accounts/management/commands/notify_log_events.py"),
        tests=("leg_platform.tests.test_log_menu",),
    ),
    "admin": Area(
        "The administration: users, rights, email texts, Graph configuration and portal settings.",
        paths=("leg_platform/accounts/admin/", "leg_platform/admin_site.py", "leg_platform/community/admin.py",
               "leg_platform/community/raw_tables.py", "leg_platform/community/templatetags/",
               "leg_platform/log_views.py", "leg_platform/logfiles.py",
               "leg_platform/addresses/admin.py", "leg_platform/accounts/permission_names.py",
               "leg_platform/accounts/forms.py", "leg_platform/templates/admin/",
               "leg_platform/*/templates/admin/"),
        tests=(
            _labels(A, "test_admin", "test_admin_acceptance", "test_admin_exposure",
                    "test_admin_spotcheck", "test_portal_settings", "test_graph_test_email_limits",
                    "test_email_texts", "test_translations", "test_acceptance_negative_authorization",
                    "test_portal_branding", "test_portal_favicon", "test_workspaces")
            + _labels(T, "test_mail_graph", "test_logs", "test_language")
            + _labels(C, "test_rights", "test_admin_tables", "test_admin_raw_tables")
        ),
    ),
    "mail": Area(
        "Outgoing email: the mail queue, request limits, texts, greeting, Graph sender and the "
        "scheduled cleanup tasks.",
        paths=(
            "leg_platform/mail.py", "leg_platform/accounts/mail_queue.py",
            "leg_platform/accounts/mail_addresses.py",
            "leg_platform/accounts/emails.py", "leg_platform/accounts/email_texts.py",
            "leg_platform/accounts/greeting.py", "leg_platform/accounts/rate_limits.py",
            "leg_platform/accounts/invitations.py",
            "leg_platform/accounts/tasks.py",
            "leg_platform/accounts/templates/accounts/email/",
            "leg_platform/accounts/management/commands/process_mail_queue.py",
            "leg_platform/accounts/management/commands/cleanup_mail_requests.py",
            "leg_platform/accounts/management/commands/run_scheduled_tasks.py",
            "leg_platform/community/management/commands/process_application_deadlines.py",
        ),
        tests=(
            _labels(A, "test_mail_queue", "test_mail_addresses", "test_email_texts", "test_greeting", "test_scheduled_tasks",
                    "test_graph_test_email_limits", "test_invalid_link_redirect", "test_invitations")
            + _labels(T, "test_mail_graph", "test_instance_configuration")
            + _labels(C, "test_application_mail", "test_application_deadlines", "test_own_iban")
        ),
    ),
    "migrations": Area(
        "Database schema: migrations and the model tests. Always creates a fresh test database "
        "and checks that no migration is missing.",
        paths=("leg_platform/*/migrations/",),
        tests=(
            _labels(A, "test_models", "test_model_rules", "test_account_lifecycle", "test_mail_queue",
                    "test_mail_address_migration")
            + _labels(T, "test_database")
            + _labels(C, "test_models", "test_migration_0004", "test_iban", "test_onboarding_models")
            + ("leg_platform.addresses.tests",)
        ),
        fresh_db=True,
        checks=("makemigrations",),
    ),
}

# Management commands and package files that no topical area above names.
AREAS["accounts-support"] = Area(
    "Rarely changed accounts files (the createsuperuser command, package files): the user "
    "model tests and the scheduled tasks.",
    paths=(
        "leg_platform/accounts/management/commands/createsuperuser.py",
        "leg_platform/accounts/management/",
    ),
    exclude=(
        "leg_platform/accounts/management/commands/delete_expired_registrations.py",
        "leg_platform/accounts/management/commands/cleanup_*.py",
        "leg_platform/accounts/management/commands/emergency_access.py",
        "leg_platform/accounts/management/commands/setup_initial_administrator.py",
        "leg_platform/accounts/management/commands/setup_instance.py",
        "leg_platform/accounts/management/commands/seed_design_accounts.py",
        "leg_platform/accounts/management/commands/process_mail_queue.py",
        "leg_platform/accounts/management/commands/run_scheduled_tasks.py",
    ),
    tests=_labels(A, "test_models", "test_model_rules", "test_scheduled_tasks"),
)

FULL = "full"
SCOPED = "scoped"
NOTHING = "nothing"


def matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        # A directory pattern: everything below it (the "*" of a pattern also matches "/").
        return fnmatch.fnmatchcase(path, pattern + "*")
    return fnmatch.fnmatchcase(path, pattern)


def _any(patterns, path):
    return any(matches(pattern, path) for pattern in patterns)


def areas_of(path: str) -> list[str]:
    """Return the names of the areas that a repository-relative path belongs to."""
    return [
        name
        for name, area in AREAS.items()
        if _any(area.paths, path) and not _any(area.exclude, path)
    ]


def is_core(path: str) -> bool:
    return _any(CORE_PATHS, path)


def test_module_label(path: str) -> str | None:
    """Return the dotted label of a test module path, or None if it is not one."""
    parts = path.split("/")
    if parts[-1].startswith("test_") and parts[-1].endswith(".py") and "tests" in parts[:-1]:
        return ".".join(parts)[: -len(".py")]
    return None


def label_exists(label: str) -> bool:
    """True if the dotted label names an existing module or package of the repository."""
    base = ROOT.joinpath(*label.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def labels_cover(label: str, module: str) -> bool:
    """True if running ``label`` runs the test module ``module``."""
    return module == label or module.startswith(label + ".")


def _git_output(*arguments: str) -> bytes:
    """Run git in the repository and return its standard output; a failure stops the script."""
    try:
        result = subprocess.run(
            ["git", *arguments], cwd=ROOT, capture_output=True, check=False, timeout=60
        )
    except FileNotFoundError:
        raise SystemExit("git is not available; the changed files cannot be determined.") from None
    if result.returncode != 0:
        raise SystemExit("git failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _git(*arguments: str) -> list[str]:
    """Return the NUL-separated entries of the output of ``git ... -z``.

    A file name that is not valid UTF-8 cannot be mapped to an area. The script stops with a clear
    message instead of guessing (fail closed): rename the file, or run the full suite.
    """
    try:
        return _git_output(*arguments).decode("utf-8").split("\0")
    except UnicodeDecodeError:
        raise SystemExit(
            "A changed file has a name that is not valid UTF-8 and cannot be mapped to a test "
            "area. Rename it or run the full suite (-Full)."
        ) from None


def verified_revision(revision: str) -> str:
    """Return the full commit hash of ``revision``; refuse anything that is not an existing commit.

    The value comes from the command line and is passed to git as an argument. A value that starts
    with "-" could be read as an option (for example ``--output=<file>``), so it is refused, and
    ``--end-of-options`` (git 2.24 and later) separates the value from the options as well. A git
    that does not know ``--end-of-options`` fails here and the value is refused (fail closed).
    """
    if revision.startswith("-") or "\0" in revision:
        raise SystemExit(f"--since must be a revision, not an option: {revision!r}")
    try:
        resolved = _git_output(
            "rev-parse", "--verify", "--quiet", "--end-of-options", revision + "^{commit}"
        )
    except SystemExit:
        raise SystemExit(f"--since must name an existing commit: {revision!r}") from None
    return resolved.decode("utf-8").strip()


def changed_files(since: str | None = None) -> list[str]:
    """Return the changed files: working tree, staged, untracked and (optionally) since a revision."""
    files: set[str] = set()
    entries = [entry for entry in _git("status", "--porcelain=v1", "-z", "-uall") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        status, path = entry[:2], entry[3:]
        files.add(path)
        if "R" in status or "C" in status:
            index += 1  # The next entry is the old path of a rename: it counts as changed too.
            if index < len(entries):
                files.add(entries[index])
        index += 1
    if since:
        commit = verified_revision(since)
        files.update(name for name in _git("diff", "--name-only", "-z", commit, "--") if name)
    return sorted(files)


def plan(files=None, area_names=None, full=False) -> dict:
    """Return what to run: ``mode``, ``areas``, ``labels``, ``fresh_db``, ``checks``, ``reasons``."""
    reasons: list[str] = []
    selected: list[str] = []
    labels: list[str] = []
    if full:
        reasons.append("full suite requested")
    for name in area_names or ():
        if name not in AREAS:
            raise SystemExit(f"Unknown area {name!r}. Known areas: {', '.join(AREAS)}")
        selected.append(name)
    for path in files or ():
        path = path.replace("\\", "/")
        module = test_module_label(path)
        if is_core(path):
            reasons.append(f"core file: {path}")
        elif module is not None:
            if not TEST_LABEL.fullmatch(module):
                # A label is passed to ``manage.py test`` as an argument; a name that could be read
                # as an option or that has unusual characters is not trusted (D-072, L-2).
                reasons.append(f"test module with an unusual name: {path}")
            elif label_exists(module):
                labels.append(module)  # A deleted test module has nothing to run.
        else:
            found = areas_of(path)
            if not found:
                reasons.append(f"unmapped file: {path}")
            selected.extend(found)
    if reasons:
        return {
            "mode": FULL, "areas": [], "labels": [], "fresh_db": True, "checks": [],
            "reasons": reasons,
        }
    selected = list(dict.fromkeys(selected))
    for name in selected:
        labels.extend(AREAS[name].tests)
    if not selected and not labels:
        return {
            "mode": NOTHING, "areas": [], "labels": [], "fresh_db": False, "checks": [],
            "reasons": ["no changed files"],
        }
    labels.extend(ALWAYS)
    checks = []
    for name in selected:
        for check in AREAS[name].checks:
            if check not in checks:
                checks.append(check)
    return {
        "mode": SCOPED,
        "areas": selected,
        "labels": minimal_labels(labels),
        "fresh_db": any(AREAS[name].fresh_db for name in selected),
        "checks": [CHECK_COMMANDS[check] for check in checks],
        "reasons": [],
    }


def minimal_labels(labels) -> list[str]:
    """Remove duplicates and labels that another label already covers."""
    unique = list(dict.fromkeys(labels))
    return [
        label
        for label in unique
        if not any(other != label and labels_cover(other, label) for other in unique)
    ]


def _print_list() -> None:
    width = max(len(name) for name in AREAS)
    for name, area in AREAS.items():
        flags = " [fresh database]" if area.fresh_db else ""
        print(f"{name.ljust(width)}  {len(area.tests)} label(s){flags}  {area.description}")
    print()
    print("Core files (the full suite runs):")
    for pattern in CORE_PATHS:
        print(f"  {pattern}")
    print("Added to every scoped run: " + ", ".join(ALWAYS))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print the areas")
    plan_parser = sub.add_parser("plan", help="print the plan as JSON")
    plan_parser.add_argument("--area", action="append", default=[], help="area name (repeatable)")
    plan_parser.add_argument("--changed", action="store_true", help="map the changed files")
    plan_parser.add_argument("--since", help="also map the files changed since this revision")
    plan_parser.add_argument("--full", action="store_true")
    plan_parser.add_argument("--file", action="append", default=[], help="map this path (repeatable)")
    arguments = parser.parse_args(argv)
    if arguments.command == "list":
        _print_list()
        return 0
    files = list(arguments.file)
    if arguments.changed or arguments.since:
        files.extend(changed_files(arguments.since))
    names = [name for value in arguments.area for name in value.split(",") if name]
    if not (names or files or arguments.full or arguments.changed or arguments.since):
        parser.error("give --area, --changed, --since, --file or --full")
    result = plan(files=files, area_names=names, full=arguments.full)
    result["files"] = len(files)
    # One string per command (the arguments never contain a space): easy to read in PowerShell.
    result["checks"] = [" ".join(command) for command in result["checks"]]
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
