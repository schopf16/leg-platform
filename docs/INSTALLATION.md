# Installation

This guide grows with the project. Installation of stage and production instances is documented once the hosting details are decided (D-008).

Related documents: `docs/STARTUP.md` starts, checks, uses and stops the complete local environment (including the synthetic local address directory); `docs/DEPLOYMENT.md` is the runbook for a VPS (a proposal, D-053); `docs/OPERATIONS.md` describes roles, rules and the CI.

## Instance Presentation

Branding is configured through the variables documented in `.env.example` (D-051).
Leave them empty to use the neutral platform defaults. Colours are six-digit hexadecimal
values. The application refuses to start if the accent lacks 4.5:1 contrast with its text,
the page background or the surface, or if highlight text would lack contrast.

Before deploying, the operator also checks that accents are not red and cannot be confused
with the status or energy colours in `docs/DESIGN.md`. The automatic contrast check does
not verify the meaning of colours.

Provide both the light and dark logo as local static `.svg` or `.png` files. Use relative
static paths, never an absolute path or an external URL. Logos are embedded as images, not
inline SVG. Set `INSTANCE_STATIC_DIRECTORY` to an existing absolute directory outside
the repository containing these files. Its contents are public: never put configuration,
personal data or secrets there. Check every SVG for scripts, event handlers and external references before
deployment; serve static files with a restrictive CSP. Run Django's `collectstatic` using
the instance's configured static directories before serving the files. A missing file is
an operator error; the startup path validation does not prove that the file exists.

Colour mode and language (D-056): the pages start light and German. The person switches with the two buttons in the top right corner; there is no automatic mode that follows the device setting. The choice is kept only in the `sessionStorage` of the browser tab (no cookie, nothing is sent to the server), so a new tab starts light and German again. The buttons need JavaScript; without it they are hidden. Print output hides navigation, the corner buttons and interactive controls and shows the instance name and date.

The platform defaults of the colours are the values of the design package of the owner (accent `#005BA2` and `#6AA9E0`, highlight `#DDDD22`; D-056); an instance replaces them with the variables above. The addresses of the privacy notice and the legal notice in the footer are maintained by the system administrator in the administration ("Portal-Einstellungen"); the privacy address falls back to `INSTANCE_PRIVACY_URL` while the setting is empty. The footer also shows the host of `INSTANCE_WEBSITE_URL` as a link.

Supported browsers (D-003): Chrome and Edge 123 or newer, Firefox 128 or newer, Safari 17.5
or newer, and Opera 109 or newer. Devices that cannot update to these versions are not
supported. Responsive checks do not establish full WCAG 2.2 AA conformity.

## Local Address Directory

The application never sends profile addresses to an external service. Suggestions query
only a local copy of the official directory, attributed to the
Federal Office of Topography swisstopo in the Swiss German translation (D-049). A missing, stale or unavailable
directory gives an explicit advisory warning and never blocks registration or saving.
Foreign addresses and post office boxes are not checked against building addresses.

The operator creates a separate MariaDB database with
`CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci` and InnoDB tables.
Give each environment/instance its own account with **SELECT only** on that directory.
Configure the `ADDRESS_DB_*` variables. Do not reuse the application account.
The public directory may be shared across instances and is not part of application
backups. It can be reconstructed from the official source.

Create a different importer account for this same database with schema and write
privileges. Put `ADDRESS_IMPORT_DB_USER` and `ADDRESS_IMPORT_DB_PASSWORD` only in the
protected environment of the isolated importer, never in the web service. Normal settings
refuse to start if importer credentials are present. Importer settings cannot run a web server.
Apply the traceable migrations:

```powershell
python manage.py migrate --database=default
python manage.py migrate addresses --database=address_import --settings=leg_platform.import_settings
```

The first command runs with the web/migration environment without importer credentials.
The second runs with the isolated importer environment. The router creates the queue only
in the application database and the directory tables only in the address database.
Actual grants and MariaDB directory migration/import must be checked by the operator;
local synthetic SQLite tests do not verify these.

Run a supervised isolated worker:

```powershell
python manage.py process_address_imports --settings=leg_platform.import_settings
```

