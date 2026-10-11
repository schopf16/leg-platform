# Architecture – LEG-Platform

## Goal

A simple, secure and maintainable architecture with the lowest possible operating effort and good portability between hosting providers. The software must also be operable by municipalities without in-depth IT knowledge.

## Intended Stack

* Python
* Django
* MySQL
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
| Encryption of the Microsoft Graph client secret | `cryptography` | Release 50.0.2 (2026-09-30); requires Python ≥ 3.9 (except 3.9.0 and 3.9.1). |

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
* **Authenticated user:** a logged-in user without additional permissions. Can only access their own profile.

Only system administrators can change `is_staff`, `is_superuser`, group memberships, direct permissions and roles. Because Django warns that anyone who can edit users can elevate their own permissions, the admin enforces this centrally: for non-superusers, these fields and the management of groups and permissions are not available; accounts of staff users and system administrators (including their own) cannot be changed, blocked, deactivated or deleted; and the passwords, email addresses and second factors of other accounts cannot be changed. Customer number, retention flag and the normalised unique email column are read-only for everyone. Negative tests cover this (`docs/SECURITY.md`, section 11).

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

* **Customer number:** six digits, generated with a cryptographically secure random generator (Python `secrets`) exactly once, when the registration is completed (password form submitted successfully via the verification link; the link is consumed only then), stored as text so that leading zeros are possible, unique and never reused. Unverified accounts have no customer number (the field is nullable), so that registrations that are never verified do not use up numbers. Numbers of deleted accounts are kept in a list of retired numbers without any personal reference. On a collision with an existing or retired number, a new number is drawn. Confirming an email address change and reactivation never change the number; the initial system administrator receives a number from the setup command. Because two tables cannot share one unique index, "never reused" is preferably enforced in the database with a single table of all issued numbers with a unique key (decided at implementation). Displayed as two blocks of three digits. The customer number is not a login identifier.
* **Login:** an authentication backend accepts the email address together with the password. Email addresses are looked up only through the normalised unique column. The abuse protection counts failed attempts per normalised email address and treats nonexistent addresses the same way as existing ones. The backend is based on Django's `ModelBackend` or reproduces its protection against timing differences for nonexistent users. How `django-axes` can be given the normalised address is verified in its documentation before implementation.
* **Normalisation of email addresses:** defined in one place (at least removal of surrounding whitespace and conversion to lower case) and used for storage and for all lookups and per-address counters (login, registration, password reset, email change, counting of failed attempts and rate limits).
* **Email uniqueness among accounts that are not deactivated:** a conditional unique constraint is not possible, because Django ignores the condition on MySQL ("The `condition` argument is ignored with MySQL and MariaDB as neither supports conditional indexes."). Instead, an additional nullable column contains the normalised (lower-case) email address while the account is not deactivated and `NULL` when it is deactivated. A normal unique index on this column enforces uniqueness in the database, because "A `UNIQUE` index permits multiple `NULL` values for columns that can contain `NULL`" (MySQL 8.4 Reference Manual). The column is maintained in one central place of the model logic and covered by tests, including concurrent requests.

Sources: https://docs.djangoproject.com/en/stable/topics/auth/customizing/, https://docs.djangoproject.com/en/stable/ref/models/indexes/, https://docs.djangoproject.com/en/stable/ref/models/constraints/, https://dev.mysql.com/doc/refman/8.4/en/create-index.html (checked on 2026-10-09)

## Account Lifecycle

The account lifecycle (D-016) is implemented centrally:

* An account can log in once its email address is verified and a password is set.
* Status values (English keys): unverified, active, blocked, deactivated (`docs/PROJECT.md`, "Account States"). A temporary lockout is managed by the abuse protection package, not as an account status.
* Unverified, blocked and deactivated accounts are additionally marked as inactive (`is_active = False`), and unverified accounts have no usable password until verification, so that Django's own protective behaviour applies: Django's password reset sends no email to inactive users or users without a usable password and shows no error message.
* Unverified accounts are deleted automatically when their verification link expires.
* Every path that deletes, deactivates, reactivates, blocks, unblocks, promotes or demotes accounts, changes the email address or the normalised email column, or changes the retention flag goes through the central account logic, including admin bulk actions, `list_editable`, `QuerySet.update()` and management commands. A custom manager or queryset for the user model that rejects bulk changes to these fields is one way to enforce this. Django's bulk action "delete selected objects" uses `QuerySet.delete()` and does not call the model's `delete()` method; it is disabled for the user model (`get_actions()`) or replaced via `delete_queryset()` (https://docs.djangoproject.com/en/5.2/ref/contrib/admin/actions/, checked on 2026-10-09).
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
* client secret, stored encrypted; the encryption key is derived from Django's `SECRET_KEY` with HKDF and a fixed, purpose-specific label, and the secret is encrypted with Fernet (both from `cryptography`), so that operators do not need to manage an additional key; a database backup alone does not reveal the secret
* keys derived from `SECRET_KEY_FALLBACKS` are tried for decryption, and the secret is then encrypted again with the current key; if no key matches, the admin clearly asks for the secret to be entered again
* expiry date of the client secret with a warning before expiry
* test send with understandable feedback

The exact `cryptography` API is verified against the documentation of the installed version before implementation.

Local development, stage and production have separate settings and credentials. So do different instances. The environment variable `EMAIL_MODE` controls delivery; which values are allowed depends on `APP_ENV`:

| Value | `local` | `stage` | `production` | Behaviour |
| ----- | ------- | ------- | ------------ | --------- |
| `console` | allowed (default) | refused | refused | Emails are only written to the console or test output. Outside local development, security links would end up in server logs. |
| `allowlist` | allowed | required | refused | Emails are sent via Microsoft Graph, but only to the addresses in `EMAIL_ALLOWED_RECIPIENTS`; all others are discarded and logged without content. Locally only with a dedicated development app registration and sender mailbox, separate from stage and production (D-020, `docs/SECURITY.md`, sections 7 and 8); the local recipient addresses are kept only in the developer's user environment variable `EMAIL_ALLOWED_RECIPIENTS`, never in the repository. Without allowed recipients, or with an entry that is not one complete address, the application refuses to start. |
| `graph` | refused | refused | required | Emails are sent via Microsoft Graph without restriction. |

"Refused" means that the application refuses to start. Outside `APP_ENV=local`, `EMAIL_MODE` has no default and must be set; a missing or unknown `APP_ENV` prevents the application from starting. Security settings are secure by default and are only relaxed explicitly for `APP_ENV=local` (`docs/SECURITY.md`, sections 3, 8 and 11). Enforced in `leg_platform/environment.py`. Until the Microsoft Graph backend exists (D-006), `allowlist` and `graph` use a placeholder backend that refuses to send (D-028). Automated tests never send real email: Django's test runner replaces the backend with its in-memory backend, and tests use only addresses under reserved domains (RFC 2606), which a test checks for the whole repository.

Links in emails (verification, password reset, email change) are built from the canonical base URL in the instance configuration, not from the request. This also works for emails that are not triggered by a request (e.g. management commands). `ALLOWED_HOSTS` still lists only the host names of the application (not the instance's website; D-025).

Security notifications ask the person to reply if they did not request the change; replies go to the sender mailbox.

Sources: https://docs.djangoproject.com/en/stable/topics/auth/default/, https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts, https://docs.djangoproject.com/en/stable/ref/settings/#secret-key, https://django-otp-official.readthedocs.io/en/stable/overview.html (checked on 2026-10-09)

## Security Logging

Security-relevant events are logged centrally (D-018). Logs contain no passwords, tokens or complete security links and only as much personal data as necessary. Only system administrators can view them. Log entries of deleted accounts are kept without personal reference, using an internal number instead of the customer number. The security log is append-only within the application: the admin offers no permission to add, change or delete entries, not even for system administrators. This also applies to the failed-attempt and lockout records of `django-axes`; whether its admin allows changing or deleting them is verified against the installed version, and the admin is restricted if necessary. Retention is configured per instance in the server configuration with minimum values (categories and defaults in `docs/SECURITY.md`, section 10); expired entries are deleted automatically, including the failed-attempt records of the abuse protection package. How the deletion is triggered (e.g. a scheduled management command) is decided at implementation together with the hosting.

## User Interface and Devices

* The user interface is responsive and usable on desktop computers, tablets and mobile phones. In phase 1, all user functions are fully usable on a mobile phone (`docs/PROJECT.md`, "Supported Devices").
* Tabler is "built on Bootstrap 5"; Bootstrap "is developed mobile first" and requires the responsive viewport meta tag.
* Tabler's README states minimum browser versions (Chrome/Edge 123, Firefox 128, Safari 17.5, Opera 109) because it uses CSS features without fallbacks. Older devices whose browser cannot be updated beyond these versions (e.g. older iPhones) are therefore not supported. The supported browsers are documented for operators and users.
* Static assets, including Tabler, fonts and icons, are served by the instance itself in fixed versions.
* The design language is defined in `docs/DESIGN.md` (D-024). It is implemented through Tabler's CSS variables and attributes (section 9 there); instance branding (logo, accent colours) comes from the instance configuration.
* The application has no public home page; its entry point is the login page. It runs on its own host name, never under a path of another website (D-025). A subdomain of the website's registrable domain provides origin isolation but is still the same site; the cookie rules in `docs/SECURITY.md`, section 3, apply.

Sources: https://github.com/tabler/tabler, https://getbootstrap.com/docs/5.3/getting-started/introduction/ (checked on 2026-10-09)

## Data Storage

MySQL is the intended database. Django migrations are the traceable source for schema changes.

Later documents and receipts must not be stored exclusively in an ephemeral container file system. The concrete storage solution will be decided once corresponding functions are planned.

## Environments

* Local
* Stage
* Production

Each environment uses separate credentials and configurations. Production data is not copied into development environments.

## Portability

* source code in Git
* reproducible installation and deployment steps
* configuration outside the source code
* documented database migrations
* documented backup and restore procedures
* no unnecessary dependency on proprietary runtime mechanisms of a hosting provider

## Deliberately Open

Not yet decided:

* final hosting provider and hosting plan
* container-based or direct server deployment
* final document storage
* whether AGOV may be used as the second factor (request by the project owner pending), AGOV integration details, and whether password login can later be disabled for system administrators
* architecture of the later billing and financial modules
* concrete roles and their permissions, and whether they use the Django admin or application views; decided per function in phase 2: pure data maintenance is a candidate for the Django admin, frequent guided work of the LEG's management for application views in the Clear register (`docs/DESIGN.md`)
* whether instance branding values (logo files, accent colours) are kept in environment variables, files or the database; storing logos in the database would imply a web upload, which needs its own security design (`docs/SECURITY.md`, section 6, secure uploads)
* whether Django's `redirect_authenticated_user` option of the login view is used to send logged-in users from the login page to their profile; Django's documentation warns that enabling it can allow social media fingerprinting (https://docs.djangoproject.com/en/5.2/topics/auth/default/, as cited in the security review of 2026-10-11); decided at implementation
* minimum number of participants for publishing aggregated figures, and the public interface needed for it (D-025)
* which instance-specific values are kept in environment variables and which in the database
* scheduling of the automatic deletion of expired log entries and of unverified accounts whose verification link has expired
* redaction of tokens in request logs: Django's request logging writes the path of every response with status 400 or higher; a logging filter that removes tokens from paths, with a test, is required when the first URL with a token is introduced (`docs/SECURITY.md`, sections 6 and 10)
* an optional guard in the settings that refuses to start outside `APP_ENV=local` while the placeholder email backend is active (security review of 2026-10-11, I-6; not implemented, the deployment gates in the README apply instead)
* whether compiled translation files (`.mo`) are committed or compiled during deployment
* imprint (legal notice) and the content of the privacy notice (D-022)
* which functions are restricted on mobile phones in later phases
* timing and scope of later multilingual support

These points are decided only when they are actually needed for the next development step.
