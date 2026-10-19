# Architecture – LEG-Platform

## Goal

Simple, secure, maintainable architecture with the lowest possible operating effort and good portability between hosting providers. Municipalities without in-depth IT knowledge can operate it.

## Intended Stack

* Python, Django, MariaDB (Django's MySQL backend; D-002, D-030).
* Tabler for the user interface; the Django admin for system administration.
* Microsoft Graph for outgoing email.
* Security packages: `django-axes` (login abuse protection), `django-otp` (TOTP, recovery codes), `cryptography` (Fernet, HKDF).
* Only minimum versions are specified (D-015): the oldest technically possible versions that still receive vendor security updates, from official sources, proven by tests. Newer packages are preferred over custom developments; choice: D-015.
* Minimum Django 6.0: the oldest supported line with built-in CSP (`SECURE_CSP`). Its security support ends in April 2027; then the minimum is raised (D-015, D-026, D-028).

## Architecture Principles

* Django owns authentication, sessions, permissions, forms and server-side business logic. Tabler is presentation only. Domain permissions are implemented explicitly on the server.
* Reusable functionality is central; external services sit behind clear interfaces.
* Configuration, secrets and instance-specific values are not in the source code. Deployment and migrations are traceable and repeatable.

## Deployment Model: One Instance per Municipality

* Not multi-tenant (D-013). Each municipality has its own instance: database, configuration, secrets, users, system administrators. Instances share no data; one code base serves all.
* Instance values (name, domain, contact details) are instance configuration, never in code, templates or migrations.
* A new instance is set up with the documented installation steps only. Operation, update and backup are independent per instance.

## Language and Internationalization

* Rules: `AGENTS.md`, section 2. Default language Swiss German: `LANGUAGE_CODE = "de-ch"`. Project translations: `locale/de_CH/LC_MESSAGES/django.po` through `LOCALE_PATHS` (highest precedence).
* Django's `de_CH` locale holds only formats. Texts fall back from `de-ch` to `de`, which contains "ß". The project files override every such text that can appear in the UI, including third-party texts. An automated check ensures no displayed text contains "ß".
* Languages (D-056): Swiss German (`de-ch`, the instance default), French (`fr`), Italian (`it`) and English (`en`, the source texts). The person chooses with the language menu; the language cookie decides, never the browser's `Accept-Language` (`leg_platform/language.py`). Catalogues: `locale/de_CH`, `locale/fr`, `locale/it` (the last two have the entries of `de_CH`, translated separately). A text without a translation is English, not German (`install_english_fallback`). Emails and background jobs use the instance default language.
* Stored values such as statuses are English keys shown through translated labels. User data (names, addresses, role names) is not translated.

## Users and Roles

* Terminology: `docs/PROJECT.md`. Fixed access levels:
  * **system administrator**: `is_superuser` with `is_staff`, all permissions;
  * **staff user**: `is_staff` without `is_superuser`; admin login with assigned permissions only; uses the management area (D-086);
  * **authenticated user**: no additional permissions; cards, start page and task tiles act on `request.user` only.
* Only system administrators change `is_staff`, `is_superuser`, group memberships, direct permissions and roles; staff cannot change accounts of staff or administrators or another account's password, email address or second factor (`docs/SECURITY.md`, section 4; `AGENTS.md`, section 6).
* Customer number and retention flag are read-only for everyone. The normalised email column is not editable and not shown (D-035; the admin login field is labelled "email address").
* Roles are Django groups, created and linked to permissions at runtime. No code, migration or fixture creates, names or checks roles; checks use permissions with English codenames that say what can be done, not who does it.
* Whether later roles work in the Django admin or in application views is decided when those roles are defined.
* Privileged access is never granted through public registration.

## User Model and Login Identifiers

A custom user model from the first migration (D-021; changing `AUTH_USER_MODEL` later "can be complex", Django documentation).

* **Customer number:** six digits from `secrets`, generated once when the registration is completed, stored as text (leading zeros), unique, never reused, nullable (unverified accounts have none).
  * "Never reused" is enforced by one table of all issued numbers (`IssuedCustomerNumber`, no personal reference; D-034). On a collision a new number is drawn.
  * Email change and reactivation never change it. It is no login identifier.
* **Login:** a backend accepts the email address with the password. Addresses are looked up only through the normalised unique column. Abuse protection counts failures per normalised address and treats nonexistent addresses like existing ones. The backend is based on `ModelBackend` or reproduces its protection against timing differences.
* **Normalisation of email addresses** is defined in one place (`leg_platform/accounts/normalization.py`: NFKC, trimming, lower case, IDNA ASCII domain with UTS #46, ASCII-only local part; D-034, D-035). Storage and every lookup and per-address counter use it.
  * Rejected: quoted local parts, domain literals, IP addresses, `localhost`, invisible or control characters.
  * Unverified: mail-provider delivery equivalence for compatibility-mapped variants.
* **Email uniqueness among accounts that are not deactivated.** Django ignores conditional unique constraints on MariaDB (system check `models.W036`).
  * A nullable column holds the normalised address while the account is not deactivated and `NULL` otherwise, under a normal unique index (SQL allows several `NULL` values in `UNIQUE`).
  * Maintained in one central place of the model logic, covered by tests including concurrency. It is the `USERNAME_FIELD` (`normalized_email`, D-034).
* **Profile fields:** the fields of D-032 with the eCH-0010 structure unchanged. Unused fields are not shown. One postal code field stored as Swiss (four digits) or foreign postal code (D-036). Country with `django-countries` (default `CH`). Optional phone number with `django-phonenumber-field` in E.164 (D-034).
* The string representation is "Account" with the customer number or "Unconfirmed account", never an address or name. Admin log entries of accounts store only a neutral text with the internal number (D-018, D-036).
* **Collations (D-031):** the database default `utf8mb4_uca1400_ai_ci` is case- and accent-insensitive. Identifier columns compared exactly (normalised email, token and keyed-hash values, customer number) use `utf8mb4_bin` through `db_collation`.

## Account Lifecycle

States and rules: `docs/PROJECT.md`, "Account States" and "Account Lifecycle" (D-016). Implementation constraints:

* A temporary lockout belongs to the abuse protection package, not to a status. Unverified, blocked and deactivated accounts are `is_active = False`. Unverified accounts have no usable password, so Django's password reset sends them no email.
* Every path that changes status, promotion, email address, normalised column or retention flag, or deletes accounts, goes through the central account logic (`docs/SECURITY.md`, section 4). A custom manager and queryset (also the base manager) reject bulk changes to these fields, bulk deletion and bulk creation (D-034). "Delete selected objects" is disabled for the user model or replaced via `delete_queryset()`.
* The retention flag changes only from unset to set, also in the admin. Direct database access can bypass this, so database privileges are restricted (`docs/SECURITY.md`, section 9).

## Authentication

* Password login with the email address (D-021). Two-factor (TOTP) is mandatory for staff users and system administrators, optional for users; recovery codes cover a lost device (D-004). Abuse protection with an established package (D-017). Behind reverse proxies the number of trusted proxies is configured per instance (`AXES_IPWARE_PROXY_COUNT`). Rules: `docs/SECURITY.md`, section 3.
* **Preparation for AGOV** (`docs/PROJECT.md`, "AGOV and Passkeys"):
  * Completion of the second factor is checked in one central place (middleware and decorator or mixin), independent of the method. Protected functions, the mandatory second factor and re-authentication rely only on it.
  * TOTP enrolment, verification and recovery codes are separate (`django-otp`: `OTPMiddleware` adds `user.is_verified()`).
  * No AGOV-specific code in advance. An external identity maps to an internal account only through a stored, unambiguous identifier, never solely through a matching email address.

## Email

* Sending is encapsulated behind one application interface: `leg_platform.mail.GraphEmailBackend` (D-006). It fails closed while no configuration exists (D-028).
* The Graph configuration lives in the database, maintained in a dedicated admin section (D-006): tenant ID, client ID, sender address, certificate authentication (signed client assertion, no client secret), public certificate with expiry warning, private key, test send. Key handling: `docs/SECURITY.md`, sections 7 and 8.
* `EMAIL_MODE` controls delivery; the allowed values depend on `APP_ENV`. Stage behaves exactly like production (D-070): `APP_ENV=stage` is only a label, the columns `stage` and `production` are identical.

| Value | `local` | `stage` | `production` | Behaviour |
| ----- | ------- | ------- | ------------ | --------- |
| `console` | allowed (default) | refused | refused | Console or test output only (elsewhere, security links would end up in server logs). |
| `allowlist` | allowed | refused | refused | Sent via Graph only to `EMAIL_ALLOWED_RECIPIENTS`; others are discarded and logged without content. Only locally, with a dedicated development app registration and sender mailbox (D-020, `docs/SECURITY.md`, sections 7 and 8). Recipients live only in the developer's user environment variable. Without recipients, or with an entry that is not one complete address, the application refuses to start. A non-empty value is refused in stage and production (D-070). |
| `graph` | refused | required | required | Sent via Graph without restriction (on stage: real emails to real addresses, D-070). |

* Outside `APP_ENV=local`, `EMAIL_MODE` has no default and must be set; "refused" means the application refuses to start. A missing or unknown `APP_ENV` prevents the start. `leg_platform/environment.py` has no rule distinguishing stage from production (`docs/SECURITY.md`, sections 3, 8, 11).
* Links in emails are built from the canonical base URL of the instance configuration, never from the request. `ALLOWED_HOSTS` lists only the application's host names (D-025). Automatic emails do not invite a reply (D-038; sender default `noreply@<host>`; further email rules: `docs/SECURITY.md`, sections 3 and 8).
* Editable texts (D-039): `EmailText` holds an optional subject and body per email (empty = default). Placeholders are defined per email, validated when saved and rendered by plain `{name}` substitution without the template engine.
* The greeting of the 14 notifications is `{greeting}` (D-068; the cleaned name is captured once in `MailJob.greeting_name`; `docs/SECURITY.md`, section 3).
* **Background sending (D-038).**
  * Requests for registration, password reset and address change count against the limits and store one `MailJob`.
  * The worker `process_mail_queue` looks up the address, issues the token, sends the email outside any database transaction and deletes the job. Request path and response are identical for every address.
  * Rate-limit counters are database rows locked per key, shared by all processes. Client and instance limits are read first, so a refused request stores nothing (D-058).

## Security Logging

* Security-relevant events are logged centrally, append-only and visible only to system administrators (D-018; rules and retention: `docs/SECURITY.md`, section 10). The scheduled cleanup deletes expired entries.
* Request logs must not carry tokens. A logging filter removes them from paths for every token URL (`leg_platform/log_filters.py`, D-037, D-067). Every further URL with a token extends it, with a test (`docs/SECURITY.md`, sections 6 and 10).

## User Interface and Devices

* Responsive; all user functions fully usable on a mobile phone (`docs/PROJECT.md`, "Supported Devices"). Tabler (Bootstrap 5, mobile first) needs the responsive viewport meta tag. Minimum browsers: Chrome/Edge 123, Firefox 128, Safari 17.5, Opera 109. Older devices that cannot be updated are not supported. The supported browsers are documented for operators and users.
* Static assets (Tabler, fonts, icons) are served by the instance in fixed versions. No inline CSS, theme URL parameters or third-party assets.
* Design language: `docs/DESIGN.md` (D-024), implemented through Tabler's CSS variables.
* Instance branding (name, website, colours, two logos and the favicon in the database) wins per field over the validated environment variables, which win over platform defaults. Resolved by `leg_platform.accounts.portal_settings.portal_values`. A same-origin generated stylesheet carries the colours (D-051, D-056, D-073). Dedicated public views serve the uploaded logos and the favicon (`docs/SECURITY.md`, section 6).
* The colour mode is not handled by the server: the page is rendered light and external scripts keep the choice in the tab's `sessionStorage`. The language is chosen on the server (language cookie, `leg_platform/language.py`, `docs/SECURITY.md` section 6).
* No public home page; the entry point is the login page.
* The application runs on its own host name, never under a path of another website (D-025). A subdomain of the website's registrable domain is still the same site, so the cookie rules of `docs/SECURITY.md`, section 3, apply.

## Management Area and the Community Domain

Two Django apps (D-077 to D-087):

* **`leg_platform.community`**: the domain without pages.
  * Models: transformer circuits, locations (unique on the normalised address), metering points (status derived from dates and the day, no scheduler), LEGs, person and LEG assignments (the previous assignment ends the day before the new one), append-only `HistoryEntry`, `PersonRecord` (staff-only side record; IBAN **encrypted** via `community.iban`), `ShareChange`, `GridOperator`, list preferences.
  * The rules are plain modules tested without a browser. Identifier columns use the exact collation (D-031).
  * Only `GridOperator` and the raw admin tables (D-091) are registered in the Django administration; the tables are read-only for staff with the `view` right and editable without checks for system administrators (`is_superuser`; owner order 2026-10-19). The community models have no Django permissions except `view` (D-086, D-091).
* **`leg_platform.management`**: views, forms and templates under `/management/`.
  * Every view uses `access.ManagementAccessMixin` or `@management_view` (`docs/SECURITY.md`, section 4). A test walks the URL configuration and fails for a route without the access check.
  * Lists share one list framework (state parsed from the address on the server).
  * Printing builds PDFs with ReportLab in memory from the same list functions as the pages. One module knows ReportLab markup and escapes every text.
* Every active staff account may use every page of the area (D-086, approved deviation from D-012). The Django administration keeps its permission model.
* The address directory is advisory (D-066). A suggestion is stored on the location so lists can filter without asking the directory per row.
* The grid operator's name is never in code: texts use `%(operator)s` in the message id and the name from `GridOperator` (neutral fallback "Netzbetreiber"). Templates use `{% operator_name as operator %}`.

## Data Storage

* MariaDB is the database (D-002, D-030); MySQL is not supported. Django migrations are the source for schema changes.
* Documents and receipts must not live only in an ephemeral container file system. Storage is decided once such functions are planned.
* **Address directory (D-033, D-049, D-052, D-075, D-090).**
  * The public directory of building addresses is an own SQLite file (alias `addresses`; `ADDRESS_DIRECTORY_FILE`, default `/var/lib/leg-platform/address-directory.sqlite3`), **not part of any backup**. The import job row (`ImportRequest`: claim, progress, last error) stays in MariaDB. A router keeps the two apart; no foreign key to profile data.
  * Connections are read-only (`PRAGMA query_only`) unless the process called `storage.mark_writer()` (optional worker, commands `import_address_directory` and `seed_local_address_directory`, `setup_instance`, `migrate`; never `wsgi.py` or `asgi.py`) or the import thread of the web process called `storage.mark_thread_writer()` (`addresses/runner.py`).
  * Requests only read (a prefix is a range over folded key columns, never `LIKE`) and start or show the import. The import runs in a daemon thread of one web worker (claim in the job row, heartbeat, stale-job takeover) or in the optional worker `process_address_imports`.
  * A new directory is inserted as an invisible generation in chunked transactions and published by switching `Directory.generation` in one short transaction. Only that and the previous generation are kept.
  * A missing or empty file means "no suggestions". The design assumes one host (D-075).

## Environments

* Local; stage; production.
* Stage behaves exactly like production: same security configuration, delivery mode `graph` and validation. It differs only by its label `APP_ENV=stage`, own database, credentials, secrets and logs (D-070).
* Production data is not copied into development environments or stage.

## Portability

Source code in Git; reproducible installation and deployment; configuration outside the source code; documented migrations, backup and restore; no unnecessary dependency on proprietary mechanisms of a hosting provider.

## Deliberately Open

Not yet decided; decided only when needed for the next development step:

* hosting details beyond provider and plan of the Ittigen instance (Infomaniak "VPS Lite 1", D-008) and of other instances; container-based or direct server deployment; final document storage; architecture of the later billing and financial modules
* whether AGOV may be used as the second factor (request by the project owner pending), integration details, and whether password login can later be disabled for system administrators
* concrete roles and their permissions, and whether they use the Django admin or application views (pure data maintenance: admin; frequent guided work: application views in the Clear register, `docs/DESIGN.md`)
* the review of the uploaded logos (D-073; security review and browser checks open)
* whether Django's `redirect_authenticated_user` of the login view is used (Django's documentation warns it can allow social media fingerprinting)
* minimum number of participants for publishing aggregated figures, and the public interface needed for it (D-025); imprint and content of the privacy notice (D-022)
* an optional guard in the settings that refuses to start outside `APP_ENV=local` while the placeholder email backend is active (not implemented; the deployment gates in the README apply instead)
* which functions are restricted on mobile phones in later phases; timing and scope of later multilingual support
