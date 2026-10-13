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

Phase 1 is in progress. The remaining work is:

* Password change and the TOTP flow are implemented and await the security review and the owner's confirmations (D-040).
* Implemented on 2026-10-13, confirmed by the owner and awaiting the security review (D-041): the persistent security log with retention and a read-only administration view, idle and absolute session timeouts, the reset of a lost second factor by system administrators (link for users, immediate for staff users and administrators), the emergency command `emergency_access` (including the command that encrypts the second factor keys again), the command `setup_initial_administrator` with the forced password change and second factor setup at the first login, and `run_scheduled_tasks`, which bundles the cleanup commands.
* Remaining second-factor work: ending other sessions when a user turns the second factor on or off (the mechanism exists, D-041). The TOTP keys are stored encrypted and the recovery codes as digests (D-040).
* Open checks and decisions from the second-factor reviews (D-040): a third review of the code budget, and the CI check of the compiled catalogue (`msgunfmt`).
* Profile editing and account lifecycle workflows: editing and deleting one's own account, email change/verification, block/deactivation/reactivation, session invalidation and last-administrator protection.
* Swiss address-directory validation and autocompletion (D-032, D-033), plus the remaining responsive views for phase 1 user workflows (member navigation with menu and tab bar, dark mode and colour mode switch, print stylesheet).
* Email change must use the same request budget (D-038); token redaction for every further token-bearing URL (the registration, password reset and second factor removal URLs are covered).
* Microsoft Graph configuration and delivery; a read-only view of the failed-login and lockout records of the abuse protection package (D-017); the security log events of functions that do not exist yet (blocking, deactivation, deletion, changes of another account's password or email address, Graph configuration; D-041); the scheduling itself of the worker `process_mail_queue --loop` and of `run_scheduled_tasks` with the hosting (D-008).
* Instance branding from the configuration (logo, accent and highlight colour), the imprint (D-022) and documented supported browsers.
* Owner decision: retain or remove the redundant `normalized_email` column (the lower-casing of local parts was re-confirmed on 2026-10-13, D-035).
* Verification: CI run 38273753482 (commit 8da72f3, 2026-10-12) passed both jobs, minimum (Python 3.12, Django 6.0, MariaDB 10.11) and latest (Python 3.14, Django 6.1, MariaDB 12.3), with deprecation warnings treated as errors. The changes of 2026-10-13 (D-037 to D-039) were run locally only (Python 3.14, Django 6.1.2, MariaDB 12.3: 519 tests with deprecation warnings as errors; Django 6.0.9 and `check --deploy` were last run before D-039); no CI run covers them yet. The changes of D-040 and D-041 (2026-10-13) were run locally with Python 3.14, Django 6.1.2 and MariaDB 12.3: 725 tests passed with deprecation warnings as errors; Django 6.0.9, `check --deploy` and the CI check of the compiled catalogue were not run for D-041.

Deployment gates: no stage or production instance is deployed before

1. Microsoft Graph email delivery is implemented with allowlist restrictions outside production (D-006, D-028).
2. TOTP and the central second-factor check protect the complete login and admin flows (D-004, D-017): implemented in D-040, the reset of a lost second factor and the first-login setup of the initial administrator in D-041; both still to be reviewed by the `security-reviewer`.
3. Token-bearing URLs are redacted from request logs and covered by tests (`docs/SECURITY.md`, section 10): `leg_platform/log_filters.py` covers the registration, password reset and second factor removal links; to be repeated for every further token URL.
4. Axes and separate IP-attempt retention are configured and scheduled; the TOTP flow calls Django `login()` only after every required factor is verified (D-017, D-018).
5. The worker `process_mail_queue --loop` runs as a supervised service (without it, no email is sent), and `run_scheduled_tasks` (which bundles `delete_expired_registrations`, `cleanup_mail_requests`, `cleanup_login_ip_attempts`, `cleanup_security_log` and `clearsessions`) is scheduled (D-037, D-038, D-041). The rate limits and the timing-comparable requests of registration and password reset are implemented (D-038).
