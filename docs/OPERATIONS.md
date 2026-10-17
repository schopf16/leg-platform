# Operations Guide for LEG-Platform

Status: draft. Steps marked **NOT YET VERIFIED** have not been run or checked on a real system.

For the operator: moving the code, the automatic checks, and the rules and settings of stage and production. The executable runbook is `docs/DEPLOYMENT.md`.

## 1. Purpose and roles

* **Project owner (LEG Ittigen):** decides scope and risks; approves each deployment to production.
* **Developer:** works on the development computer (Windows, local test settings only, never stage or production secrets or data; D-020; `docs/SECURITY.md`, section 7); the tester and main session also run checks there (D-019). **Second computer:** receives the code by USB (**NOT YET VERIFIED**: its setup).
* **Operator:** types the commands on the servers, keeps the secrets, makes the backups. The owner states (2026-10-16, D-074) that **Infomaniak** operates stage and production; which person acts, what product and access are used and who keeps the secrets is not recorded and not assumed (`docs/DEPLOYMENT.md`, section 1).
* **GitHub:** stores the code and runs CI; it deploys nothing.
* **Stage:** the test system, checked by the owner first; behaves exactly like production and sends real emails (D-070). **Production:** the real system.

The Ittigen instance is planned on an Infomaniak "VPS Lite 1" (D-008). Start every deployment with `docs/DEPLOYMENT.md`, section 0.5 (D-053, D-064). Local environment: `docs/STARTUP.md`.

## 2. Rules that always apply

1. Never put secrets (passwords, secret key, database passwords, tokens, private keys) into Git, onto the USB stick, into chat, tickets, logs or screenshots. Keep them only as protected environment variables or in a secret store on the server, never in a file inside the project folder.
2. Do not open `.env` or other secret files to read values. Do not print the environment (`env`, `set`, `Get-ChildItem env:`).
3. Keep stage and production apart: separate databases, accounts and secrets; never reuse a secret between instances; never use production data in tests or on the development computer.
4. Do not deploy to production before the owner has checked stage and approved, nor while a deployment gate in `README.md` ("Current Status") is open.
5. The application reads its settings only from environment variables, not from a `.env` file (`.env.example`; D-027).
6. Change the database structure only through Django migrations; on a server they run at every start (`setup_instance`, D-076). Never change tables by hand.
7. Backups are the operator's job; test a restore before the first production use (`docs/SECURITY.md`, section 9).
8. If a secret may have leaked, tell the project owner at once and replace it (**NOT YET VERIFIED:** the replacement steps for each secret).
9. Report a check as passed only after you have seen it pass.

## 3. Step by step

### A. Move the code by USB to the second computer

**NOT YET VERIFIED.** A git bundle contains only commits (no `.env` or local account files); a secret that was ever committed stays in the history. `git bundle create <USB drive>\leg-platform.bundle --all`, `git bundle verify` (must report valid), on the second computer `git clone` it; create local settings by hand, never copy them.

### B. Push to GitHub

**NOT YET VERIFIED** (the remote has not been checked). `git remote -v` (no password or token in the address); `git push`. A push to `main` starts CI; if a variant fails, do not deploy and tell the developer. GitHub secret scanning and push protection should be on (D-027; **NOT YET VERIFIED**).

### C. What the automatic checks (CI) do

`.github/workflows/ci.yml` (push or pull request to `main`; minimum and latest variant) runs the version, translation, installation, system, deployment and migration checks and the test suite. It does **not** deploy, has no access to stage or production and uses no repository secrets. `.github/dependabot.yml` proposes weekly updates for the developer to review.

### D. Deploy to stage (first)

**Routine deployment of an existing instance:** `docs/DEPLOYMENT.md`, section 0.5, then section 12.3. D2 to D10 are one-time setup; do not repeat them on an update. **NOT YET VERIFIED** on a server.

**D1. Check the deployment gates** (`README.md`, "Current Status"). If any is open, stop.

**D2. Prepare the server** (operating system, web server with HTTPS, reverse proxy, logs, maintenance): `docs/DEPLOYMENT.md`, sections 2 to 7.

