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
* `docs/DEPLOYMENT.md`: entry point for "deploy to stage" and "deploy to production" (section 0.5), with a reusable non-secret instance record, a separate update procedure and one-time VPS installation instructions (D-053, D-064). Production requires explicit release approval. The current stage configuration and the revised procedure still need live verification.
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

The first phase is implemented: login and entry pages, the frame with the start page and task tiles, the three-step registration, the profile in cards with a second email address and the second factor, address suggestions in the registration and the profile, security notifications with a greeting by name, the portal settings in the administration (name, addresses, logos, colours), the friendly error pages and the grouped administration. The details are in `docs/DECISIONS.md` (D-056 to D-074), not here. The automated tests pass locally (about 2,000 tests, 3 to 4 minutes with `scripts/test.ps1`, `docs/STARTUP.md` section 13). The work is **not yet formally accepted**: `docs/ACCEPTANCE.md` is ticked only for what has been verified on a real system.

The imprint and the privacy notice are not part of the portal: the portal only links their two addresses (portal settings); the operator writes and maintains the texts elsewhere (D-022, D-074). Notes for the operator are in `docs/DEPLOYMENT.md`, section 14.

**Open now**

* The AGOV answer: waiting for the external answer (D-004). Nothing to do until it arrives.
* The owner's own human review of the security-critical changes (D-019): second factor, reset, account lifecycle, administration, stage equal to production (D-070), the logo upload and the right to change the portal settings (D-073).

**Planned next, in this order**

A. **Verification on a real system**, when nothing is left under "Open now" (cannot be done on the development machine). The operator of stage and production is Infomaniak (owner statement, D-074).

* The push for the CI run: the last passed run is commit `5602760` (2026-10-14, run 38630847244, both jobs: minimum with Python 3.12, Django 6.0 and MariaDB 10.11; latest with Python 3.14, Django 6.1 and MariaDB 12.3). Every later code change needs a new CI run. The MariaDB-tagged tests run only there.
* Inspect the existing stage setup once and record it (D-064).
* Microsoft Graph delivery with real credentials and the Entra/Exchange setup. Afterwards open one link mail in Outlook, Apple Mail, Gmail and a Swiss webmail and click the link (plain text mails, D-069).
* Address directory: the real database with SELECT-only grants and a real import; the MariaDB query plan and collation of the approximate search (`docs/DEPLOYMENT.md` section 10.2); the bounds 1000 streets, 300 comparisons, 0.25 s and the threshold 0.75 are not measured against the real directory; `AXES_IPWARE_PROXY_COUNT` must be correct (`docs/INSTALLATION.md`).
* Real host: HTTPS, HSTS, cookies, the request body limits including the `3m` location for the logo upload, the proxy and client IP check (finding F-1 in `docs/DEPLOYMENT.md`); ASGI deployments need the proxy body limit (D-052); the supervised services and the scheduler (D-008).
* Real phones, browsers and screen readers (WCAG 2.2 AA is not established by automated tests), including an SVG logo with style attributes under the sandbox policy, the live colour preview under the Content Security Policy and the portal settings at 375 px (D-073).
* Every step of `docs/DEPLOYMENT.md`, including the import account of the address worker, which the operator creates during the initial setup (`docs/OPERATIONS.md`, section 5).

B. **Later phases, not built:** Mitteilungen, Standorte, Rechnungen and Energie (menu entries marked "später", WL-27); proof of work for the mail forms (ALTCHA, D-069); address suggestions on the account page of the administration (D-069); the notification of system administrators when the portal addresses change (D-073). See also "Not Part of the First Phase".

**Rule for developers:** a new admin URL must be wrapped with `admin_site.admin_view()` (the route walk test fails otherwise).

Deployment gates: no stage or production instance is deployed before

1. Microsoft Graph email delivery is tested with real credentials (a real test send) and restricted to the sender mailbox (D-006, D-028). Stage delivers like production, without a recipient restriction (D-070).
2. The owner's human review of the second-factor, reset, lifecycle and administration changes (D-019) and a passed CI run of the current code on both jobs.
3. HTTPS, secure cookies, HSTS and a request body size limit of the web server are confirmed on the real host (`docs/INSTALLATION.md`, D-052).
4. Token-bearing URLs stay redacted from request logs (`leg_platform/log_filters.py` covers the registration, password reset, second factor removal and email change links); repeat for every further token URL (`docs/SECURITY.md`, section 10).
5. The worker `process_mail_queue --loop` and the address worker `process_address_imports` run as supervised services (separate settings and credentials for the importer), and `run_scheduled_tasks` is scheduled (D-008, D-037, D-038, D-041, D-052).