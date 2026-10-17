# Installation

Local development setup and instance configuration. Stage and production: `docs/DEPLOYMENT.md`, section 0.5. Starting, checking and stopping the local environment: `docs/STARTUP.md`. Roles, rules and CI: `docs/OPERATIONS.md`.

## Instance Presentation

The administrator maintains name, website, addresses of the privacy notice and the legal notice, two logos and the colours in the administration ("Inhalte", "Portal-Einstellungen"; D-073). A stored value wins; an empty field falls back to the variables of `.env.example` (D-051), then to the neutral platform defaults. Environment only (shown read only to system administrators): host name, `INSTANCE_BASE_URL`, allowed hosts, mail mode and sender, Graph credentials, database, secrets, retention minimums, proxy settings (an editable host name would let anyone redirect all email links).

**Logos (D-073).** One logo for light mode and one for dark mode (both or none), PNG (up to 1 MB, 64 to 4000 pixels per side) or SVG (up to 200 KB, without scripts, styles, images, links or editor data). They are stored **in the database** (`accounts_portalsettings`), so **the logos belong to the database backup**. They are served publicly at `/branding/logo/light/` and `/branding/logo/dark/`; the reverse proxy must pass these paths and may cache them for a day. It must allow a request body of at least 3 MiB for `/admin/accounts/portalsettings/1/change/` (the application refuses a larger body with 413).

Fallback while no logos are uploaded: `INSTANCE_LOGO_LIGHT` and `INSTANCE_LOGO_DARK`, local static `.svg` or `.png` files with relative static paths (never absolute or external). Set `INSTANCE_STATIC_DIRECTORY` to an existing absolute directory outside the repository. Its contents are public: never put configuration, personal data or secrets there. Check every SVG for scripts, event handlers and external references and run `collectstatic` with the instance's static directories. A missing file is an operator error; the startup validation does not prove it exists.

Colours are six-digit hexadecimal values; the application refuses to start if the accent lacks 4.5:1 contrast with its text, the page background or the surface, or highlight text lacks contrast. The administration form applies the same rules; a failing database value is not used (a warning is logged). Platform defaults: accent `#005BA2` and `#6AA9E0`, highlight `#DDDD22` (D-056).

The texts of the login and registration pages are changed only in `locale/<language>/LC_MESSAGES/django.po` (compile with `msgfmt`, deploy). The administration start page shows what still has to be set (box "Erste Schritte"; `docs/OPERATIONS.md`, section 5a).

**Grid operator (D-086):** a system administrator enters the name once in the administration (group "Inhalte", "Netzbetreiber"); until then the texts say "Netzbetreiber". It is instance data, never part of code, migrations or the environment file. The transformer circuit number format (`TRA` and digits) is that of the operator in the owner's specification; another operator needs `numbers.TRANSFORMER_CIRCUIT_NUMBER` adapted.

Supported browsers (D-003): Chrome and Edge 123, Firefox 128, Safari 17.5, Opera 109 or newer.

## Address Directory

Profile addresses never leave the application; suggestions query only a local copy of the official directory. The user interface shows no source and no warning about it (D-066); the swisstopo source reference belongs to the privacy notice and imprint (D-049, D-022). A missing, stale or unavailable directory is logged and shown on the operator page "Address directory" only; it never blocks registration or saving.

The directory is an own SQLite file (D-075), **not part of any backup**. Optional `ADDRESS_DIRECTORY_FILE`: absolute path outside the repository, no control characters. Default outside `APP_ENV=local`: `/var/lib/leg-platform/address-directory.sqlite3` (`StateDirectory=leg-platform` in the systemd units); with `APP_ENV=local`: `~/.leg-platform-dev/address-directory.sqlite3`. A missing file is created as mode 0600 (folder 0700). Processes open it read-only unless they are writers (worker, `import_address_directory`, `seed_local_address_directory`, `setup_instance`, `migrate`; never the web processes). Web and worker must run on the same host with the same settings; the file must not be on a network file system.

`python manage.py setup_instance` (below) creates the file and tables; manual equivalent (`migrate` alone never creates them):

```powershell
python manage.py migrate addresses --database=addresses
```

**Restore:** the file is not restored from a backup; start the services (`setup_instance` recreates it) and the worker imports within 24 hours, or an administrator presses the button (about 12 minutes, about 143 MB, D-075). A corrupted file is deleted and rebuilt (`docs/DEPLOYMENT.md`, section 10.2).

Run a supervised worker (same environment as the web application; the web process never downloads or imports):

```powershell
python manage.py process_address_imports
```

