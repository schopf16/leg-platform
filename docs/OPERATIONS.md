# Operations Guide for LEG-Platform

Status: draft, 2026-10-14. Steps marked **NOT YET VERIFIED** have not been run or checked on a real system.

This guide is for the operator. It explains how to move the code, how the automatic checks work, and how to set up the stage and production systems. It does not explain the program code.

## 1. Purpose and roles

Purpose: move the code safely from the development computer to GitHub and to the servers, and keep the servers running.

Roles:

* **Project owner (LEG Ittigen):** decides the scope and the risks. Approves each deployment to production.
* **Developer:** implements the code on the development computer. The tester and main session also run checks and tests there (D-019); no production data is used.
* **Operator:** the person who sets up and runs the stage and production servers. In plain words: this is the person who types the commands on the servers, keeps the secrets and makes the backups. It can be the owner or another person. The owner states (2026-10-16, D-074) that **Infomaniak** operates stage and production; which person at Infomaniak acts, what product and access are used, and who keeps the secrets is not recorded and is not assumed (`docs/DEPLOYMENT.md`, section 1).
* **Development computer:** a Windows computer for development. It uses local test settings only. It never holds stage or production secrets (`docs/DECISIONS.md`, D-020; `docs/SECURITY.md`, section 7).
* **Second computer:** a second computer that receives the code by USB. **NOT YET VERIFIED:** its setup.
* **GitHub:** stores the code and runs the automatic checks (CI). It does not deploy anything.
* **Stage:** the test system. The owner checks it here first. It behaves exactly like production (D-070): only its label `APP_ENV=stage`, its database, secrets and logs differ. It sends real emails.
* **Production:** the real system for the users.

Hosting: the Ittigen instance is planned on an Infomaniak "VPS Lite 1" (`docs/DECISIONS.md`, D-008). On 2026-10-15 the owner reported that stage has been set up; the actual configuration has not been inspected for this revision. Start every deployment with `docs/DEPLOYMENT.md`, section 0.5 (D-053, D-064): reuse the instance record and discover technical facts rather than repeating initial-setup questions. The local environment is described in `docs/STARTUP.md`.

## 2. Rules that always apply

1. Never put secrets into Git, onto the USB stick, into chat, tickets, logs or screenshots. Secrets are passwords, the secret key, database passwords, tokens and private keys.
2. Store secrets only as protected environment variables or in a secret store on the server. Never in a file inside the project folder.
3. Do not open `.env` or other secret files to read their values.
4. Do not print the environment (for example `env`, `set` or `Get-ChildItem env:`).
5. Keep stage and production apart. Use separate databases, separate accounts and separate secrets. Never reuse a secret between instances.
6. Never use production data in tests or on the development computer.
7. Do not deploy to production before the owner has checked stage and approved the deployment.
8. Do not deploy while a deployment gate in `README.md` ("Current Status") is still open (see step D1).
9. The application reads its settings only from environment variables. It does not read a `.env` file (`leg_platform/settings.py` passes `os.environ` to the loader; `.env.example`; `docs/DECISIONS.md`, D-027).
10. Change the database structure only through Django migrations. On a server they run automatically at every start (`setup_instance`, D-076); never change tables by hand.
11. Backups are the operator's job. Test a restore before the first production use (`docs/SECURITY.md`, section 9).
12. If a secret may have leaked, tell the project owner at once and replace the secret. **NOT YET VERIFIED:** the replacement steps for each secret.
13. Report a check as passed only after you have seen it pass.

## 3. Step by step

### A. Move the code by USB to the second computer

Status: **NOT YET VERIFIED**

A git bundle contains only the commits in Git. Files that are not in Git, such as `.env`, the virtual environment and any local account file, are not copied.

