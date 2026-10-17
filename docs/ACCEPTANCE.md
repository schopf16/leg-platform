# Acceptance Criteria – First Phase

The security criteria are defined only in `docs/SECURITY.md`, section 11; they are part of this acceptance (section 10). A criterion is ticked only if it is implemented and verified on a real system; tests not run and open security questions are reported explicitly.

## 1. Project and Setup

* [ ] Minimum versions of Python, Django, MariaDB and all packages are documented with sources, still receive vendor security updates and are proven by tests (D-015). Migrations run reproducibly.

## 2. Login Page, Registration and Login

* [ ] The login page is the entry point (login, password reset, registration, links to the privacy notice and the instance's website); every page accessible without login links to the privacy notice (D-022, D-025). No public home page: the root URL leads to the login page or, when logged in, to the start page (D-057), subject to the central second-factor check.
* [ ] Registration with the email address only, verification link, password (step 2 of 3), required profile data (step 3 of 3) activates the account; the customer number is assigned only when the last step succeeds (D-021, D-058). Login with the email address (not the customer number); logout; password change and reset.

## 3. Two-Factor Authentication

* [ ] Users can set up an authenticator app (TOTP) optionally and remove it after re-authentication; staff users and system administrators must set it up before using any function; recovery codes are issued at setup and can be regenerated.
* [ ] A system administrator can reset or remove another account's second factor (`docs/SECURITY.md`, section 3); the last system administrator's only via the management command on the server.
* [ ] Completion of the second factor is checked in one central place, TOTP-specific parts separate (`docs/PROJECT.md`, "AGOV and Passkeys").

## 4. Profile and Account Lifecycle

* [ ] Profile as five cards that change in place and work without a script (D-059); name and postal address required.
* [ ] Address suggestions and the not-found notice appear only while an address is edited, as in `docs/DESIGN.md`, section 6; nothing else about the directory is shown, no source named (D-066). Verified so far only with a stub and a synthetic directory; not accepted on a real system.
* [ ] Email change as in `docs/PROJECT.md`, "Changing the Email Address"; second address, pending request and stop link as in D-059; every change of second factor, password or address is mailed to every address of the account.
* [ ] Block, unblock, delete and reactivate (administrators), self-deletion (users) and re-registration of the same address follow D-016; the status (`docs/PROJECT.md`, "Account States") is displayed understandably.

## 5. First System Administrator

* [ ] Created at the first start by `manage.py setup_instance` from `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (D-076; `setup_initial_administrator` remains an alternative, D-005): password never printed or logged, ignored once an administrator exists; created only while the account table is empty (otherwise an error); two starts cannot create two administrators; the instance is not public until the first login is complete, because the initial password has no expiry (`docs/DEPLOYMENT.md`, section 9). At the first login the password must be changed and two-factor set up.

## 6. Roles and Permissions

* [ ] A system administrator can create, rename and delete roles (groups), link them to permissions, assign roles and grant or remove `is_staff` and `is_superuser`. No role is hard-coded (no code, migration or fixture creates, names or checks roles).

## 7. Instance Configuration

* [ ] No instance-specific value is hard-coded in code, templates or migrations; a second instance can be set up from the same code base with only the documented steps and its own configuration.
* [ ] The installation guide states that the application runs on its own host name, not under a path of another website, with the residual risk of a subdomain of the website's registrable domain (D-025), and describes the operator's manual branding checks (`docs/DESIGN.md`, sections 3.2 and 10).
* [ ] Portal settings work as in D-073 (rights, precedence of administration, environment and default, security settings read-only, 403 without the right, security events without file content). Automated tests pass; **not ticked until the browser checks of D-073 (SVG in `<img>` under the sandbox policy, live preview under the CSP, 375 px and 200 % zoom), the security review and the reverse proxy limit (V35 in `docs/DEPLOYMENT.md`) are done.**

## 8. Email

* [ ] Central interface for outgoing email; the Graph configuration section of the admin never shows the private key, shows the certificate expiry with a warning, and its test send gives understandable feedback.
* [ ] The editable email texts follow D-039 (unknown placeholders, missing personal link, typed addresses and template syntax refused; default, preview, test email only to the own address, versions; no access without the permission).
* [ ] Security notifications tell the person to contact the operator (instance contact where configured), do not invite a reply, and greet by name only as in D-068 (`docs/DESIGN.md`, section 8; also in preview and test email); the name is blanked in queued notifications when the account is deleted; no other email greets by name.
* [ ] The installation guide explains how to restrict the Microsoft application to the sender mailbox ("RBAC for Applications") and that the sender mailbox must be monitored.

## 9. Security Logging

* [ ] System administrators can view the security log; retention is configurable per instance (D-018). A long session ("Stay signed in") of a staff user or system administrator is written to the log and announced by email, and a system administrator can end all sessions of another account (D-100; automated tests).

## 10. Security

* [ ] All criteria in `docs/SECURITY.md`, section 11, are implemented with passing tests.

## 11. Devices

* [ ] All user functions of the first phase (`docs/PROJECT.md`, "Supported Devices") are fully usable on desktop, tablet and phone. Supported browsers and minimum versions are documented.

## 12. Design Language

* [ ] All application views follow `docs/DESIGN.md`; configured instance colours are checked for contrast (documented); status is never shown by colour alone; keyboard operation and a visible focus indicator work on all phase 1 pages.
* [ ] Light and dark mode work only through the corner button, kept for the tab only, negative logo in dark mode; the print stylesheet hides navigation and controls; fonts and icons are served by the instance with the font licence texts (D-056).
* [ ] The error pages (404, 403, expired link, 500) are in the portal design (`navigation/VORGABEN.md`, section 8a), show no technical detail and work without session, database and context processor (D-054, D-060). The administration start page is as in D-060.
* [ ] Forms of the management area (D-097): one form per subject, identical wherever used (`test_form_identity.py`); a locked value comes from the page's object, never from the POST; numbers are checked while typing and on the server with the same messages; the suggestion list is a white panel under the field, usable by keyboard and touch; the cursor is in the first field on a desktop (not on a phone, not after an error); "Systemverwaltung" is the last menu entry; small buttons and back links are 44 px high on touch. Browser checks (desktop, 375 px, touch, screen reader) are part of the acceptance. **Status 2026-10-17 (tester):** server side verified by the full suite (2838 tests, OK) and an independent probe (number padding, power fields, quota 204.1 %, inline circuit creation in one transaction, assigning a point to a location and to a LEG, forged locked fields, hostile `next` values, identical controls at every entry point, one menu template). **Not verified, so not ticked:** the scripts in a real browser (combobox, checks while typing, cursor, 44 px targets), touch, screen reader, 375 px.
* [ ] Corrections after the browser acceptance (D-097, designer report UX-001 to UX-016): after a tap on a suggestion the list stays closed and the keyboard does not return; a move names the change of the transformer circuit; "Person missing?" leads back to the metering point; the next-step banner is neutral and the only message after a create; the import and log pages use the admin breadcrumbs and buttons, are 44 px high on touch and fit 375 px; the failure reason is an alert; the spinner stands still under reduced motion. **Status 2026-10-17 (developer):** covered by automated tests (`test_review_corrections.py`, `test_dialog_scripts.py`, `test_import_runner.py`, `test_logs.py`). **Not verified, so not ticked:** touch behaviour on a real phone, the 375 px admin layout, screen reader, stylesheets in a real browser.
* [ ] Request budgets (D-098): person search limited per account (300 per minute, then 429), text cut to 100 characters and five words, both suggestion scripts stop after a 429 (automated tests; no browser run).
* [ ] Metering point number and menu (D-102): the 20-character part takes A-Z, 0-9 and the hyphen (small letters become capitals); shorter entries get zeros on the left; longer ones lose leading zeros only, otherwise they are invalid with a clear message; script and server use the same rule. "Systemverwaltung" is shown only to an account that can use the administration, in both menus. **Status 2026-10-17 (developer):** server tests for every case (23; 21 and 22 characters with leading zeros; letters; hyphen; small letters; forbidden characters; all zeros; 20 exact; 25 zeros), the script in headless Chrome where installed (skipped otherwise), menu tests for a customer, staff without a right, staff with one right and a superuser. **Not verified, so not ticked:** script and menu in a real browser on a phone.

## 13. Language

* [ ] Everything technical and all documentation is English only (`AGENTS.md`, section 2).
* [ ] All user-facing texts are translatable with a complete Swiss German translation (`de-ch`); no displayed text contains "ß" (also not from Django or third-party packages; automated check); translation files can be generated and compiled reproducibly. **Status 2026-10-17 (tester, this system):** `msgfmt --check` exit 0, the committed `.mo` equals the compiled `.po` (0 differences), no "ß" in the `.po` or in any source file of the package, no fuzzy and no untranslated own text after `makemessages` in a throw-away copy. Not verified: every page in a browser. The stale texts that no source used were removed in work package C (D-101).

## 14. Fact Base

* [ ] Decisions in `docs/DECISIONS.md` state their sources; unverified points are identified and clarified with the project owner or marked open.

## 15. Documentation

* [ ] Documented for operators without in-depth IT knowledge: installation, environment variables, first administrator, new instance, Graph setup with the sender-mailbox restriction, the procedure after a report of an unrequested change (block, verify identity, restore, log), the lost second factor (`docs/SECURITY.md`, section 3), operator duties (security updates, backups, which keep deleted data until they expire, sender mailbox monitoring, reverse proxy, privacy notice), translations, tests, known limitations and open risks.

## Not Required for Acceptance

Everything in `docs/PROJECT.md`, "Explicitly Not Included"; AGOV only as the preparation of section 3.
