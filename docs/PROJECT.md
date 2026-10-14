# Project Scope – LEG-Platform

## Goal

Build a secure, maintainable foundation for the later administration of an LEG. The project was initiated by the LEG Ittigen and is first operated for Ittigen. The software is developed neutrally; other municipalities can run their own, independent instance from the same code base (D-013, D-014).

## Terminology

These terms are binding for all project documents and for naming in code.

| Term | Meaning |
| ---- | ------- |
| LEG-Platform | Name of this software. Python package name: `leg_platform`. |
| LEG | Local electricity community (German: "lokale Elektrizitätsgemeinschaft") under Art. 17d of the Swiss Electricity Supply Act (StromVG). By law, an LEG covers at most the area of one political municipality. It may span several localities with different postal codes within that municipality. |
| Instance | One independent installation of the application with its own database, configuration and users, operated for one municipality. |
| Instance configuration | All instance-specific values (e.g. name of the community or municipality, domain, canonical base URL, contact details, link to the privacy notice, link to the instance's website, branding such as logo and accent colour). Kept outside the code. |
| System administrator | Responsible for one instance: technical configuration, user management, management of roles and permissions. Has all permissions within the instance. Implemented as a Django superuser (`is_superuser`, together with `is_staff`, which Django requires for access to the admin). |
| Staff user | An account with access to the Django admin (`is_staff`) without superuser status. Can only use the functions for which permissions have been assigned, directly or through roles. Depending on these permissions, it can work with user data and contact users. |
| User | A person who can log in: their email address is verified and a password is set. |
| Authenticated user | A logged-in user without additional permissions. Can only access their own profile. |
| Customer number | A unique, randomly generated six-digit number assigned to an account when the registration is completed (password form submitted successfully via the verification link), and never reused. Displayed in two blocks of three digits (e.g. `482 915`). It identifies the account (e.g. later on invoices) but is not a login identifier (D-021). |
| Role | A named set of permissions, created and maintained at runtime in the admin (Django group). A later content administrator, for example, is a role. |
| Permission | A right to perform a specific function, defined in code with an English codename. |
| Retention obligation | A flag on an account indicating that the account has been used for something subject to a retention obligation. Once set, it can never be unset (D-016). |

The account states (unverified, active, temporarily locked, blocked, deactivated) are defined in the section "Account States".

Server and hosting operations (operating system, database server, deployment) take place outside the application and are not an application role.

## Technology

* Python
* Django
* MariaDB
* Tabler for the user interface
* Django admin for system administration
* Microsoft Graph for outgoing email
* Git for version control
* Django's translation mechanism for the user interface
* Established packages for security functions instead of custom developments, e.g. for two-factor authentication, login abuse protection and encryption (candidates in `docs/ARCHITECTURE.md`)

Only minimum versions are specified: the oldest versions that are technically possible and still receive security updates from their vendor. They are determined from official sources and proven by tests (D-015).

## Language

All software and project documentation are in English without exception: code, identifiers, database, migrations, comments, commit messages, logs, tests, configuration, URL paths and all documents in this repository.

Only the user interface is language-dependent:

* The default language is Swiss German (`de-ch`). The character "ß" is never used.
* All user-facing texts are translatable from the start, so that additional languages can be added later without restructuring.
* Additional languages and a language selection are not part of the first phase.

Details are defined in `AGENTS.md`, section 2.

## Working Basis

No assumptions are made. Requirements, technical statements and legal requirements are verified against official sources or the actual state of the project. When in doubt, the project owner is asked. Details are defined in `AGENTS.md`, section 3.

## User Functions

* Login page as the entry point of the application, with login, password reset, registration and links to the privacy notice and the instance's website; there is no public home page (D-025)
* Registration
* Email verification
* Login with email address, and logout
* Two-factor authentication with an authenticator app (optional for users, mandatory for staff users and system administrators)
* Password change
* Password reset via a single-use, time-limited link
* Viewing and editing one's own profile
* Validation of Swiss addresses and address autocompletion with a local copy of the official directory of building addresses (D-033)
* Changing one's own email address (effective only after the new address is verified)
* Deleting one's own account (with re-authentication)
* Display of an understandable account status
* Link to the instance's privacy notice at registration and on all pages accessible without login (D-022)

## Supported Devices

* The application must be usable on desktop computers, tablets and mobile phones (responsive user interface).
* In the first phase, all user functions listed above must be fully usable on a mobile phone, in particular registration, email verification, login with two-factor authentication, password change and reset, editing the profile, changing the email address and deleting the account.
* Later phases may restrict the range of functions on mobile phones because of the small screen. Which functions are affected is not specified yet.

## Design Language

The binding design language for all application views of all phases is defined in `docs/DESIGN.md` (D-024): one shared foundation with the registers "Clear" (entering, confirming and managing data) and "Warm" (informing), instance branding (logo, accent colour) as instance configuration, WCAG 2.2 level AA as target, light and dark mode, and a print stylesheet.

## Profile Data and Purpose

| Field | Required | Purpose |
| ----- | -------- | ------- |
| Customer number | generated when the registration is completed | Identification of the account; later reference on invoices |
| Name: salutation, first name, last name; or company name with optional contact person (D-032, D-036) | company name or first and last name | Identification; addressing invoices |
| Postal address: address addition (e.g. a second person or c/o), street, house number, post office box, postal code, town, country (D-032, D-036) | postal code, town, country, and street or post office box | Sending invoices by post |

The user model keeps all fields of the eCH-0010 structure (D-032). Fields that are not used are stored but not shown: title, organisation name add-ons, second address line, dwelling number, post office box text and locality (D-036).
| Email address | yes | Login, account notifications, sending invoices by email |
| Phone number | no | Queries |

All required fields are collected when the registration is completed, i.e. on the page reached through the verification link (section "Registration"), so that the profile is complete as a basis for the later LEG functions. Invoicing itself is not part of the first phase; the purposes are recorded so that only necessary data is collected, and they are communicated to the person in the privacy notice (DSG Art. 6 para. 3 and Art. 19; D-022).

## Registration

* The registration form asks only for the email address and links to the privacy notice (project owner, 2026-10-13). Until the address is verified, only the address is stored.
* After submission, the response is always the same generic message, e.g. "A link to activate your account has been emailed to the address provided." (OWASP Authentication Cheat Sheet).
* If the email address does not belong to an active, temporarily locked or blocked account, an unverified account without a customer number is created and a time-limited, single-use verification link is sent. An earlier pending registration with the same address is replaced.
* Via the verification link, the person enters the profile data (name, postal address, optional phone number) and sets the password. The link is valid for a limited time (proposal: 24 hours) and stays valid after an unsuccessful submission, e.g. a rejected password. Only when the form has been submitted successfully is the account active, and only then is a customer number assigned and shown in the profile; the link is consumed only at that point. Unverified registrations do not use up customer numbers (D-021). Verification therefore never activates a password that the person clicking the link did not choose. When the form has been submitted successfully, the person is logged in (owner decision of 2026-10-14, D-055: they have just proved control of the mailbox and chosen the password) and sees a page that offers the setup of two-factor authentication as the one recommended next step; skipping it leads to the profile, which reminds them in the session.
* Unverified accounts and their data are deleted automatically when the verification link expires.
* If the email address already belongs to an active, temporarily locked or blocked account, no account is created. An email to that address points to login and password reset.
* Registration and the emails it triggers are rate-limited, so that the function cannot be used to flood mailboxes. A new verification email is obtained by registering again; there is no separate function to resend it.

## Changing the Email Address

Users can change their email address themselves (D-016). The email address is only changed once the new address has been verified:

* The change requires re-authentication with the current password and, if set up, the second factor.
* A time-limited, single-use verification link is sent to the new address.
* Until the new address is verified, the account continues to use the old address unchanged.
* Only after successful verification is the old address replaced; the account continues under the new address.
* The old address receives a notification that the email address was changed, without a link that could undo or confirm anything. The notification states that the person should contact the operator immediately if they did not request the change; it does not invite a reply.
* Only the most recent pending change can be confirmed; earlier links become invalid.
* If the new address belongs to an active, temporarily locked or blocked account, the change is not performed. The user sees the same generic message as for a successful request; the new address receives an information email instead of a verification link. The check is repeated when the link is confirmed.
* If the new address belongs to an unverified account, the verification link is sent as usual; confirming it deletes the pending registration, because the confirmation proves control of the address.

## Account States

| State | Cause | Login | Password reset | Email address | Ended by |
| ----- | ----- | ----- | -------------- | ------------- | -------- |
| Unverified | Registration; email address not yet verified, no password set | no | no | replaced by a new registration with the same address | Verification and setting the password, or automatic deletion when the verification link expires |
| Active | Email address verified, password set | yes | yes | reserved | – |
| Temporarily locked | Too many failed login attempts | no, until the cool-off period has expired | yes | reserved | Expiry of the cool-off period |
| Blocked | Decision of a system administrator, e.g. misuse or suspected account takeover | no | no | reserved | A system administrator |
| Deactivated (shown as "Deleted (retention obligation)", D-055) | Deletion of an account with a retention obligation; the person is told that the account is deleted and that logging in is no longer possible | no | no | released for new registrations | A system administrator ("Restore account"), only if no active, temporarily locked or blocked account uses the email address |

Responses never reveal whether an account is blocked or deactivated. The stored key of the state stays `deactivated`; only its display label changed (D-055).

## Account Lifecycle

The account lifecycle is defined in `docs/DECISIONS.md` (D-016). In short:

* A person can log in once their email address is verified and a password is set. There is no additional approval step.
* System administrators can block and unblock accounts, including the accounts of other system administrators.
* Every account has a retention obligation flag. It is not set at registration. Later functions set it when an account is used for something subject to a retention obligation. Once set, it can never be unset.
* Deleting an account, by the user or by a system administrator, removes all of its data if no retention obligation is set. Security log entries are kept without personal reference until they expire (D-018).
* Deleting an account with a retention obligation deactivates it instead; the administration shows it as "Deleted (retention obligation)". The user cannot reactivate it. The user is told only that the account is deleted and that logging in is no longer possible (D-055).
* The same email address can be used for a new registration after deletion or deactivation.
* A system administrator can reactivate a deactivated account, unless an active, temporarily locked or blocked account with the same email address exists. A pending unverified registration with that address is deleted on reactivation.
* At least one active, not blocked system administrator always remains: the last one can neither be deleted, deactivated or blocked nor lose superuser status.
* If system administrators lock each other out or misuse their rights, the operator restores access with a management command on the server.
* Any promotion beyond the authenticated user requires an explicit action by an authorized superior. Nobody can promote their own account.
* Backups are the responsibility of the operator; the application does not process them. Data deleted in the application can remain in backups until they expire; the installation guide states this.

## Access Levels and Roles

The role model is defined in `docs/DECISIONS.md` (D-012).

### Fixed Access Levels

Three access levels are fixed:

* **System administrator:** responsible for the instance and has all permissions (implemented as Django `is_superuser` with `is_staff`). Two-factor authentication is mandatory.
* **Staff user:** has access to the Django admin (`is_staff`) and only the permissions assigned to it. Two-factor authentication is mandatory.
* **Authenticated user:** a logged-in user without additional permissions. Can only view and edit their own profile. Two-factor authentication is optional.

Only system administrators manage roles and their permissions, assign roles, grant or remove direct permissions, and grant or remove `is_staff` and `is_superuser`. This is enforced technically, including for staff users who have permissions to change users. Staff users can never change, block, deactivate or delete the accounts of staff users or system administrators, including their own account in the admin, and never change the password, email address or second factor of another account.

### Further Roles

Further roles are not defined or hard-coded at this time. They are created later in the admin as Django groups, named freely, renamed if needed and linked to permissions.

The first phase provides:

* management of roles (groups) and their permissions by system administrators
* assignment of roles to users by system administrators
* authorization checks based on permissions, not on role names

Role names such as "Content administrator", "Auditor", "Banker" or "Moderator" are examples of possible later roles only. They are not created, hard-coded or implemented in the first phase.

Roles are never implemented by granting superuser status (`is_superuser`). Only system administrators have it.

## Instances

Each municipality runs its own, independent instance (D-013).

* An instance has its own database, configuration, users and system administrators. Instances share no data.
* A person who uses several instances has a separate, independent account in each instance and may use the same email address in each.
* Instance-specific values are part of the instance configuration and are never hard-coded.
* A new instance is set up from the same code base using only the documented installation steps and its own configuration.
* The application runs on its own host name (e.g. a subdomain of the LEG's website), never under a path of another website. Information about the LEG is provided by the instance's website, which links to the login page (D-025).
* Operation, updates, backups and data of an instance are the responsibility of its operator.

## First System Administrator

The initial system administrator account is set up manually via a controlled Django management command run in a terminal on the server (D-005).

* The email address is read from the local, Git-ignored `.env` file of the instance and counts as verified, because the operator enters it on the server. The command assigns the customer number. The command asks for the initial password interactively with hidden input. The password is never stored in `.env`, in other files or in logs.
* There is no setup function on the website.
* The command only works as long as no system administrator exists (any account with `is_superuser`, including blocked and deactivated ones). It never overwrites or resets an existing account.
* At the first login, the password must be changed and two-factor authentication must be set up before anything else is possible.

## Email

Outgoing email is sent via Microsoft Graph (D-006).

Intended use:

* email verification
* password reset
* security notifications: changes of email address, password and two-factor authentication
* account notifications where needed

Security notifications state that the person should contact the operator immediately if they did not request the change, and name the instance's contact information where configured; they do not invite a reply (D-038). The installation guide describes the procedure after such a report (block the account, verify the person's identity, restore the email address or password, log the incident).

The system administrator configures Microsoft Graph in a dedicated section of the Django admin:

* tenant ID, client ID and sender address (the "tenant ID" is the Microsoft Entra term for the Microsoft organisation)
* certificate and private key (D-006): the private key is write-only; after saving, only the public certificate and its expiry date are displayed, with a warning before expiry
* a test send with understandable feedback

The private key is stored encrypted. The application authenticates with the certificate, not with a client secret (D-006). In Microsoft Entra / Exchange Online, the application is restricted to the sender mailbox. The installation guide explains these steps for operators without in-depth IT knowledge.

Configuration and credentials are separated between local, stage and production, and between instances. Local and stage delivery is controlled by default.

## AGOV and Passkeys

The first version supports login with the email address and a password, combined with an authenticator app (TOTP) as the second factor.

AGOV (the login service of the Swiss authorities) is intended as the second factor. The project owner has asked AGOV whether this use is permitted; the answer is pending. Until it is available, the documented TOTP second factor is used. If AGOV is permitted, the second factor is to be switched to AGOV without changing the architecture and without weakening security. The switch is expected to take place before the instance goes live, so that no productive TOTP enrolments have to be migrated.

Preparation in the first phase is limited to the following; no AGOV integration is built:

* Whether the second factor has been completed is checked in one central place, independent of the method used. Authorization, the mandatory second factor for staff users and system administrators, re-authentication and the account lifecycle do not depend on TOTP specifically.
* The TOTP-specific parts (enrolment, verification, recovery codes) are kept separate from this central check, so that they can be replaced or supplemented.
* An AGOV identity is linked to an internal account only through a stored, unambiguous identifier, never solely through a matching email address. Linking requires re-authentication, is notified by email and logged, like setting up TOTP; otherwise an attacker who knows only the password could link their own AGOV identity to the account.
* Before a switch, the security requirements (recovery, enrolment, re-authentication, reset of a lost factor, logging) are reviewed and documented again; a switch must keep at least the current level of security.

Whether password login can later be disabled for system administrators once AGOV or passkeys are available is open.

Account linking must not rely solely on a matching email address.

Passkeys are to be considered as a security goal. They are only implemented once the concrete solution, including recovery, enrolment and account protection, has been securely designed and tested.

## Environments

* Local: development and tests
* Stage: tests in a separate, production-like environment
* Production: later productive operation

Databases, secrets and email configurations must be separated per environment.

## Explicitly Not Included

* LEGs as entities and all domain functions
* billing calculations and invoicing
* electricity cost distribution
* productive financial processes
* bank file import
* payout execution
* AGOV integration (only prepared, see "AGOV and Passkeys") and passkeys
* final hosting decision
* multi-tenancy (several municipalities in one instance)
* predefined roles other than system administrator, staff user and authenticated user
* additional user interface languages and language selection
* a public home page and information about the LEG (provided by the instance's website, D-025)
* publication of aggregated figures (e.g. number of members, production and use within the LEG); the minimum number of participants for such figures is decided per instance when they are introduced (D-025)
* the content of the privacy notice (provided by the operator)
* an imprint (legal notice): deliberately open; expected on the instance's website; whether the login page links to it is decided together with the imprint (D-022, D-025)

## Extensibility

The architecture should allow later modules without making the first phase unnecessarily complex.

Security-relevant decisions are documented with their sources. Unverified points are explicitly marked as open.