**D3. Create the database and the application account** (the one thing the application cannot do itself; commands: `docs/DEPLOYMENT.md`, section 3.4; privileges: `docs/INSTALLATION.md`, "Database privileges"): an **empty** database and its one account, nothing else by hand (D-076). Only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX` on this one database: no global rights, no test database, connecting only from the application host. A split into a migration and a runtime account is open (D-008).

**D4. Set the configuration:** the variables of section 4 as protected environment variables of the service; check that the names are set, never print the values. **D5. Install the code:** the commit that passed CI.

**D6. Check the settings** with the settings of this system; stop on an error: `python manage.py check --deploy --database default --fail-level WARNING`.

**D7. Apply the database structure:** `python manage.py setup_instance` migrates at every start (on a server through `leg-setup.service`, `docs/DEPLOYMENT.md`, section 8.0) and runs from an **empty** database; never run `migrate` or edit tables by hand on a server.

**D8. The first system administrator** is created at the first start by `setup_instance` (D-076; steps: `docs/DEPLOYMENT.md`, section 9): the person who will administer the instance writes `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (at least 15 characters) into the protected environment file themselves; the AI neither chooses nor sees the password. The account is created only while the account table is empty (accounts without an administrator: nothing is created, an error is printed). The administrator must change the password and set up the second factor at the first login (D-041, D-042). **The instance must not be publicly reachable until then:** the initial password has no expiry, and whoever knows it and reaches the login first can take the account. **Remove `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` from the environment file after the first login and restart the services.** Alternative without a password in a file: leave the variable unset and run `python manage.py setup_initial_administrator` once in a terminal (hidden input; the email variable set for that command only).

**D9. Mail worker** (`python manage.py process_mail_queue --loop`, supervised; without it no email is sent) and **D10. cleanup tasks** (`python manage.py run_scheduled_tasks` regularly, planned every 15 minutes; `docs/INSTALLATION.md`, "Scheduled tasks").

**D11. Email mode for stage:** `EMAIL_MODE=graph`, exactly as production (D-070; `docs/DEPLOYMENT.md`, section 5.4): stage sends real emails to the addresses typed in, so testers use only their own or test addresses; `console` and `allowlist` are refused and a non-empty `EMAIL_ALLOWED_RECIPIENTS` stops the start. Stage has its own Graph configuration (`docs/INSTALLATION.md`).

**D12. Emergency command** `emergency_access` (lost second factors, blocked accounts): only when the owner asks (`docs/INSTALLATION.md`, D-044).

**D13. Acceptance test:** `docs/ACCEPTANCE.md` on stage with test accounts only; no real personal data of third parties without need (D-070). Before it confirm the stage checklist of `docs/DEPLOYMENT.md`, section 5.4.

**D14. Report** to the owner what was done and every step still NOT YET VERIFIED.

### E. Deploy to production

An existing production instance uses `docs/DEPLOYMENT.md`, section 12.3, with its own instance record and explicit owner approval for the exact release accepted on stage; never rerun initial setup or copy stage databases, accounts or secrets. The first production installation (**NOT YET VERIFIED**):

1. Get the owner's written approval after stage was checked, and check that all deployment gates in `README.md` are closed.
2. Repeat D2 to D10 and D6 with production values (no stage data, secrets or accounts); `EMAIL_MODE=graph` is the only mode production allows (`leg_platform/environment.py`).
3. Test a backup and a restore before the first real use (`docs/SECURITY.md`, section 9). Report to the owner.

### F. Backups and the secret key

**NOT YET VERIFIED**; method, schedule and retention are open. Back up the database. Keep a safe copy of `DJANGO_SECRET_KEY` and `DJANGO_SECRET_KEY_FALLBACKS` in the secret store: the encrypted Graph private key is in the database, and if the secret key changes without a fallback the Graph certificate must be uploaded again (`docs/SECURITY.md`, section 7). Test a restore; write down date and result.

## 4. Configuration checklist for stage and production

Only names are listed; never write values into a document, ticket or the repository. **Required** = must be set; **Optional**; **Conditional** = needed only in the stated case. Stage and production are identical.

