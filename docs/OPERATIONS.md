# Operations Guide for LEG-Platform

Status: draft. **NOT YET VERIFIED** = not run or checked on a real system.

For the operator: moving the code, automatic checks, rules and settings of stage and production. The executable runbook is `docs/DEPLOYMENT.md`; this guide does not repeat its commands.

## 1. Purpose and roles

* **Project owner (LEG Ittigen):** decides scope and risks; approves each deployment to production.
* **Developer:** development computer only (Windows, local test settings, never stage or production secrets or data; D-020; `docs/SECURITY.md`, section 7). Tester and main session also run checks there (D-019).
* **Second computer:** receives the code by USB. Its setup is **NOT YET VERIFIED**.
* **Operator:** types the commands on the servers, keeps the secrets, makes the backups. The owner states (2026-10-16, D-074) that **Infomaniak** operates stage and production. Not recorded, not assumed: person, product, access, who keeps the secrets (`docs/DEPLOYMENT.md`, section 1).
* **GitHub:** stores the code and runs CI; deploys nothing.
* **Stage:** test system, checked by the owner first; behaves like production and sends real emails (D-070). **Production:** the real system.

The Ittigen instance is planned on an Infomaniak "VPS Lite 1" (D-008). Start every deployment with `docs/DEPLOYMENT.md`, section 0.5 (D-053, D-064). Local environment: `docs/STARTUP.md`.

## 2. Rules that always apply

1. Never put secrets (passwords, secret key, database passwords, tokens, private keys) into Git, onto the USB stick, into chat, tickets, logs or screenshots. Keep them only as protected environment variables or in a secret store on the server, never in a file inside the project folder.
2. Do not open `.env` or other secret files to read values. Do not print the environment (`env`, `set`, `Get-ChildItem env:`).
3. Keep stage and production apart: separate databases, accounts and secrets; never reuse a secret; never use production data in tests or on the development computer.
4. No production deployment before the owner has checked stage and approved, nor while a deployment gate in `README.md` ("Current Status") is open.
5. The application reads settings only from environment variables, not from `.env` (`.env.example`; D-027).
6. Change the database structure only through Django migrations. On a server they run at every start (`setup_instance`, D-076). Never change tables by hand.
7. Backups are the operator's job. Test a restore before the first production use (`docs/SECURITY.md`, section 9).
8. A secret that may have leaked: tell the project owner at once and replace it (**NOT YET VERIFIED:** the replacement steps for each secret).
9. Report a check as passed only after you have seen it pass.

## 3. Step by step

### A. Move the code by USB to the second computer

**NOT YET VERIFIED.** A git bundle holds only commits (no `.env`, no local account files). A secret that was ever committed stays in the history.

1. `git bundle create <USB drive>\leg-platform.bundle --all`
2. `git bundle verify` (must report valid).
3. On the second computer: `git clone` the bundle. Create local settings by hand; never copy them.

### B. Push to GitHub

**NOT YET VERIFIED** (remote not checked): `git remote -v` (no password or token in the address), `git push`. A push to `main` starts CI; if a variant fails, do not deploy and tell the developer. GitHub secret scanning and push protection should be on (D-027; **NOT YET VERIFIED**).

### C. What the automatic checks (CI) do

`.github/workflows/ci.yml` (push or pull request to `main`; minimum and latest variant) runs the version, translation, installation, system, deployment and migration checks and the test suite. It does **not** deploy, has no access to stage or production, uses no repository secrets. `.github/dependabot.yml` proposes weekly updates for the developer to review.

### D. Deploy to stage (first)

Routine deployment of an existing instance: `docs/DEPLOYMENT.md`, section 0.5, then 12.3. D2 to D10 are one-time setup; do not repeat them on an update. **NOT YET VERIFIED** on a server.