1. Open PowerShell in `C:\KBData\LEG-Portal`.
2. Run `git status`. Commit or set aside every change you want to keep. Uncommitted changes are not in the bundle.
3. Run `git bundle create <USB drive>\leg-platform.bundle --all`. Replace `<USB drive>` with the drive letter of the stick.
4. Run `git bundle verify <USB drive>\leg-platform.bundle`. It must report that the bundle is valid.
5. Copy only the bundle file to the stick. Do not copy the project folder.
6. On the second computer, run `git clone <USB drive>\leg-platform.bundle LEG-Portal`.
7. Create any local settings on the second computer by hand. Do not copy them from the development computer.
8. Ask the developer before you copy: a secret that was ever committed stays in the Git history, even if it was removed later.

### B. Push to GitHub

Status: **NOT YET VERIFIED** (the Git remote has not been checked)

1. Run `git remote -v`. Check the address. It must not contain a password or a token.
2. Run `git status`. Check that only the intended files are changed.
3. Run `git push`.
4. Open the Actions tab of the repository on GitHub. A CI run starts for a push to `main`.
5. Wait until both CI jobs are green. If a job fails, do not deploy. Tell the developer.
6. Check that GitHub secret scanning and push protection are on. `docs/DECISIONS.md` (D-027) records them as enabled. **NOT YET VERIFIED** on the repository today.

### C. What the automatic checks (CI) do

Status: the workflow file was read on 2026-10-14.

The file is `.github/workflows/ci.yml`.

* It starts on a push to `main` and on a pull request to `main`.
* It has one job with two variants:
  * **minimum:** Python 3.12, Django 6.0, MariaDB 10.11.
  * **latest:** Python 3.14, Django 6.1, MariaDB 12.3.
* It checks, in order: the minimum version pins, the compiled translation files, the installation, the Django version, the system checks, the deployment checks with production-style settings, missing migrations, and the full test suite.
* It does **not** deploy. It has no access to stage or production. It uses no repository secrets. Its database runs in a throwaway container.
* `.github/dependabot.yml` asks for weekly update proposals for GitHub Actions and Python packages. The developer reviews them.

### D. Deploy to stage (first)

**Routine deployment:** use `docs/DEPLOYMENT.md`, section 0.5, then section 12.3 for an existing instance. Steps D2 to D10 below describe one-time setup only; do not repeat them on an update. The detailed runbook specifies the technical execution; the gates and safety rules in this guide still apply. The operator verifies CI and server facts and runs ordinary non-destructive migrations without asking the owner to design the database.

Status: **NOT YET VERIFIED**. The install guide says that stage and production steps are documented only after the hosting is decided (`docs/INSTALLATION.md`, top of the file; `docs/DECISIONS.md`, D-008). The steps below are collected from the rules in the project documents. They are not yet tested on a server.

**D1. Check the deployment gates.** Read the list in `README.md` ("Current Status", "Deployment gates"). If any gate is open, stop. On 2026-10-14 the gates are open. The list is:

1. Microsoft Graph: the admin configuration and the test send are open.
2. The security review of the second-factor and reset flows is open.
3. Token redaction must be checked for every token URL.
4. Axes and the IP-attempt retention must be configured and scheduled.
5. The mail worker and the scheduled tasks must be running as services.

**D2. Prepare the server.** Operating system, web server with HTTPS, reverse proxy, log settings and maintenance: **open** (`docs/DECISIONS.md`, D-008). Do not guess these values. A proposed runbook with the owner decisions it needs is `docs/DEPLOYMENT.md` (D-053); it is not yet verified on a server.

**D3. Create the database and the application account.** This is the one thing the application cannot do itself (it needs the database root or the hosting panel). Create an **empty** database and its one account; nothing else is created by hand (D-076). Use the statement from `docs/INSTALLATION.md` ("Database privileges"):

```sql
CREATE DATABASE leg_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;
```