It handles queued manual imports (permission `addresses.request_directory_import`, logged in the security log) and makes one automatic attempt every 90 days (D-052), every 24 hours while the directory is missing or empty (D-076). If the published directory is the official file and passes the integrity check, a manual request ends without a download, otherwise it imports again (D-076). The importer refuses to run with less than 4 GB free where the file lives. Manually:

```powershell
python manage.py import_address_directory
```

Allow outbound HTTPS only to `data.geo.admin.ch` for the worker. It verifies the official SHA-256 multihash before parsing. No second import runs at the same time (lease on the directory row, expires after 6 hours). Reserve disk for **three** generations at the peak (about 0.9 GB each), the temporary download and the write-ahead log. A hard-killed job (state stays `running`) is taken over only after its lease has expired; before that confirm that no importer remains active (`docs/DEPLOYMENT.md`, section 8.4). Do not blindly reset its state.

`POST /addresses/lookup/` accepts at most 4096 bytes of body. The web server or reverse proxy must also limit request bodies for the whole instance (for example `client_max_body_size` in Nginx), because Django's limits apply only after the request has been received; **this limit is mandatory before any stage or production deployment**.

`manage.py seed_local_address_directory` is for local development only (`docs/STARTUP.md`, section 10); never use it for stage or production.

## Local Development

For Windows development machines (D-027, D-030). Never use these steps or credentials for stage or production. The database is MariaDB (D-002). Chocolatey 2 or later is required.

### Step 1: Remove the old MySQL installation

Only needed where the earlier setup script installed MySQL (folders `C:\tools\mysql` and `C:\ProgramData\MySQL\data`, service `MySQL`). The setup script refuses to start while these exist and prints the exact commands, adapted to what it finds. Run them in PowerShell **as administrator** with no other user signed in. The first command checks that none of the folders is a link (symbolic link, junction, mount point): if it lists one, run nothing else and ask for help. The others stop and disable the service, run `choco uninstall mysql -y`, delete the service and the folders with `[IO.Directory]::Delete(<path>, $true)` (does not follow links inside). All old MySQL databases are lost (nothing of value); errors for parts that no longer exist are expected. If the service `MySQL` belongs to another MySQL installation, the script warns instead of printing service commands; do not stop or delete that service.

### Step 2: Run the setup

1. Check as administrator that the Windows Firewall is on and blocks inbound connections for all profiles, and that no rule allows inbound connections to the database or to file sharing (SMB, port 445):

   ```powershell
   Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction
   Get-NetFirewallPortFilter -PolicyStore ActiveStore | Where-Object { $_.LocalPort -in '3306', '445' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallApplicationFilter -PolicyStore ActiveStore | Where-Object { $_.Program -like '*mysqld*' -or $_.Program -like '*mariadbd*' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Action Allow -Enabled True | Where-Object { $port = $_ | Get-NetFirewallPortFilter; $app = $_ | Get-NetFirewallApplicationFilter; $svc = $_ | Get-NetFirewallServiceFilter; -not $_.Owner -and -not $app.Package -and $app.Program -eq 'Any' -and $svc.Service -eq 'Any' -and $port.Protocol -in 'Any', 'TCP' -and $port.LocalPort -eq 'Any' } | Select-Object DisplayName, Profile
   ```

   The first must show `Enabled True` and `DefaultInboundAction Block` for every profile; the others must show nothing (the fourth lists inbound allow rules not restricted to a port, program, service or Store app). If the second lists "File and Printer Sharing" rules, disable them during the setup or use a trusted network. Limit: none evaluates remote address restrictions, other protocols or rule order. If Windows asks whether `mysqld` may communicate on networks, deny it.