* **D1.** Check the deployment gates (`README.md`, "Current Status"). Any open: stop.
* **D2. Server** (OS, web server with HTTPS, reverse proxy, logs, maintenance): `docs/DEPLOYMENT.md`, sections 2 to 7.
* **D3. Database and application account** (the one thing the application cannot do itself). Commands: `docs/DEPLOYMENT.md`, section 3.4. Privileges: `docs/INSTALLATION.md`, "Database privileges". An **empty** database and its one account, nothing else by hand (D-076). Only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX` on this one database; no global rights, no test database, connect only from the application host. A migration/runtime account split is open (D-008).
* **D4. Configuration:** the variables of section 4 as protected environment variables of the service. Check that the names are set; never print the values.
* **D5. Code:** the commit that passed CI.
* **D6. Settings check** (stop on an error): `python manage.py check --deploy --database default --fail-level WARNING`.
* **D7. Database structure:** `python manage.py setup_instance` migrates at every start (on a server through `leg-setup.service`, `docs/DEPLOYMENT.md`, section 8.0) and runs from an **empty** database. Never run `migrate` or edit tables by hand on a server. The address directory file is the exception only as described in `docs/INSTALLATION.md` (additive in-place migrations run by `setup_instance`: one to two minutes, about 410 MB more, free disk needed; the lookups are unavailable meanwhile).
* **D8. First system administrator** (created by `setup_instance` at the first start, D-076; steps: `docs/DEPLOYMENT.md`, section 9). The person who will administer the instance writes `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (at least 15 characters) into the protected environment file; the AI neither chooses nor sees it. Created only while the account table is empty (accounts without an administrator: nothing is created, an error is printed). First login: change the password, set up the second factor (D-041, D-042). **The instance must not be publicly reachable until then** (the initial password has no expiry). **Remove `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` after the first login and restart the services.** Alternative without a password in a file: leave it unset and run `python manage.py setup_initial_administrator` once in a terminal (hidden input; the email variable set for that command only).
* **D9. Mail worker:** `python manage.py process_mail_queue --loop`, supervised. Without it no email is sent.
* **D10. Cleanup tasks:** `python manage.py run_scheduled_tasks` regularly, planned every 15 minutes (`docs/INSTALLATION.md`, "Scheduled tasks").
* **D11. Email mode for stage:** `EMAIL_MODE=graph`, as production (D-070; `docs/DEPLOYMENT.md`, section 5.4). Stage sends real emails: testers use only their own or test addresses. `console` and `allowlist` are refused; a non-empty `EMAIL_ALLOWED_RECIPIENTS` stops the start. Stage has its own Graph configuration (`docs/INSTALLATION.md`).
* **D12. Emergency command** `emergency_access` (lost second factors, blocked accounts): only when the owner asks (`docs/INSTALLATION.md`, D-044).
* **D13. Acceptance test:** `docs/ACCEPTANCE.md` on stage with test accounts only; no real personal data of third parties without need (D-070). Before it confirm the stage checklist of `docs/DEPLOYMENT.md`, section 5.4.
* **D14. Report** to the owner what was done and every step still NOT YET VERIFIED.

### E. Deploy to production

An existing production instance: `docs/DEPLOYMENT.md`, section 12.3, with its own instance record and explicit owner approval for the exact release accepted on stage. Never rerun initial setup; never copy stage databases, accounts or secrets. First production installation (**NOT YET VERIFIED**):

1. Owner's written approval after stage was checked; all deployment gates in `README.md` closed.
2. Repeat D2 to D10 and D6 with production values (no stage data, secrets or accounts). `EMAIL_MODE=graph` is the only mode production allows (`leg_platform/environment.py`).
3. Test a backup and a restore before the first real use (`docs/SECURITY.md`, section 9). Report to the owner.

### F. Backups and the secret key

**NOT YET VERIFIED.** Method, schedule and retention are open. Back up the database. Keep a safe copy of `DJANGO_SECRET_KEY` and `DJANGO_SECRET_KEY_FALLBACKS` in the secret store: the encrypted Graph private key is in the database; if the secret key changes without a fallback, upload the Graph certificate again (`docs/SECURITY.md`, section 7). Test a restore; write down date and result. Commands: `docs/DEPLOYMENT.md`, section 12.

## 4. Configuration checklist for stage and production

Names only; never write values into a document, ticket or the repository. Stage and production are identical. Full list, defaults and values: `docs/DEPLOYMENT.md`, section 5.2 and `.env.example`.

