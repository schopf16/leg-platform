# Installation

Local development setup and instance configuration. Stage and production: `docs/DEPLOYMENT.md`, section 0.5. Start, check, stop the local environment: `docs/STARTUP.md`. Roles, rules, CI: `docs/OPERATIONS.md`.

## Instance Presentation

The administrator maintains name, website, privacy and legal notice addresses, two logos, the favicon and the colours in the administration ("Inhalte", "Portal-Einstellungen"; D-073). A stored value wins. An empty field falls back to the variables of `.env.example` (D-051), then to the neutral platform defaults. Environment only (read-only for system administrators): host name, `INSTANCE_BASE_URL`, allowed hosts, mail mode and sender, Graph credentials, database, secrets, retention minimums, proxy settings (an editable host name would let anyone redirect all email links).

**Logos and favicon (D-073).**

* One logo for light mode, one for dark mode (both or none). PNG (up to 1 MB, 64 to 4000 pixels per side) or SVG (up to 200 KB, no scripts, styles, images, links or editor data).
* Favicon: one optional file for both modes, PNG (up to 128 KB, 64 to 4000 pixels per side) or SVG (up to 64 KB); no ICO. Without an upload the built-in icon is used.
* Stored **in the database** (`accounts_portalsettings`): they belong to the database backup.
* Served publicly at `/branding/logo/light/`, `/branding/logo/dark/` and `/branding/favicon/`. The proxy passes these paths and may cache them for a day. It allows a request body of at least 3 MiB for `/admin/accounts/portalsettings/1/change/` (larger: 413 from the application).
* Fallback while none is uploaded: `INSTANCE_LOGO_LIGHT`, `INSTANCE_LOGO_DARK` (relative static `.svg` or `.png` paths, never absolute or external). `INSTANCE_STATIC_DIRECTORY`: an existing absolute directory outside the repository; its contents are public (no configuration, personal data or secrets). Check every SVG for scripts, event handlers and external references. Run `collectstatic` with the instance's static directories. Startup validation does not prove that a file exists.

Colours are six-digit hexadecimal values. The application refuses to start if the accent lacks 4.5:1 contrast with its text, the page background or the surface, or highlight text lacks contrast. The administration form applies the same rules; a failing database value is not used (warning logged). Platform defaults: accent `#005BA2` and `#6AA9E0`, highlight `#DDDD22` (D-056).

The texts of the login and registration pages change only in `locale/<language>/LC_MESSAGES/django.po` (compile with `msgfmt`, deploy). The administration start page shows what still has to be set (box "Erste Schritte"; `docs/OPERATIONS.md`, section 5a).

**Grid operator (D-086):** a system administrator enters the name once in the administration ("Inhalte", "Netzbetreiber"). It is instance data, never part of code, migrations or the environment file. The transformer circuit number format (`TRA` and digits) is that of the current operator; another operator needs `numbers.TRANSFORMER_CIRCUIT_NUMBER` adapted.

Supported browsers (D-003): Chrome and Edge 123, Firefox 128, Safari 17.5, Opera 109 or newer.

## Address Directory

Profile addresses never leave the application; suggestions query only a local copy of the official directory. The UI shows no source and no warning (D-066). The swisstopo source reference belongs to the privacy notice and imprint (D-049, D-022). A missing, stale or unavailable directory is shown on the operator page "Address directory" only; it never blocks registration or saving.

