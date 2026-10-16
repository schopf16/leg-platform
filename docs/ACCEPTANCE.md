# Acceptance Criteria – First Phase

The security-relevant acceptance criteria are defined only in `docs/SECURITY.md`, section 11. They are part of this acceptance (section 10) and are not repeated here.

## 1. Project and Setup

* [ ] Minimum versions of Python, Django, MariaDB and all packages are documented with their sources, still receive security updates from their vendor and are proven by tests (D-015).
* [ ] Database migrations run reproducibly.

## 2. Login Page, Registration and Login

* [ ] The login page is the entry point of the application; it offers login, password reset and registration and links to the instance's privacy notice and website. All other pages accessible without login link to the privacy notice as well (D-022, D-025).
* [ ] The application has no public home page; the root URL leads to the login page or, for logged-in users, to the start page with the greeting and the task tiles (D-057), subject to the central second-factor check (staff users and system administrators without a completed second factor are led to its setup).
* [ ] A person can register with the email address only; the privacy notice is linked.
* [ ] Via the link sent, the email address is verified, the password is set (step 2 of 3) and the required profile data is entered (step 3 of 3); the account is then active (D-058).
* [ ] A customer number is assigned when the registration is completed, i.e. when the last step "Konto erstellen" is submitted successfully (not at registration and not when the link is merely opened; D-021, D-058), and displayed on the start page in two blocks of three digits.
* [ ] Login works with the email address; the customer number is not a login identifier (D-021).
* [ ] Logout works.
* [ ] Password change and password reset work.

## 3. Two-Factor Authentication

* [ ] Users can optionally set up an authenticator app (TOTP) and remove it again after re-authentication.
* [ ] Staff users and system administrators must set up two-factor authentication before they can use any function; the setup is understandable.
* [ ] Recovery codes are issued at setup and can be generated again.
* [ ] A system administrator can reset or remove the second factor of another account as described in `docs/SECURITY.md`, section 3; the second factor of the last system administrator can be reset via a management command on the server.
* [ ] Whether the second factor has been completed is checked in one central place, independent of the method, and the TOTP-specific parts are kept separate from it, so that a later switch to AGOV does not change the architecture (`docs/PROJECT.md`, "AGOV and Passkeys").

## 4. Profile and Account Lifecycle

* [ ] Users can view their own profile as five cards that change in place (personal details, email addresses, password, two-factor authentication, delete account) and work without a script (D-059).
* [ ] Users can change permitted profile data of their own; name and postal address are required, the further details (salutation, company, address addition, post office box, phone number) are optional.
* [ ] While an address is edited (registration step 3 and the profile edit form), suggestions and "Did you mean ..." are offered for Swiss addresses and the not-found notice appears for a complete address that the directory does not know; nothing about the directory is shown in the overview, after saving, or when the directory cannot answer; the user interface names no source of the directory (D-066). Verified so far only with a stub and a synthetic directory (`docs/DECISIONS.md`, D-066); not accepted on a real system.
* [ ] Users can change their email address as described in `docs/PROJECT.md`, "Changing the Email Address", and can add a second address that receives every notification but never serves for the login or the reset (D-059). A pending request shows its deadline and can be cancelled; the notice to the old address has a link that stops the change without a login.
* [ ] Every change of the second factor, the password or an address is mailed to every address of the account.
* [ ] System administrators can block and unblock accounts.
* [ ] Users can delete their own account; system administrators can delete accounts. Deletion follows the retention obligation rule (D-016).
* [ ] The same email address can be used for a new registration after deletion or deactivation.
* [ ] A system administrator can reactivate a deactivated account under the conditions of D-016.
* [ ] The account status (`docs/PROJECT.md`, "Account States") is displayed understandably.

## 5. First System Administrator

* [ ] The first system administrator account is set up with a management command on the server; the email address is read from the local `.env`, the initial password is entered interactively.
* [ ] At the first login, the password must be changed and two-factor authentication must be set up.

## 6. Roles and Permissions