* **Required:** `APP_ENV` (`stage` or `production`), `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `INSTANCE_BASE_URL`, `INSTANCE_PRIVACY_URL`, `EMAIL_MODE` (`graph`), `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
* **Required once:** `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` (may stay), `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (secret; remove it after the first login and restart the services, D-076).
* **Conditional:** `DJANGO_SECURE_PROXY_SSL_HEADER` (only if a proxy ends HTTPS in front of Django; form `HTTP_<HEADER>,<value>`); `AXES_IPWARE_PROXY_COUNT` (number of proxies; verify the topology first, `docs/INSTALLATION.md`, "Trusted Proxies"; default **NOT YET VERIFIED**).
* **Optional:** `DJANGO_SECRET_KEY_FALLBACKS`, `DJANGO_DEBUG` (`False` or unset; `True` is refused outside `local`), `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` (HSTS default one year; may be lowered for tests, not disabled; subdomains default `False`; D-028), `INSTANCE_NAME` (default `LEG Platform`, at most 100 characters), `INSTANCE_WEBSITE_URL`, `EMAIL_SENDER_ADDRESS` (default `noreply@` plus the host of `INSTANCE_BASE_URL`), `DB_HOST`, `DB_PORT` (defaults `127.0.0.1`, `3306`; encrypted transport for a remote host is open, D-028), `SECURITY_LOG_RETENTION_DAYS`, `SECURITY_LOG_LONG_RETENTION_DAYS` (defaults 30 and 365 days).
* **Not read any more (D-116):** `EMAIL_ALLOWED_RECIPIENTS`, `EMAIL_SENDER_ADDRESS`, `CONTACT_EMAIL_ADDRESS`; a value stops the start. The sender and the contact address are set in the administration, chapter "Email".
* `DJANGO_SECRET_KEY`: at least 50 characters, at least 5 different. `DJANGO_ALLOWED_HOSTS`: exact host names, no wildcards. `INSTANCE_BASE_URL`: `https://` origin without path, host in `DJANGO_ALLOWED_HOSTS`.
* Microsoft Graph settings (sender, certificate) are entered in the Django admin, not in the environment (`README.md`, gate 1).

Names follow `leg_platform/environment.py` and `leg_platform/settings.py`; not checked against a running server.

## 5. Address register (Swiss building address directory)

Postal addresses are checked against a local copy of the official swisstopo directory (D-033, D-049, D-050, D-075; `docs/ARCHITECTURE.md`, "Data Storage"; `docs/DEPLOYMENT.md`, sections 3.5, 8.4 and 10.2). Not yet run against the real directory. No address leaves the server.

* **Terms (owner, 2026-10-14, D-049):** free use, source named (the STAC record says "proprietary"; recorded as a difference). No attribution in the software (D-066). The source reference belongs in the operator's privacy notice and imprint (D-074).
* **Update (option C, D-050, D-090):** a permitted user presses the import button in the administration; the web application imports in a thread of its own process (no worker needed). The automatic refresh is the daily run of the scheduled tasks (`scheduled_address_import`: update flag, no building numbers yet, regular refresh), once per day from the configured time (default 03:00); a staff visit starts nothing. Needs outbound HTTPS to `data.geo.admin.ch` (import) and `api3.geo.admin.ch` (building search, `docs/SERVICES.md` S-4). SHA-256 compared with the catalogue; a difference rejects the file. Warning after 92 days. The security review of option C is open.
* **No import account (D-075/D-076):** an own disposable public-data SQLite file, **not part of any backup**. After a restore or address-schema change, startup creates/rebuilds it and durably queues the official import in MariaDB, without a startup download. The optional worker or next scheduled-task tick consumes the queue, independent of daily refresh time or automatic-refresh policy. Without a consumer the job stays queued. Until successful import no address validation/suggestions; application data is untouched. Busy readers/importers or reset errors defer this optional step non-fatally; retry setup later. Details and recovery: `docs/INSTALLATION.md`, "Address Directory".
* **Logs (D-099):** standard error (journald: `journalctl -u leg-platform-web`); with `LOG_DIRECTORY` (default `/var/log/leg-platform` outside local, `~/.leg-platform-dev/logs` locally) also `application.log` and `security.log`, readable and downloadable by a system administrator in "Protokolldateien". Import results are also in the security log.
* **Run `manage.py` as the service user (D-099):** `sudo -u <service user> …`. Any command that starts the settings (`manage.py migrate`, `createsuperuser`, `shell`) creates the log folder and files. Started as `root`, they belong to `root` and the service user can then not append. Fix: `chown -R <service user> <LOG_DIRECTORY>` (folder mode 0700), restart the services. Not tested on a server.
* **Import schedule per server: OPEN** (D-052, D-075).