* Own SQLite file (D-075), **not part of any backup**. Optional `ADDRESS_DIRECTORY_FILE`: absolute path outside the repository, no control characters.
* Default outside `APP_ENV=local`: `/var/lib/leg-platform/address-directory.sqlite3` (`StateDirectory=leg-platform` in the systemd units). With `APP_ENV=local`: `~/.leg-platform-dev/address-directory.sqlite3`.
* A missing file is created with mode 0600 (folder 0700). Processes open it read-only unless they are writers (worker, `import_address_directory`, `seed_local_address_directory`, `setup_instance`, `migrate`; never the web processes). Web and worker run on the same host with the same settings. No network file system.
* `python manage.py setup_instance` (below) creates the file and tables. Local development only: `python manage.py migrate addresses --database=addresses` (`migrate` alone never creates them). On a server never run it by hand (D-076): start `leg-setup.service`; a hand-run as `root` would create a root-owned 0600 file the service cannot open (`docs/DEPLOYMENT.md`, 10.2, "Repair").
* **Schema changes (D-075, D-076):** public catalogue rows are disposable. `setup_instance` creates a missing file or rebuilds a file whose schema differs from the models, then queues the official import in MariaDB. It never downloads during setup and never deletes application data. A current clean schema is not requeued. Normal CSV refresh is generation-based and atomic; it never deletes the file.
* **In-place migrations** (`Migration.in_place = True`: `addresses.0020`, `0021`; D-111). They add the tables `AddressPlace` and `AddressStreet` and the index `addr_house` (the file is about 1.7 GB for 3.3 million addresses). `setup_instance` applies them to the existing file (`leg_platform/addresses/setup.py`): no re-download, no import.
  * Needs free disk of about 35 % of the file size twice (new tables and indexes, WAL) plus 256 MB. Otherwise the migration is skipped with a message, setup goes on, and the lookups answer "unavailable" until setup is run again.
  * Refused while an import is alive. An incomplete schema afterwards is an error; the file is not wiped.
  * By hand before a deployment: `python manage.py migrate addresses --database=addresses` (stop the import first).
  * Migrations of application-side job/lookup models only do not touch the file.
* **Availability:** the optional worker processes the queued request on its next poll; otherwise `run_scheduled_tasks` consumes it at its next tick (every 15 minutes on the server), independent of the daily refresh time and `ADDRESS_IMPORT_AUTOMATIC`. Without a consumer it stays queued. Until an import succeeds, address validation and suggestions are unavailable; stored data remains. A busy file, live import or rebuild failure is reported non-fatally; run setup again later, preferably with address consumers stopped. Never delete the `.reset` marker to bypass a failed rebuild. Corruption repair: `docs/DEPLOYMENT.md`, section 10.2.

**The button needs no extra service (D-090); the automatic refresh is the daily run of `run_scheduled_tasks`.** Procedure, limits, recovery: `docs/DEPLOYMENT.md`, sections 8.3, 8.4 and 10.2.

* Triggers: an administrator with the right `addresses.request_directory_import` presses "Request directory update" (administration, "Adressverzeichnis"; at once, in a thread of the web process); or the daily run (`scheduled_address_import`, once per day from the configured time, default 03:00) finds an update due: the update flag is set (an address was found neither locally nor at swisstopo), the directory has no building numbers (EGID) yet, the regular refresh is due (every 90 days) or the directory is missing or empty (every 24 hours). At most one download per day. A visit of a staff member starts nothing. `ADDRESS_IMPORT_AUTOMATIC=False` turns the daily run off.
* Needs: outbound HTTPS to `data.geo.admin.ch` (import; web and tasks unit) and `api3.geo.admin.ch` (building search of signed-in persons; web unit). At least 4 GiB free on the disk of the directory file at the start of an import (the archive is written there as a temporary file, not to the system temporary folder). The file is about 1.7 GB per generation and holds up to three while importing (about 5.1 GB): reserve 10 GB for the folder. The tasks unit needs the state directory like the web unit (`docs/DEPLOYMENT.md`, 8.3).
* Verifies the official SHA-256 multihash before parsing. One import at a time (the job row is the claim; the lease expires after 6 hours). A web job without a sign of life for about 90 seconds counts as dead. A hard-killed worker job (state stays `running`) is taken over only after its lease: never reset its state blindly.
* Start, end and failure go to the application log (`leg_platform.addresses`) and the security log. If the published directory is the official file and passes the integrity check, a manual request ends without a download (D-076).

