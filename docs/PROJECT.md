# Project Scope – LEG-Platform

## Goal

Build a secure, maintainable foundation for the later administration of an LEG, initiated by the LEG Ittigen and first operated for it. The software is neutral; other municipalities run their own, independent instance from the same code base (D-013, D-014).

## Terminology

These terms are binding for all project documents and for naming in code.

| Term | Meaning |
| ---- | ------- |
| LEG-Platform | Name of this software. Python package name: `leg_platform`. |
| LEG | Local electricity community ("lokale Elektrizitätsgemeinschaft") under Art. 17d of the Swiss Electricity Supply Act (StromVG). By law an LEG covers at most the area of one political municipality; it may span several localities with different postal codes within it. |
| Instance | One independent installation with its own database, configuration and users, operated for one municipality. |
| Instance configuration | All instance-specific values (name, domain, canonical base URL, contact details, privacy notice and website links, branding such as logo and accent colour), kept outside the code: the server environment and, for the presentation (name, website, privacy and legal addresses, colours, logos), the portal settings of the administration with the environment as fallback (D-073). |
| System administrator | Responsible for one instance: configuration, users, roles and permissions; all permissions. A Django superuser (`is_superuser` with `is_staff`). Takes part in the LEG like every account (D-113). |
| Staff user | `is_staff` without superuser status; only assigned permissions, plus the management area (D-086). Takes part in the LEG like every account (D-113). |
| Staff area | The management area (`/management/`) and the system administration (`/admin/`); opens in a second browser tab from the start page of "My area" (D-113). |
| User | A person who can log in: email address verified and password set. |
| Authenticated user | A logged-in user without additional permissions; accesses only their own profile and, with a customer number, the participation (D-105). |
| Customer number | Unique, random six-digit number assigned when the registration is completed, never reused; displayed in two blocks of three digits (`482 915`); identifies the account (e.g. later on invoices), not a login identifier (D-021). |
| Role | A named set of permissions, maintained at runtime in the admin (Django group). |
| Permission | A right to perform a specific function, defined in code with an English codename. |
| Retention obligation | A flag on an account showing it was used for something subject to a retention obligation; once set, never unset (D-016). |

Server and hosting operations are not an application role.

## Technology

`docs/ARCHITECTURE.md`.

## Language

English for all software and documentation; user interface Swiss German (`de-ch`), no further languages in the first phase (`AGENTS.md`, section 2).

## User Functions

* Login page as the entry point (login, password reset, registration, privacy notice and website links); no public home page (D-025); privacy notice link also on all pages accessible without login (D-022)
* Registration with email verification; login, logout; password change and reset via a single-use, time-limited link
* Two-factor authentication with an authenticator app (optional for users, mandatory for staff and system administrators)
* Own profile with validation of Swiss addresses and suggestions from a local copy of the official building address directory (D-033); an understandable account status
* Changing one's own email address (effective only after verification); deleting one's own account (with re-authentication)

## Supported Devices

* Usable on desktop computers, tablets and mobile phones (responsive). All user functions must be fully usable on a mobile phone, from registration to account deletion.
* Later phases may restrict functions on mobile phones; not specified yet (printing of lists in the management area is desktop only).

## Design Language

`docs/DESIGN.md` is binding (D-024).

## Profile Data and Purpose

| Field | Required | Purpose |
| ----- | -------- | ------- |
| Customer number | generated when the registration is completed | Identification; reference on invoices |
| Name: salutation, first name, last name; or company name with optional contact person (D-032, D-036) | company name or first and last name | Identification; addressing invoices |
| Postal address: address addition, street, house number, post office box, postal code, town, country (D-032, D-036) | postal code, town, country, and street or post office box | Sending invoices by post |
| Email address | yes | Login, account notifications, sending invoices by email |
| Phone number | no | Queries |

The user model keeps all fields of the eCH-0010 structure (D-032); unused fields are stored but not shown (D-036). Only necessary data is collected; the purposes are communicated in the privacy notice (DSG Art. 6 para. 3 and Art. 19; D-022). Invoicing is not part of the first phase.

## Registration

