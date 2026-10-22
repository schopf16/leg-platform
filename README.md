# LEG-Platform

Web application for local electricity communities (German: "lokale Elektrizitätsgemeinschaft", LEG) in Switzerland.

Initiated by the LEG Ittigen, developed neutrally. Other municipalities run their own, independent instance from the same code base with their own configuration (`docs/DECISIONS.md`, D-013, D-014). References to Ittigen mean the initiating instance.

## Project Goal

The first phase creates a secure, extensible foundation. LEG domain functions, billing and financial functions are not part of it.

## Technology Direction

* Python, Django, MariaDB (minimum 10.11, used 12.3 LTS; MySQL not supported; D-002, D-030). Tabler (responsive; `docs/DESIGN.md`). Django admin for system administration.
* Email: Microsoft Graph, configured in the Django admin. Login: email address with password, or passkey (D-106).
* Two-factor: authenticator app (TOTP), mandatory for staff users and system administrators.
* Environments: local, stage, production. Hosting of the Ittigen instance: Infomaniak "VPS Lite 1" (owner, 2026-10-11, D-008); other hosting details open.
* One independent instance per municipality. Minimum versions only: as old as possible while still receiving vendor security updates (D-015).
* Software and documentation in English. User interface: Swiss German (`de-ch`, no "ß") by default; French, Italian and English can be chosen (D-056).

## Binding Rules

1. `CONDUCT.md`: protected rules of conduct for all AI agents ("don't be evil").
2. `AGENTS.md`: shared development rules, language rules, fact-based work without assumptions.
3. `CLAUDE.md`: entry point for Claude Code.
4. `docs/SECURITY.md`: binding security policy.
5. `docs/PROJECT.md`: scope. `docs/ACCEPTANCE.md`: acceptance criteria.
6. `docs/DECISIONS.md`: all decisions with sources.
7. `docs/DESIGN.md`: binding design language (D-024).
8. `docs/CODEMAP.md`: what is where, where to change what, conventions, test map. With `CONDUCT.md` and `AGENTS.md` it is the fixed reading core of every agent.

`AGENTS.md`, section 1, has the topic index that tells which documents a task needs.

## Operation Documents

* `docs/STARTUP.md`: start, check, use and stop the local environment (Windows, PowerShell); test commands; troubleshooting.
* `docs/DEPLOYMENT.md`: entry point for "deploy to stage" and "deploy to production" (section 0.5), reusable non-secret instance record, update procedure, one-time VPS installation (D-053, D-064). Production needs explicit release approval. The current stage configuration and the revised procedure still need live verification.
* `docs/INSTALLATION.md`: configuration variables, local MariaDB setup, Microsoft Graph steps, address directory.
* `docs/OPERATIONS.md`: roles, rules, CI, step list for stage and production.

## First Phase

Scope: `docs/PROJECT.md`. In short: login page as entry point (no public home page, D-025); registration with email verification; login protected against automated attacks; password change and reset; two-factor (TOTP, mandatory for staff users and system administrators); own profile (view, edit, delete); account lifecycle with deactivation for retention obligations; user, role and role-assignment management; Microsoft Graph configuration with a test send; security logging; instance configuration outside the code; all user functions usable on mobile phones.

## Not Part of the First Phase

* LEGs as entities and all domain functions (phase 2 starts with them, D-077; see "Current Status").
* Billing, invoicing, calculation of electricity costs or energy distribution, productive financial processes, bank file processing, payout processes.
* Multi-tenancy. User interface languages beyond Swiss German, French, Italian and English. Public home page and publication of aggregated figures (D-025).

## Licence

BSD 3-Clause License (`LICENSE`), the same as Django (D-029). Dependencies are installed separately and keep their own licences (`mysqlclient`: GPL-2.0-or-later); all are listed in `docs/THIRD_PARTY_LICENSES.md`. No legal advice.

## Current Status

**What exists.**

* Phase 1: login and entry pages, the frame with start page and task tiles, three-step registration, the profile in cards with a second email address and the second factor, address suggestions, security notifications, portal settings, error pages, grouped administration.
* Phase 2: the management area ("Verwaltung") for staff (locations, metering points, LEGs, persons, overview, PDF printing, "Person anlegen", support dialogs). Open to every active staff user with a completed second factor (D-086). Details: `docs/DECISIONS.md`.
* Onboarding, staff side (D-103, D-104): the overview queues, the list "Vorgänge" with the page of an application (review, return, registration at the grid operator, confirmation, completion, roll back) and of a deregistration (confirm the date), notices, the list "Verträge" (upload, set as current, delete, download) and the chapter "Erinnerungen und Fristen" in the settings. Staff pages: not verified in a real browser. The customer pages are built (D-105): the menu and the start page with the card "Ihr Weg in die LEG", the page "Gebäude", the registration wizard and the contract page and download; the deregistration pages and the profile cards of the owner's design are built (D-105); a staff account has no customer pages. Customer pages: rendered in Edge, not verified on a real phone; the deregistration page and the new profile cards are verified by automated server tests only, not yet in a browser.
* The automated tests pass locally (`scripts/test.ps1`, `docs/STARTUP.md` section 13).
* The work is **not yet formally accepted**: `docs/ACCEPTANCE.md` is ticked only for what was verified on a real system.
* Imprint and privacy notice are not part of the portal. The portal only links their two addresses (portal settings); the operator maintains the texts elsewhere (D-022, D-074). Operator notes: `docs/DEPLOYMENT.md`, section 14.