Optional supervised worker (same environment as the web application; claims the same job). It handles queued manual imports and makes the automatic attempt (D-052). It refuses to run with less than 4 GB free where the file lives:

```powershell
python manage.py process_address_imports
```

Manual import:

```powershell
python manage.py import_address_directory
```

**Logs (D-099).** The portal writes to the standard error output of its processes (journald under systemd) and, with `LOG_DIRECTORY`, to `application.log` and `security.log` in that folder. Default locally `~/.leg-platform-dev/logs`; outside local `/var/log/leg-platform` (needs `LogsDirectory=leg-platform` in the systemd units); `LOG_DIRECTORY=off` writes no files. System administrators read, watch live and download them in the administration ("Protokolldateien"). The files are bounded (10 MB, 5 older files each) and readable by the service user only.

`POST /addresses/lookup/` accepts at most 4096 bytes of body. The web server or reverse proxy must also limit request bodies for the whole instance (for example `client_max_body_size` in Nginx), because Django's limits apply only after the request has been received; **this limit is mandatory before any stage or production deployment** (`docs/DEPLOYMENT.md`, section 7.3). `manage.py seed_local_address_directory` is for local development only (`docs/STARTUP.md`, section 10); never use it for stage or production.

## Local Development

For Windows development machines (D-027, D-030). Never use these steps or credentials for stage or production. The database is MariaDB (D-002). Chocolatey 2 or later is required.

### Step 1: Remove the old MySQL installation

Only where a MySQL installation from the former setup script exists (folders `C:\tools\mysql` and `C:\ProgramData\MySQL\data`, service `MySQL`). The setup script refuses to start while these exist and prints the exact commands, adapted to what it finds. Run them in PowerShell **as administrator** with no other user signed in.

1. The first command checks that none of the folders is a link (symbolic link, junction, mount point). If it lists one: run nothing else and ask for help.
2. The others stop and disable the service, run `choco uninstall mysql -y`, delete the service and the folders with `[IO.Directory]::Delete(<path>, $true)` (does not follow links inside).
3. All old MySQL databases are lost. Errors for parts that no longer exist are expected.
4. If the service `MySQL` belongs to another MySQL installation, the script warns instead of printing service commands. Do not stop or delete that service.

### Step 2: Run the setup

1. Check as administrator that the Windows Firewall is on and blocks inbound connections for all profiles, and that no rule allows inbound connections to the database or to file sharing (SMB, port 445):

   ```powershell
   Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction
   Get-NetFirewallPortFilter -PolicyStore ActiveStore | Where-Object { $_.LocalPort -in '3306', '445' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallApplicationFilter -PolicyStore ActiveStore | Where-Object { $_.Program -like '*mysqld*' -or $_.Program -like '*mariadbd*' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Action Allow -Enabled True | Where-Object { $port = $_ | Get-NetFirewallPortFilter; $app = $_ | Get-NetFirewallApplicationFilter; $svc = $_ | Get-NetFirewallServiceFilter; -not $_.Owner -and -not $app.Package -and $app.Program -eq 'Any' -and $svc.Service -eq 'Any' -and $port.Protocol -in 'Any', 'TCP' -and $port.LocalPort -eq 'Any' } | Select-Object DisplayName, Profile
   ```

   * First command: `Enabled True` and `DefaultInboundAction Block` for every profile. The others: nothing (the fourth lists inbound allow rules not restricted to a port, program, service or Store app).
   * The second lists "File and Printer Sharing" rules: disable them during the setup or use a trusted network.
   * Limit: none evaluates remote address restrictions, other protocols or rule order.
   * Windows asks whether `mysqld` may communicate on networks: deny.

2. Open a 64-bit PowerShell **as administrator** in the project directory:

   ```powershell
   .\scripts\local\setup-dev-windows.ps1
   ```

   * The script first asks for a new password for the local MariaDB `root` accounts: at least 15 characters, printable ASCII only, none of `#`, `"`, `'`, `\` and `;`, no leading or trailing space. Keep it in your password manager.
   * It refuses to start if the old MySQL, a service named `MySQL` or `MariaDB`, a MariaDB installation, or `C:\my.ini`, `C:\my.cnf`, `%WINDIR%\my.ini`, `%WINDIR%\my.cnf` exists.
   * It installs Python 3.14 and MariaDB 12.3 (`mariadb.install`) and starts MariaDB **without network access** (`SKIPNETWORKING=1`; only the named pipe `MySQL`) without giving the root password to the installer. It disables (not deletes) the installer's firewall rule for `mysqld.exe` and hardens MariaDB. It creates the database `leg_platform` and the account `leg_platform`@`127.0.0.1` ("Database privileges"). It switches MariaDB to `127.0.0.1:3306` only with the named pipe off. It stores `APP_ENV=local`, `DB_*` and `DJANGO_SECRET_KEY` as user environment variables without printing them.
   * An upgrade or repair of MariaDB (`choco upgrade mariadb.install`) may enable the firewall rule again: check with the third firewall command and `Disable-NetFirewallRule` it.
   * **Phase without a root password:** until the script sets the password, MariaDB runs with a passwordless `root`, reachable by any program on the computer and by users of other computers over file sharing (SMB). Run the script only when no untrusted user or program is active and with file sharing blocked (firewall check).

   User environment variables are inherited by every process of the Windows user, including AI tools. Never print the environment (`Get-ChildItem env:`, `set`, `env`); it contains `DB_PASSWORD` and `DJANGO_SECRET_KEY` (D-027).

### Step 3: Restart VS Code

Close and reopen VS Code and every terminal to receive the new environment variables (all variables: `.env.example`). Then create the virtual environment and install the project with the newest compatible packages (D-015):

```powershell
py -3.13 -m venv .venv        # any Python from 3.12 to 3.14
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install --upgrade -e .
```

Checks, tests, development server, test accounts, address directory seed: `docs/STARTUP.md` (`check` and `makemigrations` need the running MariaDB).

Email: locally emails go to the console (`EMAIL_MODE=console`); Graph is refused locally (D-116).

### Instance Configuration

Each instance sets its values in the environment (`.env.example`, D-013, D-037). They are validated at startup; invalid values prevent the start:

* `INSTANCE_NAME`: shown in pages and email subjects (optional, default "LEG Platform"; the administration value wins, D-073).
* `INSTANCE_BASE_URL`: the address of the portal (links in emails and WebAuthn), an `https://` origin without path, e.g. `https://portal.example.org`. Its host must be in `DJANGO_ALLOWED_HOSTS`. Required outside `APP_ENV=local`; locally it defaults to `http://127.0.0.1:8000`.
* Sender of emails and address for the messages of the contact form: the chapter "Email" of the administration (`GraphConfiguration`, D-116); the server does not read them. Without a sender no email is sent.
* `INSTANCE_PRIVACY_URL`: absolute `https://` URL of the privacy notice. Required outside `APP_ENV=local`; the administration value wins.
* `INSTANCE_WEBSITE_URL`: absolute `https://` URL of the website (optional fallback).

Email delivery (D-038):

* The worker `python manage.py process_mail_queue --loop` must run as a supervised service (restart on failure). Without it no email is sent. Without `--loop` it processes the queue once. A job is tried three times, then given up. Unprocessed jobs are deleted after one hour (they hold the address in plain text).
* The sender domain needs SPF, DKIM and DMARC records. The sender address must be one the service may use.
* The Graph backend sends no `Auto-Submitted` header (D-048). Automatic replies (out-of-office) of the sender mailbox must stay switched off (D-006).

### Microsoft Graph sender

D-006. The operator does these steps in Microsoft Entra ID and Exchange Online. Configuration and test send: the chapter "Email" of the Django administration (system administrators only): tenant ID, client ID, sender address, contact address, certificate and private key together (the key is never shown again). Source: Microsoft Learn, RBAC for Applications (https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).

1. In Microsoft Entra ID, register one application for the instance, upload its certificate (public part only), add the application permission `Mail.Send` with admin consent (removed again in step 5).
2. Note the application (client) ID and the object ID of the enterprise application (service principal); not the IDs of the app registration page.
3. In Exchange Online PowerShell as member of the Organization Management role group, replace the placeholders:

   ```powershell
   New-ServicePrincipal -AppId <application (client) ID> -ObjectId <object ID of the enterprise application> -DisplayName "LEG-Platform sender"
   New-ManagementScope -Name "LEG-Platform sender mailbox" -RecipientRestrictionFilter "EmailAddresses -eq 'smtp:<sender address>'"
   New-ManagementRoleAssignment -App <application (client) ID> -Role "Application Mail.Send" -CustomResourceScope "LEG-Platform sender mailbox"
   ```

4. Test: `Test-ServicePrincipalAuthorization -Identity <application (client) ID> -Resource <sender address>` must show `Mail.Send` with `InScope` `True`. Changes take effect after the Exchange cache refresh (30 minutes to 2 hours).
5. In Microsoft Entra ID, remove the unscoped `Mail.Send` permission (otherwise it adds up to access to all mailboxes).
6. Switch off automatic replies of the sender mailbox: `Set-MailboxAutoReplyConfiguration -Identity <sender address> -AutoReplyState Disabled`.

The sender address receives replies and bounces; protect its mailbox. It must be the mailbox of step 3.

Upload size (D-048): certificate and private key are files of at most 16 KiB each, checked only after the whole request has been received. The web server or reverse proxy must limit the request body size (value open, D-048). The portal settings page (two logos) is limited to 3 MiB by `leg_platform/middleware.py`; the proxy limit must not be lower for that page.

### Scheduled tasks

The scheduler is decided with the hosting (D-008, D-037, D-038, D-041).

* `python manage.py run_scheduled_tasks` runs every cleanup task once (the task list is in the command; it ends with the daily address directory run `scheduled_address_import`, which needs the state directory and the network, `docs/DEPLOYMENT.md`, 8.3). Application deadlines (reminders, rollbacks, deregistrations; D-103) run in it too.
* Schedule this one command, for example every 15 minutes (cron or systemd timer on Linux, task scheduler on Windows). A failing task does not stop the others; the command then exits with an error, which the scheduler must report.
* The worker `process_mail_queue --loop` is not part of it; it is a supervised service.

### Setup of the instance and initial system administrator

D-005, D-041, D-076. `python manage.py setup_instance` is idempotent and runs on every start of the services (on a server via `leg-setup.service`, `docs/DEPLOYMENT.md`, section 8.0; locally by hand, `docs/STARTUP.md`). It:

1. applies pending migrations (`migrate --noinput`);
2. only while the account table is completely empty and `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` is set, creates the initial system administrator with `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD`;
3. creates or rebuilds the disposable public address schema when missing or changed and queues its official import, without downloading during setup (a failure there never stops the rest; "Address Directory").

An installation needs only an **empty** database with its one account ("Database privileges") and the environment file.

`BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` is a secret: the file must be readable only by its owner (mode 0600). It is ignored once a system administrator exists. A missing password or one failing the validators (at least 15 characters) makes the command refuse, without printing it. At the first login the password must be replaced and two-factor authentication set up. **Remove `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` from the environment file after the first login.** Alternative without a password in a file: leave it unset and run `python manage.py setup_initial_administrator` with `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` set for that command. It asks twice for the password with hidden input, runs only while no system administrator exists and logs its use.

### Emergency commands

D-004, D-005, D-041. Only on the server. Each use is logged (long retention) and asks for confirmation (`--no-input`, given before the action, skips it). Accounts are named by their internal number (change form of the account in the administration):

* `python manage.py emergency_access reset-second-factor N`: removes the second factor of any account, also of the last system administrator. All its sessions end; the owner is notified by email.
* `python manage.py emergency_access unblock-account N`: sets a blocked account active again (a blocked account receives no password reset email).
* `python manage.py emergency_access reencrypt-second-factor-keys`: encrypts all second factor keys with the current `SECRET_KEY` (see "Changing `SECRET_KEY`"); reports keys that cannot be decrypted.

Staff users and system administrators must type their email address to confirm; `--no-input` is refused for them. Use `--allow-last-administrator` only if losing the last system administrator's second factor is intended.

Lockout paths (D-044):

* Password forgotten, second factor available: "Forgot password?" (single-use link, 24 hours); no operator action.
* Second factor lost: a system administrator resets it on the change form of the account (for staff users and system administrators it takes effect at once). With no other system administrator use `emergency_access reset-second-factor N`. The person then logs in with the password and sets up a new second factor (required for staff users and system administrators, D-042).
* Account blocked: `emergency_access unblock-account N`.

### Operation notes

* Security log (D-018, D-041): `cleanup_security_log` deletes entries after `SECURITY_LOG_RETENTION_DAYS` (default 30) or, for changes of rights and roles, second factor resets and emergency commands, `SECURITY_LOG_LONG_RETENTION_DAYS` (default 365).
* Customer numbers: from 90 % of the six-digit numbers issued, each issue logs a warning (logger `leg_platform.accounts.models`); alarm on it (D-038, D-052).
* Request limits per client IP use the address Django sees (`AXES_IPWARE_PROXY_COUNT`, below). **A wrong count has a visible effect (D-058): when the shared budget of the client address is used up, every visitor gets "Zu viele Anfragen, bitte versuchen Sie es später erneut." (HTTP 429) on registration, password reset and email change. Check the count before go-live and watch the log line "Requests that send email are refused" (at most once a minute).** The reverse proxy should additionally limit the plain request rate.
* The verification link has a secret token in its path (`/register/verify/<token>/`). The application removes it from its own logs; web server and proxy access logs must not keep it (or must be restricted and rotated).

### Trusted Proxies for Login IP Protection

`AXES_IPWARE_PROXY_COUNT` defaults to `0`: login abuse protection uses the direct TCP peer (`REMOTE_ADDR`) and ignores `X-Forwarded-For`.

* Reached only through reverse proxies: set it to the exact number of proxies between the client and Django, and only after verifying that each proxy removes or validates client-supplied forwarding headers and appends its own address.
* IPware validates the chain strictly. An unexpected or malformed chain produces no client IP and the login is refused. A wrong value can group users under a proxy address or refuse logins: verify it against the actual proxy topology before enabling a stage or production instance.
* Client IPs are not stored in Axes (D-017, D-023).

**Address suggestions need the same setting (D-052, D-065, D-111).** `/addresses/lookup/` counts requests per client IP for an anonymous visitor (60 per minute, 5 for administrators; IPv6 per /64), per account for a signed-in account (120 per minute) and for the whole instance (600 per minute). With the count at `0` behind a reverse proxy, all visitors share the proxy's budget and everybody sees "temporarily limited". Saving the profile is never affected.

### Changing `SECRET_KEY`

Second factor keys are encrypted with a key derived from `SECRET_KEY`; recovery codes are stored as digests under it (D-040). **IBANs are encrypted the same way (D-087, own label).**

1. Put the old value into `DJANGO_SECRET_KEY_FALLBACKS`. Keys and recovery codes keep working; a key is encrypted again with the new one at its next use.
2. Do not remove the old value before every active account with a second factor has logged in once (except accounts whose second factor a system administrator reset). To encrypt all keys at once run `python manage.py emergency_access reencrypt-second-factor-keys` (D-041). Remove the old value once it reports no key that cannot be decrypted.
3. IBANs: run `python manage.py reencrypt_ibans` (prints numbers only, never a value; **exits with an error if any IBAN could not be decrypted**: keep the old key in the fallbacks then). Remove the old value from the fallbacks only after it succeeded. An IBAN under a key that is no longer known is unreadable and has to be entered again.
4. Recovery codes cannot be converted: they work only while the old value stays in the fallbacks; afterwards their owners create new ones. The Microsoft Graph private key follows the same rule (D-006).

The in-place IBAN encryption (D-087) leaves old plain values in the pre-update backup, the binary log and freed table pages (`docs/DEPLOYMENT.md`, section 12.3, step 6).

### Recovery after a failed setup

If a step after the MariaDB installation fails or the script is interrupted (Ctrl+C), it stops the `MySQL` service, sets it to `Disabled` and checks. If it reports failure, or the console window was closed during the setup, do it as administrator (`sc.exe stop MySQL`, `sc.exe config MySQL start= disabled`) and check with `Get-Service MySQL | Select-Object Status, StartType`.

Fix the cause shown by the script and run as administrator:

```powershell
.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
```

* It removes the MariaDB installation only if exactly one MariaDB is installed, below `C:\Program Files`, with the Chocolatey package `mariadb.install`.
* It lists what it removes, asks you to type `DELETE` and for the new `root` password, and uninstalls with `msiexec.exe /x <product code> /qn /norestart`. Exit code 3010 = success, restart required: restart Windows and run it again without `-RemoveFailedInstallation`.
* It removes the Chocolatey package records and the MariaDB `bin` entry of the machine-wide `PATH`, and continues with the normal setup. All local MariaDB databases are lost.

If the removal is interrupted after the uninstall, a new run finds only Chocolatey package records and stops. Then, as administrator:

1. `choco uninstall mariadb -y -n --skip-autouninstaller` (only if `choco list` shows it) and `choco uninstall mariadb.install -y -n --skip-autouninstaller` (a plain `choco uninstall` fails with exit code 1619, D-030).
2. Remove `C:\Program Files\MariaDB <version>\bin` from the machine-wide `PATH`. Delete `C:\Program Files\MariaDB <version>` if it exists.
3. Sign out and in, run the script again.
4. If the service still exists after `sc.exe delete`: close the Services console and Task Manager or restart Windows, repeat.

For any other MariaDB or MySQL installation the script changes nothing.

Tests of the script's helper functions (no system change):

```powershell
powershell -NoProfile -File scripts\local\test-setup-dev-windows.ps1
pwsh -NoProfile -File scripts\local\test-setup-dev-windows.ps1
```

### Database privileges

The application account receives only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX`, granted on the database level, and no global privileges:

* `SELECT`, `INSERT`, `UPDATE`, `DELETE`: data access. `CREATE`, `DROP`, `ALTER`, `INDEX`: schema changes by Django migrations. `REFERENCES` is not granted.
* Locally also on `test_leg_platform` (Django creates and drops the test database as the configured user).
* `_` is a wildcard in database-level grants. The script escapes it (`leg\_platform`) and checks the stored names.
* Create every database of an instance with `CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci` (D-030, D-031), e.g. `CREATE DATABASE leg_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;` (the setup script does this locally). Identifier columns (normalised email, tokens, customer number) use `utf8mb4_bin` per column (D-031).
* `manage.py test --parallel` is not supported with this account (MariaDB error 1044, D-071). Do not widen the grants for it.

Stage and production: the same privileges only on the instance's own database, no test database, no global privileges, for an account that can connect only from the application host. `DROP` on the database level also allows dropping the whole database. A split into a migration account and a runtime account (`SELECT`, `INSERT`, `UPDATE`, `DELETE` only) is decided with the hosting (D-008).

Apply migrations with `python manage.py setup_instance` (or `python manage.py migrate`). CI (`.github/workflows/ci.yml`) uses an application account with exactly these privileges.