* The form asks only for the email address and links to the privacy notice (owner, 2026-10-13); until verified only the address is stored. The response is always the same generic message.
* If the address does not belong to an active, temporarily locked or blocked account, an unverified account without a customer number is created and a time-limited (proposal: 24 hours), single-use link is sent, replacing an earlier pending registration; otherwise no account is created and an email to that address points to login and password reset.
* Via the link the person chooses the sign-in method (password, password and app, or passkey), sets it up and enters the profile data; the link stays valid after an unsuccessful submission and is consumed only by the successful last step, which activates the account and assigns the customer number (D-021). Verification never activates a sign-in method the clicking person did not choose. Completing logs the person in (D-055) and offers two-factor setup as the one recommended next step.
* Unverified accounts are deleted when the link expires; registration and its emails are rate-limited.

## Changing the Email Address

Users change their address themselves (D-016); it changes only once the new address is verified:

* Re-authentication with one proof: the code of the second factor if set up, otherwise the current password (D-062). A single-use, time-limited link goes to the new address; until verified the old address stays.
* After verification the old address receives a notification without a link, telling the person to contact the operator immediately if they did not request it. Only the most recent pending change can be confirmed.
* If the new address belongs to an active, temporarily locked or blocked account the change is not performed; the user sees the same generic message as for success, the new address receives an information email, and the check is repeated at confirmation. If it belongs to an unverified account the link is sent as usual and confirming deletes the pending registration.

## Account States

| State | Cause | Login | Password reset | Email address | Ended by |
| ----- | ----- | ----- | -------------- | ------------- | -------- |
| Unverified | Registration; address not verified, no sign-in method in force | no | no | replaced by a new registration with the same address | Verification, sign-in method and details, or automatic deletion when the link expires |
| Active | Address verified, password set | yes | yes | reserved | – |
| Temporarily locked | Too many failed login attempts | no, until the cool-off period has expired | yes | reserved | Expiry of the cool-off period |
| Blocked | Decision of a system administrator (misuse, suspected takeover) | no | no | reserved | A system administrator |
| Deactivated (shown as "Deleted (retention obligation)", D-055) | Deletion of an account with a retention obligation | no | no | released for new registrations | A system administrator ("Restore account"), only if no active, temporarily locked or blocked account uses the address |

Responses never reveal whether an account is blocked or deactivated. The stored key is `deactivated` (D-055).

## Account Lifecycle

Defined in D-016:

* No approval step: a person can log in once the address is verified and a password is set. System administrators can block and unblock accounts, including other system administrators'.
* The retention obligation flag is set by later functions, never at registration, never unset.
* Deleting an account removes all its data if no retention obligation is set; security log entries are kept without personal reference until they expire (D-018). Otherwise the account is deactivated; the user cannot reactivate it (D-055).
* The same address can be registered again after deletion or deactivation. A system administrator can reactivate a deactivated account unless an active, temporarily locked or blocked account has that address; a pending unverified registration with it is deleted.
* At least one active, not blocked system administrator always remains; if administrators lock each other out the operator restores access with a management command on the server. Nobody is promoted beyond the authenticated user without an explicit action by an authorized superior, and nobody promotes their own account.
* Deleted data can remain in backups until they expire (installation guide).

## Access Levels and Roles

The role model is D-012; levels and enforcement: `docs/ARCHITECTURE.md`, "Users and Roles"; rules: `AGENTS.md`, section 6, and `docs/SECURITY.md`, section 4.

* **Fixed access levels.** Two-factor is mandatory for system administrators and staff users, optional for authenticated users. Only system administrators manage roles and permissions, assign roles and change `is_staff`, `is_superuser` and direct permissions. Staff users never change, block, deactivate or delete accounts of staff or system administrators (including their own in the admin) and never change another account's password, email address or second factor. **Every level may take part in the LEG** with its own account (a customer number is enough; D-113, owner decision 2026-10-23): staff users and system administrators register buildings and metering points like everybody and, in addition, work in the staff area. The owner accepts that such an account can approve its own application in the management area (D-086).
* **Further roles.** None is defined or hard-coded. They are created later in the admin as Django groups, named freely and linked to permissions. Authorization checks use permissions, never role names. Roles are never implemented by granting superuser status.