Give the application account only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX` on this one database. No global rights. No test database. The account must connect only from the application host. A split into a migration account and a runtime account is open (D-008). The host name to allow is open.

**D4. Set the configuration.** Enter the variables from section 4 as protected environment variables for the service that runs the application. How they are loaded on the server is open (`docs/DECISIONS.md`, D-027). Check that the names are set. Never print the values.

**D5. Install the code.** Use the same commit that passed CI. The installation method on the server is open.

**D6. Check the settings.** Run the same deployment check that CI runs, with the settings of this system:

```
python manage.py check --deploy --database default --fail-level WARNING
```

Stop if the command reports an error.

**D7. Apply the database structure.** Nothing to run by hand (D-076): the setup command `python manage.py setup_instance` applies the migrations at every start of the services (on a server through the unit `leg-setup.service`, `docs/DEPLOYMENT.md`, section 8.0). It runs from an **empty** database; never run `migrate` or edit tables by hand on a server.

**D8. The first system administrator.** Created automatically at the first start by the same command (D-076), from two variables in the protected environment file:

1. The person who will administer the instance writes `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (at least 15 characters) into the environment file themselves, before the first start (`docs/DEPLOYMENT.md`, section 9). The AI does not choose or see the password.
2. Start the services. The account is created only while the account table is completely empty (a first start). If accounts exist but no system administrator, nothing is created and a clear error is printed (`docs/DEPLOYMENT.md`, section 9).
3. The administrator must change the password and set up the second factor at the first login. Rights work only with a second factor (`docs/DECISIONS.md`, D-041 and D-042). **The instance must not be publicly reachable until this is done:** the initial password has no expiry, and whoever knows it and reaches the login first can take the account (`docs/DEPLOYMENT.md`, section 9).
4. **Remove `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` from the environment file after the first login and restart the services.** The start prints a reminder while it is still set; it is ignored once a system administrator exists.
5. Alternative without a password in a file: leave the password variable unset and run `python manage.py setup_initial_administrator` once in a terminal (it asks for the password with hidden input; the email variable is set for that one command only).

**D9. Start the mail worker as a supervised service.** Run `python manage.py process_mail_queue --loop`. Restart it after a failure. Without it, no email is sent. The supervisor tool is open.

**D10. Schedule the cleanup tasks.** Run `python manage.py run_scheduled_tasks` regularly. The planned interval is every 15 minutes (`docs/INSTALLATION.md`). The scheduler is open (D-008). The command bundles these cleanups: expired registrations, mail requests, login IP attempts, the security log, the counts of wrong entries in the confirmations and sessions.

**D11. Set the email mode for stage.** On stage, `EMAIL_MODE` is `graph`, exactly as in production (D-070): stage sends real emails to the addresses that are typed in, without restriction. `console` and `allowlist` are refused. Remove `EMAIL_ALLOWED_RECIPIENTS` if an earlier setup had it (a non-empty value stops the application at startup on stage and production). Testers use only their own or test addresses; a mistyped address receives real mail. The Microsoft Graph sender steps are in `docs/INSTALLATION.md`; stage has its own Graph configuration. **NOT YET VERIFIED** on a server.

**D12. Emergency command.** The command `emergency_access` is for lost second factors and blocked accounts. Use it only when the owner asks you to. The procedure is in `docs/INSTALLATION.md` and `docs/DECISIONS.md` (D-044). **NOT YET VERIFIED** on a server.

**D13. Acceptance test.** Run the checks in `docs/ACCEPTANCE.md` on stage with test accounts only. Do not use real personal data of third parties without need (D-070, `docs/DEPLOYMENT.md`, section 5.4). Stage sends real emails (D11): use only addresses that you control. Before the test, confirm the checklist of `docs/DEPLOYMENT.md`, section 5.4: a clear stage marker in `INSTANCE_NAME` and, recommended, access protection of the stage host at the reverse proxy.

**D14. Report.** Tell the owner what was done. List every step that is still NOT YET VERIFIED.

### E. Deploy to production

For an existing production instance use the same update procedure in `docs/DEPLOYMENT.md`, section 12.3, with its own instance record and explicit owner approval for the exact release accepted on stage. Do not rerun initial setup or copy stage databases, accounts or secrets. The first production installation follows the steps below.