It handles queued manual imports and, once implemented and verified under D-052, makes
one automatic attempt every 90 days. Failed automatic attempts are logged and wait
another 90 days; manual attempts do not alter that clock. A user with the runtime permission
`addresses.request_directory_import` can request a manual import in the admin.
Concurrent requests are coalesced. The web request itself performs no download.
The public lookup `POST /addresses/lookup/` accepts at most 4096 bytes of body; the application
refuses more before parsing it, but the web server or reverse proxy must also limit request
bodies for the whole instance (for example `client_max_body_size` in Nginx), because Django's
own limits are applied only after the request has been received (under ASGI also the body limit
of the application's middleware); this limit is a mandatory step before any stage or production
deployment.
An operator can also run:

```powershell
python manage.py import_address_directory --settings=leg_platform.import_settings
```

Allow outbound HTTPS only to `data.geo.admin.ch` for the importer. It verifies the official
SHA-256 multihash before parsing, bounds the download/archive/rows and publishes atomically.
Existing directory generations are retained; reserve disk capacity for them and for temporary
downloads and database transaction logs. A hard-killed running job requires investigation;
confirm no importer remains active before operator recovery. Do not blindly reset its state.

For local development only, `leg_platform.local_directory_settings` adds a throw-away SQLite directory with a few invented addresses (`manage.py seed_local_address_directory`); it refuses to be imported unless `APP_ENV=local`. The steps are in `docs/STARTUP.md`, section 10. Never use it for stage or production.

Run the complete local tests sequentially:

```powershell
python -W error::DeprecationWarning -W error::PendingDeprecationWarning manage.py test --settings=leg_platform.test_settings --noinput
```

The application tests use the configured local MariaDB test database; address tests use
isolated synthetic SQLite databases. Never use production credentials or data.

## Local Development

For Windows development machines (D-027, D-030). Never use these steps or their credentials for stage or production. The database is MariaDB (D-002); MySQL is not supported. Chocolatey 2 or later is required.

### Step 1: Remove the old MySQL installation

Only needed on a computer where the earlier version of the setup script installed MySQL (as on the current development machine: folders `C:\tools\mysql` and `C:\ProgramData\MySQL\data`, Windows service `MySQL`). The setup script refuses to start while these exist and prints these commands, adapted to what it finds.

Open PowerShell **as administrator** ("Run as administrator"), make sure no other user is signed in to the computer, and run:

```powershell
# 1. Must print nothing; otherwise stop and ask for help:
Get-Item -LiteralPath 'C:\tools', 'C:\tools\mysql', 'C:\ProgramData\MySQL', 'C:\ProgramData\MySQL\data' -Force -ErrorAction SilentlyContinue | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
Stop-Service -Name MySQL -Force
sc.exe config MySQL start= disabled
choco uninstall mysql -y
sc.exe delete MySQL
[IO.Directory]::Delete('C:\tools\mysql', $true)
[IO.Directory]::Delete('C:\ProgramData\MySQL\data', $true)
# Optional; fails if the folder is not empty (other MySQL installations):
[IO.Directory]::Delete('C:\ProgramData\MySQL')
```

What this does, in plain words:

* The first command checks that none of the folders is a link to somewhere else (a symbolic link, junction or mount point). It must print nothing. If it lists a folder, do not run the other commands; ask for help.
* The next commands stop the old MySQL service, prevent it from starting again, uninstall the Chocolatey package and delete the service.
* The last commands delete the old program folder and the old database folder. `[IO.Directory]::Delete` with `$true` deletes a folder with its content and does not follow links inside it ("This method does not recurse through the reparse point", Microsoft Learn, "Directory.Delete Method"). `C:\ProgramData\MySQL` itself is deleted only if it is then empty, because MySQL's own installer keeps data of other installations there.
* All databases of the old MySQL installation are lost. It never worked, so it contains no data of value.
* Commands for parts that no longer exist report an error; that is expected. The folder `C:\tools` itself may remain; it is no longer used by this project.

If the service `MySQL` belongs to another MySQL installation on the computer, the script does not print the service commands and warns instead; do not stop or delete that service for this project.

### Step 2: Run the setup

1. Check as administrator that the Windows Firewall is on and blocks inbound connections for all profiles, and that no rule allows inbound connections to the database or to file sharing (SMB, used for named pipes from other computers):

   ```powershell
   Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction
   Get-NetFirewallPortFilter -PolicyStore ActiveStore | Where-Object { $_.LocalPort -in '3306', '445' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallApplicationFilter -PolicyStore ActiveStore | Where-Object { $_.Program -like '*mysqld*' -or $_.Program -like '*mariadbd*' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Action Allow -Enabled True | Where-Object { $port = $_ | Get-NetFirewallPortFilter; $app = $_ | Get-NetFirewallApplicationFilter; $svc = $_ | Get-NetFirewallServiceFilter; -not $_.Owner -and -not $app.Package -and $app.Program -eq 'Any' -and $svc.Service -eq 'Any' -and $port.Protocol -in 'Any', 'TCP' -and $port.LocalPort -eq 'Any' } | Select-Object DisplayName, Profile
   ```

   The first command must show `Enabled True` and `DefaultInboundAction Block` for every profile; the other three must show nothing. If the second command lists "File and Printer Sharing" rules, disable them for the time of the setup or run the setup only on a trusted network. The fourth lists enabled inbound allow rules that are not restricted to a port, a program, a service or a Windows Store app; such a rule would also allow these connections. Rules with an owner or a package are network isolation rules of Windows Store apps and match only traffic of that app (Microsoft Learn, "New-NetFirewallRule", parameters `-Owner` and `-Package`), so they are not listed. Limit: the second command finds only rules whose local port is exactly 3306 or 445, not port ranges or lists that contain them; the commands do not evaluate remote address restrictions, rules for other protocols or the order of block and allow rules. If Windows asks whether `mysqld` may communicate on networks, deny it. Sources: Microsoft Learn, "Get-NetFirewallProfile" (`-PolicyStore ActiveStore` "retrieves the active profile conditions"), "Get-NetFirewallPortFilter", "Get-NetFirewallApplicationFilter", "Get-NetFirewallServiceFilter", "Get-NetFirewallRule" and "New-NetFirewallRule" (NetSecurity module).

2. Open a 64-bit PowerShell **as administrator** in the project directory and run:

   ```powershell
   .\scripts\local\setup-dev-windows.ps1
   ```

   The script first asks for a new password for the local MariaDB `root` accounts, before anything is installed. The password needs at least 15 characters, only printable ASCII characters, none of `#`, `"`, `'`, `\` and `;`, and no leading or trailing space, because it is written into an SQL string and into an option file, where these characters have a special meaning (MariaDB documentation, "Configuring MariaDB with Option Files"). Keep it in your password manager; the script does not store it.

   What the script then does, in plain words:

   * It stops without changing anything if the old MySQL installation of step 1, any Windows service named `MySQL` or `MariaDB`, or a MariaDB installation exists, or if one of the settings files `C:\my.ini`, `C:\my.cnf`, `%WINDIR%\my.ini` or `%WINDIR%\my.cnf` exists (MariaDB programs would read them). If a MariaDB service of an earlier, unfinished run of this script exists (interrupted, or failed before the end), it first stops and disables that service, because it may still run without a root password, and shows a red warning.
   * It removes the old entry `C:\tools\mysql\current\bin` from your user `PATH` (other entries and the value type are kept) and warns about MySQL folders in the machine-wide `PATH`, which it does not change.
   * It installs Python 3.14 with Chocolatey, unless the package `python314` is already installed.
   * It installs MariaDB 12.3 with the Chocolatey package `mariadb.install` into a folder below `C:\Program Files` chosen by the installer (by default `MariaDB 12.3`; only administrators can change files there). The installer is told to start MariaDB **without network access** (`SKIPNETWORKING=1`): at first, MariaDB can only be reached through a local named pipe called `MySQL`. The root password is not given to the installer, because it would then appear in the installer's command line and in Chocolatey's log.
   * It checks the installation: the program folder lies below `C:\Program Files` and contains no links to other places; nobody except Administrators, SYSTEM and TrustedInstaller can change files there; the service runs the MariaDB program from that folder with the settings file `my.ini` in the data folder, under its own service account `NT SERVICE\MySQL`; the settings file has networking switched off.
   * It turns off the Windows Firewall rule that the MariaDB installer adds for `mysqld.exe` (an inbound "allow" rule for any address). MariaDB listens only on this computer anyway; the rule is disabled, not deleted, so that uninstalling MariaDB can still remove it. An upgrade (`choco upgrade mariadb.install`) or a repair of MariaDB may create or enable the rule again; check afterwards with the third firewall command above and disable the rule (`Disable-NetFirewallRule`) if it is listed.
   * It restricts the data folder (the databases) to Administrators, SYSTEM and the service account. Without this, every user of the computer could read the database files, because the folder inherits read access for all users from `C:\Program Files`.
   * Over the named pipe, after checking that the pipe really belongs to the MariaDB service and waiting until no other program is connected to MariaDB, it removes accounts without a name, `root` accounts for other computer names, and the demo database `test` with its access for everybody (it would also have given everybody access to `test_leg_platform`), and sets the root password. The very first statement stops everything if another program is connected. Afterwards it checks that nothing was added or changed in the meantime: only the expected accounts exist (`root` for `localhost`, `127.0.0.1` and `::1`, the locked internal account `mariadb.sys`, and `PUBLIC`), none without a password, all `root` accounts with the same password and the normal password method only; only the system databases exist; there are no events and no triggers at all, and the stored programs and views are exactly the ones MariaDB 12.3.3 creates itself (by name and owner); the event scheduler is off; there are no extra plugins or functions; the privilege tables contain only what MariaDB creates itself; nobody else is connected; the query log is off; shared memory connections are off if the server has that setting at all (MariaDB 12.3 no longer has it, see D-030). The contents of MariaDB's own stored programs are not compared.
   * It creates the database `leg_platform` (with `utf8mb4` and `utf8mb4_uca1400_ai_ci`; it fails if the database already exists) and the account `leg_platform`@`127.0.0.1` with the privileges described in "Database privileges" below, on `leg_platform` and `test_leg_platform` only, and checks the stored grants.
   * It then switches MariaDB to network access on `127.0.0.1:3306` only (this computer only), turns the named pipe off (`my.ini`; there is no shared memory setting to write, because MariaDB 12.3 does not have one and would not start with an unknown option) and restarts it; the restart also undoes any setting changed at runtime in the meantime. It checks with `Get-NetTCPConnection` that MariaDB listens nowhere else, checks inside the server that `bind_address` is `127.0.0.1`, networking is on, the named pipe is off, shared memory connections are off or not available in the server, the port is 3306, the query logs are off and write only into the data folder, and the feedback plugin (which would send usage data to mariadb.org) is not active, and checks that the application account can log in. It also turns the event scheduler off permanently (`event-scheduler=DISABLED`; Django does not use events) and repeats all checks of the previous step over the network connection as `root`@`127.0.0.1` (which also tests that login), now including the application account, which must have no global privileges and exactly the privileges described below. A login as `root`@`::1` cannot be tested (MariaDB listens only on `127.0.0.1`); that account is checked to have the same password as the others.
   * It keeps the service start type `Manual` until everything has succeeded and sets it to `Automatic` at the end.
   * It stores the local configuration (`APP_ENV=local`, `DB_*`, `DJANGO_SECRET_KEY`) as user environment variables without printing generated values. `EMAIL_ALLOWED_RECIPIENTS` is not touched.

   Phase without a root password: the installer starts MariaDB with a `root` account without a password. This lasts until the script sets the password: the rest of the installation, the checks of the installation (the service is stopped during the permission checks), and the checks before the hardening. During this time MariaDB can only be reached through its named pipe, but any program on this computer can use that pipe, and so can a user of another computer who can sign in to this one over file sharing (SMB, port 445), because MariaDB does not refuse pipe connections from the network. The checks above detect other connections and the changes such a visitor could make inside MariaDB; they cannot detect files MariaDB might have been made to write in its own folders during that time. Therefore: run the script only when no untrusted user or program is active on the computer, and with file sharing blocked (firewall check above). Passwords are handed to the MariaDB client only in a temporary file that only administrators can read; it is created with these permissions and deleted right after use.

   The Chocolatey package adds the `bin` folder of the installation (by default `C:\Program Files\MariaDB 12.3\bin`) to the machine-wide `PATH`, so that the MariaDB command line tools can be used (e.g. `mariadb --host=127.0.0.1 --user=root --password`). Only administrators can change that folder.

   User environment variables are inherited by every process of the Windows user, including AI tools and their subprocesses. Never print the environment (e.g. `Get-ChildItem env:`, `set`, `env`); it contains `DB_PASSWORD` and `DJANGO_SECRET_KEY` (D-027).

### Step 3: Restart VS Code

Close and reopen VS Code (and every open terminal), so that they receive the new environment variables. All variables are described in `.env.example`.

Then create a virtual environment in the project directory and install the project with the newest compatible packages (D-015):

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install --upgrade -e .
```

`.venv` is ignored by Git. Run the checks and the tests:

```powershell
.venv\Scripts\python manage.py check --database default
.venv\Scripts\python manage.py makemigrations --check --dry-run
.venv\Scripts\python manage.py test
```

Without a running MariaDB server, only the tests that need no database can run: `manage.py test --exclude-tag mariadb`. With Django 6.1, the system checks that `check` and other commands such as `makemigrations` run also access the database ("The check management command now supplies all databases if not specified", Django 6.1 release notes), so these commands need a running MariaDB server.

Local development server (for design reviews and manual tests): `python manage.py runserver --noreload` needs `DJANGO_DEBUG=True` or `DJANGO_ALLOWED_HOSTS` (for example `127.0.0.1,localhost`) in the environment of that process, because Django refuses to start the server with `DEBUG` off and no allowed hosts; both are only allowed with `APP_ENV=local`. Set them for the process only (in PowerShell: `$env:DJANGO_DEBUG = 'True'` in the terminal that starts the server). The server listens on `http://127.0.0.1:8000/`, the value `INSTANCE_BASE_URL` has locally by default. Pages that need an account use the throw-away accounts of `seed_design_accounts` (see "Instance Configuration"). The complete start, check, stop and test procedure for agents is in `docs/STARTUP.md`.

Email: locally, emails are written to the console by default (`EMAIL_MODE=console`). To test delivery via Microsoft Graph locally, use `EMAIL_MODE=allowlist` with a dedicated development app registration and sender mailbox, never the credentials of stage or production, and set the allowed recipient addresses only as the user environment variable `EMAIL_ALLOWED_RECIPIENTS` (D-028). The Microsoft Graph backend is implemented and authenticates with a certificate (D-006); the admin section for its configuration is not implemented yet. Until a configuration exists, every send attempt in this mode fails with an error and no email is sent. Automated tests never send real email.

### Instance Configuration

Design test accounts: for design reviews, `python manage.py seed_design_accounts` creates one throw-away account per access level (an authenticated user, a staff user without any role, a system administrator; addresses under `example.org`) in the local database. It refuses to run unless `APP_ENV=local`, writes the generated passwords and the authenticator keys of the two privileged accounts to a file outside the repository (default `.leg-platform-design-accounts.json` in the home directory; created once, never overwritten, owner-only on POSIX systems) and prints no secret. `--code` prints the current authenticator codes. Never run it against a database with real data (D-019).

Each instance sets its own values in the environment (`.env.example`, D-013, D-037). They are validated at startup, and invalid values prevent the start:

* `INSTANCE_NAME`: shown in pages and email subjects (optional, default "LEG Platform").
* `INSTANCE_BASE_URL`: the canonical `https://` origin of the application, e.g. `https://portal.example.org` (no path). Links in emails are built from it, never from the request, so it must be correct before any email is sent. Its host must also be in `DJANGO_ALLOWED_HOSTS`. Required outside `APP_ENV=local`.
* `INSTANCE_PRIVACY_URL`: absolute `https://` URL of the instance's privacy notice, linked at registration and on all pages without login. Required outside `APP_ENV=local`.
* `INSTANCE_WEBSITE_URL`: absolute `https://` URL of the instance's website (optional).
* `EMAIL_SENDER_ADDRESS`: sender address of all emails (optional; default `noreply@<host of INSTANCE_BASE_URL>`, locally `noreply@localhost`). If it is set, it replaces the default; the emails never invite a reply (D-038).

Email delivery (D-038):

* Emails are not sent during the request. The worker `python manage.py process_mail_queue --loop` must run as a supervised service (restart on failure); without it, no registration, reset or notification email is sent. Without `--loop` it processes the queue once and exits. Delivery is at least once: after a crash an email can be sent twice. A job is tried three times and then given up; unprocessed jobs are deleted after one hour (they hold the address in plain text until then).
* The sender domain needs SPF, DKIM and DMARC records for the sending service, and the sender address must be one the service may use. Automatic emails do not invite a reply, so the sender mailbox need not be monitored; a mailbox that does receive replies can contain personal data.
* The Microsoft Graph backend applies a timeout of 30 seconds to its requests (`GRAPH_TIMEOUT_SECONDS` in `leg_platform/mail.py`); Django forbids `EMAIL_TIMEOUT` together with `MAILERS`.
* The Microsoft Graph backend does not send the header `Auto-Submitted`. Microsoft Learn (message resource type, https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0, checked 2026-10-14) states: "Add custom headers only when creating a message, and name them starting with "x-"." (D-048). Automatic replies (out-of-office) of the sender mailbox must therefore stay switched off (D-006).

Microsoft Graph sender (D-006; the steps are done by the operator in Microsoft Entra ID and Exchange Online; the configuration and the test send are in the section "Microsoft Graph configuration" of the Django administration, which only system administrators see: enter tenant ID, client ID and sender address, then upload the certificate and the private key together; the private key is never shown again; the test send goes only to the address of the logged-in administrator and is limited to 12 per hour and 24 per 24 hours). Sources: Microsoft Learn, RBAC for Applications (https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac) and New-ManagementScope (https://learn.microsoft.com/en-us/powershell/module/exchangepowershell/new-managementscope), checked 2026-10-14.

1. In Microsoft Entra ID, register one application for the instance, upload its certificate (public part only), and add the application permission `Mail.Send` with admin consent. The permission is removed again in step 5.
2. Note the application (client) ID and the object ID of the enterprise application (service principal). Do not use the IDs of the app registration page; they differ.
3. Connect to Exchange Online PowerShell as a member of the Organization Management role group, and replace the placeholders with the values of the instance:

   ```powershell
   New-ServicePrincipal -AppId <application (client) ID> -ObjectId <object ID of the enterprise application> -DisplayName "LEG-Platform sender"
   New-ManagementScope -Name "LEG-Platform sender mailbox" -RecipientRestrictionFilter "EmailAddresses -eq 'smtp:<sender address>'"
   New-ManagementRoleAssignment -App <application (client) ID> -Role "Application Mail.Send" -CustomResourceScope "LEG-Platform sender mailbox"
   ```

4. Test the assignment: `Test-ServicePrincipalAuthorization -Identity <application (client) ID> -Resource <sender address>` must show the `Mail.Send` permission with `InScope` set to `True`. Changes take effect for sending after the cache of Exchange Online is refreshed (30 minutes to 2 hours).
5. In Microsoft Entra ID, remove the unscoped `Mail.Send` permission. Otherwise the permission of Entra ID and the scoped permission add up to access to all mailboxes.
6. Switch off automatic replies of the sender mailbox (see above); the cmdlet is `Set-MailboxAutoReplyConfiguration -Identity <sender address> -AutoReplyState Disabled` (Microsoft Learn, checked 2026-10-14).

The sender address receives replies and bounces. Its mailbox content must therefore be protected like the other mailboxes of the instance. The sender address configured in the application must be the same address as the mailbox in step 3.

Upload size of the certificate (D-048): the administration accepts the certificate and the private key as files of at most 16 KiB (16384 bytes) each. The application checks this only after the whole request has been received. Django's `DATA_UPLOAD_MAX_MEMORY_SIZE` does not limit file uploads, and under ASGI the entire request may be spooled to disk before Django enforces its limit (Django documentation, https://docs.djangoproject.com/en/6.0/ref/settings/#data-upload-max-memory-size, checked 2026-10-14). The web server or reverse proxy in front of the application must therefore limit the request body size of the instance. The value of this limit is not decided yet (D-048).

Scheduled tasks (the scheduler itself is decided with the hosting, D-008, D-037, D-038, D-041):

* `python manage.py run_scheduled_tasks` runs every cleanup task once: `delete_expired_registrations` (unverified accounts whose verification link expired or does not exist), `cleanup_mail_requests` (unused request counters, expired reset and email change links, unprocessed jobs), `cleanup_login_ip_attempts` (see below), `cleanup_security_log` (security log entries after their retention) and Django's `clearsessions` (expired sessions). Schedule this one command, for example every 15 minutes, with the scheduler of the server (cron or a systemd timer on Linux, the task scheduler on Windows); the exact setup is written down when the hosting is decided. A failing task does not stop the others; the command then exits with an error, which the scheduler must report. A test makes sure that every `cleanup_*` and `delete_expired_*` command is part of it.
* The worker `process_mail_queue --loop` is not part of it; it runs as a supervised service (see above).

Initial system administrator (D-005, D-041): set `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` in the environment of the instance and run `python manage.py setup_initial_administrator` in a terminal on the server. The command asks for the password twice with hidden input (it refuses to read it from a pipe or a file), runs only while no system administrator exists, never changes an existing account and logs its use. Give the person the initial password through a secure channel; at the first login the password must be replaced and two-factor authentication must be set up before anything else is possible. Remove `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` afterwards.

Emergency commands (D-004, D-005, D-041), only on the server, each use is logged (kept for the long period) and asks for confirmation (`--no-input`, given before the action, skips it). The accounts are named by their internal number, shown on the change form of an account in the administration:

* `python manage.py emergency_access reset-second-factor N`: removes the second factor of any account, also of the last system administrator; all sessions of the account end and its owner is notified by email.
* `python manage.py emergency_access unblock-account N`: sets a blocked account active again.
* `python manage.py emergency_access reencrypt-second-factor-keys`: encrypts all second factor keys again with the current `SECRET_KEY` (see "Changing `SECRET_KEY`"); it reports the keys that cannot be decrypted.

Which path applies when an account is locked out (D-044):

* Password forgotten, second factor available: the person clicks "Forgot password?" on the login page, opens the link in the email (single use, valid 24 hours), sets a new password and logs in with the second factor. No operator action is needed.
* Second factor lost: a system administrator resets it on the change form of the account in the administration. For users, the reset is sent as a link to the owner; for staff users and system administrators it takes effect at once.
* Second factor lost and no other system administrator available: run `python manage.py emergency_access reset-second-factor N` on the server. The person then logs in with the password (set with "Forgot password?" if it is lost too) and sets up a new second factor; staff users and system administrators need one (D-042).
* Account blocked: run `python manage.py emergency_access unblock-account N` on the server. A blocked account receives no password reset email.
* Staff users and system administrators must type their email address to confirm an emergency command; `--no-input` is refused for them. Use `--allow-last-administrator` only if losing the last system administrator's second factor is intended.

Security log (D-018, D-041): the system administrators see it in the administration; nobody can change or delete entries there. Entries are deleted only by `cleanup_security_log` (part of `run_scheduled_tasks`) after `SECURITY_LOG_RETENTION_DAYS` (default 30) or, for changes of rights and roles, resets of the second factor and emergency commands, `SECURITY_LOG_LONG_RETENTION_DAYS` (default 365). Persons with direct database access can change the table; this is accepted, because whoever can log in to the server can do everything anyway (D-041).

Session timeouts (D-041): users are logged out after 30 minutes without a request or 8 hours after the login; staff users and system administrators after 15 minutes and 4 hours. They are set in `leg_platform/settings.py` (not per instance).

Customer numbers: from 90 % of the six-digit numbers issued, each issue logs a warning (logger `leg_platform.accounts.models`, "N of 1000000 customer numbers have been issued"); set up an alarm on it in the log monitoring, because the administration notice is visible only to system administrators who are logged in (D-038, D-052).

Request limits per client IP address use the address that Django sees (`AXES_IPWARE_PROXY_COUNT`, see below); with a wrong proxy count every request counts against one shared budget or, without any resolvable address, one strict shared budget (a warning is logged). **A wrong count now has a visible effect (D-058): when the shared budget of the client address is used up, every visitor of the instance gets "Zu viele Anfragen, bitte versuchen Sie es später erneut." (HTTP 429) on registration, password reset and email change. Check the count before go-live and watch the log line "Requests that send email are refused" (at most once a minute).** The reverse proxy should additionally limit the plain request rate, because the application limits only the functions that send email.

Web server and reverse proxy access logs are outside the application. The verification link contains a secret token in its path (`/register/verify/<token>/`); the application removes it from its own logs, but the operator must configure the web server and proxy so that their access logs do not keep it (or restrict and rotate them).

### Trusted Proxies for Login IP Protection

`AXES_IPWARE_PROXY_COUNT` defaults to `0`: login abuse protection uses the address of the direct TCP peer (`REMOTE_ADDR`) and ignores `X-Forwarded-For`. If Django is reached only through reverse proxies, set this variable to the exact number of proxies between the client and Django. Set it only after verifying that each proxy removes or validates client-supplied forwarding headers and appends its own address. IPware validates the chain strictly; an unexpected or malformed chain produces no client IP and the login is refused. A wrong value can group users under a proxy address or refuse logins, so the deployment value must be verified against the actual proxy topology before enabling a stage or production instance. Client IPs are not stored in Axes; the independent lockout counter stores only a keyed digest (D-017, D-023).

The `cleanup_login_ip_attempts` management command removes expired pseudonymous IP attempts, empty buckets, expired account attempts of the abuse protection and second factor setups that were never confirmed. It takes each bucket in a short transaction and skips one that a request holds, so it never waits for a request. A stage or production scheduler must run it regularly (for example every 15 minutes) as part of `run_scheduled_tasks`; the schedule is not yet configured because the hosting setup is still open (D-008, D-018, D-040, D-041).

### Changing `SECRET_KEY`

Second factor keys are encrypted with a key derived from `SECRET_KEY` and recovery codes are stored as digests under it (D-040). When `SECRET_KEY` is replaced, put the old value into `DJANGO_SECRET_KEY_FALLBACKS`: keys and recovery codes then keep working, and a key is encrypted again with the new one the next time it is used. Do not remove the old value from the fallbacks before every active account with a second factor has logged in with it once; the accounts that did not need their second factor reset by a system administrator. To encrypt all keys again at once, run `python manage.py emergency_access reencrypt-second-factor-keys` (D-041); after it, the old value can be removed from the fallbacks once the command reports no key that cannot be decrypted (recovery codes cannot be converted: they keep working only while the old value stays in the fallbacks; after it is removed they stop working, and their owners create new ones after logging in with the app). The Microsoft Graph private key has the same rule (D-006).

### Recovery after a failed setup

If a step after the MariaDB installation fails or the script is interrupted with Ctrl+C, the script first waits until Windows Installer has finished (at most 10 minutes; the installer may still start the service), then tries to stop the `MySQL` service and set its start type to `Disabled` and checks the result. If it reports that this did not work, or if the console window was closed during the setup (the cleanup may then not run), stop and disable the service yourself as administrator (`sc.exe stop MySQL`, `sc.exe config MySQL start= disabled`) and check with `Get-Service MySQL | Select-Object Status, StartType`. The next run of the script also stops and disables a MariaDB service whose setup was not finished (start type not `Automatic`, or networking still off or the named pipe still on in `my.ini`) before it does anything else.

Fix the cause shown by the script and run as administrator:

```powershell
.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
```

The script removes the MariaDB installation automatically only if exactly one MariaDB is installed, below `C:\Program Files`, with the Chocolatey package `mariadb.install`, and the service `MySQL` (if it exists) runs the program of that installation. It lists what it will remove, asks you to type `DELETE` and asks for the new `root` password. It then checks everything again, determines the product code of the MariaDB installer package from the hidden uninstall entry with the same name and folder (it stops without further changes unless there is exactly one), stops the service and sets it to `Disabled`, uninstalls MariaDB directly with Windows Installer (`msiexec.exe /x <product code> /qn /norestart`; the installer removes its service and, by default, its data folder; exit code 3010 means success with a restart required: the script then completes the removal and stops, and you restart Windows and run it again without `-RemoveFailedInstallation`), removes the Chocolatey package records without running their scripts (`choco uninstall <package> -y -n --skip-autouninstaller`, `mariadb` if present, then `mariadb.install`), removes the entry `<MariaDB folder>\bin` from the machine-wide `PATH` (every other entry stays unchanged and in order), deletes a remaining service only if it belongs to that installation, deletes the program folder if it is left over (only if it lies below `C:\Program Files` and contains no links), and continues with the normal setup. All local MariaDB databases are lost.

If the removal is interrupted after Windows Installer has uninstalled MariaDB, a new run finds no MariaDB installation but still the Chocolatey package records, and stops without changes. Then, in a PowerShell window opened as administrator: run `choco uninstall mariadb -y -n --skip-autouninstaller` (only if that package is listed by `choco list`) and `choco uninstall mariadb.install -y -n --skip-autouninstaller` (a plain `choco uninstall` fails, see D-030), remove the entry `C:\Program Files\MariaDB <version>\bin` from the machine-wide `PATH` (System Properties, Environment Variables), delete the folder `C:\Program Files\MariaDB <version>` if it still exists, sign out and in again, and run the script again.

The package's own uninstall script is not used: in `mariadb.install` 12.3.3 it passes the uninstall command to a Chocolatey helper that ignores it for Windows Installer packages, so `msiexec` runs without a product and fails with exit code 1619 (D-030).

If the script reports that the service still exists after `sc.exe delete`, close the Services console and Task Manager or restart Windows, and run the command again; every step checks the current state and can be repeated. User environment variables of an earlier run are overwritten by a successful run.

The old MySQL installation is never removed automatically; see step 1. For any other MariaDB or MySQL installation or service, the script changes nothing; check it and remove it manually if it is no longer needed.

The `PATH` cleanup and the user environment variables apply to the Windows account that runs the script. Run it elevated with your own account ("Run as administrator"); the script warns if the account differs from the signed-in user.

The helper functions of the setup script are tested without changing the system (no administrator rights needed; the creation of the temporary option file with the owner Administrators is tested only in an elevated PowerShell):

```powershell
powershell -NoProfile -File scripts\local\test-setup-dev-windows.ps1
pwsh -NoProfile -File scripts\local\test-setup-dev-windows.ps1
```

### Database privileges

The application account receives only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX`, granted on the database level, and no global privileges:

* `SELECT`, `INSERT`, `UPDATE`, `DELETE`: data access of the application.
* `CREATE`, `DROP`, `ALTER`, `INDEX`: schema changes by Django migrations. `REFERENCES` is not granted: MariaDB lists it as "Unused" (MariaDB documentation, "GRANT").
* Locally also on `test_leg_platform`: "The test database is created by the user specified by `USER`" (Django documentation, "Writing and running tests"); Django runs `CREATE DATABASE` and `DROP DATABASE` for it (source `django/db/backends/base/creation.py`). MariaDB allows granting `CREATE` on databases "that do not yet exist" (MariaDB documentation, "GRANT").
* In database-level grants, `_` is a wildcard ("database names in GRANT can contain % and _ wildcards", MariaDB documentation, "GRANT"); the script escapes it (`leg\_platform`), so that the grant covers exactly the named databases, and checks the stored names.
* Character set and collation: every database of an instance must be created with `CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci` (available from MariaDB 10.10.1 on; D-030, D-031). MariaDB 10.11 would otherwise create it with `latin1`, because that is its default server character set (`utf8mb4` is the default only from MariaDB 11.6). The setup script does this locally; operators of stage and production do it when they create the instance's database, e.g. `CREATE DATABASE leg_platform CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;`. Django creates its test database with the same settings (`TEST` in the settings). Identifier columns (normalised email address, tokens, customer number) will use the exact collation `utf8mb4_bin` per column (D-031).
* Parallel test runs (`manage.py test --parallel`) are not supported with this account: they clone the test database into further databases (`test_leg_platform_1`, ...), which needs other privileges and databases.

Pattern for stage and production: the same privileges, only on the instance's own database, no test database and no global privileges, for an account that can connect only from the application host. `DROP` on the database level also allows dropping the whole database; a stricter split into a migration account (schema privileges) and a runtime account (`SELECT`, `INSERT`, `UPDATE`, `DELETE` only) is decided together with the hosting details (D-008).

The privileges were taken from the sources above and verified on 2026-10-12 on the development machine: creating the test database (which runs all migrations) and the full test suite ran with this account (D-030).

Apply migrations with `python manage.py migrate` only to a database created for this project; the custom user model `accounts` (D-021) is part of the migrations. The test runner creates and removes its own test database.

Continuous integration (`.github/workflows/ci.yml`) runs the same checks and tests with the minimum versions (Python 3.12, Django 6.0, MariaDB 10.11) and with the newest versions (Python 3.14, Django 6.1, MariaDB 12.3), with an application account that has exactly the privileges above (created by root in a CI step).