2. Open a 64-bit PowerShell **as administrator** in the project directory:

   ```powershell
   .\scripts\local\setup-dev-windows.ps1
   ```

   The script first asks for a new password for the local MariaDB `root` accounts: at least 15 characters, printable ASCII only, none of `#`, `"`, `'`, `\` and `;`, no leading or trailing space. Keep it in your password manager.

   The script refuses to start if the old MySQL, a service named `MySQL` or `MariaDB`, a MariaDB installation, or `C:\my.ini`, `C:\my.cnf`, `%WINDIR%\my.ini`, `%WINDIR%\my.cnf` exists. It installs Python 3.14 and MariaDB 12.3 (`mariadb.install`), starts MariaDB **without network access** (`SKIPNETWORKING=1`; only the named pipe `MySQL`) without giving the root password to the installer, disables (not deletes) the installer's firewall rule for `mysqld.exe`, hardens MariaDB, creates the database `leg_platform` and the account `leg_platform`@`127.0.0.1` ("Database privileges"), switches MariaDB to `127.0.0.1:3306` only with the named pipe off, and stores `APP_ENV=local`, `DB_*` and `DJANGO_SECRET_KEY` as user environment variables without printing them. An upgrade or repair of MariaDB (`choco upgrade mariadb.install`) may enable the firewall rule again: check with the third firewall command and `Disable-NetFirewallRule` it.

      **Phase without a root password:** until the script sets the password, MariaDB runs with a passwordless `root`, reachable by any program on the computer and by users of other computers over file sharing (SMB). Run the script only when no untrusted user or program is active and with file sharing blocked (firewall check).

   User environment variables are inherited by every process of the Windows user, including AI tools. Never print the environment (`Get-ChildItem env:`, `set`, `env`); it contains `DB_PASSWORD` and `DJANGO_SECRET_KEY` (D-027).

### Step 3: Restart VS Code

Close and reopen VS Code and every terminal to receive the new environment variables (all variables: `.env.example`). Then create the virtual environment and install the project with the newest compatible packages (D-015):

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install --upgrade -e .
```

Checks, tests, development server, test accounts and the address directory seed: `docs/STARTUP.md` (`check` and `makemigrations` need the running MariaDB).

Email: locally emails go to the console (`EMAIL_MODE=console`). To test Graph delivery use `EMAIL_MODE=allowlist` (only with `APP_ENV=local`) with a dedicated development app registration and mailbox, never stage or production credentials, and set the recipients only as the user environment variable `EMAIL_ALLOWED_RECIPIENTS` (D-028). Stage and production allow only `EMAIL_MODE=graph` (D-070).

### Instance Configuration

Each instance sets its values in the environment (`.env.example`, D-013, D-037); they are validated at startup and invalid values prevent the start:

* `INSTANCE_NAME`: shown in pages and email subjects (optional, default "LEG Platform"; the administration value wins, D-073).
* `INSTANCE_BASE_URL`: canonical `https://` origin without path, e.g. `https://portal.example.org`. Email links are built from it, never from the request. Its host must be in `DJANGO_ALLOWED_HOSTS`. Required outside `APP_ENV=local`.
* `INSTANCE_PRIVACY_URL`: absolute `https://` URL of the privacy notice. Required outside `APP_ENV=local`; the administration value wins.
* `INSTANCE_WEBSITE_URL`: absolute `https://` URL of the website (optional fallback).
* `EMAIL_SENDER_ADDRESS`: sender of all emails (optional; default `noreply@<host of INSTANCE_BASE_URL>`, locally `noreply@localhost`). Emails never invite a reply (D-038).

Email delivery (D-038):

* Emails are not sent during the request. The worker `python manage.py process_mail_queue --loop` must run as a supervised service (restart on failure); without it no email is sent. Without `--loop` it processes the queue once. A job is tried three times, then given up; unprocessed jobs are deleted after one hour (they hold the address in plain text).
* The sender domain needs SPF, DKIM and DMARC records, and the sender address must be one the service may use.
* The Graph backend sends no `Auto-Submitted` header (Microsoft Learn allows custom headers only with an `x-` prefix, D-048). Automatic replies (out-of-office) of the sender mailbox must therefore stay switched off (D-006).

### Microsoft Graph sender

D-006. The operator does these steps in Microsoft Entra ID and Exchange Online. Configuration and test send are in the section "Microsoft Graph configuration" of the Django administration (system administrators only): tenant ID, client ID, sender address, certificate and private key together (the key is never shown again). Source: Microsoft Learn, RBAC for Applications (https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).

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

Upload size (D-048): certificate and private key are files of at most 16 KiB each, checked only after the whole request has been received. The web server or reverse proxy must limit the request body size (value open, D-048); the portal settings page (two logos) is limited to 3 MiB by `leg_platform/middleware.py`, so the proxy limit must not be lower for that page.

### Scheduled tasks

The scheduler is decided with the hosting (D-008, D-037, D-038, D-041).

* `python manage.py run_scheduled_tasks` runs every cleanup once: `delete_expired_registrations`, `cleanup_mail_requests`, `cleanup_login_ip_attempts`, `cleanup_security_log`, `cleanup_confirmation_failures` and Django's `clearsessions`. Schedule this one command, for example every 15 minutes (cron or systemd timer on Linux, task scheduler on Windows). A failing task does not stop the others; the command then exits with an error, which the scheduler must report.
* The worker `process_mail_queue --loop` is not part of it; it is a supervised service.

### Setup of the instance and initial system administrator

D-005, D-041, D-076. `python manage.py setup_instance` is idempotent and runs on every start of the services (on a server via `leg-setup.service`, `docs/DEPLOYMENT.md`, section 8.0; locally by hand, `docs/STARTUP.md`). It (1) applies pending migrations (`migrate --noinput`), (2) while no system administrator exists and `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` is set, creates the initial system administrator with `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD`, and (3) creates the address directory file and tables (a failure there never stops the rest). An installation needs only an **empty** database with its one account ("Database privileges") and the environment file.

`BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` is a secret: the file must be readable only by its owner (mode 0600); it is ignored once a system administrator exists. A missing password or one failing the validators (at least 15 characters) makes the command refuse, without printing it. At the first login the password must be replaced and two-factor authentication set up. **Remove `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` from the environment file after the first login.** Alternative without a password in a file: leave it unset and run `python manage.py setup_initial_administrator` with `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` set for that command; it asks twice for the password with hidden input, runs only while no system administrator exists and logs its use.

### Emergency commands

D-004, D-005, D-041. Only on the server; each use is logged (long retention) and asks for confirmation (`--no-input`, given before the action, skips it). Accounts are named by their internal number (change form of the account in the administration):

* `python manage.py emergency_access reset-second-factor N`: removes the second factor of any account, also of the last system administrator; all its sessions end and the owner is notified by email.
* `python manage.py emergency_access unblock-account N`: sets a blocked account active again (a blocked account receives no password reset email).
* `python manage.py emergency_access reencrypt-second-factor-keys`: encrypts all second factor keys with the current `SECRET_KEY` (see "Changing `SECRET_KEY`"); reports keys that cannot be decrypted.

Staff users and system administrators must type their email address to confirm; `--no-input` is refused for them. Use `--allow-last-administrator` only if losing the last system administrator's second factor is intended.

Lockout paths (D-044): password forgotten with second factor available: "Forgot password?" (single-use link, 24 hours), no operator action. Second factor lost: a system administrator resets it on the change form of the account (for staff users and system administrators it takes effect at once); with no other system administrator use `emergency_access reset-second-factor N`, then the person logs in with the password and sets up a new second factor (required for staff users and system administrators, D-042). Account blocked: `emergency_access unblock-account N`.

### Operation notes

* Security log (D-018, D-041): `cleanup_security_log` deletes entries after `SECURITY_LOG_RETENTION_DAYS` (default 30) or, for changes of rights and roles, second factor resets and emergency commands, `SECURITY_LOG_LONG_RETENTION_DAYS` (default 365).
* Customer numbers: from 90 % of the six-digit numbers issued, each issue logs a warning (logger `leg_platform.accounts.models`); alarm on it (D-038, D-052).
* Request limits per client IP use the address Django sees (`AXES_IPWARE_PROXY_COUNT`, below). **A wrong count has a visible effect (D-058): when the shared budget of the client address is used up, every visitor gets "Zu viele Anfragen, bitte versuchen Sie es später erneut." (HTTP 429) on registration, password reset and email change. Check the count before go-live and watch the log line "Requests that send email are refused" (at most once a minute).** The reverse proxy should additionally limit the plain request rate.
* The verification link has a secret token in its path (`/register/verify/<token>/`); the application removes it from its own logs, but web server and proxy access logs must not keep it (or must be restricted and rotated).

### Trusted Proxies for Login IP Protection

`AXES_IPWARE_PROXY_COUNT` defaults to `0`: login abuse protection uses the direct TCP peer (`REMOTE_ADDR`) and ignores `X-Forwarded-For`. If Django is reached only through reverse proxies, set it to the exact number of proxies between the client and Django, and only after verifying that each proxy removes or validates client-supplied forwarding headers and appends its own address. IPware validates the chain strictly; an unexpected or malformed chain produces no client IP and the login is refused. A wrong value can group users under a proxy address or refuse logins: verify it against the actual proxy topology before enabling a stage or production instance. Client IPs are not stored in Axes (D-017, D-023).

**Address suggestions need the same setting (D-052, D-065).** `/addresses/lookup/` counts requests per client IP (60 per minute, 5 for administrators; IPv6 per /64) and for the whole instance (600 per minute). With the count at `0` behind a reverse proxy, all visitors share the proxy's budget and everybody sees "temporarily limited". Saving the profile is never affected.

### Changing `SECRET_KEY`