* [ ] A system administrator can create, rename and delete roles (groups) and link them to permissions.
* [ ] A system administrator can assign roles to users and grant or remove `is_staff` and `is_superuser`.
* [ ] No role is hard-coded; no code, migration or fixture creates, names or checks specific roles.
* [ ] The access levels, the role model and their terminology are documented.
* [ ] Functions not yet implemented are not presented as finished.

## 7. Instance Configuration

* [ ] No instance-specific values (e.g. name of the community or municipality, domain, contact details) are hard-coded in code, templates or migrations.
* [ ] All instance-specific values, including the canonical base URL and the link to the privacy notice, are documented as part of the instance configuration.
* [ ] A second instance can be set up from the same code base using only the documented installation steps and its own configuration.
* [ ] The installation guide states that the application runs on its own host name (e.g. a subdomain) and not under a path of another website, and states the residual risk of a subdomain of the website's registrable domain compared with a separate registrable domain (D-025).
* [ ] The installation guide describes the operator's manual checks of instance branding: the accent colour is not a red tone and not confusable with the error or energy colours, and static SVG logo files contain no scripts or external references (`docs/DESIGN.md`, sections 3.2 and 10). The table in `docs/DESIGN.md`, section 3.2, states where the operator changes each branding value.
* [ ] The portal settings (D-073): a holder of the right to change them (and a system administrator) maintains the name, the website, the privacy and legal addresses, the colours and the two logos in the administration; an empty field uses the environment value, then the platform default; the host name, the base address and the other security settings are shown read only to system administrators and cannot be changed there; a staff user without the right gets 403, with only the view right the page is read only; a change and a rejected upload are security events without the content of a file. Automated tests of this unit pass (`test_logo_images.py`, `test_portal_branding.py` and the areas `branding`, `admin`, `templates`, `static`, `translations`, `migrations`, `mail`); **not ticked until the browser checks of D-073 (SVG in `<img>` under the sandbox policy, the live preview under the Content Security Policy, 375 px and 200 % zoom), the security review and the reverse proxy limit (V35 in `docs/DEPLOYMENT.md`) are done.**

## 8. Email

* [ ] The application has a central interface for outgoing email.
* [ ] The Django admin has a dedicated section for the Microsoft Graph configuration (tenant ID, client ID, sender address, certificate and private key, expiry date of the certificate).
* [ ] The admin shows the public certificate and its expiry date, never the private key, and warns before the certificate expires.
* [ ] A test send provides understandable feedback.
* [ ] The administration lists all emails with editable subject and text and a placeholder cheat sheet; the editor refuses unknown placeholders, a missing personal link, typed web or email addresses and template syntax; the default can be restored; users without the permission cannot read or change the texts (D-039).
* [ ] The editor offers a preview of the typed text with example links and a test email that goes only to the address of the logged-in account; neither saves anything. Every change of a text is kept as a version that can be restored; a version that is no longer valid is refused (D-039).
* [ ] Security notifications tell the person to contact the operator immediately if they did not request the change, with the contact information of the instance where configured; they do not invite a reply.
* [ ] The security notifications greet "Guten Tag Vorname Nachname" when the account belongs to a person with a first and a last name that pass the guard, and the neutral "Guten Tag" otherwise (company account, one name, unsuitable or address-like name, the former login address, a removed second address, the deleted account, also in the preview and the test email); a name that a person has asked to erase is blanked in the queued notifications when the account is deleted; no other email greets by name; the editor offers `{greeting}` only for the 14 notifications (D-068).
* [ ] The installation guide explains how to restrict the Microsoft application to the sender mailbox ("RBAC for Applications") and that the sender mailbox must be monitored.

## 9. Security Logging

* [ ] System administrators can view the security log.
* [ ] The retention periods are configurable per instance (D-018).

## 10. Security

* [ ] All criteria in `docs/SECURITY.md`, section 11, are implemented, and the corresponding tests exist and pass.
* [ ] Tests and their results are documented.

## 11. Devices

* [ ] The user interface is usable on desktop computers, tablets and mobile phones.
* [ ] All user functions of the first phase (in particular registration, email verification, login with two-factor authentication, password change and reset, editing the profile, changing the email address and deleting the account) are fully usable on a mobile phone.
* [ ] The supported browsers and their minimum versions are documented.