**Not built, deliberately open in the management area:** "Anmeldung starten" and "Abmeldung starten" by the staff for a customer (the owner's design `vorgaenge/VORGABEN.md` section 9; the services for it do not exist), change of the login address by staff (D-081, D-087 item 10), billing.

**Open, by who acts.**

* **Owner:**
  * the own human review of the security-critical changes (D-019): second factor, reset, account lifecycle, administration, stage equal to production (D-070), logo upload and the right to change the portal settings (D-073), the access rule of the management area (D-086).
* **Owner decisions open (from the onboarding work, D-103 to D-105):**
  * buildings are shared by all customers of one EGID, each sees only the own metering points; staff who also take part as customers need a separate account (staff get no customer pages);
  * "Frühestens am <date>" on the deregistration page (deviates from `ABMELDUNG.md` section 3), contract re-acceptance after a new version, mails for completion and confirmed dates (none are sent), the terms "Anmeldung"/"Abmeldung" in French and Italian ("demande", "richiesta", "désinscription", "uscita"), the two menu entries "Vertrag" and "Gesellschaftsvertrag" for one page;
  * the legal reading of "four months to month end" and of the click acceptance of the contract (`RECHERCHE-VERTRAG-DIGITAL.md`, Frage 22);
  * the privacy notice of the operator must name the swisstopo request (S-4 in `docs/SERVICES.md`).
* **Operator (Infomaniak, D-074):**
  * confirm the legal points of the retention flag that could not be verified with the tools: version of the OR in force, that the instance operator is obliged under Art. 957, the date from which an account may be deleted again (D-086 item 2);
  * the verification on a real system (below; not possible on the development machine).

**Verification on a real system.**

* Inspect and record the existing stage setup (D-064). Every step of `docs/DEPLOYMENT.md` during the initial setup.
* Microsoft Graph delivery with real credentials. Then open one link mail in Outlook, Apple Mail, Gmail and a Swiss webmail and click the link (D-069).
* Address directory: a real import (time, file size, `StateDirectory=`, file modes) and the query plans of the lookup (`docs/DEPLOYMENT.md` section 10.2). Not measured: the bounds 1000 streets, 300 comparisons, 0.25 s and the threshold 0.75. `AXES_IPWARE_PROXY_COUNT` must be correct (`docs/INSTALLATION.md`).
* Real host: HTTPS, HSTS, cookies, request body limits including the `3m` logo upload location, proxy and client IP check (F-1 in `docs/DEPLOYMENT.md`; ASGI needs the proxy body limit, D-052), supervised services and the scheduler (D-008).
* Real phones, browsers and screen readers (WCAG 2.2 AA is not established by automated tests); an SVG logo with style attributes under the sandbox policy; the live colour preview under the CSP; the portal settings at 375 px (D-073).
* The automatic first start (D-076): `leg-setup.service` (`manage.py setup_instance`) on a real server with an **empty** MariaDB database and only the environment file. A restart of the web service runs the unit again (`docs/DEPLOYMENT.md`, section 8.0, V3a, V4a). The first login forces password change and second factor; the password line is removed afterwards. Not run on a real server yet.

**Later phases, not built:** Mitteilungen, Standorte, Rechnungen, Energie (menu entries marked "später", WL-27); proof of work for the mail forms (ALTCHA, D-069); address suggestions on the account page of the administration (D-069); notification of system administrators when the portal addresses change (D-073). See "Not Part of the First Phase".

**Rule for developers:**

* A new admin URL is wrapped with `admin_site.admin_view()` (the route walk test fails otherwise).
* A new management URL (`/management/`) uses `ManagementAccessMixin` or `@management_view` from `leg_platform/management/access.py` (route walk test `leg_platform/management/tests/test_access.py`). There is no permission to name (D-086). A new page adds one line to `leg_platform/management/navigation.py`.

**Deployment gates.** No stage or production instance is deployed before:

1. Microsoft Graph email delivery is tested with real credentials (a real test send) and restricted to the sender mailbox (D-006, D-028). Stage delivers like production, without a recipient restriction (D-070).
2. The owner's human review of the second-factor, reset, lifecycle and administration changes (D-019) and a passed CI run of the current code on both jobs.
3. HTTPS, secure cookies, HSTS and a request body size limit of the web server are confirmed on the real host (`docs/INSTALLATION.md`, D-052).
4. Token-bearing URLs stay redacted from request logs (`leg_platform/log_filters.py` covers the registration, password reset, second factor removal and email change links); repeat for every further token URL (`docs/SECURITY.md`, section 10).
5. The worker `process_mail_queue --loop` and the address worker `process_address_imports` run as supervised services (both with the settings of the application, D-075), and `run_scheduled_tasks` is scheduled (D-008, D-037, D-038, D-041, D-052).