| Name | Need | What it is for |
| --- | --- | --- |
| `APP_ENV` | Required | `stage` or `production`; any other value stops the start. |
| `DJANGO_SECRET_KEY` | Required | At least 50 characters, at least 5 different; never reuse. |
| `DJANGO_SECRET_KEY_FALLBACKS` | Optional | Old keys during a key change, comma-separated. |
| `DJANGO_DEBUG` | Optional | `False` or unset; `True` is refused outside `local`. |
| `DJANGO_ALLOWED_HOSTS` | Required | Exact host names of the portal; no wildcards. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Optional | Trusted `https://` origins; no wildcards. |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Conditional | Only if a proxy ends HTTPS in front of Django; form `HTTP_<HEADER>,<value>`. |
| `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | Optional | HSTS: default one year, may be lowered for tests, not disabled (D-028); subdomains default `False` (operator decision). |
| `AXES_IPWARE_PROXY_COUNT` | Conditional | Number of proxies in front of the portal; verify the topology first (`docs/INSTALLATION.md`, "Trusted Proxies"). Default: **NOT YET VERIFIED**. |
| `INSTANCE_NAME` | Optional | Default `LEG Platform`, at most 100 characters. |
| `INSTANCE_BASE_URL` | Required | Canonical origin, `https://`, no path; its host must be in `DJANGO_ALLOWED_HOSTS`; used for links in email. |
| `INSTANCE_PRIVACY_URL` | Required | Address of the privacy notice. |
| `INSTANCE_WEBSITE_URL` | Optional | Address of the municipality's website. |
| `EMAIL_MODE` | Required | `graph`; `console` and `allowlist` are refused (D-070). |
| `EMAIL_ALLOWED_RECIPIENTS` | Not used | Local `allowlist` only; a non-empty value stops stage and production at startup (D-070). |
| `EMAIL_SENDER_ADDRESS` | Optional | Default `noreply@` plus the host of `INSTANCE_BASE_URL`. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Required | Instance database, application account and its password. |
| `DB_HOST`, `DB_PORT` | Optional | Defaults `127.0.0.1`, `3306`. Encrypted transport for a remote host is open (D-028). |
| `SECURITY_LOG_RETENTION_DAYS`, `SECURITY_LOG_LONG_RETENTION_DAYS` | Optional | Defaults 30 and 365 days. |
| `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` | Required once | First administrator's address (D8); may stay. |
| `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` | Required once (secret) | At least 15 characters; no expiry until the first login. **Remove it after the first login and restart the services** (D-076). |

Microsoft Graph settings (sender, certificate) are not environment variables; they are entered in the Django admin (`README.md`, gate 1). Names follow `leg_platform/environment.py` and `leg_platform/settings.py`; not checked against a running server. Complete list: `docs/DEPLOYMENT.md`, section 5.2.

## 5. Address register (Swiss building address directory)

Postal addresses are checked against a local copy of the official swisstopo directory (D-033, D-049, D-050, D-075; `docs/ARCHITECTURE.md`, "Data Storage"; `docs/DEPLOYMENT.md`, sections 3.5 and 10.2). Not yet run against the real directory. No address is sent to an external service.

* **Terms (owner, 2026-10-14, D-049):** free use, source named (the STAC record says "proprietary"; recorded as a difference). The software shows no attribution (D-066); the source reference belongs in the privacy notice and imprint, which the operator writes elsewhere (owner, 2026-10-16, D-074).
* **Update (option C, D-050; D-090):** a permitted user presses the import button in the administration and the web application imports (a thread of its own process; no worker needed, the page shows progress and the reason of a failure); the operator's import command and the worker stay available. The automatic refresh starts by itself at the first visit of a staff member after the due time (no scheduler). Outbound HTTPS to `data.geo.admin.ch` is needed. The SHA-256 of the downloaded file is compared with the swisstopo catalogue value; the file is not used if they differ. A warning appears after 92 days. The security review of option C is open.
* **No import account (D-075):** the directory is an own SQLite file, **not part of any backup** (the backup is the MariaDB database and the environment file). After a server restore start the services; the web application imports at the first visit of a staff member (within 24 hours), or an administrator presses the import button. Until it finished there are no address suggestions.
* **Where the logs are (D-099):** every process writes to its standard error output (journald under systemd: `journalctl -u leg-platform-web`). With `LOG_DIRECTORY` (default `/var/log/leg-platform` outside local, `~/.leg-platform-dev/logs` locally) the portal also writes `application.log` (import start, end and failure, warnings and errors) and `security.log`; a system administrator reads them live and downloads them in the administration, "Protokolldateien". The security events (including every import result) are also in the security log of the administration.
* **Run `manage.py` as the service user (D-099):** the log folder and the log files are created by the first process that starts with the settings, also by `manage.py migrate`, `createsuperuser` or `shell`. A command that an operator starts as `root` creates the folder and the files as `root`; the service user then cannot append to them (the portal falls back to the standard error output only, and the files stay without new lines). Start every `manage.py` command as the service user (`sudo -u <service user> …`); if a file belongs to the wrong owner, correct the owner of the folder and of both files (`chown -R <service user> <LOG_DIRECTORY>`, folder mode 0700) and restart the services. Not tested on a server.
* **Import schedule per server: OPEN** (D-052, D-075).