Status: **NOT YET VERIFIED**

1. Get the owner's approval in writing after stage has been checked.
2. Check that all deployment gates in `README.md` are closed.
3. Do not copy stage data, stage secrets or stage accounts to production.
4. Repeat steps D2 to D10 with production values.
5. Set `EMAIL_MODE` to `graph`. Production allows only this mode (`leg_platform/environment.py`).
6. Run the deployment check (step D6) with the production values.
7. Test a backup and a restore before the first real use (`docs/SECURITY.md`, section 9). **NOT YET VERIFIED.**
8. Report to the owner. List every step that is still NOT YET VERIFIED.

### F. Backups and the secret key

Status: **NOT YET VERIFIED**. The backup method, schedule and retention are open.

* Back up the database.
* Keep a safe copy of `DJANGO_SECRET_KEY` and of `DJANGO_SECRET_KEY_FALLBACKS` in the secret store. The encrypted Graph private key is stored in the database. If the secret key changes without a fallback, the Graph certificate must be uploaded again (`docs/SECURITY.md`, section 7).
* Test a restore. Write down the date and the result.

## 4. Configuration checklist for stage and production

Only the names are listed here. Never write the values into a document, a ticket or the repository.

Legend: **Required** = must be set. **Optional** = may be set. **Conditional** = needed only in the case stated.

| Name | Stage | Production | What it is for |
| --- | --- | --- | --- |
| `APP_ENV` | Required | Required | Must be `stage` or `production`. Any other value stops the start. |
| `DJANGO_SECRET_KEY` | Required | Required | Secret key. At least 50 characters and at least 5 different characters. Never reuse it. |
| `DJANGO_SECRET_KEY_FALLBACKS` | Optional | Optional | Old secret keys during a key change, comma-separated. |
| `DJANGO_DEBUG` | Optional | Optional | Must be `False` or not set. `True` is refused outside `local`. |
| `DJANGO_ALLOWED_HOSTS` | Required | Required | Exact host names of the portal. No wildcards. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Optional | Optional | Trusted `https://` origins. No wildcards. |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Conditional | Conditional | Only if a proxy ends HTTPS in front of Django. Form `HTTP_<HEADER>,<value>`. |
| `DJANGO_SECURE_HSTS_SECONDS` | Optional | Optional | HSTS duration. Default one year. Can be lowered for tests, not disabled (`docs/DECISIONS.md`, D-028). |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | Optional | Optional | Operator decision. Default `False`. |
| `AXES_IPWARE_PROXY_COUNT` | Conditional | Conditional | Number of proxies in front of the portal. Verify the topology first (`docs/INSTALLATION.md`, "Trusted Proxies"). Default: **NOT YET VERIFIED**. |
| `INSTANCE_NAME` | Optional | Optional | Name of the instance. Default `LEG Platform`. At most 100 characters. |
| `INSTANCE_BASE_URL` | Required | Required | Canonical origin of the portal. `https://`, no path. Its host must be in `DJANGO_ALLOWED_HOSTS`. Used for links in email. |
| `INSTANCE_PRIVACY_URL` | Required | Required | Address of the privacy notice. Required outside `local`. |
| `INSTANCE_WEBSITE_URL` | Optional | Optional | Address of the municipality's website. |
| `EMAIL_MODE` | Required | Required | Stage and production: `graph` (identical, D-070). `console` and `allowlist` are refused. |
| `EMAIL_ALLOWED_RECIPIENTS` | Not used | Not used | Applies only to `EMAIL_MODE=allowlist`, which exists only for `local`. Do not set it on stage or production: a non-empty value stops the application at startup (D-070). |
| `EMAIL_SENDER_ADDRESS` | Optional | Optional | Sender address. Default `noreply@` plus the host of `INSTANCE_BASE_URL`. |
| `DB_NAME` | Required | Required | Name of the instance database (for example `leg_platform`). |
| `DB_USER` | Required | Required | Name of the application database account. |
| `DB_PASSWORD` | Required | Required | Password of the application database account. |
| `DB_HOST` | Optional | Optional | Database host. Default `127.0.0.1`. Encrypted transport for a remote host is open (`docs/DECISIONS.md`, D-028). |
| `DB_PORT` | Optional | Optional | Database port. Default `3306`. |
| `SECURITY_LOG_RETENTION_DAYS` | Optional | Optional | Retention of the security log in days. Default 30. |
| `SECURITY_LOG_LONG_RETENTION_DAYS` | Optional | Optional | Longer retention for selected events in days. Default 365. |
| `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` | Required once | Required once | Address of the first system administrator. Used by `setup_instance` only while the account table is completely empty, and by `setup_initial_administrator` only while no system administrator exists; may stay afterwards. |
| `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` | Required once (secret) | Required once (secret) | Initial password of the first system administrator, at least 15 characters. Used only while the account table is completely empty. It has no expiry until the first login (`docs/DEPLOYMENT.md`, section 9). **Remove it from the environment file after the first login and restart the services** (D-076). |

