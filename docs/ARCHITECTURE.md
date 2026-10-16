# Architecture – LEG-Platform

## Goal

A simple, secure and maintainable architecture with the lowest possible operating effort and good portability between hosting providers. The software must also be operable by municipalities without in-depth IT knowledge.

## Intended Stack

* Python
* Django
* MariaDB (through Django's MySQL backend, D-002, D-030)
* Tabler for the user interface
* Django admin for system administration
* Microsoft Graph for outgoing email

Only minimum versions are specified (D-015): the oldest versions that are technically possible and still receive security updates from their vendor. They are determined from official sources and proven by tests. Assumptions about versions or compatibility are not permitted. Example: Django 4.2 no longer receives security updates since 7 April 2026; Django 5.2 LTS receives them until April 2028 (https://www.djangoproject.com/download/, checked on 2026-10-09). The minimum is Django 6.0, the oldest supported release line with built-in Content Security Policy support (`SECURE_CSP`); its security support ends in April 2027, when the minimum is raised (D-015, D-026, D-028).

### Candidate Packages for Security Functions

Established packages are preferred over custom developments. The following candidates were checked on 2026-10-09 against their official package index entries and documentation. The final choice and minimum versions are decided with D-015, before installation.

| Purpose | Candidate | Verified facts (2026-10-09) |
| ------- | --------- | --------------------------- |
| Login abuse protection | `django-axes` | Tracks failed login attempts; release 8.3.1 (2026-02-11); declares Django 4.2, 5.2, 6.0; requires Python ≥ 3.10. |
| Two-factor authentication (TOTP) | `django-otp` | TOTP devices compatible with Google Authenticator via the `otpauth` URL scheme; static tokens usable as recovery codes; release 1.7.4 (2026-10-08). How the Django admin is protected with it must still be verified in its documentation. |
| Encryption of the Microsoft Graph certificate private key and of the second factor secrets | `cryptography` | Release 50.0.2 (2026-09-30); requires Python ≥ 3.9 (except 3.9.0 and 3.9.1). |

Sources: https://pypi.org/project/django-axes/, https://github.com/jazzband/django-axes, https://pypi.org/project/django-otp/, https://django-otp-official.readthedocs.io/en/stable/overview.html, https://pypi.org/project/cryptography/

## Architecture Principles

* Django is responsible for authentication, sessions, permissions, forms and server-side business logic.
* Tabler is used for presentation and does not replace server-side security checks.
* The Django admin is used for system administration.
* Domain permissions are implemented explicitly and on the server side.
* Established, maintained packages are preferred over custom developments, especially for security functions.
* Reusable functionality is implemented centrally, not copied multiple times.
* External services are integrated behind clear interfaces.
* Configuration and secrets are not built into the source code.
* Instance-specific values are part of the instance configuration and are not built into the source code.
* Deployment and database migrations must be traceable and repeatable.
* The software and its documentation are in English without exception. Only the user interface is language-dependent.
* Architecture decisions are based on verified facts from official sources and are recorded with their sources in `docs/DECISIONS.md`.

## Deployment Model: One Instance per Municipality

The application is not multi-tenant (D-013). Each municipality runs its own, independent instance.

* Each instance has its own database, configuration, secrets, users and system administrators. Instances share no data.
* The same code base serves all instances. Instance-specific values (e.g. name of the community or municipality, domain, contact details) are part of the instance configuration and are never hard-coded in code, templates or migrations.
* A new instance is set up using only the documented installation steps and its own configuration.
* Each instance is operated, updated and backed up independently by its operator.

## Language and Internationalization

All technical parts are in English: identifiers, database schema, stored key values, migrations, permission codenames, comments, logs, tests, configuration, URL paths, commit messages and documentation. The complete list is in `AGENTS.md`, section 2.

The user interface uses Django's translation mechanism:

* User-facing texts are marked in Python with `gettext` or `gettext_lazy` and in templates with `{% translate %}` and `{% blocktranslate %}`.
* Source texts in code are English.
* The default user interface language is Swiss German: `LANGUAGE_CODE = "de-ch"`.
* The project translations are kept in a directory listed in `LOCALE_PATHS` (`locale/de_CH/LC_MESSAGES/django.po`). `LOCALE_PATHS` has the highest precedence when Django looks up translations.
* Django's own `de_CH` locale only contains formats, not texts. For texts, Django falls back from `de-ch` to `de`. Django's `de` translations contain "ß". The project translation files therefore override every such text that can appear in the user interface, including texts of third-party packages. An automated check ensures that no displayed text contains "ß".
* In phase 1 there is only Swiss German and no language selection. Additional languages are added later through additional translation files, the `LANGUAGES` setting and, if needed, `LocaleMiddleware`.
* Stored values such as statuses are English keys. They are displayed through translated labels.
* User data (e.g. names, addresses, role names created in the admin) is not translated.

Sources: https://docs.djangoproject.com/en/stable/topics/i18n/translation/, https://docs.djangoproject.com/en/stable/ref/settings/#language-code, https://docs.djangoproject.com/en/stable/ref/settings/#locale-paths, Django source `django/conf/locale/de_CH/` and `django/conf/locale/de/` (branch `main`, checked on 2026-10-09).

## Users and Roles

Django user accounts are distinguished from roles and account status.

The terminology (instance, system administrator, staff user, user, customer number, role, permission, retention obligation) is defined in `docs/PROJECT.md`.

Fixed access levels:

* **System administrator:** responsible for the instance. Implemented as Django `is_superuser` with `is_staff`; `is_superuser` has all permissions without them being assigned individually, and Django requires `is_staff` for logging in to the admin.
* **Staff user:** `is_staff` without `is_superuser`. Can log in to the admin and use only the permissions assigned to it.
* **Authenticated user:** a logged-in user without additional permissions. Can only access their own profile and their own account (the cards of the profile, the start page and the task tiles act on `request.user` only).

Only system administrators can change `is_staff`, `is_superuser`, group memberships, direct permissions and roles. Because Django warns that anyone who can edit users can elevate their own permissions, the admin enforces this centrally: for non-superusers, these fields and the management of groups and permissions are not available; accounts of staff users and system administrators (including their own) cannot be changed, blocked, deactivated or deleted; and the passwords, email addresses and second factors of other accounts cannot be changed. Customer number and retention flag are read-only for everyone. The normalised unique email column cannot be changed in the admin either; it is not shown, because it repeats the email address (D-035), and its label is "email address", which is also the label of the admin login field. Negative tests cover this (`docs/SECURITY.md`, section 11).

Further roles:

* Roles are Django groups. They are created, named, renamed and linked to permissions at runtime in the admin.
* No role is hard-coded. No code, migration or fixture creates, names or checks specific roles.
* Authorization checks use permissions only (e.g. `has_perm`, `permission_required`, `PermissionRequiredMixin`), never group names. Renaming a role therefore never changes the behaviour of the application.
* Permissions for application functions are defined in code with English codenames. They describe what can be done, not who does it.
* Access to the admin requires `is_staff`. `is_staff` and `is_superuser` are separate from roles and are only granted by a system administrator.
* Whether later roles work in the Django admin or in application views built with Tabler is decided when those roles are defined.

Privileged access must never be granted through public registration. Any promotion beyond the authenticated user requires an explicit action by an authorized superior; nobody can promote their own account.

Sources: https://docs.djangoproject.com/en/stable/topics/auth/default/, https://docs.djangoproject.com/en/stable/ref/contrib/auth/, https://docs.djangoproject.com/en/stable/ref/contrib/admin/

## User Model and Login Identifiers

The project uses a custom user model from the first migration (D-021). Django's documentation recommends setting up a custom user model when starting a new project and warns that changing `AUTH_USER_MODEL` after database tables have been created "can be complex".

* **Customer number:** six digits, generated with a cryptographically secure random generator (Python `secrets`) exactly once, when the registration is completed (password form submitted successfully via the verification link; the link is consumed only then), stored as text so that leading zeros are possible, unique and never reused. Unverified accounts have no customer number (the field is nullable), so that registrations that are never verified do not use up numbers. Numbers of deleted accounts are kept in a list of retired numbers without any personal reference. On a collision with an existing or retired number, a new number is drawn. Confirming an email address change and reactivation never change the number; the initial system administrator receives a number from the setup command. Because two tables cannot share one unique index, "never reused" is enforced in the database with a single table of all issued numbers (`IssuedCustomerNumber`, the number as primary key, without personal reference; D-034). Displayed as two blocks of three digits. The customer number is not a login identifier.
* **Login:** an authentication backend accepts the email address together with the password. Email addresses are looked up only through the normalised unique column. The abuse protection counts failed attempts per normalised email address and treats nonexistent addresses the same way as existing ones. The backend is based on Django's `ModelBackend` or reproduces its protection against timing differences for nonexistent users. How `django-axes` can be given the normalised address is verified in its documentation before implementation.
* **Normalisation of email addresses:** defined in one place (`leg_platform/accounts/normalization.py`: Unicode normalisation form NFKC, removal of surrounding whitespace, lower case, and domain mapping to IDNA ASCII with UTS #46; account local parts are ASCII only and the canonical form is stored in `email` (lower case, IDNA ASCII domain; D-035); D-034) and used for storage and for all lookups and per-address counters (login, registration, password reset, email change, counting of failed attempts and rate limits). Quoted local parts, domain literals, IP addresses, `localhost` and invisible/control characters are rejected. Mail-provider delivery equivalence for compatibility-mapped variants is unverified.
* **Email uniqueness among accounts that are not deactivated:** a conditional unique constraint is not possible, because Django does not support conditions on MariaDB ("The `condition` argument is ignored with MySQL and MariaDB as neither supports conditional indexes.", Django's model index reference; system check `models.W036`: "<database> does not support unique constraints with conditions"). Instead, an additional nullable column contains the normalised (lower-case) email address while the account is not deactivated and `NULL` when it is deactivated. A normal unique index on this column enforces uniqueness in the database, because "A `UNIQUE` constraint allows multiple `NULL` values because in SQL, `NULL` is never equal to another `NULL`" (MariaDB documentation, "MariaDB Indexes Guide"). The column is maintained in one central place of the model logic and covered by tests, including concurrent requests. It is the `USERNAME_FIELD` of the user model (`normalized_email`, D-034).
* **Profile fields:** the fields of D-032 are fields of the user model with the eCH-0010 structure unchanged; unused fields are not shown, and forms show one postal code field that is stored as the Swiss postal code (Switzerland, four digits) or the foreign postal code (D-036); country with `django-countries` (default `CH`), optional phone number with `django-phonenumber-field`, stored in E.164 (D-034). The string representation is "Account" with the customer number, or "Unconfirmed account", never an email address or name; the admin user list shows and searches the customer number. Admin log entries of accounts store only a neutral text with the internal number, which the change form shows (D-018, D-036).
* **Collations (D-031):** the database default `utf8mb4_uca1400_ai_ci` is case- and accent-insensitive and would treat e.g. `josé@example.org` and `jose@example.org` as equal. Identifier columns compared exactly therefore use the binary collation `utf8mb4_bin` through Django's `db_collation`: the normalised email address, token and keyed-hash values, and the customer number. All other text keeps the database default. Tests cover addresses that differ only in accents or case.

Sources: https://docs.djangoproject.com/en/stable/topics/auth/customizing/, https://docs.djangoproject.com/en/stable/ref/models/indexes/, https://docs.djangoproject.com/en/stable/ref/models/constraints/ (checked on 2026-10-09), https://docs.djangoproject.com/en/6.0/ref/checks/, https://mariadb.com/docs/server/mariadb-quickstart-guides/mariadb-indexes-guide (checked on 2026-10-11 after the switch from MySQL to MariaDB, D-030)

## Account Lifecycle

The account lifecycle (D-016) is implemented centrally:

* An account can log in once its email address is verified and a password is set.
* Status values (English keys): unverified, active, blocked, deactivated (`docs/PROJECT.md`, "Account States"). A temporary lockout is managed by the abuse protection package, not as an account status.
* Unverified, blocked and deactivated accounts are additionally marked as inactive (`is_active = False`), and unverified accounts have no usable password until verification, so that Django's own protective behaviour applies: Django's password reset sends no email to inactive users or users without a usable password and shows no error message.
* Unverified accounts are deleted automatically when their verification link expires.
* Every path that deletes, deactivates, reactivates, blocks, unblocks, promotes or demotes accounts, changes the email address or the normalised email column, or changes the retention flag goes through the central account logic, including admin bulk actions, `list_editable`, `QuerySet.update()` and management commands. A custom manager and queryset for the user model reject bulk changes to these fields, bulk deletion and bulk creation; they are also the base manager (D-034). Django's bulk action "delete selected objects" uses `QuerySet.delete()` and does not call the model's `delete()` method; it is disabled for the user model (`get_actions()`) or replaced via `delete_queryset()` (https://docs.djangoproject.com/en/5.2/ref/contrib/admin/actions/, checked on 2026-10-09).
* All other sessions of an account are ended after password changes or resets, email address changes, resets of the second factor, blocking, deactivation and removal of rights.
* The retention obligation flag can only change from unset to set. The application rejects any attempt to unset it, including in the Django admin. This is enforced at application level; persons with direct database access can bypass it, which is why database privileges are restricted (`docs/SECURITY.md`, section 9).
* Account deletion removes all data of the account if no retention obligation is set; otherwise the account is deactivated.
* At least one active, not blocked system administrator always remains. Emergency access is restored with a management command on the server.

## Authentication

* The first phase uses a password login with the email address (D-021).
* Two-factor authentication with an authenticator app (TOTP) is mandatory for staff users and system administrators and optional for users (D-004). Recovery codes allow recovery after losing the device.
* Login and recovery are protected against automated abuse with an established package (D-017). Lockouts are temporary. A successful password reset ends only the lockout of the account, not the counters of IP addresses. Registration, password reset and email change requests are rate-limited per IP address and per normalised target email address; the per-address limit restricts the emails sent, and links already sent stay valid. Whether `django-axes` can end an account's lockout without lowering IP-based counts (its documentation states that `axes_reset_username` "will clear lockouts and records for the given usernames") is verified before implementation; if not, the account's lockout is ended differently or IP counters are kept separately. Behind reverse proxies, the number of trusted proxies is configured per instance (`AXES_IPWARE_PROXY_COUNT` in `django-axes`); the package documentation warns that clients can forge IP headers.
* Password rules use Django's password validators with a minimum length of 15 characters (`docs/SECURITY.md`, section 3).
* Session IDs are renewed at login, after the second factor has been completed, and after password and privilege changes; idle and absolute timeouts follow the OWASP Session Management Cheat Sheet.
* Preparation for AGOV as the second factor (`docs/PROJECT.md`, "AGOV and Passkeys"): whether the second factor has been completed is checked in one central place (e.g. middleware and a decorator or mixin), independent of the method. Protected functions, the mandatory second factor for staff users and system administrators, and re-authentication rely only on this check. TOTP enrolment, verification and recovery codes are kept separate from it. `django-otp` supports this structure: its `OTPMiddleware` "adds `user.is_verified()` as a counterpart to `user.is_authenticated()`", and further device types are provided by plugins that subclass `django_otp.models.Device`. Whether AGOV can be connected in this way, or whether the central check has to evaluate a different verified state, is decided once AGOV's answer and documentation are available; no AGOV-specific code is written in advance.
* An external identity (AGOV, later passkeys) is mapped to an internal account only through a stored, unambiguous identifier, never solely through a matching email address.

## Email

Sending is encapsulated behind a central application interface. The concrete Microsoft Graph implementation must not be built separately in every function.

The Microsoft Graph configuration is kept in the database and maintained in a dedicated section of the Django admin (D-006):

* tenant ID, client ID, sender address
* authentication with a certificate (client credentials flow with a signed client assertion, D-006), not with a client secret; Microsoft recommends certificates for production (Microsoft Learn, checked 2026-10-14)
* certificate (public part, with its expiry date) and private key; the private key is stored encrypted, the encryption key is derived from Django's `SECRET_KEY` with HKDF and a fixed, purpose-specific label, and the private key is encrypted with Fernet (both from `cryptography`), so that operators do not need to manage an additional key; a database backup alone does not reveal the private key; the private key is never displayed again
* keys derived from `SECRET_KEY_FALLBACKS` are tried for decryption, and the private key is then encrypted again with the current key; if no key matches, the admin clearly asks for the certificate and private key to be uploaded again
* expiry date of the certificate with a warning before expiry
* test send with understandable feedback

The exact `cryptography` API is verified against the documentation of the installed version before implementation.

Local development, stage and production have separate settings and credentials. So do different instances. The environment variable `EMAIL_MODE` controls delivery; which values are allowed depends on `APP_ENV`. Stage behaves exactly like production (D-070): `APP_ENV=stage` is only a label of the instance role, and the rows `stage` and `production` are identical.

| Value | `local` | `stage` | `production` | Behaviour |
| ----- | ------- | ------- | ------------ | --------- |
| `console` | allowed (default) | refused | refused | Emails are only written to the console or test output. Outside local development, security links would end up in server logs. |
| `allowlist` | allowed | refused | refused | Emails are sent via Microsoft Graph, but only to the addresses in `EMAIL_ALLOWED_RECIPIENTS`; all others are discarded and logged without content. Only locally, with a dedicated development app registration and sender mailbox, separate from stage and production (D-020, `docs/SECURITY.md`, sections 7 and 8); the local recipient addresses are kept only in the developer's user environment variable `EMAIL_ALLOWED_RECIPIENTS`, never in the repository. Without allowed recipients, or with an entry that is not one complete address, the application refuses to start. A non-empty `EMAIL_ALLOWED_RECIPIENTS` is refused at startup with `APP_ENV=stage` and `APP_ENV=production` (D-070). |
| `graph` | refused | required | required | Emails are sent via Microsoft Graph without restriction. On stage this means real emails to real addresses (D-070). |

"Refused" means that the application refuses to start. Outside `APP_ENV=local`, `EMAIL_MODE` has no default and must be set; a missing or unknown `APP_ENV` prevents the application from starting. Security settings are secure by default and are only relaxed explicitly for `APP_ENV=local` (`docs/SECURITY.md`, sections 3, 8 and 11). Enforced in `leg_platform/environment.py`, which contains no rule that distinguishes stage from production. `allowlist` and `graph` use the Microsoft Graph backend `leg_platform.mail.GraphEmailBackend` (D-006), which fails closed while no configuration exists (D-028). Automated tests never send real email: Django's test runner replaces the backend with its in-memory backend, and tests use only addresses under reserved domains (RFC 2606), which a test checks for the whole repository.

Links in emails (verification, password reset, email change) are built from the canonical base URL in the instance configuration, not from the request. This also works for emails that are not triggered by a request (e.g. management commands). `ALLOWED_HOSTS` still lists only the host names of the application (not the instance's website; D-025).

Security notifications tell the person to contact the operator if they did not request the change (contact information from `INSTANCE_WEBSITE_URL` where configured); automatic emails do not invite a reply, and the sender address (default `noreply@<host>`) is not presented as a contact address (D-038).

Editable texts (D-039): `EmailText` holds an optional subject and body per email; empty means the default. `accounts/email_texts.py` defines the placeholders per email, validates a text when it is saved and renders it when the email is sent, by plain substitution of `{name}` markers without the template engine; a stored text that is no longer valid is replaced by the default. The greeting of the 14 notifications (D-068) is the placeholder `{greeting}`: `accounts/greeting.py` cleans the name (allow-list guard) and builds the greeting from two catalogue texts; `mail_queue.enqueue_for_account` captures the cleaned name once in `MailJob.greeting_name` and only for addresses that belong to the account at that time; the worker passes it to the sender, so the account is not read again for the greeting; `User.erase_greeting_names` (called by `delete_or_deactivate` and `delete`) blanks the name in the queued jobs of an account that is deleted or deactivated, and `greeting.render` checks the stored name again (`is_clean`).

Sending in the background (D-038): requests for registration, password reset and an address change count the request against the limits and store one `MailJob`; the worker `process_mail_queue` looks up the address, issues the token, sends the email outside any database transaction and deletes the job. This makes the request path and the response identical for every address. Counters are rows (`RateLimitBucket`, `RateLimitEvent`) locked per key in the database, shared by all processes; the limits of the client address and of the instance are read first, so a refused request stores nothing (D-058). The modules are `accounts/rate_limits.py`, `mail_queue.py`, `bot_check.py`, `resend.py`, `registration.py`, `password_reset.py` and `email_change.py`; the views of the entry pages and of the links are in `accounts/views.py`, the views of the profile cards in `accounts/profile_views.py` (D-059).

Sources: https://docs.djangoproject.com/en/stable/topics/auth/default/, https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts, https://docs.djangoproject.com/en/stable/ref/settings/#secret-key, https://django-otp-official.readthedocs.io/en/stable/overview.html (checked on 2026-10-09)

## Security Logging

Security-relevant events are logged centrally (D-018). Logs contain no passwords, tokens or complete security links and only as much personal data as necessary. Only system administrators can view them. Log entries of deleted accounts are kept without personal reference, using an internal number instead of the customer number. The security log is append-only within the application: the admin offers no permission to add, change or delete entries, not even for system administrators. This also applies to the failed-attempt and lockout records of `django-axes`; whether its admin allows changing or deleting them is verified against the installed version, and the admin is restricted if necessary. Retention is configured per instance in the server configuration with minimum values (categories and defaults in `docs/SECURITY.md`, section 10); expired entries are deleted automatically, including the failed-attempt records of the abuse protection package. How the deletion is triggered (e.g. a scheduled management command) is decided at implementation together with the hosting.

## User Interface and Devices

* The user interface is responsive and usable on desktop computers, tablets and mobile phones. In phase 1, all user functions are fully usable on a mobile phone (`docs/PROJECT.md`, "Supported Devices").
* Tabler is "built on Bootstrap 5"; Bootstrap "is developed mobile first" and requires the responsive viewport meta tag.
* Tabler's README states minimum browser versions (Chrome/Edge 123, Firefox 128, Safari 17.5, Opera 109) because it uses CSS features without fallbacks. Older devices whose browser cannot be updated beyond these versions (e.g. older iPhones) are therefore not supported. The supported browsers are documented for operators and users.
* Static assets, including Tabler, fonts and icons, are served by the instance itself in fixed versions.
* The design language is defined in `docs/DESIGN.md` (D-024). It is implemented through Tabler's CSS variables and attributes (section 9 there); instance branding (logo, accent colours) is maintained in the administration with the instance configuration as the fallback (D-073).
* Instance branding: the portal settings of the administration (name, website, colours, two logos stored in the database) win per field over the validated environment variables, which win over the platform defaults; one accessor (`leg_platform.accounts.portal_settings.portal_values`) resolves them and checks them again when it reads them, and a same-origin generated stylesheet carries the colours (D-051, D-056, D-073). The uploaded logos are served by a dedicated public view (`/branding/logo/<light|dark>/`) with a fixed content type, `nosniff` and a sandbox policy. The colour mode and the language are not handled by the server: the page is always rendered light, and the choice of the person is kept in the `sessionStorage` of the tab by external scripts (no cookie, no request). No inline CSS, theme URL parameters or third-party assets are introduced.
* The application has no public home page; its entry point is the login page. It runs on its own host name, never under a path of another website (D-025). A subdomain of the website's registrable domain provides origin isolation but is still the same site; the cookie rules in `docs/SECURITY.md`, section 3, apply.

Sources: https://github.com/tabler/tabler, https://getbootstrap.com/docs/5.3/getting-started/introduction/ (checked on 2026-10-09)

## Management Area and the Community Domain

Phase 2 (D-077) adds two Django apps.

* **`leg_platform.community`**: the domain, without pages. Models: `TransformerCircuit`, `Location` (unique on the normalised address, through a SHA-256 digest), `MeteringPoint` (33-character number, direction consumption or feed-in, optional location, dates that drive a derived status), `Leg`, `PersonAssignment` and `LegAssignment` (periods; the previous assignment ends the day before the new one), `HistoryEntry` (append only), `ManagementSettings` (one row), `PersonRecord` (the staff-only side record of an account: note, BKW customer number, shares, IBAN and paper invoice read-only, address suggestion state) and `ListPreference`. The rules are plain modules, tested without a browser: `numbers` (metering point and transformer circuit numbers), `status`, `assignments`, `legs` (members, consumer:producer ratio, discount tier, quota problems), `problems`, `addressing` (normalisation and the directory suggestion on top of `leg_platform.addresses.services`), `locations` (accept or dismiss a suggestion), `history`, `settings_service` and `preferences`. Identifier columns use the exact collation (D-031). The models are not registered in the Django administration; their rights (default rights and `correct_person_address`) are offered to roles by `accounts/permission_names.py`.
* **`leg_platform.management`**: views, forms, templates and static files under `/management/`. Every view uses `access.ManagementAccessMixin` or `@management_view` (staff, second factor completed, the permissions of the page; 404 for non-staff, 403 for missing rights); the menu group is built from `navigation.MENU` (one line per page); `returns.py` is the safe way back; the shared building blocks are the includes `management/_*.html`, `page.html`, `dialog.html`, `management.css` and `info-bubble.js`. A test walks the URL configuration and fails for a route without the access check.

Permissions are the only access rule (D-012); the menu only reflects them. The status of a metering point follows from its dates and the day, without a scheduler. The address directory stays advisory (D-066): a suggestion for a location is stored on it so that lists can filter without asking the directory per row.

Block 2 (D-078) adds the list framework and the pages of the locations. Domain modules: `transformer_circuits`, `metering_points` (creating one at a location), `location_list` (search, filters, sort and the rows of the lists as queries) and more services in `locations` (create, change the address with the merge, circuit, confirm, archive, delete). In `management`: `lists.py` (state of a list from the address, pages, head cells, saved widths; used by every later list), `location_views.py`, `location_forms.py`, the templates `_list_*.html`, `dialog_form.html`, `locations/*.html` and `static/management/js/list.js` (resizing, submit on change, expanding rows; only an enhancement, the pages work without JavaScript).

Sources: D-077, D-078 and their sources.

Block 3 (D-079) adds the metering points. Domain modules: `metering_points` (now also the steps towards the BKW, `next_step`, archive, delete and `chain`), `metering_point_list` (search, quick filters with counts, problems, sort), `person_search` (the bounded search of accounts for an assignment), `leg_switch` (the move mask: the related metering points of the same person at the same location, one transaction) and `location_list.resolve` (a location by typing). In `management`: `metering_point_views.py`, `metering_point_forms.py`, `summaries.py`, the templates `metering_points/*.html` and `_duplicate.html`, and `static/management/js/lookup.js` (suggestions and the check of the number on leaving the field; only an enhancement). `management:metering_points_leg` is the one dialog "assign / change LEG" for the page of the metering point and for the pages of the LEGs.

Sources: D-077, D-078, D-079 and their sources.

## Data Storage

MariaDB is the database (D-002, D-030); MySQL is not supported. Django migrations are the traceable source for schema changes.

Later documents and receipts must not be stored exclusively in an ephemeral container file system. The concrete storage solution will be decided once corresponding functions are planned.

**Address directory (D-033, D-049, D-052, D-075).** The public directory of building addresses (`Directory`, `Address`) is an own SQLite file on the server (the database alias `addresses`, `leg_platform.addresses.sqlite`; path `ADDRESS_DIRECTORY_FILE`, default `/var/lib/leg-platform/address-directory.sqlite3`), **not part of any backup**; the import queue row (`ImportRequest`) stays in the MariaDB database. A minimal router (`addresses/router.py`) keeps the two apart; there is no second account. The directory has no foreign key to any profile data. The web process opens the file read-only (`PRAGMA query_only`: every connection to the directory is read-only unless the process called `storage.mark_writer()`, which only the worker, the operator commands `import_address_directory` and `seed_local_address_directory`, `setup_instance` and the project's `migrate` do; `wsgi.py` and `asgi.py` do not) and only reads it (the lookup, `addresses/services.py`, a prefix is a range over folded key columns, never `LIKE`) and queues an import request (`addresses/jobs.request_import`); the download and the import (`addresses/importer.py`) run only in the worker process `process_address_imports`, which uses the same settings. The new directory is inserted as an invisible generation in chunked transactions and published by switching `Directory.generation` in one short transaction; afterwards only that generation and the previous one are kept, older ones are deleted in batches. A missing or empty file means "no suggestions". The design assumes one host (D-075).

## Environments

* Local
* Stage: behaves exactly like production (same security configuration, same delivery mode `graph`, same validation); it differs only by its label `APP_ENV=stage`, its own database, credentials and secrets, and its own logs (D-070)
* Production

Each environment uses separate credentials and configurations. Production data is not copied into development environments or into stage.

## Portability

* source code in Git
* reproducible installation and deployment steps
* configuration outside the source code
* documented database migrations
* documented backup and restore procedures
* no unnecessary dependency on proprietary runtime mechanisms of a hosting provider

## Deliberately Open

Not yet decided:

* hosting details beyond provider and plan of the Ittigen instance (Infomaniak "VPS Lite 1", D-008), and the hosting of other instances
* container-based or direct server deployment
* final document storage
* whether AGOV may be used as the second factor (request by the project owner pending), AGOV integration details, and whether password login can later be disabled for system administrators
* architecture of the later billing and financial modules
* concrete roles and their permissions, and whether they use the Django admin or application views; decided per function in phase 2: pure data maintenance is a candidate for the Django admin, frequent guided work of the LEG's management for application views in the Clear register (`docs/DESIGN.md`)
* the review of the uploaded logos (D-073: web upload of logos is built, in the database, with the rules of `docs/SECURITY.md`, section 6; the security review and the browser checks are open)
* whether Django's `redirect_authenticated_user` option of the login view is used to send logged-in users from the login page to their profile; Django's documentation warns that enabling it can allow social media fingerprinting (https://docs.djangoproject.com/en/5.2/topics/auth/default/, as cited in the security review of 2026-10-11); decided at implementation
* minimum number of participants for publishing aggregated figures, and the public interface needed for it (D-025)
* which instance-specific values are kept in environment variables and which in the database: decided in D-073 (name, website, colours, logos, privacy and legal addresses in the database with the environment as the fallback; host name, base address, allowed hosts, mail mode and sender, credentials, retention and proxy settings only in the environment)
* scheduling of the automatic deletion of expired log entries and of unverified accounts whose verification link has expired
* redaction of tokens in request logs: Django's request logging writes the path of every response with status 400 or higher; a logging filter that removes tokens from paths is in place for the registration link, the password reset link, the second-factor removal link and the two links of an address change (confirmation and stop; `leg_platform/log_filters.py`, D-037, D-067) and must be extended, with a test, for every further URL with a token (`docs/SECURITY.md`, sections 6 and 10)
* an optional guard in the settings that refuses to start outside `APP_ENV=local` while the placeholder email backend is active (security review of 2026-10-11, I-6; not implemented, the deployment gates in the README apply instead)
* imprint (legal notice) and the content of the privacy notice (D-022)
* which functions are restricted on mobile phones in later phases
* timing and scope of later multilingual support

These points are decided only when they are actually needed for the next development step.