## 5a. Settings in the administration and settings that only the operator changes

The administration start page ("Verwaltung") has the box "Erste Schritte" with the live status of the settings a new instance needs. Nothing secret is shown.

| What | Who changes it | Where |
| --- | --- | --- |
| Name, website, privacy and legal notice addresses, the two logos, the favicon, the colours (D-073; table: `docs/DESIGN.md`, section 3.2) | A system administrator, or staff with "Portal-Einstellungen: ansehen/ändern" (a role gets them at runtime) | Administration, group "Inhalte", "Portal-Einstellungen" (an empty field uses the server configuration; uploaded logos and favicon are in the database backup) |
| Host name, base URL, allowed hosts, email mode, database, secrets, retention, proxy settings; fallback values of the presentation | Operator only, via environment variables and the instance static directory (`docs/INSTALLATION.md`) | Read-only for system administrators in "Portal-Einstellungen", "Serverkonfiguration (nur lesen)" |
| Texts of the emails (each of the 21 emails) | System administrator, or staff with "Mailtext: ansehen/ändern" | Administration, "Inhalte", "Mailtexte" |
| Microsoft Graph, security log, blocked logins, groups | System administrator only | Administration, groups "System" and "Benutzer" |
| **Texts of the login and registration pages** | **Operator only, through the translation files** (`locale/<language>/LC_MESSAGES/django.po`, compiled with `msgfmt`, then deploy) | Not editable in the administration |

There are no ready-made groups or roles (`AGENTS.md`, section 6; D-012). A system administrator makes a group from the rights of the list (German names, e.g. "Mailtext: ändern").

## 6. Open items

Decide or answer each before the step that depends on it.

1. **Import schedule per server** (D-052, D-075); security review of address update option C.
2. **Hosting details** (OS, web server, TLS, scheduler, installation method; D-008, D-027): mostly specified by `docs/DEPLOYMENT.md`, not verified on a server.
3. **Database account split** (one account or a migration and a runtime account), allowed host name, encrypted transport to a remote database (D-008, D-028). The CI minimum job on MariaDB 10.11 is the open check of the privilege set.
4. **Proxy count and proxy headers:** `AXES_IPWARE_PROXY_COUNT`, `DJANGO_SECURE_PROXY_SSL_HEADER` (`docs/INSTALLATION.md`).
5. **Upload size limit at the web server** (e.g. the Graph certificate; `docs/DEPLOYMENT.md`, section 7.3).
6. **Secure channel for the first password:** how the first administrator receives it.
7. **`config.local.json`:** appears nowhere in the repository. The owner confirms whether such a file is wanted; until then this guide does not use it.
8. **Stage environment:** whether a separate stage server exists (D-008). **Second computer:** its setup and who uses it.
9. **Backup method, schedule, retention and restore test** (`docs/SECURITY.md`, section 9); supervision of mail worker and scheduler (D-008, D-041).
10. **Operator:** Infomaniak (D-074). Not recorded: the product, the person who acts, the access arrangements. Deployment gates: `README.md`.

**NOT YET VERIFIED** additionally: the default of `AXES_IPWARE_PROXY_COUNT`, the Graph sender steps, the emergency command on a server.

## 7. Sources

`AGENTS.md`, `README.md` (gates), `docs/INSTALLATION.md`, `docs/SECURITY.md` (sections 3, 7 to 10), the decisions cited inline (`docs/DECISIONS.md`), `.github/workflows/ci.yml`, `.github/dependabot.yml`, `leg_platform/environment.py` and `leg_platform/settings.py` (names and defaults of the variables; values were not read).