Notes:

* Microsoft Graph settings (sender, certificate) are not environment variables. They are entered in the Django admin. The admin configuration is still open (`README.md`, gate 1).
* The list of names follows `leg_platform/environment.py` and `leg_platform/settings.py` as read on 2026-10-14. The names were not checked against a running server.

## 5. Address register (Swiss building address directory)

The portal may check postal addresses against an official register of the Swiss federal topographic office, swisstopo (`docs/DECISIONS.md`, D-033, D-049, D-050).

Status: **built** (D-049 to D-052, D-075); not yet run against the real directory. Step A (research) is done. The owner decided the licence, the attribution, the update option and the columns on 2026-10-14.

| Topic | Status | Who decides |
| --- | --- | --- |
| Use of the data | Planned. Addresses are checked only against the local copy. The stored profile address stays exactly as entered. No address is sent to an external service. | Decided (D-049, item 4) |
| Terms of use and licence | **Decided 2026-10-14 (owner):** free use; the source must be named. Basis: the owner's copy of the swisstopo terms and the general opendata.swiss rule "Open use. Must provide the source." The STAC record says "proprietary"; this is recorded as a difference (D-049). | Owner (D-049) |
| Attribution text | **Decided 2026-10-14 (owner):** "Bundesamt für Landestopografie swisstopo". **Changed 2026-10-15 (owner, D-066):** the software shows no attribution in the user interface; the text belongs to the privacy notice and the imprint of each instance (owner content, D-022). Whether that place satisfies the terms of use is **settled by the owner on 2026-10-16** (D-074): the source reference goes into the privacy notice and the imprint, which the operator writes and maintains elsewhere; the portal only links the two addresses. | Owner (D-049, D-066, D-074) |
| Update rule | **Decided 2026-10-14 (owner): option C** (D-050). An import can be started by a user with a dedicated permission from the admin area; it runs as a background job. The operator's own import command stays available. The server needs an outbound HTTPS connection to `data.geo.admin.ch`. Security conditions apply (D-050); the security reviewer must review them. | Owner (D-050) |
| Reminder for old data | **Decided 2026-10-14 (owner):** a warning after 92 days, which matches "once per quarter or manually". | Owner (D-050) |
| Import account | **None (D-075).** There is no separate import account and no separate address database account: the directory is an own SQLite file on the server (not part of any backup) that the web application reads and the worker writes with the normal settings. Nothing to create during the initial setup; the folder comes from the service unit (`StateDirectory=`). | Decided (owner, D-075) |
| Import schedule per server | **OPEN.** | Owner (D-049, item 5) |
| Integrity check before use | Planned: compare the SHA-256 value of the downloaded file with the value in the swisstopo catalogue for the same file. Do not use the file if they differ. | Decided as a rule for step B (D-049) |
| Columns kept locally | **Decided 2026-10-14 (owner):** street, house number, postal code and locality. Nothing else is stored (D-049). | Owner (D-049) |