## 5a. Settings in the administration and settings that only the operator changes

The start page of the administration ("Verwaltung") has the box "Erste Schritte" with the live status of the settings a new instance needs; nothing secret is shown.

| What | Who changes it | Where |
| --- | --- | --- |
| Name, website, privacy and legal notice addresses, the two logos, the colours (D-073; table: `docs/DESIGN.md`, section 3.2) | A system administrator, or staff with "Portal-Einstellungen: ansehen/ändern" (a role gets them at runtime) | Administration, group "Inhalte", "Portal-Einstellungen" (an empty field uses the server configuration; uploaded logos are in the database backup) |
| Host name, base URL, allowed hosts, email mode and sender, database, secrets, retention, proxy settings; fallback values of the presentation | Operator only, via environment variables and the instance static directory (`docs/INSTALLATION.md`) | Read-only for system administrators in "Portal-Einstellungen", "Serverkonfiguration (nur lesen)" |
| Texts of the emails (each of the 21 emails) | System administrator, or staff with "Mailtext: ansehen/ändern" | Administration, "Inhalte", "Mailtexte" |
| Microsoft Graph, security log, blocked logins, groups | System administrator only | Administration, groups "System" and "Benutzer" |
| **Texts of the login and registration pages** | **Operator only, through the translation files** (`locale/<language>/LC_MESSAGES/django.po`, compiled with `msgfmt`, then deploy) | Not editable in the administration |

There are no ready-made groups or roles (`AGENTS.md`, section 6; D-012): a system administrator makes a group from the rights of the list (German names, e.g. "Mailtext: ändern").

## 6. Open items

Each must be decided or answered before the step that depends on it.

1. **Import schedule per server** (D-052, D-075); security review of address update option C.
2. **Hosting details** (operating system, web server, TLS, scheduler, installation method; D-008, D-027): mostly specified by `docs/DEPLOYMENT.md`, not verified on a server.
3. **Database account split** (one account or a migration and a runtime account), allowed host name, encrypted transport to a remote database (D-008, D-028); the CI minimum job on MariaDB 10.11 is the open check of the privilege set.
4. **Proxy count and proxy headers:** `AXES_IPWARE_PROXY_COUNT`, `DJANGO_SECURE_PROXY_SSL_HEADER` (`docs/INSTALLATION.md`).
5. **Upload size limit at the web server** (e.g. the Graph certificate; `docs/DEPLOYMENT.md`, section 7.3).
6. **Secure channel for the first password:** how the first administrator receives it.
7. **`config.local.json`:** appears nowhere in the repository; the owner is to confirm whether such a file is wanted; until then this guide does not use it.
8. **Stage environment:** whether a separate stage server exists (D-008). **Second computer:** its setup and who uses it.
9. **Backup method, schedule, retention and restore test** (`docs/SECURITY.md`, section 9); supervision of mail worker and scheduler (D-008, D-041).
10. **Operator:** Infomaniak (D-074); not recorded: the product, the person who acts and the access arrangements. Deployment gates: `README.md`.

**NOT YET VERIFIED** additionally: the default of `AXES_IPWARE_PROXY_COUNT`, the Graph sender steps, the emergency command on a server.

## 7. Sources

`AGENTS.md`, `README.md` (gates), `docs/INSTALLATION.md`, `docs/SECURITY.md` (sections 3, 7 to 10), the decisions cited inline (`docs/DECISIONS.md`), `.github/workflows/ci.yml`, `.github/dependabot.yml`, `leg_platform/environment.py` and `leg_platform/settings.py` (names and defaults of the variables; values were not read).