## Instances

Each municipality runs its own, independent instance (D-013): own database, configuration, users and administrators; no shared data. A person using several instances has a separate account in each. The application runs on its own host name, never under a path of another website; the LEG's website links to the login page (D-025). Set-up: `docs/ARCHITECTURE.md`, "Deployment Model".

## First System Administrator

The initial account is created by a controlled command on the server (D-005, D-076), not on the website:

* The email address comes from `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and counts as verified; the password is read by `setup_instance` from `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` or asked by `setup_initial_administrator` with hidden input (`docs/SECURITY.md`, section 7; `docs/OPERATIONS.md`, D8); never stored elsewhere or logged.
* It works only while no system administrator exists (any account with `is_superuser`, including blocked and deactivated ones) and never overwrites or resets an account.
* At the first login the password must be changed and two-factor set up before anything else is possible.

## Email

Outgoing email goes via Microsoft Graph (D-006; `docs/ARCHITECTURE.md`, "Email"; rules: `docs/SECURITY.md`, sections 3 and 8). Stage delivers like production (D-070).

* Security notifications tell the person to contact the operator immediately if they did not request the change, name the instance's contact information where configured, and do not invite a reply (D-038).
* They greet by first and last name only for a person with a suitable name and only at addresses belonging to the account at that time, otherwise neutrally. The name is blanked at once for a deleted or deactivated account (D-068; `docs/DESIGN.md`, section 8).
* The owner of the mailbox of a confirmed second address learns the account holder's name (D-068, L4; input for the privacy notice).
* The application is restricted to the sender mailbox in Microsoft Entra / Exchange Online (`docs/INSTALLATION.md`).

## Passkeys

Passkeys are an optional login method next to password with authenticator app (TOTP). One active credential suffices for every access level, including administrator activation and promotion; the last active passkey cannot be removed. An additional passkey on another device is strongly recommended. Saving an additional key ends other sessions and keeps the current one. Rules, recovery and reset: D-106 (`docs/DECISIONS.md`). A lost method is replaced by email while no building, metering point or IBAN is entered for the account; after that, and always for staff users and system administrators, the LEG administrator starts the recovery after an identity check.

Choosing the login method (D-106, "Guidance"): the step "Sign-in method" compares the methods in plain language and recommends "password and app" to users. It is offered in step 2 of the registration, in the start tile "Choose how you sign in", in the profile card "Sign-in method", after a reset and at the first login of an administrator. Users choose password, password and app, or passkey; administrators choose password and app or passkey.

## Environments

Local; stage (identical to production in behaviour and configuration, sends real emails, D-070); production. Separate databases, secrets and email configurations; no production data on stage.

## Explicitly Not Included

* billing, invoicing, electricity cost distribution, productive financial processes, bank file import, payouts
* final hosting decision, multi-tenancy
* predefined roles other than system administrator, staff user and authenticated user
* user interface languages beyond Swiss German, French, Italian and English (D-056)
* a public home page and information about the LEG (the instance's website, D-025); publication of aggregated figures (minimum number of participants decided per instance, D-025)
* the content of the privacy notice and imprint: written by the operator elsewhere; the portal only links the two addresses entered in the portal settings (D-022, D-025, D-073, D-074)

## Extensibility

Later modules must fit without complicating the first phase.

## Phase 2: Management Area for Staff Users

Phase 2 (D-077): the administration pages ("Verwaltung") for staff users, from the owner's design specification (`LEG-Design-Vorgaben/verwaltung/`, outside the repository): locations, metering points, LEGs, persons (D-078 to D-081), the overview with problem rows, PDF printing of lists (D-082, D-083; desktop only), "Person anlegen" with the encrypted IBAN (D-087). Structure: `docs/ARCHITECTURE.md`. Open (not built): "Anmeldung starten", change of the login address by staff, billing (D-077 "Open points"; D-087).

Persons are accounts. Every active staff user may use every page of the area (D-086, owner decision 2026-10-17, approved deviation from D-012). The name of the grid operator (called "BKW" in the owner's specification) is instance data entered by a system administrator in the Django administration (D-086).