**What the "import account" (D-049, D-074) was, and why it does not exist any more.** The earlier plan had a second database account with write rights that only the address-import worker would use, next to a read-only account for the web application and a separate address database. The owner did not want to create accounts and said that the address directory is not important data and can simply be loaded again (2026-10-16, D-075). The directory is therefore stored in an own SQLite file on the server (the owner added that it must not be in the backup, so it is not in the main database) and the import worker uses the same settings as the web application. Nothing has to be created for it. **The file is not backed up:** the backup is the MariaDB database and the environment file. After a restore of a server, start the services; the worker makes the first automatic attempt within 24 hours at the latest (while the directory is missing or empty the attempt is due again 24 hours after the last one, instead of 90 days), or an administrator with the permission to request an import presses the button in the administration. Until the import has finished there are no address suggestions (nobody is told). The web application still never downloads or imports anything: the admin button only queues a request, and the worker `process_address_imports` does the work. After each successful import only the new and the previous version of the directory are kept (D-075).

## 5a. Settings in the administration and settings that only the operator changes

The start page of the administration ("Verwaltung") has the box "Erste Schritte" with the live status of the settings that a new instance needs: name, logo and colours ("own settings" or "server configuration"), the addresses of the privacy notice and the legal notice, the email texts (how many of the 18 are customised) and the email delivery (Microsoft Graph, ready or not). Nothing secret is shown there.

| What | Who changes it | Where |
| --- | --- | --- |
| Name of the portal, website, address of the privacy notice, address of the legal notice, the two logos (PNG or SVG), the colours (D-073) | A system administrator, or a staff member with the rights "Portal-Einstellungen: ansehen/ändern" (a role gets them at runtime; give them only to people who are responsible for the portal) | Administration, group "Inhalte", "Portal-Einstellungen" (https only; an empty field uses the server configuration; the uploaded logos belong to the database backup) |
| Host name, base URL, allowed hosts, email mode and sender, database, secrets, retention, proxy settings | Operator only, with the environment variables | Shown read only to system administrators in "Portal-Einstellungen", "Serverkonfiguration (nur lesen)"; `docs/INSTALLATION.md` |
| Texts of the emails (subject and text of each of the 20 emails) | System administrator, or a staff member with the rights "Mailtext: ansehen/ändern" | Administration, group "Inhalte", "Mailtexte" (preview, test email, earlier versions, "use the default text again") |
| Microsoft Graph (sender, certificate), security log, blocked logins, groups | System administrator only | Administration, group "System" and "Benutzer" |
| Fallback values of the name, website, logos and colours (used while the field is empty in the administration) | Operator, with environment variables and the instance static directory | `docs/INSTALLATION.md`, `docs/DESIGN.md` section 3.2 |
| **Texts of the login and registration pages** (title, lead sentence, the benefit points, the labels) | **Operator only, through the translation files** (`locale/<language>/LC_MESSAGES/django.po`, compiled with `msgfmt`, then deploy). They are not editable in the administration. | Source texts in the templates under `leg_platform/templates` and `leg_platform/accounts/templates` |

There are no ready-made groups or roles (`AGENTS.md`, section 6; D-012). A system administrator makes a group from the rights of the list; the pages "Gruppen" explain which right is needed for what. The names of the rights in the lists are German ("Mailtext: ändern").

## 6. Open items

Each item must be decided or answered before the step that depends on it.

1. **Licence and terms of the address register:** decided 2026-10-14 (section 5). Closed.
2. **Attribution:** decided 2026-10-14 (section 5); changed 2026-10-15 (D-066): not shown in the user interface, placed in the privacy notice and the imprint; settled by the owner on 2026-10-16 (D-074). Closed.
3. **Update option and 92-day reminder:** decided 2026-10-14, option C (section 5). Closed. The security review of option C is open.
4. **Import account, its name and its database alias:** closed (D-075): there is no import account and no separate alias.
5. **Import schedule per server:** open (D-049, item 5).
6. **Hosting details:** operating system and its maintenance, web server, TLS, backups, server access, the scheduler (D-008)
7. **Installation method on the server and how the configuration is loaded:** (D-027, D-008)
8. **Database account split:** one account or a migration and a runtime account; the allowed host name; encrypted transport to a remote database (D-008, D-028)
9. **Database privileges:** decided in D-049, decision 6: the application account has `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX` and no `REFERENCES` (`docs/INSTALLATION.md`). Verified locally on 2026-10-14: the full test suite (791 tests) passed with this account. Closed for the local setup; the CI minimum job on MariaDB 10.11 is the open check.
10. **Proxy count and proxy headers:** the value of `AXES_IPWARE_PROXY_COUNT` and whether `DJANGO_SECURE_PROXY_SSL_HEADER` is needed (`docs/INSTALLATION.md`)
11. **Upload size limit at the web server:** the maximum size of uploaded files, for example the Graph certificate (**NOT YET VERIFIED**)
12. **Secure channel for the first password:** how the first administrator receives the password
13. **Configuration file `config.local.json`:** this name appears nowhere in the repository (not in `docs/`, `leg_platform/`, `scripts/`, `.gitignore` or the README). Owner to confirm whether such a file is wanted. Until then, this guide does not use it.
14. **Stage environment:** whether a separate stage server will exist (`docs/DECISIONS.md`, D-008 open list)
15. **Second computer:** its setup and who uses it (**NOT YET VERIFIED**)
16. **Deployment gates:** all five gates in `README.md` are open on 2026-10-14
17. **Backup method, schedule, retention and restore test** (`docs/SECURITY.md`, section 9)
18. **Mail worker and scheduler:** choice of supervisor and scheduler on the server (D-008, D-041)
19. **Operator:** who sets up and runs stage and production (section 1). Answered by the owner on 2026-10-16: Infomaniak (D-074). Not recorded: the product, the person who acts and the access arrangements.

## 7. Sources

Read on 2026-10-14. Only the parts named here were used.

* `AGENTS.md`, `CLAUDE.md`: rules on secrets, language, verification and the security keeper role.
* `README.md`: project status, the deployment gates ("Current Status"), the open items.
* `docs/INSTALLATION.md`: local setup, "Database privileges", "Changing `SECRET_KEY`", "Trusted Proxies for Login IP Protection", the cleanup command, the note that stage and production steps are not yet documented. Local setup steps are for development only.
* `docs/SECURITY.md`: sections 3, 7, 8, 9, 10 (secrets, credentials per instance, backups, deployment checks).
* `docs/DECISIONS.md`: D-008 (hosting), D-020 (no production or stage secrets on development machines), D-027 (environment variables, public repository, CI), D-028 (settings defaults, HSTS), D-033 (address validation), D-041 and D-042 (scheduled tasks, first administrator, second factor), D-044 (emergency procedure), D-049 and D-050 (address register).
* `.github/workflows/ci.yml`: the CI jobs and steps, read in full. The workflow has no deploy step.
* `.github/dependabot.yml`: weekly update proposals.
* `.gitignore`: ignores `.env`, `.env.*` (except `.env.example`), `secrets/`, `*.secret`, `build/`, `.venv` and `venv`.
* `leg_platform/environment.py` and `leg_platform/settings.py`: names of the environment variables and their defaults. Values were not read.
* `leg_platform/accounts/management/commands/`: names of the management commands.
* `scripts/local/setup-dev-windows.ps1`: local development only. Read lines 1 to 900; the rest of the script was not read. Not used for stage or production.

**NOT YET VERIFIED:** all steps in section 3 marked as such; the Git remote; the USB process; the server procedures; the proxy topology; the default of `AXES_IPWARE_PROXY_COUNT`; the upload size limit; the secure channel for the password; the Graph sender steps; the emergency command on a server.
