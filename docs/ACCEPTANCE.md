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

* [ ] System administrators can view the security log; retention is configurable per instance (D-018).

## 10. Security

* [ ] All criteria in `docs/SECURITY.md`, section 11, are implemented with passing tests.

## 11. Devices

* [ ] All user functions of the first phase (`docs/PROJECT.md`, "Supported Devices") are fully usable on desktop, tablet and phone. Supported browsers and minimum versions are documented.

## 12. Design Language

* [ ] All application views follow `docs/DESIGN.md`; configured instance colours are checked for contrast (documented); status is never shown by colour alone; keyboard operation and a visible focus indicator work on all phase 1 pages.
* [ ] Light and dark mode work only through the corner button, kept for the tab only, negative logo in dark mode; the print stylesheet hides navigation and controls; fonts and icons are served by the instance with the font licence texts (D-056).
* [ ] The error pages (404, 403, expired link, 500) are in the portal design (`navigation/VORGABEN.md`, section 8a), show no technical detail and work without session, database and context processor (D-054, D-060). The administration start page is as in D-060.

## 13. Language

* [ ] Everything technical and all documentation is English only (`AGENTS.md`, section 2).
* [ ] All user-facing texts are translatable with a complete Swiss German translation (`de-ch`); no displayed text contains "ß" (also not from Django or third-party packages; automated check); translation files can be generated and compiled reproducibly.

## 14. Fact Base

* [ ] Decisions in `docs/DECISIONS.md` state their sources; unverified points are identified and clarified with the project owner or marked open.

## 15. Documentation

* [ ] Documented for operators without in-depth IT knowledge: installation, environment variables, first administrator, new instance, Graph setup with the sender-mailbox restriction, the procedure after a report of an unrequested change (block, verify identity, restore, log), the lost second factor (`docs/SECURITY.md`, section 3), operator duties (security updates, backups, which keep deleted data until they expire, sender mailbox monitoring, reverse proxy, privacy notice), translations, tests, known limitations and open risks.

## Not Required for Acceptance

Everything in `docs/PROJECT.md`, "Explicitly Not Included"; AGOV only as the preparation of section 3.
