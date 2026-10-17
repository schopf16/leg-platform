# Architecture – LEG-Platform

## Goal

A simple, secure and maintainable architecture with the lowest possible operating effort and good portability between hosting providers; operable by municipalities without in-depth IT knowledge.

## Intended Stack

* Python, Django, MariaDB (Django's MySQL backend, D-002, D-030)
* Tabler for the user interface; the Django admin for system administration
* Microsoft Graph for outgoing email
* Security packages: `django-axes` (login abuse protection), `django-otp` (TOTP and recovery codes), `cryptography` (Fernet, HKDF)

Only minimum versions are specified (D-015): the oldest versions that are technically possible and still receive vendor security updates, from official sources and proven by tests; no assumptions about versions. The minimum is Django 6.0, the oldest supported line with built-in Content Security Policy support (`SECURE_CSP`); its security support ends in April 2027, when the minimum is raised (D-015, D-026, D-028).

### Candidate Packages for Security Functions

Established packages are preferred over custom developments; choice and minimum versions: D-015.

## Architecture Principles

* Django owns authentication, sessions, permissions, forms and server-side business logic; Tabler is presentation only. Domain permissions are implemented explicitly on the server.
* Reusable functionality is implemented centrally; external services sit behind clear interfaces. Configuration, secrets and instance-specific values are not in the source code. Deployment and migrations are traceable and repeatable.

## Deployment Model: One Instance per Municipality

Not multi-tenant (D-013). Each municipality runs its own instance with its own database, configuration, secrets, users and system administrators; instances share no data; one code base serves all. Instance-specific values (name, domain, contact details) are instance configuration, never hard-coded in code, templates or migrations. A new instance is set up using only the documented installation steps and is operated, updated and backed up independently.

## Language and Internationalization

Rules: `AGENTS.md`, section 2. Default language Swiss German: `LANGUAGE_CODE = "de-ch"`; project translations in `locale/de_CH/LC_MESSAGES/django.po` through `LOCALE_PATHS` (highest precedence). Django's `de_CH` locale holds only formats; texts fall back from `de-ch` to `de`, whose translations contain "ß", so the project translation files override every such text that can appear in the user interface, including third-party texts; an automated check ensures that no displayed text contains "ß". Only Swiss German exists, without language selection; further languages would come through translation files, `LANGUAGES` and, if needed, `LocaleMiddleware`. Stored values such as statuses are English keys shown through translated labels; user data (names, addresses, role names) is not translated.

## Users and Roles

Terminology: `docs/PROJECT.md`. Fixed access levels: **system administrator** (`is_superuser` with `is_staff`, all permissions), **staff user** (`is_staff` without `is_superuser`; admin login with assigned permissions only; uses the management area, D-086), **authenticated user** (no additional permissions; cards, start page and task tiles act on `request.user` only).

Only system administrators change `is_staff`, `is_superuser`, group memberships, direct permissions and roles; the admin enforces this centrally, and accounts of staff users and system administrators (including one's own) and other accounts' passwords, email addresses and second factors cannot be changed there (rules: `docs/SECURITY.md`, section 4; negative tests: section 11). Customer number and retention flag are read-only for everyone; the normalised email column is not editable and not shown (D-035; the label of the admin login field is "email address").

* Roles are Django groups, created, named and linked to permissions at runtime in the admin. No code, migration or fixture creates, names or checks specific roles; checks use permissions, never group names. Permissions for application functions are defined in code with English codenames that describe what can be done, not who does it.
* Whether later roles work in the Django admin or in application views is decided when those roles are defined.

Privileged access is never granted through public registration; any promotion requires an explicit action by an authorized superior, and nobody promotes their own account.

## User Model and Login Identifiers

A custom user model from the first migration (D-021; changing `AUTH_USER_MODEL` after tables exist "can be complex", Django documentation).

* **Customer number:** six digits from `secrets`, generated exactly once when the registration is completed, stored as text (leading zeros), unique, never reused; unverified accounts have none (nullable). "Never reused" is enforced by one table of all issued numbers (`IssuedCustomerNumber`, no personal reference; D-034); on a collision a new number is drawn. Email change and reactivation never change it. Not a login identifier.
* **Login:** an authentication backend accepts the email address with the password; addresses are looked up only through the normalised unique column. The abuse protection counts failed attempts per normalised address and treats nonexistent addresses like existing ones. The backend is based on `ModelBackend` or reproduces its protection against timing differences.
* **Normalisation of email addresses** is defined in one place (`leg_platform/accounts/normalization.py`: NFKC, trimming, lower case, IDNA ASCII domain with UTS #46, ASCII-only local part; D-034, D-035) and used for storage and for every lookup and per-address counter. Quoted local parts, domain literals, IP addresses, `localhost` and invisible or control characters are rejected. Mail-provider delivery equivalence for compatibility-mapped variants is unverified.
* **Email uniqueness among accounts that are not deactivated:** Django ignores conditional unique constraints on MariaDB (system check `models.W036`). Instead a nullable column holds the normalised address while the account is not deactivated and `NULL` otherwise, under a normal unique index (SQL allows several `NULL` values in a `UNIQUE` index). It is maintained in one central place of the model logic, covered by tests including concurrency, and is the `USERNAME_FIELD` (`normalized_email`, D-034).
* **Profile fields:** the fields of D-032 with the eCH-0010 structure unchanged; unused fields are not shown; one postal code field stored as the Swiss (four digits) or foreign postal code (D-036); country with `django-countries` (default `CH`); optional phone number with `django-phonenumber-field` in E.164 (D-034). The string representation is "Account" with the customer number or "Unconfirmed account", never an address or name; admin log entries of accounts store only a neutral text with the internal number (D-018, D-036).
* **Collations (D-031):** the database default `utf8mb4_uca1400_ai_ci` is case- and accent-insensitive. Identifier columns compared exactly (normalised email, token and keyed-hash values, customer number) use `utf8mb4_bin` through `db_collation`.

## Account Lifecycle

States and rules: `docs/PROJECT.md`, "Account States" and "Account Lifecycle" (D-016). Implementation constraints:

* A temporary lockout is managed by the abuse protection package, not as a status. Unverified, blocked and deactivated accounts are also `is_active = False`, and unverified accounts have no usable password, so Django's password reset sends them no email.
* Every path that changes status, promotion, email address or normalised column, or the retention flag, or deletes accounts, goes through the central account logic, including admin bulk actions, `list_editable`, `QuerySet.update()` and management commands (`docs/SECURITY.md`, section 4). A custom manager and queryset (also the base manager) reject bulk changes to these fields, bulk deletion and bulk creation (D-034); "Delete selected objects" is disabled for the user model or replaced via `delete_queryset()`.
* All other sessions of an account end on credential, address, second-factor, status and rights changes (`docs/SECURITY.md`, section 3).
* The retention flag can only change from unset to set; the application rejects any attempt to unset it, also in the admin. Direct database access can bypass this, which is why database privileges are restricted (`docs/SECURITY.md`, section 9).

## Authentication

* Password login with the email address (D-021). Two-factor authentication (TOTP) is mandatory for staff users and system administrators and optional for users; recovery codes cover a lost device (D-004).
* Login and recovery are protected against automated abuse with an established package (D-017); registration, reset and email change requests are rate-limited per IP and per normalised target address. Behind reverse proxies the number of trusted proxies is configured per instance (`AXES_IPWARE_PROXY_COUNT`; clients can forge IP headers). Password, session and rate-limit rules: `docs/SECURITY.md`, section 3.
* **Preparation for AGOV** (`docs/PROJECT.md`, "AGOV and Passkeys"): whether the second factor is completed is checked in one central place (middleware and decorator or mixin) independent of the method; protected functions, the mandatory second factor and re-authentication rely only on it; TOTP enrolment, verification and recovery codes are separate (`django-otp`: `OTPMiddleware` adds `user.is_verified()`). No AGOV-specific code is written in advance. An external identity (AGOV, later passkeys) maps to an internal account only through a stored, unambiguous identifier, never solely through a matching email address.

## Email

Sending is encapsulated behind a central application interface (`leg_platform.mail.GraphEmailBackend`, D-006; it fails closed while no configuration exists, D-028).

The Graph configuration lives in the database, maintained in a dedicated admin section (D-006): tenant ID, client ID, sender address, certificate authentication (signed client assertion, no client secret), public certificate with expiry warning, private key, test send. The private key is encrypted with Fernet under a key derived from `SECRET_KEY` with HKDF and a fixed purpose-specific label and is never displayed again. Keys from `SECRET_KEY_FALLBACKS` are tried for decryption and the key is then re-encrypted; if none matches, the admin asks for a new upload.

`EMAIL_MODE` controls delivery; which values are allowed depends on `APP_ENV`. Stage behaves exactly like production (D-070): `APP_ENV=stage` is only a label and the rows `stage` and `production` are identical.

| Value | `local` | `stage` | `production` | Behaviour |
| ----- | ------- | ------- | ------------ | --------- |
| `console` | allowed (default) | refused | refused | Written to console or test output only (outside local development, security links would end up in server logs). |
| `allowlist` | allowed | refused | refused | Sent via Graph only to `EMAIL_ALLOWED_RECIPIENTS`; others are discarded and logged without content. Only locally with a dedicated development app registration and sender mailbox (D-020, `docs/SECURITY.md`, sections 7 and 8); the recipients live only in the developer's user environment variable, never in the repository. Without recipients, or with an entry that is not one complete address, the application refuses to start; a non-empty value is refused with stage and production (D-070). |
| `graph` | refused | required | required | Sent via Graph without restriction (on stage: real emails to real addresses, D-070). |

"Refused" means the application refuses to start. Outside `APP_ENV=local`, `EMAIL_MODE` has no default and must be set; a missing or unknown `APP_ENV` prevents the start. Security settings are secure by default and relaxed only for `APP_ENV=local` (`docs/SECURITY.md`, sections 3, 8 and 11); `leg_platform/environment.py` has no rule distinguishing stage from production. Automated tests never send real email and use only addresses under reserved domains (RFC 2606), which a test checks for the whole repository.

Links in emails are built from the canonical base URL of the instance configuration, never from the request. `ALLOWED_HOSTS` lists only the application's host names (D-025).

Security notifications tell the person to contact the operator if they did not request the change; automatic emails do not invite a reply and the sender (default `noreply@<host>`) is not presented as a contact address (D-038). Editable texts (D-039): `EmailText` holds an optional subject and body per email (empty = default), placeholders defined per email, validated when saved and rendered by plain `{name}` substitution without the template engine. The greeting of the 14 notifications is `{greeting}` (D-068; the cleaned name is captured once in `MailJob.greeting_name`; `docs/SECURITY.md`, section 3).

Background sending (D-038): requests for registration, password reset and address change count against the limits and store one `MailJob`; the worker `process_mail_queue` looks up the address, issues the token, sends the email outside any database transaction and deletes the job, so request path and response are identical for every address. Rate-limit counters are database rows locked per key and shared by all processes; client and instance limits are read first, so a refused request stores nothing (D-058).

## Security Logging

Security-relevant events are logged centrally (D-018): no passwords, tokens or complete security links, only necessary personal data; visible only to system administrators; entries of deleted accounts are kept without personal reference. The log is append-only within the application: the admin offers no permission to add, change or delete entries, not even for system administrators, and the `django-axes` failed-attempt and lockout records are restricted likewise. Retention: `docs/SECURITY.md`, section 10; expired entries are deleted by the scheduled cleanup. Request logs must not carry tokens: a logging filter removes them from paths for every token URL (`leg_platform/log_filters.py`, D-037, D-067) and must be extended, with a test, for every further URL with a token (`docs/SECURITY.md`, sections 6 and 10).

## User Interface and Devices

* Responsive; all user functions fully usable on a mobile phone (`docs/PROJECT.md`, "Supported Devices"). Tabler (Bootstrap 5, mobile first) needs the responsive viewport meta tag and states minimum browser versions (Chrome/Edge 123, Firefox 128, Safari 17.5, Opera 109); older devices that cannot be updated are not supported. The supported browsers are documented for operators and users.
* Static assets (Tabler, fonts, icons) are served by the instance itself in fixed versions. No inline CSS, theme URL parameters or third-party assets.
* The design language is `docs/DESIGN.md` (D-024), implemented through Tabler's CSS variables. Instance branding (name, website, colours, two logos in the database) wins per field over the validated environment variables, which win over platform defaults, resolved by `leg_platform.accounts.portal_settings.portal_values`; a same-origin generated stylesheet carries the colours (D-051, D-056, D-073). Uploaded logos are served by a dedicated public view (`docs/SECURITY.md`, section 6). Colour mode and language are not handled by the server: the page is rendered light and the choice is kept in the tab's `sessionStorage` by external scripts.
* No public home page; the entry point is the login page. The application runs on its own host name, never under a path of another website (D-025); a subdomain of the website's registrable domain is still the same site, so the cookie rules of `docs/SECURITY.md`, section 3, apply.

## Management Area and the Community Domain

Two Django apps (D-077 to D-087):

* **`leg_platform.community`** is the domain without pages: transformer circuits, locations (unique on the normalised address), metering points (derived status from dates and the day, no scheduler), LEGs, person and LEG assignments (the previous assignment ends the day before the new one), append-only `HistoryEntry`, `PersonRecord` (staff-only side record; IBAN **encrypted** via `community.iban`), `ShareChange`, `GridOperator` and list preferences. The rules are plain modules tested without a browser. Identifier columns use the exact collation (D-031). Only `GridOperator` is registered in the Django administration (system administrators only); the community models have no Django permissions (D-086).
* **`leg_platform.management`**: views, forms and templates under `/management/`. Every view uses `access.ManagementAccessMixin` or `@management_view` (rules: `docs/SECURITY.md`, section 4); a test walks the URL configuration and fails for a route without the access check. Lists share one list framework (state parsed from the address on the server). Printing builds PDFs with ReportLab in memory from the same list functions as the pages; one module knows ReportLab markup and escapes every text.

Every active staff account may use every page of the area (D-086, approved deviation from D-012); the Django administration keeps its permission model. The address directory is advisory (D-066): a suggestion is stored on the location so lists can filter without asking the directory per row. The grid operator's name is never written into code: texts use `%(operator)s` in the message id and the name from `GridOperator` (neutral fallback "Netzbetreiber"); templates use `{% operator_name as operator %}`.

## Data Storage

MariaDB is the database (D-002, D-030); MySQL is not supported. Django migrations are the source for schema changes. Documents and receipts must not live only in an ephemeral container file system; storage is decided once such functions are planned.

**Address directory (D-033, D-049, D-052, D-075).** The public directory of building addresses is an own SQLite file (alias `addresses`; `ADDRESS_DIRECTORY_FILE`, default `/var/lib/leg-platform/address-directory.sqlite3`), **not part of any backup**; the import queue row (`ImportRequest`) stays in MariaDB. A router keeps the two apart; no foreign key to profile data. Every connection is read-only (`PRAGMA query_only`) unless the process called `storage.mark_writer()`, which only the worker, the commands `import_address_directory` and `seed_local_address_directory`, `setup_instance` and `migrate` do (never `wsgi.py` or `asgi.py`). The web process only reads (a prefix is a range over folded key columns, never `LIKE`) and queues an import request; download and import run only in the worker `process_address_imports`. A new directory is inserted as an invisible generation in chunked transactions and published by switching `Directory.generation` in one short transaction; only that and the previous generation are kept. A missing or empty file means "no suggestions". The design assumes one host (D-075).

## Environments

* Local; stage (behaves exactly like production: same security configuration, delivery mode `graph` and validation; differs only by its label `APP_ENV=stage`, own database, credentials, secrets and logs, D-070); production. Production data is not copied into development environments or stage.

## Portability

Source code in Git; reproducible installation and deployment; configuration outside the source code; documented migrations, backup and restore; no unnecessary dependency on proprietary mechanisms of a hosting provider.

## Deliberately Open

Not yet decided (decided only when needed for the next development step):

* hosting details beyond provider and plan of the Ittigen instance (Infomaniak "VPS Lite 1", D-008) and of other instances; container-based or direct server deployment; final document storage; architecture of the later billing and financial modules
* whether AGOV may be used as the second factor (request by the project owner pending), integration details, and whether password login can later be disabled for system administrators
* concrete roles and their permissions, and whether they use the Django admin or application views (pure data maintenance is a candidate for the admin, frequent guided work for application views in the Clear register, `docs/DESIGN.md`)
* the review of the uploaded logos (D-073; the security review and the browser checks are open)
* whether Django's `redirect_authenticated_user` of the login view is used (Django's documentation warns it can allow social media fingerprinting)
* minimum number of participants for publishing aggregated figures, and the public interface needed for it (D-025); imprint and content of the privacy notice (D-022)
* an optional guard in the settings that refuses to start outside `APP_ENV=local` while the placeholder email backend is active (security review 2026-10-11, I-6; not implemented, the deployment gates in the README apply instead)
* which functions are restricted on mobile phones in later phases; timing and scope of later multilingual support
