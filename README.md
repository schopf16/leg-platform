# LEG-Platform

Web application for local electricity communities (German: "lokale Elektrizitätsgemeinschaft", LEG) in Switzerland.

The project was initiated by the LEG Ittigen. The software is developed neutrally, so that other interested municipalities can run their own, independent instance from the same code base with their own configuration (`docs/DECISIONS.md`, D-013 and D-014). References to Ittigen in the documentation refer to the initiating instance.

## Project Goal

The first phase creates a secure, extensible foundation. LEG domain functions, billing and financial functions are explicitly not part of this phase.

## Technology Direction

* Backend: Python and Django
* Database: MariaDB (minimum 10.11, used 12.3 LTS; MySQL is not supported, D-002, D-030)
* User interface: Tabler, responsive for desktop computers, tablets and mobile phones; design language in `docs/DESIGN.md`
* System administration: Django admin
* Outgoing email: Microsoft Graph, configured in the Django admin
* Login: email address with password
* Two-factor authentication: authenticator app (TOTP), mandatory for staff users and system administrators; a later switch to AGOV as the second factor is prepared (approval requested, answer pending)
* Development: local, then stage, later production
* Hosting of the Ittigen instance: Infomaniak "VPS Lite 1" (decided by the project owner on 2026-10-11, D-008); the remaining hosting details are open
* Deployment model: one independent instance per municipality
* Supported versions: minimum versions only, as old as technically possible while still receiving vendor security updates (D-015)
* Language of software and documentation: English (code, database, comments, commits, documents, etc.)
* User interface language: Swiss German (`de-ch`, no "ß") by default, prepared for additional languages

## Binding Rules

1. `CONDUCT.md` contains the protected rules of conduct for all AI agents ("don't be evil").
2. `AGENTS.md` contains the shared development rules, including the language rules and the obligation to work fact-based without assumptions.
3. `CLAUDE.md` is the entry point for Claude Code.
4. `docs/SECURITY.md` contains the binding security policy.
5. `docs/PROJECT.md` defines the scope.
6. `docs/ACCEPTANCE.md` defines the acceptance criteria.
7. `docs/DECISIONS.md` records all decisions with their sources.
8. `docs/DESIGN.md` defines the binding design language of the user interface (D-024).

Read the relevant rules before every larger change.

## First Phase

* Login page as the entry point; no public home page, information about the LEG is provided by its own website (D-025)
* Registration and email verification
* Login with email address, and logout, protected against automated attacks
* Two-factor authentication with an authenticator app (mandatory for staff users and system administrators, optional for users)
* Password change and password reset
* Personal user profile: viewing, editing and deleting one's own account
* All user functions fully usable on mobile phones
* Account lifecycle including deactivation for later retention obligations
* User management, role management and role assignment
* Microsoft Graph email configuration in the Django admin, including a test send
* Security logging
* Instance configuration outside the code, so that further instances can be set up
* Secure baseline configuration and automated tests
* Preparation for AGOV as the second factor: the completed second factor is checked centrally and independently of the method; this is intended to allow a switch from TOTP to AGOV without changing the architecture or weakening security, and a switch requires a new security review (`docs/PROJECT.md`, "AGOV and Passkeys")

## Not Part of the First Phase

* LEGs as entities and all domain functions
* Billing logic and invoicing
* Calculation of electricity costs or energy distribution
* Productive financial processes
* Bank file processing
* Productive payout processes
* AGOV integration (only prepared) and passkeys
* Multi-tenancy (several municipalities in one instance)
* Additional user interface languages and language selection
* Public home page and publication of aggregated figures (D-025)

## Principles

Security, data protection and server-side access control are part of development from the start. A function is not considered secure merely because it works.

Established, maintained solutions are preferred over custom developments, especially for security functions.

The software is written in English without exception. Only the user interface is language-dependent.

No assumptions are made. Decisions are based on verified facts from official sources. When in doubt, ask.

## Licence

LEG-Platform is licensed under the BSD 3-Clause License (`LICENSE`), the same licence as Django (`docs/DECISIONS.md`, D-029). The dependencies are installed separately and keep their own licences; `mysqlclient` is licensed under GPL-2.0-or-later. The licences of all dependencies are listed in `docs/THIRD_PARTY_LICENSES.md`. This information is not legal advice.

## Current Status

Phase 1 is in progress. Implemented and verified on the development machine:

* Django project skeleton with validated, secure-by-default settings read from environment variables, a placeholder email backend that refuses every send, and a strict Content Security Policy without `'unsafe-inline'`. Every new page must work without inline scripts and styles (D-026, D-028).
* Custom user model `leg_platform.accounts` with the canonical email address as login identifier, the table of issued customer numbers, the first migration and tests (D-021, D-031, D-034, D-035).
* Local setup script for Windows with MariaDB 12.3 LTS, completed on the development machine on 2026-10-12 (D-027, D-030; `docs/INSTALLATION.md`).
* On 2026-10-12: `check`, `makemigrations --check --dry-run`, the full test suite on MariaDB 12.3.3 with the restricted application account, and the minimum constraints against `pyproject.toml`, all without issues.

Not yet verified: the continuous integration for the current changes. The project owner decided on 2026-10-12 to leave its result open. Its translation check fails until the compiled `.mo` files are committed, because GNU gettext is not installed on the development machine (D-034).

The repository is public on GitHub (D-027).

Deployment gates: no stage or production instance is deployed before

1. the Microsoft Graph email backend is implemented (D-006); until then, the placeholder backend raises an error on every send, so that a function sending email would answer with an error page and could reveal whether an account exists. `EMAIL_MODE=allowlist` with an empty `EMAIL_ALLOWED_RECIPIENTS` already refuses the start; the backend must deliver only to the allowed recipients (D-028);
2. two-factor authentication and `django-axes` protect the admin and the login (D-004, D-017);
3. tokens in URLs are redacted from request logs: Django's `django.request` and `django.security.csrf` loggers write the request path of every response with status 400 or higher (`log_response` in the installed Django source), so a token in a path would end up in the logs. A redaction filter and a test are required when the first URL with a token is introduced (`docs/SECURITY.md`, section 10).

Next steps (open Phase 1 work, in this order):

1. Compile the translation files with `python manage.py compilemessages` (needs GNU gettext) and commit the `.mo` files; the continuous integration check fails until then (D-034).
2. Admin of the user model with the protection against self-promotion. The account string uses the internal ID; the user-management admin must show it for support lookup. Restrict or unregister the default `auth.Group` admin to system administrators (AGENTS.md, section 6; security review of 2026-10-12, finding L-8). Until `django-axes` and `django-otp` are wired (step 3), the admin at `admin/` has password-only login and must not be reachable outside local development (finding H-2).
3. Registration, login backend, `django-axes` and `django-otp`. Notes for these steps (security review of 2026-10-12): `normalized_email` is not editable, so model forms skip it in `validate_unique()`; registration must handle a duplicate without revealing that an account exists (L-9); Django's `PasswordResetForm` looks up `email__iexact` on the `email` column, so the password reset must look up accounts only through the normalised column (L-10; `docs/SECURITY.md`, section 3). `ModelBackend.authenticate()` calls `set_password(password)` for unknown usernames (Django's timing mitigation, `check_password_with_timing_attack_mitigation()`), which raises `AccountIntegrityError` for an empty submitted password; the login must not raise for unknown usernames (an enumeration oracle), so the login step must handle this before `authenticate()` (security review of 2026-10-12, finding B).
4. Forced password change at the first login of the first system administrator, and a complete profile for it (D-005; setup command).
5. Owner decisions: keep or remove the redundant `normalized_email` column (a removal needs a migration); re-confirm the lower-casing of local parts against RFC 5321, section 2.4, with the corrected account-takeover risk in D-035 (security review finding H-1).
