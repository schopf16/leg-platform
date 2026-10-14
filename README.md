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

## Operation Documents

* `docs/STARTUP.md`: start, check, use and stop the complete local environment (Windows, PowerShell), for developers and for the designer and tester agents; test commands and troubleshooting.
* `docs/DEPLOYMENT.md`: runbook to deploy an instance to a VPS (Infomaniak "VPS Lite"), for an operator or another AI assistant; a proposal that needs the owner's decisions (D-053), not yet run on a server.
* `docs/INSTALLATION.md`: configuration variables, local setup of MariaDB, Microsoft Graph steps and the address directory.
* `docs/OPERATIONS.md`: roles, rules, CI and the step list for stage and production.

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

Phase 1 is functionally complete in the local worktree and verified locally. It is **not yet formally accepted**: items that need real infrastructure, a human review or CI are open (below). Nothing is committed, pushed or deployed (the owner declined publication for CI on 2026-10-14).

Implemented and verified locally (decisions in `docs/DECISIONS.md`, D-004 to D-052):

* Login page as the entry point, registration with email verification, login with abuse protection, TOTP second factor (mandatory for staff users and system administrators) with recovery codes, password change and reset, session timeouts, reset of a lost second factor, emergency commands, initial administrator setup with forced password change.
* Profile editing, verified email change, self-service deletion with the retention rule, block/unblock/delete/reactivate by system administrators with the last-administrator protection, session invalidation (D-016, D-044).
* User, group and permission management in the Django administration, security log with retention, read-only view of the abuse protection records, Microsoft Graph configuration with certificate upload, certificate expiry warning and a rate-limited test send (D-006, D-048).
* Local copy of the official Swiss address directory (swisstopo) with advisory validation and autocompletion, background import started from the administration or by the operator, one automatic attempt every 90 days (D-033, D-049 to D-052).
* Instance branding (accent, optional highlight, logo) from the configuration with a contrast check, light/dark/automatic colour mode, member navigation, print stylesheet, documented supported browsers (D-051).
* The temporary local-only gate of the administration was removed on the owner's conditional approval after the full test run and a security re-check (D-052).

Verification on 2026-10-14 (local; Python 3.14.8, Django 6.1.2, MariaDB 12.3 for the application database; synthetic SQLite databases for the address directory, because the real directory database and its SELECT-only grants are not provisioned locally):

* Full suite: 909 tests OK with deprecation warnings as errors (`python manage.py test --settings=leg_platform.test_settings --noinput`).
* `check --deploy --fail-level WARNING` with synthetic production settings: no issues; `makemigrations --check`: no changes; compiled translation catalogue equals its source (`msgunfmt` comparison); `pip check` and the minimum pins: consistent; no PyPI vulnerability reported for the declared minimums (snapshot, D-051).
* Security reviews by the `security-reviewer` (read-only, ran no tests): no critical, high or medium findings after two corrections (D-052). This is not a statement that the application is secure; critical changes still need a human review (D-019).

Open (not verified or not possible locally):

* CI: the minimum job (Python 3.12, Django 6.0, MariaDB 10.11) and the latest job have not run for the current code (Python 3.12 and Docker are not available on the development machine). The last passed CI run is 38273753482 (2026-10-12).
* Real Microsoft Graph delivery, the Entra/Exchange setup, the real address-directory database with SELECT-only grants and a real import, the web server/proxy body limit, HTTPS/HSTS and cookies on a real host, the supervised services and the scheduler (D-008), and the browser and mobile-phone check of the pages. WCAG 2.2 AA conformity is not established by automated tests.
* Owner and operator items: AGOV answer (D-004), imprint and privacy notice content (D-022), the operator of stage and production, hosting details (D-008), a human review of the security-critical changes (D-019), and the import account credentials (D-049).
* Later: the security reviewer's remaining low items (ASGI body limit is only a proxy duty, a new admin URL must be wrapped with `admin_site.admin_view()`), and the lower `requests` minimum (needs a support source, D-048).

Deployment gates: no stage or production instance is deployed before

1. Microsoft Graph email delivery is tested with real credentials (a real test send) and restricted to the sender mailbox (D-006, D-028).
2. A human review of the second-factor, reset, lifecycle and administration changes (D-019) and a passed CI run of the current code on both jobs.
3. HTTPS, secure cookies, HSTS and a request body size limit of the web server are confirmed on the real host (`docs/INSTALLATION.md`, D-052).
4. Token-bearing URLs stay redacted from request logs (`leg_platform/log_filters.py` covers the registration, password reset, second factor removal and email change links); repeat for every further token URL (`docs/SECURITY.md`, section 10).
5. The worker `process_mail_queue --loop` and the address worker `process_address_imports` run as supervised services (separate settings and credentials for the importer), and `run_scheduled_tasks` is scheduled (D-008, D-037, D-038, D-041, D-052).