Second factor keys are encrypted with a key derived from `SECRET_KEY`, and recovery codes are stored as digests under it (D-040). **IBANs are encrypted the same way (D-087, own label): after the replacement run `python manage.py reencrypt_ibans` (prints numbers only, never a value, and **exits with an error if any IBAN could not be decrypted**: keep the old key in the fallbacks then); remove the old value from the fallbacks only after it succeeded. An IBAN under a key that is no longer known is unreadable and has to be entered again.** The in-place IBAN encryption (D-087) leaves old plain values in the pre-update backup, the binary log and freed table pages (`docs/DEPLOYMENT.md`, section 12.3, step 6).

When `SECRET_KEY` is replaced, put the old value into `DJANGO_SECRET_KEY_FALLBACKS`: keys and recovery codes keep working and a key is encrypted again with the new one at its next use. Do not remove the old value before every active account with a second factor has logged in once (except accounts whose second factor a system administrator reset). To encrypt all keys at once run `python manage.py emergency_access reencrypt-second-factor-keys` (D-041); remove the old value once it reports no key that cannot be decrypted. Recovery codes cannot be converted: they work only while the old value stays in the fallbacks; afterwards their owners create new ones. The Microsoft Graph private key follows the same rule (D-006).

### Recovery after a failed setup

If a step after the MariaDB installation fails or the script is interrupted (Ctrl+C), it stops the `MySQL` service, sets it to `Disabled` and checks. If it reports failure, or the console window was closed during the setup, do it as administrator (`sc.exe stop MySQL`, `sc.exe config MySQL start= disabled`) and check with `Get-Service MySQL | Select-Object Status, StartType`.

Fix the cause shown by the script and run as administrator:

```powershell
.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
```

It removes the MariaDB installation only if exactly one MariaDB is installed, below `C:\Program Files`, with the Chocolatey package `mariadb.install`. It lists what it removes, asks you to type `DELETE` and for the new `root` password, uninstalls with `msiexec.exe /x <product code> /qn /norestart` (exit code 3010 = success, restart required: restart Windows and run it again without `-RemoveFailedInstallation`), removes the Chocolatey package records and the MariaDB `bin` entry of the machine-wide `PATH`, and continues with the normal setup. All local MariaDB databases are lost.

If the removal is interrupted after the uninstall, a new run finds only Chocolatey package records and stops. Then, as administrator: `choco uninstall mariadb -y -n --skip-autouninstaller` (only if `choco list` shows it) and `choco uninstall mariadb.install -y -n --skip-autouninstaller` (a plain `choco uninstall` fails with exit code 1619, D-030), remove `C:\Program Files\MariaDB <version>\bin` from the machine-wide `PATH`, delete `C:\Program Files\MariaDB <version>` if it exists, sign out and in, run the script again. If the service still exists after `sc.exe delete`, close the Services console and Task Manager or restart Windows and repeat. The old MySQL is never removed automatically (step 1); for any other MariaDB or MySQL installation the script changes nothing.

Tests of the script's helper functions (no system change):

```powershell
powershell -NoProfile -File scripts\local\test-setup-dev-windows.ps1
pwsh -NoProfile -File scripts\local\test-setup-dev-windows.ps1
```

### Database privileges

The application account receives only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX`, granted on the database level, and no global privileges:

* `SELECT`, `INSERT`, `UPDATE`, `DELETE`: data access. `CREATE`, `DROP`, `ALTER`, `INDEX`: schema changes by Django migrations. `REFERENCES` is not granted.
* Locally also on `test_leg_platform` (Django creates and drops the test database as the configured user).
* `_` is a wildcard in database-level grants; the script escapes it (`leg\_platform`) and checks the stored names.
* Every database of an instance must be created with `CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci` (D-030, D-031); MariaDB 10.11 would otherwise use `latin1`. The setup script does this locally; operators of stage and production, e.g. `CREATE DATABASE leg_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;`. Identifier columns (normalised email, tokens, customer number) use the exact collation `utf8mb4_bin` per column (D-031).
* `manage.py test --parallel` is not supported with this account (it needs `test_leg_platform_1`, ...; MariaDB error 1044, D-071). Do not widen the grants for it.

Stage and production: the same privileges only on the instance's own database, no test database, no global privileges, for an account that can connect only from the application host. `DROP` on the database level also allows dropping the whole database; a split into a migration account and a runtime account (`SELECT`, `INSERT`, `UPDATE`, `DELETE` only) is decided with the hosting (D-008).

Apply migrations with `python manage.py setup_instance` (or `python manage.py migrate`). CI (`.github/workflows/ci.yml`) uses an application account with exactly these privileges.