## 12. Design Language

* [ ] All application views follow `docs/DESIGN.md`; each page uses the register defined there (Clear or Warm), and controls look the same in both registers.
* [ ] Logo, accent colours and the optional highlight colour come from the instance configuration; without instance values, the platform defaults apply.
* [ ] The colour pairs in `docs/DESIGN.md`, section 3.6, are used as specified; colour values configured for an instance are checked for the required contrast, and the check is documented.
* [ ] Status is never shown by colour alone.
* [ ] Light and dark mode work only through the button in the top right corner (default: light, also when the device prefers dark; no automatic mode, D-056); the choice is kept for the tab only; the negative logo is used in dark mode.
* [ ] A print stylesheet hides navigation and controls.
* [ ] Fonts and icons are served by the instance itself; licence texts of the fonts are included.
* [ ] The error pages (404, 403, the expired-page page of a failed security check, 500) are in the design of the portal (logo, tokens, friendly short texts of `navigation/VORGABEN.md`, section 8a), show no technical detail and work without a session, a database and a context processor (D-054, D-060).
* [ ] The start page of the administration shows the groups "Benutzer", "Inhalte" and "System", the hint where texts and footer addresses are maintained, and the name of the instance as the title (D-060).
* [ ] Keyboard operation and a visible focus indicator work on all phase 1 pages.

## 13. Language

* [ ] Code, identifiers, comments and docstrings are exclusively in English.
* [ ] Database schema (tables, columns, constraints), stored key values, permission codenames and migrations are named in English.
* [ ] Commit messages, branch names, log messages, test names, configuration keys, configuration comments and URL paths are in English.
* [ ] All project documentation is in English.
* [ ] All user-facing texts (templates, forms, messages, emails, Django admin labels, error pages) are marked as translatable.
* [ ] A complete Swiss German translation exists for all user-facing texts. The user interface shows no untranslated English texts.
* [ ] The default user interface language is Swiss German (`de-ch`).
* [ ] No displayed text contains "ß", including texts from Django, its contrib apps and third-party packages; this is verified by an automated check.
* [ ] Translation files can be generated and compiled reproducibly.

## 14. Fact Base

* [ ] Decisions in `docs/DECISIONS.md` state their sources.
* [ ] Implementation and documentation are not based on assumptions. Unverified points are explicitly identified as such and either clarified with the project owner or marked as open.

## 15. Documentation

* [ ] Installation guide available, understandable for operators without in-depth IT knowledge.
* [ ] Environment variables documented.
* [ ] Setup of the first system administrator documented.
* [ ] Setup of a new instance documented.
* [ ] Microsoft Graph setup, including the restriction to the sender mailbox, documented.
* [ ] Procedure after a report of an unrequested change documented (block the account, verify identity, restore, log).
* [ ] Procedure for a lost second factor documented: for users the confirmation via the registered email address; for staff users and system administrators the identity check in person or by calling back a phone number known independently of the account; for the last system administrator the management command (`docs/SECURITY.md`, section 3).
* [ ] Operator responsibilities documented: security updates, backups (including that deleted data remains in backups until they expire), monitoring of the sender mailbox, reverse proxy configuration, provision of the privacy notice.
* [ ] Procedure for generating and compiling translations documented.
* [ ] Tests and known limitations documented.
* [ ] Security-relevant open points, unverified points and open risks documented.

## Not Required for Acceptance

* billing calculations and invoicing
* bank file processing
* payouts
* AGOV integration (only the preparation in section 3 is required) and passkeys
* productive financial processes
* final hosting decision
* LEGs as entities and all domain functions
* multi-tenancy (several municipalities in one instance)
* predefined roles other than system administrator, staff user and authenticated user
* additional user interface languages and language selection
* the content of the privacy notice and an imprint
* a public home page and publication of aggregated figures (D-025)

## Acceptance Rule

A criterion may only be marked as done if it has been implemented and adequately verified. Tests not run and open security questions must be explicitly reported.
