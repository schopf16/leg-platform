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

Phases 1 to 5 of the design package (D-056 to D-060) are implemented: the login and the entry pages, the frame after the login with the start page and the task tiles, the three-step registration, the profile in cards with a second email address and the inline second factor, the friendly error pages and the grouped administration. The automated tests pass locally (see `docs/ACCEPTANCE.md` and the last report; the number is in the commit message). The work is **not yet formally accepted**: the items below are open. Everything else is in the code, the tests and `docs/DECISIONS.md`; `docs/ACCEPTANCE.md` is ticked only for what has been verified on a real system.

Open work:

* **Address suggestions** (designer review R-04, R-05, R-21, `docs/DECISIONS.md` D-054): the suggestion list stands far from the street field; only prefix matching works, so a typing error finds no suggestion; the warning after saving is far from the fields. A fuzzy fallback on a public endpoint needs a security check.
* **Mails:** greeting by name in security notifications (R-13, needs a new placeholder, D-039; not started in code, design notes in D-063; the concurrency test of the registration completion and the cross-account test of the five-minute window are done, D-063); the registration and the notices are plain text (no button; the link is on a line of its own).
* **Translations:** the French, Italian and English texts of the login page are drafts that native speakers must review (D-056); every other page is German only.
* **Messages (Mitteilungen), locations, invoices and energy** are menu entries marked "später"; they are not built (WL-27, `docs/PROJECT.md`).
* **Proof of work for the mail forms** (ALTCHA) is only a recommendation (D-058); it needs the approval of the designer and the study of the manufacturer's documentation.
* **Backlog from the security reviews:** a redirect of invalid links to a fixed URL instead of rendering under the token URL (D-055); ASGI deployments need the proxy body limit (D-052); a new admin URL must be wrapped with `admin_site.admin_view()` (the route walk test fails otherwise); the lower `requests` minimum needs a support source (D-048); the lockout page sends no `Cache-Control` header (Axes renders it).
* **Not verifiable locally:** CI passed for commit `5602760` on 2026-10-14 in both jobs (run 38630847244: minimum with Python 3.12, Django 6.0 and MariaDB 10.11; latest with Python 3.14, Django 6.1 and MariaDB 12.3). The MariaDB-tagged tests run only in CI, because Docker and MariaDB are not set up on the development machine; the CI run is the only evidence for them. Any later code change needs a new CI run; real Microsoft Graph delivery and the Entra/Exchange setup; the real address-directory database with SELECT-only grants and a real import; HTTPS, HSTS, cookies and the request body limit on a real host; the supervised services and the scheduler (D-008); a check of the pages on real phones and browsers (WCAG 2.2 AA is not established by automated tests); every step of `docs/DEPLOYMENT.md`.
* **Owner and operator:** AGOV answer (D-004), imprint and privacy notice content (D-022), the operator of stage and production and the other owner decisions O-1 to O-23 of `docs/DEPLOYMENT.md` (D-053), the proxy and client-IP approach (finding F-1 there), the import account credentials (D-049), and a human review of the security-critical changes (D-019).

Deployment gates: no stage or production instance is deployed before

1. Microsoft Graph email delivery is tested with real credentials (a real test send) and restricted to the sender mailbox (D-006, D-028).
2. A human review of the second-factor, reset, lifecycle and administration changes (D-019) and a passed CI run of the current code on both jobs.
3. HTTPS, secure cookies, HSTS and a request body size limit of the web server are confirmed on the real host (`docs/INSTALLATION.md`, D-052).
4. Token-bearing URLs stay redacted from request logs (`leg_platform/log_filters.py` covers the registration, password reset, second factor removal and email change links); repeat for every further token URL (`docs/SECURITY.md`, section 10).
5. The worker `process_mail_queue --loop` and the address worker `process_address_imports` run as supervised services (separate settings and credentials for the importer), and `run_scheduled_tasks` is scheduled (D-008, D-037, D-038, D-041, D-052).