# Architecture Decisions

This document records important decisions. New decisions are added with date, status, rationale, sources and possible consequences.

Decisions are based on verified facts (see D-011). Technical and legal statements are backed by an official source.

## D-001: Backend

* Date: not recorded (before 2026-10-09)
* Status: Intended
* Decision: Python and Django. Minimum versions according to D-015.
* Rationale: Proven web framework with built-in authentication, admin interface, ORM, migrations and security mechanisms.
* Sources: decision of the project owner; Django documentation (https://docs.djangoproject.com/en/stable/topics/auth/, https://docs.djangoproject.com/en/stable/ref/contrib/admin/, https://docs.djangoproject.com/en/stable/topics/migrations/, https://docs.djangoproject.com/en/stable/topics/security/)

## D-002: Database

* Date: not recorded (before 2026-10-09)
* Status: Intended
* Decision: MySQL. Minimum version according to D-015.
* Rationale: Corresponds to the previous project planning of the project owner.
* Sources: statement of the project owner. Technical behaviour relied upon (unique indexes with `NULL` values) is documented with sources in D-016.

## D-003: User Interface

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision: Tabler for the application interface and the Django admin for system administration. No second UI framework. The application interface is responsive and fully usable on mobile phones in phase 1 (`docs/PROJECT.md`, "Supported Devices").
* Rationale: No unnecessary combination of several dashboard frameworks. Tabler describes itself as an "admin dashboard UI kit" for "admin panels, dashboards and web apps" and can therefore also serve later administrative application views. Tabler is built on Bootstrap 5, which "is developed mobile first".
* Alternatives: AdminLTE 4 or CoreUI for administrative views (not pursued: a second UI framework would mean two sets of templates, styles, JavaScript dependencies and security updates without functional gain; several CoreUI components are only available in the paid PRO version).
* Sources: https://github.com/tabler/tabler, https://getbootstrap.com/docs/5.3/getting-started/introduction/, https://github.com/ColorlibHQ/AdminLTE, https://github.com/coreui/coreui-free-bootstrap-admin-template (checked on 2026-10-09)
* Consequences: Tabler states minimum browser versions (Chrome/Edge 123, Firefox 128, Safari 17.5, Opera 109); older devices whose browsers cannot be updated are not supported. This is documented for operators and users.
* Design language: `docs/DESIGN.md` (D-024).

## D-004: Authentication in Phase 1

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * Phase 1 supports password login with the email address (D-021).
  * Two-factor authentication with an authenticator app (TOTP, e.g. Google or Microsoft Authenticator) is mandatory for staff users and system administrators and optional for users. An account that receives `is_staff` or `is_superuser` must set it up at its next login; until then its session has no privileges.
  * Single-use recovery codes are issued at setup and can be generated again after re-authentication.
  * Lost second factor without recovery code: for users, a system administrator may start the removal; the application sends a time-limited, single-use confirmation link to the registered email address, and the second factor is removed only after confirmation. The sender address of an incoming email is not accepted as proof, because it can be forged (RFC 7489). For staff users and system administrators, a system administrator resets it only after verifying the person's identity in person or by calling back a phone number known independently of the account; for the last system administrator only via a management command on the server. Every reset is logged and notified.
  * A password reset never removes or bypasses the second factor. Re-authentication requires the second factor if it is set up.
  * Password rules use Django's password validators: minimum length 15 characters for all accounts, maximum at least 64, no composition rules.
  * AGOV (the login service of the Swiss authorities) is intended as the second factor. The project owner has asked AGOV whether this use is permitted (status 2026-10-09: answer pending). Until then TOTP is used. If AGOV is permitted, the second factor is switched to AGOV, expected before going live, without changing the architecture or weakening security. Phase 1 therefore checks the completed second factor in one central place, independent of the method, and keeps the TOTP-specific parts separate (`docs/PROJECT.md`, "AGOV and Passkeys"). AGOV itself is connected only after the clarification.
  * Whether password login can later be disabled for system administrators once AGOV or passkeys are available is open.
  * Passkeys are a security goal and must be assessed together with account recovery and account protection.
* Verified facts:
  * `django-otp` provides TOTP devices that can be paired with Google Authenticator via the `otpauth` URL scheme, and static tokens usable as recovery codes. Its `OTPMiddleware` "adds `user.is_verified()` as a counterpart to `user.is_authenticated()`", and "Support for additional devices is handled by plugins" that subclass `django_otp.models.Device`; the verified state is therefore not tied to TOTP.
  * OWASP Authentication Cheat Sheet: minimum password length 15 characters without MFA and 8 with MFA, maximum at least 64; with MFA, re-authentication for email changes should use multi-factor verification. Because two-factor authentication is optional for users and can be removed, 15 characters apply to all accounts.
  * OWASP Multifactor Authentication Cheat Sheet: recovery codes and a "rigorous process" to verify identity for support-based resets are suggested methods; recovery mechanisms must not provide a way to bypass MFA.
  * Large providers restrict manual resets; GitHub, for example, states: "GitHub Support will not be able to restore access to accounts with two-factor authentication enabled if you lose your two-factor authentication credentials."
* Rationale for the lost-factor rule: users only have access to their own profile, and a password reset by email already gives access to accounts without a second factor; a confirmation through the registered mailbox is therefore proportionate. A security review on 2026-10-09 found that relying on the sender address of a request would allow an attacker who knows the password to forge it; the confirmation link closes this gap. Staff users and system administrators have extensive access and require a strict identity check; there are few such accounts, so the effort stays small.
* Not yet verified: how `django-otp` protects the Django admin; to be verified in its documentation before implementation.
* Security implications: system administrators have all permissions and staff users have extensive access; a password alone is not sufficient protection for these accounts. For users, the email account is the recovery anchor; its compromise together with the password allows removing the second factor through a system administrator.
* Sources: https://django-otp-official.readthedocs.io/en/stable/overview.html, https://pypi.org/project/django-otp/, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html, https://cheatsheetseries.owasp.org/cheatsheets/Multifactor_Authentication_Cheat_Sheet.html, https://www.rfc-editor.org/rfc/rfc7489, https://docs.github.com/en/authentication/securing-your-account-with-two-factor-authentication-2fa/recovering-your-account-if-you-lose-your-2fa-credentials (checked on 2026-10-09)

## D-005: Initial System Administrator

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * The first system administrator account (Django superuser) of an instance is set up manually via a Django management command run in a terminal on the server. There is no fully automated setup and no setup function on the website.
  * The email address is read from the local, Git-ignored `.env` file of the instance. It is not recorded in the repository.
  * The command asks for the initial password interactively with hidden input. The password is never stored in `.env`, in other files or in logs.
  * The command only runs as long as no system administrator exists. It never overwrites or resets an existing account.
  * At the first login, the password must be changed and two-factor authentication must be set up before anything else is possible.
  * A separate management command restores access in an emergency (e.g. resetting the second factor of the last system administrator, unblocking a system administrator). It is run only on the server and its use is logged.
* Verified facts: Django's own `createsuperuser` command asks for the password interactively in the same way.
* Not yet verified: whether an established package provides the forced password change at first login; otherwise it is implemented as a small, tested component.
* Security implications: the password never exists in plain text in a file. A terminal on the server is required; the project owner expects it to be available for the Ittigen instance.
* Alternatives: initial password in `.env` (rejected: plain text in a file until it is removed manually); setup via the website (rejected: whoever opens the page first could take over the instance).
* Sources: https://docs.djangoproject.com/en/stable/ref/django-admin/#createsuperuser

## D-006: Email Delivery via Microsoft Graph

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * Outgoing email is sent via Microsoft Graph: email verification, password reset, security notifications and account notifications where needed.
  * The system administrator maintains the configuration in a dedicated section of the Django admin: tenant ID, client ID, sender address, client secret and its expiry date, plus a test send with understandable feedback.
  * The client secret is write-only. After saving, only "set" and the expiry date are displayed; the admin warns before expiry.
  * The client secret is stored encrypted in the database. The key is derived from Django's `SECRET_KEY` with HKDF and a fixed, purpose-specific label; the secret is encrypted with Fernet. Both come from the established `cryptography` library. Keys derived from `SECRET_KEY_FALLBACKS` are tried for decryption, after which the secret is encrypted again with the current key. If no key matches, the admin clearly asks for the secret to be entered again.
  * The configuration is accessible only to system administrators; every change is logged.
  * In Exchange Online, the application is restricted to the sender mailbox with "RBAC for Applications"; no organisation-wide `Mail.Send` permission remains in Microsoft Entra ID. The installation guide explains this step by step.
  * The environment variable `EMAIL_MODE` controls delivery: `console` (default, output only), `allowlist` (only to `EMAIL_ALLOWED_RECIPIENTS`) and `graph` (unrestricted, only allowed with `APP_ENV=production`).
  * Security notifications ask the person to reply to the email if they did not request the change. Replies go to the sender mailbox, which the operator must monitor. No separate reply address is configured.
* Verified facts:
  * The application permission `Mail.Send` "allows the app to send mail as any user without a signed-in user".
  * RBAC for Applications in Exchange Online allows restricting an application to specific mailboxes and replaces Application Access Policies. Permissions granted in Microsoft Entra ID and in RBAC for Applications are additive, so the unscoped Entra grant must be removed.
  * `SECRET_KEY` is used for cryptographic signing; Django supports rotation via `SECRET_KEY_FALLBACKS`.
* Not yet verified: the exact HKDF and Fernet API of the installed `cryptography` version; to be verified before implementation.
* Rationale: the database password alone does not protect against leaked backups or database exports, access by hosting staff or SQL injection. Deriving the key from `SECRET_KEY` adds this protection without additional effort for operators. HKDF with a purpose-specific label keeps this key separate from Django's other uses of `SECRET_KEY`.
* Alternatives: plain text storage (rejected: a leaked secret would allow sending mail as any user of the organisation); separate encryption key (rejected for now: additional burden for operators without in-depth IT knowledge); secret only in environment variables (rejected: the project owner wants the configuration in the admin); using `SECRET_KEY` directly as encryption key (rejected: no key separation).
* Open: whether certificate-based authentication is preferable to a client secret is to be checked in the Microsoft documentation.
* Sources: https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac, https://docs.djangoproject.com/en/stable/ref/settings/#secret-key, https://pypi.org/project/cryptography/

## D-007: Security Keeper

* Date: not recorded (before 2026-10-09)
* Status: Decided
* Decision: Claude Code and Codex actively prioritise security, name risks and do not silently implement insecure shortcuts. Details: `docs/SECURITY.md`.
* Security implications: the deny rules in `.claude/settings.json` (D-020) and the agent definitions in `.claude/agents/` (D-019) apply only to Claude Code, not to Codex.
* Sources: decision of the project owner

## D-008: Hosting

* Date: not recorded (before 2026-10-09)
* Status: Open
* Context: Preference of the project owner: Infomaniak.
* Decision: The final choice is made after a local prototype and a realistic assessment of operations, costs, backups, TLS and portability. A terminal (SSH) on the server is required for management commands (D-005).
* Sources: statement of the project owner

## D-009: Billing Logic

* Date: not recorded (before 2026-10-09)
* Status: Not part of phase 1
* Decision: No billing or productive financial functions in the first development step.
* Sources: decision of the project owner

## D-010: Language of Software, Documentation and User Interface

* Date: 2026-10-09
* Status: Decided
* Context: The software and its documentation should be uniform and maintainable internationally. The end users speak Swiss German. Multilingual support may follow later.
* Decision:
  * All software and project documentation are in English without exception: code, identifiers, database, stored key values, migrations, permission codenames, comments, docstrings, commit messages, branch names, logs, tests, configuration, URL paths, management commands and all documents in this repository.
  * Only the user interface is language-dependent. The default language is Swiss German, `LANGUAGE_CODE = "de-ch"`. The character "ß" is never used.
  * User-facing texts use Django's translation mechanism (English source texts, Swiss German translations in `locale/de_CH/LC_MESSAGES/django.po` under `LOCALE_PATHS`).
  * Texts that Django, its contrib apps or third-party packages deliver with "ß" are overridden in the project translation files. An automated check ensures that no displayed text contains "ß".
  * Phase 1 ships Swiss German only, without a language selection.
  * Communication between the project owner and the AI is in German. Instructions given in German are written in English when they go into the repository.
* Verified facts:
  * Django's `django/conf/locale/de_CH/` contains only `__init__.py` and `formats.py`, i.e. formats but no texts.
  * For a sublanguage without its own translation, Django falls back to the base language (`de-ch` → `de`).
  * Django's `de` translations contain "ß", e.g. "Schließen" (admin), "Datum außerhalb des zulässigen Bereichs", "Dieser Wert muss größer oder gleich … sein" (core), "Blockgröße" (auth).
  * Directories in `LOCALE_PATHS` have the highest precedence when Django looks up translations.
* Rationale: English is the common language for code, libraries and Django's documentation. The built-in translation mechanism allows multilingual support without restructuring.
* Security implications: none directly. Uniform identifiers make reviews and security checks easier.
* Alternatives: German identifiers in code (rejected); German user texts hard-coded in code (rejected, because multilingual support would then require restructuring); `LANGUAGE_CODE = "de"` (rejected, Swiss German is required).
* Sources: https://docs.djangoproject.com/en/stable/topics/i18n/translation/, https://docs.djangoproject.com/en/stable/ref/settings/#language-code, https://docs.djangoproject.com/en/stable/ref/settings/#locale-paths, https://github.com/django/django/tree/main/django/conf/locale (checked on 2026-10-09 on branch `main`; to be re-checked against the installed Django version)
* Consequences: Every user-facing text requires a Swiss German translation. Missing translations and "ß" in displayed texts are defects. Each Django upgrade and each new third-party package requires a check for new texts containing "ß".

## D-011: Fact-Based Work Without Assumptions

* Date: 2026-10-09
* Status: Decided
* Context: AI agents may have outdated knowledge or fill gaps with guesses.
* Decision:
  * No assumptions are made. Statements, decisions and implementations are based on verified facts.
  * Permitted bases are the actual state of the project and official sources: vendor documentation and release notes, official repositories, official legal texts and publications of authorities, recognised standards (e.g. OWASP, IETF, W3C) and statements by the project owner.
  * Tabloid and news portals, blogs, forums, Q&A sites, social media, SEO guides, content of unclear origin and AI-generated texts are not a basis.
  * When in doubt, ask.
* Rationale: Wrong assumptions endanger security, data protection, legal compliance and maintainability.
* Security implications: positive. Security decisions are based on documented vendor information and standards.
* Consequences: Decisions state their sources. Reports distinguish between verified and unverified points. Details in `AGENTS.md`, section 3.

## D-012: Role Model

* Date: 2026-10-09 (updated)
* Status: Decided
* Context: The concrete roles of the LEG are not yet known. Roles may be renamed later, and further roles will be added and linked to permissions over time.
* Decision:
  * Fixed access levels are the **system administrator** (responsible for the instance, implemented as Django `is_superuser` with `is_staff`, has all permissions), the **staff user** (`is_staff` without `is_superuser`, access to the admin with only the permissions assigned to it) and the **authenticated user** (logged in, no additional permissions, access to their own profile only).
  * All further roles are Django groups. They are created, named, renamed and linked to permissions in the admin. They are runtime data, not code.
  * No role is hard-coded. No code, migration or fixture creates, names or checks specific roles.
  * Authorization checks use permissions only, never group names.
  * Permissions for application functions are defined in code with English codenames.
  * New users receive no group memberships, no additional permissions, no `is_staff` and no `is_superuser`.
  * Only system administrators manage roles, permissions, role assignments, `is_staff` and `is_superuser`. This is enforced technically, including for staff users who have permissions to add or change users.
  * Staff users can never change, block, deactivate or delete the accounts of staff users or system administrators, including their own account in the admin, and never change the password, email address or second factor of another account.
  * Nobody can promote their own account. Promotions are performed only by an authorized superior; `is_superuser` is only granted by another system administrator.
  * System administrators can block other system administrators and remove their rights. At least one active, not blocked system administrator always remains; emergency access is restored via a management command on the server (D-005).
  * Example roles ("Content administrator", "Auditor", "Banker", "Moderator") are illustrations only and are not implemented.
* Verified facts:
  * A Django group is a generic way of categorising users to apply permissions; a user in a group automatically has the group's permissions.
  * The group name is a freely editable field (up to 150 characters).
  * `is_superuser` treats the user as having all permissions without assigning them individually.
  * `is_staff` allows access to the admin site; "by default, logging in to the admin requires that the user has the `is_staff` attribute set to `True`".
  * Django warns: "If you give a non-superuser the ability to edit users, this is ultimately the same as giving them superuser status because they will be able to elevate permissions of users including themselves!"
* Rationale: Django's built-in groups and permissions provide exactly the required flexibility without custom role logic.
* Security implications:
  * System administrators bypass all permission checks. The number of system administrator accounts is kept to a minimum, these accounts require strong authentication, and administrative actions are logged.
  * Anyone allowed to change users, groups or permissions without restriction can grant themselves further rights (privilege escalation). The admin therefore restricts these functions to system administrators, and delegating role assignment to other roles requires a separate, reviewed decision. Every promotion is logged.
  * A system administrator can always create a second account and promote it; "nobody can promote their own account" does not limit system administrators. Logging makes such actions traceable.
  * Later financial functions may require separation of duties (e.g. a four-eyes principle). A system administrator who can do everything conflicts with this. This must be assessed before financial modules are planned.
  * Because roles are data, tests must create the groups and permissions they need themselves and must not rely on predefined roles.
* Alternatives: hard-coded roles in code or migrations (rejected, because roles must be renamable and extensible at runtime); custom role model (rejected, Django's built-in model suffices).
* Terminology: see `docs/PROJECT.md`, section "Terminology".
* Sources: https://docs.djangoproject.com/en/stable/topics/auth/default/, https://docs.djangoproject.com/en/stable/ref/contrib/auth/, https://docs.djangoproject.com/en/stable/ref/contrib/admin/
* Consequences: Every protected function defines and checks a permission. Roles become meaningful only once a system administrator creates them in the admin and assigns permissions.

## D-013: Deployment Model – One Instance per Municipality

* Date: 2026-10-09
* Status: Decided
* Context: The application is built for the LEG in Ittigen. Other municipalities (e.g. Bolligen) may later want to use the same software. A multi-tenant design (several municipalities in one instance with tenant administrators) was discussed in detail and rejected in favour of separate instances.
* Decision:
  * The application is not multi-tenant. Each municipality runs its own, independent instance.
  * Each instance has its own database, configuration, secrets, users and system administrators. Instances share no data.
  * A person who uses several instances has a separate, independent account in each and may use the same email address in each.
  * The same code base serves all instances. Instance-specific values (e.g. name of the community or municipality, domain, canonical base URL, sender address, contact details) are part of the instance configuration and are never hard-coded in code, templates or migrations.
  * A new instance is set up from the same code base using only the documented installation steps and its own configuration.
  * Operation, updates, backups and data of an instance are the responsibility of its operator.
* Verified facts:
  * By law, an LEG covers at most the area of one political municipality (Art. 17d para. 3 StromVG). One instance per municipality therefore matches the legal maximum extent of an LEG.
  * Postal codes do not match municipal boundaries: according to the official directory of localities (swisstopo), one postal code can cover several municipalities (e.g. 3048 Worblaufen: 81.9 % of addresses in Ittigen, 18.1 % in Bern). This was relevant for the rejected multi-tenant design and becomes relevant again when locations are introduced.
  * Django's permission framework does not implement object permissions in its core. A multi-tenant design would have required a custom, central tenant separation mechanism.
* Rationale:
  * Multi-tenancy is not needed for the current goal (one LEG in Ittigen); other municipalities are only potential future users. The project rules forbid overloading the foundation with speculative functionality.
  * Separate instances with separate databases are the strongest form of data separation. A programming error cannot disclose data of one municipality to another.
  * Considerably less complexity in the data model, authorization and tests.
* Security implications:
  * Positive: no cross-municipality data disclosure is possible within the application.
  * Every instance must be kept up to date with security updates. An instance that is not maintained is a risk for its own data. Update and operating responsibilities must be clarified with every additional operator.
  * `ALLOWED_HOSTS` lists only the instance's own domains; links in emails are built from the canonical base URL of the instance.
* Alternatives: multi-tenant platform with tenants, tenant administrators and tenant switching (rejected: high complexity and a primary security boundary inside the application, without a current need).
* Sources: StromVG Art. 17d (https://fedlex.data.admin.ch/filestore/fedlex.data.admin.ch/eli/cc/2007/418/20260401/de/pdf-a/fedlex-data-admin-ch-eli-cc-2007-418-20260401-de-pdf-a-1.pdf, version of 1 April 2026), swisstopo "Amtliches Ortschaftenverzeichnis" (https://www.swisstopo.admin.ch/de/amtliches-ortschaftenverzeichnis, dataset checked on 2026-10-09), https://docs.djangoproject.com/en/stable/topics/auth/customizing/, https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts
* Consequences:
  * No tenant concept in the data model, authorization or user interface.
  * Combining several municipalities in one instance later would be a significant redesign.
  * There is no central administration across instances.

## D-014: Product Name and Neutrality

* Date: 2026-10-09
* Status: Decided
* Decision:
  * The software is called "LEG-Platform"; the Python package name is `leg_platform`.
  * The project was initiated by the LEG Ittigen. The software is developed neutrally so that other municipalities can use it. The initiator is mentioned in the README; references to Ittigen in the documentation refer to the initiating instance.
  * Configuration templates such as `.env.example` contain no instance-specific values. Personal data of operators or administrators (e.g. email addresses) is not recorded in the repository.
* Verified facts: a GitHub repository search on 2026-10-09 showed no LEG or energy project named "LEG-Platform". The name "LEG-Abrechnung" is already used (`schopf16/leg-abrechnung`). No trademark search was performed.
* Sources: GitHub repository search (https://github.com/search?q=leg-platform&type=repositories, 2026-10-09)

## D-015: Supported Versions and Versioning

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * Only minimum versions of Python, Django, MySQL and all packages are specified: the oldest versions that are technically possible and still receive security updates from their vendor.
  * The minimum versions result from the functions used and from the requirements of the packages used (e.g. `django-axes` requires Python ≥ 3.10). They are determined from official sources and proven by automated tests, not estimated.
  * No package with known security vulnerabilities is used in order to support old versions.
  * When a minimum version reaches its vendor's end of security support, the minimum version is raised.
  * The software uses semantic versioning. Raising the minimum major version of Python, Django or MySQL can lead to a new major version of the software.
* Verified facts: Django 4.2 no longer receives security updates since 7 April 2026; Django 5.2 LTS receives them until April 2028 (checked on 2026-10-09). `django-axes` 8.3.1 still declares Django 4.2, so package declarations alone do not determine the minimum version.
* Not yet verified: support periods of MySQL and Python; to be checked against the vendors' official pages in the next step.
* Open: how the minimum versions are proven by tests (e.g. a test matrix in continuous integration over the declared minimum and current versions); decided together with the minimum versions.
* Rationale: "as old as possible" alone would allow versions without security updates; declaring them as supported contradicts security by default.
* Sources: https://semver.org/, https://www.djangoproject.com/download/

## D-016: Account Lifecycle

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * A person can log in once their email address is verified and a password is set. There is no approval step.
  * The password is set only after verification, via the verification link. Unverified accounts and their data are deleted automatically when the verification link expires; a new registration with the same address replaces a pending one. Reason (security review 2026-10-09): if the password were chosen at registration, an attacker could register with someone else's address and a known password, and the victim would activate the attacker's account by clicking the link.
  * Account states: unverified, active, blocked, deactivated; in addition, the abuse protection temporarily locks accounts after too many failed attempts (`docs/PROJECT.md`, "Account States").
  * System administrators can block and unblock accounts, including those of other system administrators. Blocked accounts cannot log in or reset their password. During a temporary lockout, a password reset remains possible.
  * Every account has a retention obligation flag. It is unset at registration. Later functions set it when the account is used for something subject to a retention obligation. It can only change from unset to set; the application rejects any attempt to unset it, including in the Django admin.
  * Deleting an account, by the user or by a system administrator, without a retention obligation removes all of its data; security log entries are kept without personal reference until they expire (D-018).
  * Deleting an account with a retention obligation deactivates it. A deactivated account cannot log in or reset its password; responses do not reveal its existence. The user cannot reactivate it.
  * Email addresses are unique among accounts that are not deactivated. The same email address can therefore be used for a new registration. Uniqueness is enforced by the database with a nullable column and a normal unique index, because MySQL does not support conditional unique constraints (`docs/ARCHITECTURE.md`, "User Model and Login Identifiers").
  * A system administrator can reactivate a deactivated account unless an active, temporarily locked or blocked account with the same email address exists.
  * At least one active, not blocked system administrator always remains: the last one can neither be deleted, deactivated or blocked nor lose superuser status.
  * Users can change their email address. The change takes effect only after the new address has been verified; until then the old address remains valid. After verification the old address is replaced and the account continues under the new address. The change requires re-authentication, and the old address is notified. A change to an address of an active, temporarily locked or blocked account is not performed; the response is the same as for a successful request, and the new address receives an information email.
  * Registration with an address of an active, temporarily locked or blocked account creates no account; the response is the same as for a new registration, and the address receives an information email.
  * Confirming an email address change or reactivating an account deletes a pending unverified registration with the same address, so that a pending registration cannot block either.
  * Every path that deletes, deactivates, reactivates, blocks, unblocks, promotes or demotes accounts, changes the email address or changes the retention flag goes through the central account logic, including admin bulk actions; Django's "delete selected objects" action does not call the model's `delete()` method.
  * In phase 1, no function sets the retention obligation yet; the flag and the deactivated state are part of the foundation so that later functions can build on them.
  * Profile data and purposes: customer number (generated when the registration is completed, i.e. when the password form is submitted successfully via the verification link, D-021), name, postal address and email address (all required, collected at registration) for identification and invoices by post or email; phone number (optional) for queries. The profile is complete from the start as a basis for the later LEG functions.
  * Backups are the responsibility of the operator. Deleted data can remain in backups until they expire; the documentation states this.
* Verified facts:
  * OWASP Authentication Cheat Sheet: account creation uses a generic response such as "A link to activate your account has been emailed to the address provided."; email changes send confirmation emails to both the old and the new address.
  * Django's password reset sends no email to inactive users and users without a usable password, and shows no error message ("This prevents information leaking to potential attackers.").
  * Django ignores the `condition` of unique constraints on MySQL; MySQL unique indexes permit multiple `NULL` values.
* Security implications: the one-way rule for the retention obligation is enforced at application level; persons with direct database access can bypass it. Database privileges are therefore restricted (`docs/SECURITY.md`, section 9).
* Open: which concrete retention obligations apply and for how long is determined later from official legal sources.
* Sources: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html, https://docs.djangoproject.com/en/stable/topics/auth/default/, https://docs.djangoproject.com/en/stable/ref/models/indexes/, https://dev.mysql.com/doc/refman/8.4/en/create-index.html (checked on 2026-10-09)

## D-017: Protection Against Automated Login Attacks

* Date: 2026-10-09 (updated)
* Status: Decided (package choice to be confirmed with D-015)
* Decision:
  * Login and recovery are protected against automated abuse in the application itself, following established practice, with an established package instead of a custom development. The protection works per account and per IP address.
  * Lockouts are temporary (cool-off period), so that attackers cannot lock out other users permanently; a password reset remains possible during a temporary lockout. A successful reset ends only the lockout of that account, not the counters and lockouts of IP addresses (updated 2026-10-09: otherwise an attacker could lift the lockout of their IP address by resetting the password of their own account).
  * Login with a nonexistent email address shows the same response and lockout behaviour as a wrong password.
  * Remaining risk: attackers who know an email address can repeatedly cause temporary lockouts of that account and use up the per-address email limit. Accepted, because lockouts are temporary, the most recent link sent to the owner's mailbox stays valid when the limit is exceeded (the attacker's own requests also deliver valid links to the owner), and new emails are possible after the time window.
  * Failed attempts are counted per normalised email address. The customer number is not a login identifier (D-021).
  * Behind reverse proxies, the number of trusted proxies is configured per instance; client IP headers are only evaluated as set by trusted proxies.
  * Registration, password reset and email change requests are also rate-limited, per IP address and per normalised target email address (updated 2026-10-09: a limit per IP alone does not prevent flooding a mailbox from many addresses or repeatedly replacing another person's pending registration). The per-address limit restricts the emails sent; above it, a request is not processed and replaces nothing, so the most recent link sent stays valid. Neither the response nor the response time changes. The counters are kept only for the time window (`docs/SECURITY.md`, section 3).
  * There is no mandatory requirement for the operator; operators may add further measures.
  * Candidate: `django-axes`.
* Verified facts:
  * `django-axes` "keeps track of failed login attempts in Django-powered sites"; release 8.3.1 (2026-02-11); declares Django 4.2, 5.2 and 6.0; requires Python ≥ 3.10.
  * `django-axes` provides `AXES_IPWARE_PROXY_COUNT` and related settings and warns that "the client can spoof IP addresses by just setting the header in their request".
  * OWASP Authentication Cheat Sheet: lockout mechanisms must not allow a denial of service by locking out other users' accounts; password recovery may remain possible for locked accounts.
* Open: the concrete lockout parameters, how `django-axes` stores IP addresses and usernames, and how password reset and registration requests are throttled are verified against the package documentation and OWASP before implementation. In particular: the `django-axes` documentation states that `axes_reset_username` "will clear lockouts and records for the given usernames"; whether this also lowers IP-based counts is verified against the installed version, and if so the account's lockout is ended differently or IP counters are kept separately (security review 2026-10-09).
* Sources: https://pypi.org/project/django-axes/, https://github.com/jazzband/django-axes, https://django-axes.readthedocs.io/en/latest/4_configuration.html, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html

## D-018: Security Logging

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * Security-relevant events are logged; the list is in `docs/SECURITY.md`, section 10.
  * Logs never contain passwords, tokens, secrets or complete security links, and only as much personal data as necessary.
  * Only system administrators can view security logs.
  * Log entries of deleted accounts are kept without personal reference (e.g. only an internal number instead of name and email address).
  * Retention is configured per instance in the server configuration, not in the admin, with minimum values that cannot be undercut. Default: 1 year for changes of roles, permissions, `is_staff` and `is_superuser`, resets of the second factor of staff users and system administrators, blocking and unblocking of staff users and system administrators, changes by system administrators to the password or email address of another account, deletion, deactivation and reactivation of accounts by system administrators, use of emergency management commands and changes to the Microsoft Graph configuration, because misuse of privileges is often noticed late; 30 days for all other log entries, including the failed-attempt records of the abuse protection package. Expired entries are deleted automatically.
  * The security log, including the failed-attempt and lockout records of the abuse protection package, is append-only within the application; nobody, including system administrators, can create, change or delete entries through the admin or other application functions. Otherwise logging would not be an effective control over system administrators (D-012).
  * Log entries use an internal number, not the customer number, because the customer number appears on invoices.
  * The design follows established practice with good usability; the OWASP Logging Cheat Sheet is used as the basis and verified before implementation.
* Rationale: the project owner does not want to review logs daily and does not need entries older than about a month for operational errors. DSG Art. 6 para. 4 requires personal data to be destroyed or anonymised as soon as it is no longer required for the purpose.
* Open: how the automatic deletion is scheduled; decided at implementation together with the hosting.
* Sources: DSG (SR 235.1), Art. 6 para. 4 (https://www.fedlex.admin.ch/eli/cc/2022/491/de, version of 1 September 2023 checked on 2026-10-09; to be re-checked for later versions)

## D-019: Agent Workflow

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * `developer` implements. `tester` plans and runs tests and checks, including Django's security checks. `security-reviewer` reviews read-only.
  * `security-reviewer` is restricted technically via its `tools` list to `Read, Grep, Glob, WebFetch, WebSearch`: it cannot edit files, run commands, start subagents or instruct other agents.
  * `developer` and `tester` have no tool restrictions. They may start subagents for clearly bounded, simpler tasks (e.g. with a less expensive model) under the delegation rules in `AGENTS.md`, section 11. The delegating agent remains responsible and must review the result before using it.
  * Deliberately accepted deviation (project owner, 2026-10-09): `developer` keeps the unrestricted `Agent` tool and `tester` keeps `Edit` and `Write`, although a `tools` list or `disallowedTools` could restrict them. This is an accepted decision of the project owner, although such a restriction would be an effective technical control: it removes the tool completely and does not depend on anyone reading a permission prompt. Risks:
    * `developer` can start the `security-reviewer` or other agents itself and thereby influence the review of its own code.
    * `tester` can technically change application code, which its instructions forbid.
    * Both can change the controls themselves: the agent files (including the `tools` list of `security-reviewer`), `CLAUDE.md` and `AGENTS.md` (including the references to `CONDUCT.md`), and `.claude/settings.json` and `CONDUCT.md` only by bypassing their deny rules (e.g. through a script subprocess). `AGENTS.md`, section 12, is an instruction, not a technical control; content injected e.g. from the web could bypass it.
  * Mitigation: the rules of conduct in `CONDUCT.md`; the main session starts reviews itself and, before each review run, checks changes to `.claude/**`, `CLAUDE.md` and `AGENTS.md`; critical changes require a human review.
  * All agents and subagents follow the rules of conduct in `CONDUCT.md` ("don't be evil").
  * Self-improvement (project owner, 2026-10-09): agents may improve their agent files and the project instructions (`CLAUDE.md`, `AGENTS.md`) and record lessons learned, always under `CONDUCT.md` and never weakening security (`AGENTS.md`, section 12.2). Agent files are therefore not write-protected.
  * Protection of the premise "don't be evil" (project owner, 2026-10-09: it must never be deleted): the rules of conduct are kept in a separate file `CONDUCT.md`, because a single section of a file cannot be protected without protecting the whole file. Edit deny rules in `.claude/settings.json` cover `CONDUCT.md` and `.claude/settings.json` itself, so that the rule cannot be removed with Claude's file tools. Only the project owner changes these files. Limit (Claude Code documentation, "Configure permissions"): Edit deny rules apply to Claude's file tools and to file commands Claude Code recognises in Bash, but not "to arbitrary subprocesses that read or write files indirectly, like a Python or Node script that opens files itself"; whether they apply to PowerShell commands is not verified. The protection is therefore a technical barrier against accidental or casual changes, not a complete protection; `CONDUCT.md` itself forbids working around it. Further limits: the deny rules apply only to Claude Code, not to Codex (D-007); `.claude/settings.local.json` is not covered, and hooks configured there could run arbitrary commands (a deny rule for it is proposed to the project owner); the rule is only effective as long as `CLAUDE.md`, `AGENTS.md` and the agent files refer to `CONDUCT.md`, so these references are listed in `AGENTS.md`, section 12.2, as items that are never removed. Verified in the actual environment (2026-10-09): the Edit tool and `sed` were refused on `CONDUCT.md` and `.claude/settings.json`, also while the session's working directory was `.claude/agents`.
  * Until the Git repository exists, the main session has no baseline to compare changed control files against; it relies on the agents' reports. Initialising Git (README, next step 1) closes this gap.
  * The main session coordinates: it passes review findings to the developer, has a new review run check the corrections, and reports findings, corrections and re-checks to the project owner. Critical findings and decisions go to the project owner.
  * Each agent reads only the documents it needs (listed in its agent file), to keep token usage low; `CLAUDE.md` is loaded into every subagent automatically.
* Verified facts (Claude Code documentation):
  * Project subagents are stored in `.claude/agents/`; `name` and `description` are required.
  * If `tools` is omitted, a subagent inherits every available tool; a `tools` list restricts it to the listed tools.
  * By default, a subagent can spawn subagents of its own, up to three layers below the main conversation; omitting `Agent` from the `tools` list prevents this.
  * Each subagent starts with its own context and receives the delegation prompt written by the main session and the CLAUDE.md hierarchy.
  * Only an @-mention guarantees that a specific subagent runs.
  * When the main conversation runs in auto mode, subagents run in the same mode, and a `permissionMode` in the agent file is ignored.
  * Claude Code watches `.claude/agents/` and `~/.claude/agents/`; changed agent files are used for the next delegation without a restart. Exceptions: the first agent file in a newly created `agents` directory, agent files in directories added with `--add-dir`, and sessions started with `--disable-slash-commands`.
  * If agents with the same name exist in several locations, the project definition (`.claude/agents/`) takes precedence over the user definition (`~/.claude/agents/`). User-level agents with other names (e.g. `critical-reviewer`) are also available in this project; they are not part of this workflow.
* Verified in the actual environment (2026-10-09, Claude Code VS Code extension 2.1.295 installed; 2.1.292 is also installed): the three agents are loaded; `security-reviewer` has only the listed tools, `developer` and `tester` have all tools. A review run on the same day showed that the reviewer still used the previous version of its agent file after it had been changed. According to the documentation, changes are picked up without a restart; the cause of the observation is unknown. It was observed again on 2026-10-09 after a restart: agent files changed later in the same session were not picked up. After every change to an agent file, the session is therefore restarted and the loaded definitions are checked before the agents are relied on.
* Limits: the agents do not guarantee an independent security review. They use the same model, receive the same project instructions, and their task is formulated by the main session. Subagents started by agents are subject to the same limits. Critical changes additionally require a human review.
* Sources: https://code.claude.com/docs/en/sub-agents

## D-020: Autonomous Operation of AI Tools

* Date: 2026-10-09
* Status: Decided
* Context: The project owner does not review individual commands before approving them and wants work to proceed automatically.
* Decision:
  * Claude Code may run in auto mode; commands and file changes are not approved individually by the project owner.
  * The binding rules remain unchanged: ask before irreversible or security-critical decisions (`CLAUDE.md`, `AGENTS.md`), never publish project content without the project owner's consent, never claim unexecuted checks.
  * `.claude/settings.json` contains deny rules that keep Claude's file tools away from secret files (`.env` and similar; `.env.example` remains readable).
  * No production or stage secrets are kept on development machines.
* Verified facts (Claude Code documentation): Read and Edit deny rules apply to Claude's file tools and to file commands Claude Code recognises in Bash (e.g. `cat`, `head`), but not "to arbitrary subprocesses that read or write files indirectly, like a Python or Node script that opens files itself".
* Verified in the actual environment (2026-10-09): with the deny rules in place, Claude's Read tool refused a test file `.env`, while `.env.example` remained readable. The test file was deleted afterwards.
* Not verified: whether the deny rules also cover PowerShell commands (e.g. `Get-Content`); PowerShell is the primary shell on the development machine. The rules are anchored at the project directory and do not cover other directories.
* Security implications: there is no human approval step for individual commands. A wrong or manipulated command (e.g. through content read from the web) can run without confirmation. Mitigations: deny rules, no production secrets locally, Git history as a way back (not yet in place: the repository has not been initialised yet, README "Current Status"), human review of critical changes. Deny rules are not a complete protection.
* Alternatives: manual approval of every command (rejected by the project owner because the commands are not read before approval); sandboxing at operating system level (not assessed yet).
* Sources: https://code.claude.com/docs/en/permissions, https://code.claude.com/docs/en/sub-agents

## D-021: User Model and Login Identifiers

* Date: 2026-10-09 (updated)
* Status: Decided
* Decision:
  * The project uses a custom user model from the first migration.
  * Every account receives a customer number exactly once, when the registration is completed (password form submitted successfully via the verification link), not at registration; email address changes and reactivation keep it, and the initial system administrator receives it from the setup command: six digits, random (cryptographically secure generator), unique, never reused, stored as text. Unverified accounts have no customer number. It is displayed in two blocks of three digits (e.g. `482 915`). Numbers of deleted accounts are kept in a list of retired numbers without personal reference. The customer number is read-only for everyone.
  * Login is possible only with the email address. The customer number identifies the account (e.g. later on invoices) but is not a login identifier.
* Reason for the update (project owner, 2026-10-09, after a consistency check):
  * Assigning the number at registration would let registrations that are never verified use up the at most one million numbers permanently, because numbers are never reused.
  * With login by email address and customer number, failed attempts would have to be counted per account across both identifiers. An attacker could then find out, from the earlier lockout, whether an email address and a customer number belong to the same account, which contradicts the rule that responses do not reveal the existence of accounts. Login with the email address only removes this side channel.
* Why retired numbers stay blocked even after a complete deletion: the number is shown to the person in the profile and may appear in their correspondence; a reused number could lead to mix-ups with a former holder (e.g. payments or enquiries quoting an old number). The list contains only numbers, without any link to a person, and does not limit capacity in practice once only verified accounts receive numbers. Confirmed by the project owner on 2026-10-09.
* Verified facts:
  * Django recommends setting up a custom user model when starting a new project; changing `AUTH_USER_MODEL` after database tables have been created "can be complex" and requires manual schema changes.
  * OWASP Authentication Cheat Sheet: email addresses may be used as usernames if verified; user IDs should ideally be randomly generated rather than sequential.
* Security implications: the customer number is an identifier, not a secret; it will later appear on invoices. Because it cannot be used to log in, knowing it gives no possibility to cause lockouts or to try passwords. With six digits there are at most one million numbers; this is sufficient for one LEG and is monitored if an instance grows.
* Sources: https://docs.djangoproject.com/en/stable/topics/auth/customizing/, https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html (checked on 2026-10-09)

## D-022: Privacy Notice and Imprint

* Date: 2026-10-09 (updated 2026-10-11)
* Status: Decided (imprint open)
* Decision:
  * The instance configuration contains a link to the instance's privacy notice. It is shown at registration and on all pages accessible without login, in particular the login page (updated on 2026-10-11, D-025). The content of the privacy notice is provided by the operator.
  * The imprint (legal notice) is deliberately left open. Since the application has no public home page (D-025), it is expected to be provided by the instance's website (e.g. the website of the LEG); whether the login page links to it is decided together with the imprint.
* Verified facts: DSG Art. 19 para. 1 requires the controller to inform the data subject appropriately about the collection of personal data. Art. 6 para. 3: personal data may only be collected for a specific purpose recognisable to the data subject.
* Not yet verified: whether a later version of the DSG than the one of 1 September 2023 changes these provisions; to be re-checked on fedlex before going into production. The application does not assess whether the operator's privacy notice is legally sufficient.
* Sources: DSG (SR 235.1), Art. 6 and Art. 19 (https://www.fedlex.admin.ch/eli/cc/2022/491/de, version of 1 September 2023, checked on 2026-10-09)

## D-023: Password Hashing and Keyed Hashes

* Date: 2026-10-09
* Status: Decided
* Context: The project owner wants a high but not excessive work factor, because it is not yet known whether the hosting is billed by CPU usage or at a flat rate (D-008).
* Decision:
  * Passwords are hashed with Django's default password hasher (PBKDF2-HMAC-SHA256) and the default work factor of the installed Django version. The work factor is never lowered below this default and never below the OWASP recommendation of 600,000 iterations. It is not raised above the default for now.
  * Once the hosting is decided, the time for one password hash is measured on the actual server; per OWASP it should stay below one second. Only if the measurement and the cost model allow it is a higher work factor considered (a new decision).
  * The counters of the per-address rate limits (`docs/SECURITY.md`, section 3) store, where the package allows it, an HMAC-SHA256 of the normalised address instead of the address. The key is derived from `SECRET_KEY` with HKDF and its own purpose-specific label, as in D-006, using the `cryptography` library. A keyed hash needs no work factor: its protection comes entirely from the secret key, and its CPU cost is negligible. Before implementation it is checked whether Django provides a documented public function for this (e.g. `django.utils.crypto.salted_hmac`), which is then preferred over own code.
  * Consequences: rotating `SECRET_KEY` resets the running per-address counters once (acceptable; attackers cannot trigger a rotation, no fallback keys are needed for counters). If `SECRET_KEY` leaks, the hashes can be reversed cheaply with a list of known email addresses; the short retention limits the impact. When the rate-limit package is chosen, it is recorded whether plain addresses end up in its storage.
* Verified facts:
  * Django "uses PBKDF2 by default", has "chosen a reasonable default (and will increase it with each release of Django)", and re-hashes passwords when the number of iterations changes.
  * OWASP Password Storage Cheat Sheet: "PBKDF2-HMAC-SHA256: 600,000 iterations (recommended)"; "When you choose a work factor, strike a balance between security and performance"; "If the work factor is too high, the performance of the application may be degraded, which could be used by an attacker to carry out a denial of service attack"; "As a general rule, calculating a hash should take less than one second."
  * OWASP recommends Argon2id first ("minimum configuration of 19 MiB of memory, an iteration count of 2, and 1 degree of parallelism"); Django supports it but "it requires a third-party library" (`argon2-cffi`).
* Not yet verified: the default iteration count of the Django version that will be installed; checked at implementation against the Django release notes and source.
* Rationale: Django's default is maintained by the vendor and rises with each release, which matches "high but not excessive" without project-specific tuning. Abuse protection (D-017) limits how many hashes an attacker can trigger.
* Security implications: a higher work factor makes offline attacks on leaked hashes more expensive but increases CPU load per login and the risk of denial of service.
* Alternatives: Argon2id (OWASP's first recommendation; not chosen for now because it adds a dependency and memory use per login whose cost on the unknown hosting cannot be assessed yet; can be reconsidered with the hosting decision); a custom higher PBKDF2 work factor (not chosen until measured on the target server).
* Sources: https://docs.djangoproject.com/en/5.2/topics/auth/passwords/, https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html (checked on 2026-10-09)

## D-024: Design Language

* Date: 2026-10-11
* Status: Decided
* Context: The project owner's experience is that pages developed in later phases tend to look different from earlier ones. A binding design language is therefore defined before implementation starts. Mockups in three directions (A: warm and community-oriented; B: modern and technical; C: friendly and bright) were reviewed with the project owner; the third, combined version was confirmed.
* Decision:
  * The binding design language is `docs/DESIGN.md`.
  * One shared foundation (colours, text typeface, icons, controls, spacing, navigation) with two registers: "Clear" for entering, confirming and managing data, based on direction B, and "Warm" for informing and exploring, based on directions A and C.
  * Members on mobile phones: menu at the top left for all further pages, tab bar at the bottom for the most important pages.
  * The platform is neutral; logo, accent colour and an optional highlight colour are instance configuration. The values for the LEG Ittigen are derived from its logo.
  * Target: WCAG 2.2 level AA. Light and dark mode following the device setting, switchable. Print stylesheet.
* Verified facts:
  * All permitted colour pairs listed in `docs/DESIGN.md`, section 3.6, were calculated with the WCAG 2.2 contrast ratio formula and meet 4.5:1 for text and 3:1 for graphical objects and form field borders (success criteria 1.4.3 and 1.4.11, both level AA); section 3.6 also lists one non-permitted pair for reference.
  * W3C SVG Integration (First Public Working Draft, 2014): an SVG document referenced through `<img>` is processed without running scripts; an SVG file opened directly as a document can run scripts.
  * Tabler's documentation describes customisation via CSS variables (`--tblr-primary`, `--tblr-primary-fg`, `--tblr-font-sans-serif`, `--tblr-body-font-size`, `--tblr-gray-*`) and attributes (`data-bs-theme`, `data-bs-theme-base`, `data-bs-navbar-theme`), and states that typography variables can be overridden on any container.
  * Source Sans 3, Bricolage Grotesque and IBM Plex Mono are licensed under the SIL Open Font License 1.1, which permits bundling and redistribution with software provided the copyright notice and licence are included; Reserved Font Names apply to "Source" and "Plex". Tabler and Tabler Icons are MIT-licensed.
* Rationale: Identical controls and fixed rules for when each register applies keep later pages consistent. The Clear register follows the project owner's requirement that data entry and management be unambiguous and without decorative elements; the Warm register allows a friendly appearance where people are informed.
* Security implications: fonts and icons are served by the instance itself (`docs/SECURITY.md`, section 6). Logo files are static files provided by the operator and are embedded as images, never inlined, so that SVG content cannot execute in the page. Because an SVG file opened directly can run scripts, SVG logo files are checked for scripts and external references before deployment and/or served with a restrictive Content Security Policy (`docs/DESIGN.md`, section 10). Instance branding values are validated, and the colour mode is set without Tabler's `tabler-theme.js`, so that a crafted link cannot override the branding (`docs/DESIGN.md`, sections 3.2 and 7).
* Alternatives: one of the three directions alone (not chosen: the project owner preferred different directions for different kinds of pages); a freely configurable theme per instance (not chosen: contrast and consistency could no longer be guaranteed).
* Sources: statements of the project owner (2026-10-11); https://www.w3.org/TR/WCAG22/; https://github.com/tabler/tabler (`docs/content/ui/getting-started/customize.mdx`, `color-modes.mdx`, `docs/content/ui/plugins/theme-base-colors.mdx`, branch `main`); https://github.com/google/fonts (`ofl/sourcesans3/OFL.txt`, `ofl/bricolagegrotesque/OFL.txt`, `ofl/ibmplexmono/OFL.txt`); https://github.com/tabler/tabler-icons (all checked on 2026-10-11); W3C SVG Integration, https://www.w3.org/TR/svg-integration/ (First Public Working Draft 2014; as cited in the security review of 2026-10-11); Tabler `docs/content/ui/getting-started/color-modes.mdx` and `core/js/tabler-theme.ts` (branch `main`, as cited in the security review of 2026-10-11; behaviour of release 1.6.1 not verified)
* Consequences: every new page states its register; changes to the design language are made in `docs/DESIGN.md` and recorded here. The Tabler mechanisms are re-checked against the version chosen under D-015.

## D-025: Portal Entry Point and Host Name

* Date: 2026-10-11 (security implications refined on the same day after a security review)
* Status: Decided
* Context: The project owner wants a single public website per LEG (for Ittigen: `leg-ittigen.ch`) with all information about the LEG. People log in to the application from there. The project owner originally considered the path `leg-ittigen.ch/login`.
* Decision:
  * The application has no public home page. Its entry point is the login page, which offers login, password reset and registration, and links to the privacy notice and to the instance's website.
  * Information about the LEG and public figures (e.g. number of members, production and use within the LEG) are not part of the application. They are provided by the instance's website. Publishing figures from the application on the website would require a public interface with its own security design; this is not part of phase 1.
  * The application runs on its own host name (e.g. a subdomain such as `portal.leg-ittigen.ch`), never under a path of the instance's website. The host name is part of the instance configuration. The operator chooses between a subdomain of the website's registrable domain and a host name under a separate registrable domain; the installation guide states the residual risk of a subdomain (see "Security implications").
  * The minimum number of participants from which aggregated figures may be published is deliberately left open; each operator decides it for its instance when such figures are introduced.
  * Onboarding per building or location after registration is intended for phase 2.
* Verified facts:
  * RFC 6454, section 3.2: two URIs are part of the same origin "if they have the same scheme, host, and port"; the path is not part of the origin. A website and the application under the same host therefore share one origin.
  * RFC 6265, section 8.5: "Cookies do not always provide isolation by path." Section 8.6: servers "SHOULD NOT both run mutually distrusting services on different paths of the same host and use cookies to store security-sensitive information."
  * Django documentation, CSRF, "Limitations": "Subdomains within a site will be able to set cookies on the client for the whole domain." A subdomain of the website's registrable domain is a different origin but still the same site as the website.
* Rationale: A vulnerability in the website (e.g. its content management system or a plugin) must not be able to act within authenticated sessions of the application. A separate host name provides origin isolation: the same-origin policy separates the DOM, scripts and `localStorage` of the website and the application. For users nothing changes, as the website links to the login page.
* Security implications:
  * Origin isolation is not site isolation. If the application runs on a subdomain of the website's registrable domain (e.g. `portal.leg-ittigen.ch` below `leg-ittigen.ch`), a compromised website can still set cookies for the parent domain that the browser also sends to the application (cookie tossing). Residual risks are session fixation and injection of a CSRF cookie. The `SameSite` cookie attribute gives no protection between subdomains of the same site.
  * Countermeasures (`docs/SECURITY.md`, sections 3 and 6): `SESSION_COOKIE_DOMAIN` and `CSRF_COOKIE_DOMAIN` are not set, so that the cookies are host-only (Django's default; to be verified against the installed version); `CSRF_TRUSTED_ORIGINS` never contains the instance's website or a wildcard for the parent domain; in stage and production the session and CSRF cookie names use the `__Host-` prefix, which supporting browsers accept only for cookies that are `Secure`, have `Path=/` and no `Domain` attribute, so that a cookie set by the website for the parent domain cannot take its place. The `__Host-` prefix is defined in draft-ietf-httpbis-rfc6265bis, an Internet-Draft, not in RFC 6265; browser support and compatibility with Django's cookie handling are verified at implementation.
  * A host name under a separate registrable domain is a different site and removes the cookie risks above. The operator chooses; the installation guide states the residual risk of a subdomain.
  * `ALLOWED_HOSTS` lists only the host names of the application, not the instance's website. This clarifies "the instance's own domains" in D-013.
* Alternatives: application under a path of the website (rejected for the reasons above); public home page in the application (rejected by the project owner: one public website per LEG); a mandatory separate registrable domain (not made mandatory: the operator chooses, and the residual risk of a subdomain is documented).
* Sources: statements of the project owner (2026-10-11); https://www.rfc-editor.org/rfc/rfc6454.html, section 3.2; https://www.rfc-editor.org/rfc/rfc6265.html, sections 8.5 and 8.6 (checked on 2026-10-11); https://docs.djangoproject.com/en/5.2/ref/csrf/, "Limitations"; draft-ietf-httpbis-rfc6265bis (https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/), cookie name prefixes (both as cited in the security review of 2026-10-11, not yet re-checked in this document update; re-checked against the installed Django version and the current draft revision at implementation)
* Consequences: `docs/PROJECT.md`, `docs/ACCEPTANCE.md` and `README.md` replace the public home page with the login page as entry point. The privacy notice is linked on all pages accessible without login (D-022).

## Template for New Decisions

### D-XXX: Title

* Date:
* Status: Proposed / Decided / Rejected / Superseded
* Context:
* Decision:
* Verified facts:
* Rationale:
* Security implications:
* Alternatives:
* Sources (official sources or verification steps):
* Consequences:
