# Local Startup for Agents and Developers

Status: 2026-10-14; section 13 updated on 2026-10-16 (D-071, steps 3a and 3b; D-072). Checked against the code of the local worktree (Python 3.14.8, Django 6.1.2, MariaDB 12.3 on Windows 11, PowerShell). Items marked **UNVERIFIED** were not run; they are derived from the code or from official documentation (section 15).

This document is written so that a **designer agent** (browser based) and a **tester agent** can start, check, use and stop the complete local environment without asking questions. It applies to the local development machine only (`APP_ENV=local`, throw-away data). Stage and production are in `docs/DEPLOYMENT.md`; the one-time installation of Python and MariaDB is in `docs/INSTALLATION.md` ("Local Development").

## 1. Rules (read first)

Never do any of the following (`AGENTS.md` sections 8 and 9, `CONDUCT.md`, D-020, D-027):

* Never print, log or screenshot the process environment or any secret: no `Get-ChildItem env:`, `dir env:`, `set`, `printenv`, `[Environment]::GetEnvironmentVariables()`, `manage.py diffsettings`, `manage.py dumpdata`, `manage.py dbshell`.
* Never open `.env` or any other secret file, and never write a secret into a file, a report or a commit. Names of variables may be written, values never.
* Never use stage or production credentials, hosts or data. This environment must only connect to the local MariaDB on `127.0.0.1` with `APP_ENV=local`. Never use real personal data; use addresses under `example.org`, `example.com`, `example.net`, `.test` or `.invalid` (RFC 2606).
* Never delete or recreate the seeded design accounts or their credentials file, never run `flush`, never drop the database, and never delete the directory file of section 10, unless the main session or the project owner explicitly orders it (section 12).
* Never stop a process by name. Stop only by process ID (`Stop-Process -Id <PID>`); `Stop-Process -Name`, `taskkill /IM` and similar are forbidden. Another session may run its own server, worker or test run; do not stop processes that you did not start (section 11).
* Never run two test runs at the same time against the same MariaDB server (D-046, section 13): the runner uses one test database. Before a run check that none is active: `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'manage.py test' -and $_.Name -match 'python' }` must print nothing (the command line of your own shell may match too: look at the process names).
* Never commit or push; the main session commits.
* Never "fix" application code during a design review or a test run; report defects to the main session.

## 2. What runs where

| Part | Where | Notes |
| --- | --- | --- |
| MariaDB 12.3 | Windows service `MySQL` (the name the setup script gives the MariaDB service), `127.0.0.1:3306` only | Created by `scripts/local/setup-dev-windows.ps1`. Databases `leg_platform` (data) and `test_leg_platform` (created and dropped by the test runner). |
| Python environment | `.venv` in the repository root | `.venv\Scripts\python.exe`. Created as described in `docs/INSTALLATION.md`. |
| Development server | `http://127.0.0.1:8000/` (the designer and the acceptance); `http://127.0.0.1:8001/` (the developer's own server) | `manage.py runserver 127.0.0.1:8000 --noreload --insecure`. Use the host `127.0.0.1`, never `localhost`: the cookies are host-only and the links in emails use `127.0.0.1`. **Port 8000 belongs to the session that checks the design or accepts the work; the developer starts an own server on port 8001 (`$Port = 8001`, section 6) and never on 8000 while another session uses it.** The server has no reload and caches templates: restart it after a change of a template. |
| Mail worker | background process | `manage.py process_mail_queue --loop`. Required, see section 6. |
| Address directory (optional) | SQLite file outside the repository | Synthetic data only, section 10. |
| Logs and process IDs | `$HOME\.leg-platform-dev\` | Outside the repository. Nothing there is committed. |

All commands below are PowerShell and run from the repository root (the folder that contains `manage.py`). Every tool call starts a fresh process: environment variables set with `$env:NAME = ...` live only for that call, and a block that starts a process must set what the process needs in the same call.

## 3. Prerequisites check (read-only)

```powershell
Test-Path .\manage.py                                       # True
Test-Path .\.venv\Scripts\python.exe                        # True
.\.venv\Scripts\python.exe --version                        # Python 3.14.x (3.12 or newer is supported)
.\.venv\Scripts\python.exe -c "import django; print(django.get_version())"   # 6.1.x
Get-Service -Name MySQL | Select-Object Name, Status, StartType               # Running, Automatic
Get-NetTCPConnection -State Listen -LocalPort 3306 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort   # LocalAddress 127.0.0.1 only
Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, OwningProcess   # empty = port free
```

Check that the configuration exists without printing any value (this prints only `set` or `NOT SET`, and whether `APP_ENV` is `local`):

```powershell
foreach ($name in 'APP_ENV', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT', 'DJANGO_SECRET_KEY') {
    '{0}: {1}' -f $name, $(if ([Environment]::GetEnvironmentVariable($name, 'User')) { 'set' } else { 'NOT SET' })
}
'APP_ENV is local: ' + ([Environment]::GetEnvironmentVariable('APP_ENV', 'User') -eq 'local')
'DB_HOST is 127.0.0.1: ' + (([Environment]::GetEnvironmentVariable('DB_HOST', 'User') -in @('127.0.0.1', $null, '')))
```

Everything must be `set`, `APP_ENV is local: True` and `DB_HOST is 127.0.0.1: True`. If not, stop and report to the main session; never "repair" it by printing or editing the variables yourself.

If MariaDB is not running, starting the service needs administrator rights (`Start-Service MySQL` from an elevated PowerShell). Report it to the main session instead of trying to work around it.

## 4. Configuration

The application reads its configuration only from environment variables (`leg_platform/settings.py` passes `os.environ` to the loader, D-027); it never reads `.env`. `leg_platform/environment.py` validates them and refuses to start on missing or unsafe values.

| Name | Where it comes from | Meaning locally |
| --- | --- | --- |
| `APP_ENV` | user environment variable, set by the setup script | `local`. Required. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | user environment variables, set by the setup script | The local MariaDB and its application account. |
| `DJANGO_SECRET_KEY` | user environment variable, set by the setup script | Local secret key. |
| `EMAIL_MODE` | not set; the default `console` applies | Emails are written to the standard output of the process that sends them, which is the mail worker (section 6). Never use `allowlist` locally unless the project owner provides a dedicated development registration (D-028); `graph` is refused locally. |
| `DJANGO_ALLOWED_HOSTS` | set per process in the start block (section 5) | `127.0.0.1,localhost`. Django refuses to start `runserver` with debug off and no allowed hosts. Do not set it as a permanent user variable. |
| `INSTANCE_BASE_URL` | not set; the default `http://127.0.0.1:8000` applies | Base of all links in emails. Only if you use another port, set `http://127.0.0.1:<port>` for the server and the worker (its host must be in `DJANGO_ALLOWED_HOSTS`). |
| `INSTANCE_NAME`, `INSTANCE_WEBSITE_URL`, `INSTANCE_STATIC_DIRECTORY`, `INSTANCE_LOGO_LIGHT`, `INSTANCE_LOGO_DARK` | per process, in the start block of section 6 | The local instance presentation of the design package (D-056); since D-073 these are the **fallback**: a value that is saved in "Portal-Einstellungen" (including uploaded logos, which are stored in the local database) wins. The name "LEG-Portal", the website `https://leg-ittigen.ch` (the third item of the footer row), the directory `$HOME\.leg-platform-dev\instance-static` that holds the two logo files unchanged in its sub-folder `instance` (copied from `LEG-Design-Vorgaben\logo\`: `LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg`; they contain no script and no external reference), and their relative paths `instance/LEG-Ittigen_pos_rgb.svg` and `instance/LEG-Ittigen_neg_sw.svg`. Without them the pages show the instance name as text instead of the logo. These are public values and no secrets. |
| `DJANGO_DEBUG` | not set (False) | `True` is allowed only with `APP_ENV=local`. Not needed: production-like error handling and the full Content Security Policy are the better basis for a design review. |
| `LEG_LOCAL_ADDRESS_DIRECTORY_FILE` | optional, per process | Path of the SQLite file of section 10. Default: `$HOME\.leg-platform-local-address-directory.sqlite3`. |

Must not be set locally: `ADDRESS_IMPORT_DB_USER` and `ADDRESS_IMPORT_DB_PASSWORD` (the normal settings refuse to start when they are present, `leg_platform/address_configuration.py`), `ADDRESS_DB_*` (there is no local MariaDB directory; section 10 explains the SQLite alternative), `BOOTSTRAP_SYSTEM_ADMIN_EMAIL`, `EMAIL_ALLOWED_RECIPIENTS`.

## 5. Database and migrations

The local database `leg_platform` is a **throw-away database**: all its data is test data (D-027, D-030). The development machine already has it migrated. Check without changing anything:

```powershell
.\.venv\Scripts\python manage.py check
.\.venv\Scripts\python manage.py showmigrations --plan | Select-String -Pattern '\[ \]'
```

`check` must print `System check identified no issues (1 silenced).` (with Django 6.1 it also needs the running MariaDB, because it checks all databases). The second command must print nothing; any line is an unapplied migration.

Apply pending migrations to the **default database** only after the main session or a code change requires it, and never while a test run is active:

```powershell
.\.venv\Scripts\python manage.py migrate --database=default
```

The router (`leg_platform/addresses/router.py`) keeps the queue table `addresses_importrequest` in the default database; the directory tables of the optional address database are never created by this command.

The **optional address directory** is a separate database alias (`addresses` for reading, `address_import` for writing, D-049, D-052). Locally it is a throw-away SQLite file with synthetic data, provided by `leg_platform.local_directory_settings` (section 10). Its tables are created by a separate command with that settings module:

```powershell
.\.venv\Scripts\python manage.py migrate addresses --database=address_import --settings=leg_platform.local_directory_settings
```

Stage and production use `leg_platform.import_settings` for the same purpose (`docs/INSTALLATION.md`, "Local Address Directory"); it needs the importer credentials and must not be used locally.

## 6. Start the development server and the mail worker

Registration, password reset and email change **only queue** an email (D-038). The mail worker takes the job, issues the link and sends it. Without the worker, no verification or reset email is ever produced (symptom: the "email sent" page appears and nothing arrives). With `EMAIL_MODE=console`, the email is printed to the **standard output of the worker**, so the worker's output must be captured to a file that you can read.

Start both with one block. It refuses to start if port 8000 is taken (it never kills anything), writes logs and process IDs to `$HOME\.leg-platform-dev\` and sets the variables for both child processes. The logo files must exist once in `$HOME\.leg-platform-dev\instance-static\instance\` (one-time copy, no overwrite of other content: `New-Item -ItemType Directory -Force "$HOME\.leg-platform-dev\instance-static\instance"; Copy-Item <path of LEG-Design-Vorgaben>\logo\*.svg "$HOME\.leg-platform-dev\instance-static\instance"`); without them the pages show the name as text:

```powershell
$Repo = (Get-Location).Path
$State = Join-Path $HOME '.leg-platform-dev'
New-Item -ItemType Directory -Force -Path $State | Out-Null
$Port = 8000
$Settings = 'leg_platform.settings'     # use 'leg_platform.local_directory_settings' to enable address suggestions (section 10)

if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    "Port $Port is in use. Do not kill the process. Use the running server, or choose another port and set INSTANCE_BASE_URL (section 4)."
} elseif ((Test-Path "$State\runserver.pid") -or (Test-Path "$State\worker.pid")) {
    'A PID file exists. Run the status block of section 8 first; remove a PID file only if it is stale.'
} else {
    $env:DJANGO_ALLOWED_HOSTS = '127.0.0.1,localhost'
    if ($Port -ne 8000) { $env:INSTANCE_BASE_URL = "http://127.0.0.1:$Port" }
    # Local instance presentation (section 4): name, website and the two logo files of the design package.
    $env:INSTANCE_NAME = 'LEG-Portal'
    $env:INSTANCE_WEBSITE_URL = 'https://leg-ittigen.ch'
    $instanceStatic = Join-Path $State 'instance-static'
    if (Test-Path "$instanceStatic\instance\LEG-Ittigen_pos_rgb.svg") {
        $env:INSTANCE_STATIC_DIRECTORY = $instanceStatic
        $env:INSTANCE_LOGO_LIGHT = 'instance/LEG-Ittigen_pos_rgb.svg'
        $env:INSTANCE_LOGO_DARK = 'instance/LEG-Ittigen_neg_sw.svg'
    }
    $python = Join-Path $Repo '.venv\Scripts\python.exe'

    $server = Start-Process -FilePath $python -WorkingDirectory $Repo -WindowStyle Hidden -PassThru `
        -ArgumentList '-u', 'manage.py', 'runserver', "127.0.0.1:$Port", '--noreload', '--insecure', "--settings=$Settings" `
        -RedirectStandardOutput "$State\runserver.out.log" -RedirectStandardError "$State\runserver.err.log"
    $server.Id | Set-Content "$State\runserver.pid"

    $worker = Start-Process -FilePath $python -WorkingDirectory $Repo -WindowStyle Hidden -PassThru `
        -ArgumentList '-u', 'manage.py', 'process_mail_queue', '--loop' `
        -RedirectStandardOutput "$State\worker.out.log" -RedirectStandardError "$State\worker.err.log"
    $worker.Id | Set-Content "$State\worker.pid"
    'Started. Logs: ' + $State
}
```

Facts about this block (verified):

* `--noreload` avoids the second watcher process; `--insecure` lets `runserver` serve `/static/` although debug is off (Django `staticfiles` `runserver`: static files are served only `if settings.DEBUG or insecure_serving`). Without it the pages have no CSS.
* The virtual environment's `python.exe` is a launcher that starts the real interpreter as a child process. Each of the two processes therefore appears twice in the process list (a parent and a child, plus a hidden `conhost.exe` below the parent). The PID file holds the parent; section 11 stops both.
* `-u` makes the output unbuffered so the logs are readable while the processes run. Django's console email backend also flushes after each message (`django/core/mail/backends/console.py`).
* The development server logs requests to `runserver.err.log` (logger `django.server`, standard error); tokens of verification links in these request lines are replaced by `<redacted>` (`leg_platform/log_filters.py`). The console email in `worker.out.log` contains the **complete link**; that is why it exists only locally (`EMAIL_MODE=console` is refused in stage and production).
* If the server does not come up, read `runserver.err.log` (section 14).

Wait about five seconds, then run the health checks of section 7.

## 7. Health checks (read-only)

```powershell
curl.exe -s -o NUL -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8000/            # 302 http://127.0.0.1:8000/login/
curl.exe -s -o NUL -w '%{http_code} %{content_type}\n' http://127.0.0.1:8000/login/      # 200 text/html; charset=utf-8
curl.exe -s -o NUL -w '%{http_code} %{content_type}\n' http://127.0.0.1:8000/static/vendor/tabler/css/tabler.min.css   # 200 text/css
curl.exe -s -o NUL -w '%{http_code}\n' http://127.0.0.1:8000/admin/                      # 302 (to /admin/login/, which leads to the portal login)
curl.exe -s -o NUL -w '%{http_code}\n' http://127.0.0.1:8000/does-not-exist/              # 404
curl.exe -sI http://127.0.0.1:8000/login/                                                 # shows Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
```

Use `curl.exe`, not `curl` (in Windows PowerShell `curl` is an alias of `Invoke-WebRequest`). Expected results come from the URL configuration (`leg_platform/urls.py`, `leg_platform/accounts/urls.py`, `HomeView`) and from the tests `test_admin.py` and `test_second_factor.py`; they are not fixed by a health endpoint, because the application has none.

Worker check: `Get-Content "$HOME\.leg-platform-dev\worker.err.log" -Tail 20` must show no repeated error lines. The worker prints `Processed N mail jobs, M failed.` to `worker.out.log` only when it processed something.

## 8. Status of the running processes (read-only)

```powershell
$State = Join-Path $HOME '.leg-platform-dev'
$all = Get-CimInstance Win32_Process
foreach ($name in 'runserver', 'worker') {
    $pidFile = Join-Path $State "$name.pid"
    if (-not (Test-Path $pidFile)) { "${name}: no PID file (not started by this document)"; continue }
    $launcherId = [int](Get-Content $pidFile)
    $launcher = $all | Where-Object ProcessId -eq $launcherId
    if (-not $launcher -or $launcher.CommandLine -notlike '*.venv\Scripts\python.exe*manage.py*') {
        "${name}: PID $launcherId is not running or is not ours (stale PID file)"; continue
    }
    "${name}: running. To stop it, stop these PIDs, child first:"
    $all | Where-Object { $_.ParentProcessId -eq $launcherId -and $_.Name -eq 'python.exe' } | ForEach-Object { "  child  $($_.ProcessId)" }
    "  parent $launcherId"
}
```

The command line check protects against a reused process ID: only a process whose command line contains the repository's `.venv\Scripts\python.exe` and `manage.py` is ever reported as ours.

## 9. Test accounts

### 9.1 Seeded design accounts

**What a login shows (design package, D-056 to D-059).** After the login the start page `/` opens in the new frame (header with the logo, the name and the two corner buttons; on a desktop the left menu, on a phone the bottom bar and the menu that opens from the left). The seeded user sees two task tiles ("Adresse ergänzen" is not shown for an account with a complete address; "Zwei-Faktor-Anmeldung" while no second factor is set up). "Ausblenden" hides a tile for good on the server (`HiddenTask`); the empty state "Keine offenen Aufgaben" follows. To look at the tiles again after an acceptance, delete the rows of the design user in `accounts_hiddentask` (the administration shows the hidden tasks of an account read-only on its page, and a one-line script with the Django shell does the deletion; never touch other accounts). The profile (`/profile/`) has five cards that open in place. The administration (`/admin/`, only staff users and system administrators with a completed second factor) shows the groups "Benutzer", "Inhalte" and "System". The error pages are rendered for `/does-not-exist/` (404); a 500 and a CSRF page are covered by the tests.

`seed_design_accounts` creates three throw-away accounts, one per access level, with addresses under `example.org`: `design.user@example.org` (authenticated user), `design.staff@example.org` (staff user without any role) and `design.administrator@example.org` (system administrator). It refuses to run unless `APP_ENV=local` (`leg_platform/accounts/management/commands/seed_design_accounts.py`).

* Check whether they already exist: `Test-Path "$HOME\.leg-platform-design-accounts.json"`. If it is `True`, **the accounts exist; use them and never run the creation again** (it would refuse anyway: it never overwrites the file and refuses if a design account exists).
* Creation (only if the file does not exist, and only when the main session asks): `.\.venv\Scripts\python manage.py seed_design_accounts`. The command writes the generated passwords and the authenticator keys of the two privileged accounts to the credentials file outside the repository (default `$HOME\.leg-platform-design-accounts.json`, option `--output`, never inside the repository) and prints no secret.
* The file is JSON with the keys `user`, `staff` and `administrator`; each has `email` and `password`, and `staff` and `administrator` also have `authenticator_secret`. List the structure without values:

  ```powershell
  (Get-Content -Raw "$HOME\.leg-platform-design-accounts.json" | ConvertFrom-Json).PSObject.Properties.Name
  ```

  Read only the one email address and password that you need for a login. They are throw-away values, but treat them like secrets: never copy them into reports, screenshots, logs or committed files.
* Staff users and system administrators must complete a second factor. Print the current six-digit codes of the two privileged design accounts with:

  ```powershell
  .\.venv\Scripts\python manage.py seed_design_accounts --code
  ```

  Output: `staff: 123456` and `administrator: 654321`. A code works **once** (a code at or below the step of the last accepted one is refused) and is valid for its 30-second step plus one step of tolerance in each direction (`AuthenticatorDevice`, `leg_platform/accounts/models.py`). A second login within the same 30 seconds needs a newer code: wait and run the command again. Do not enter wrong codes repeatedly: 15 wrong codes pause code entry for the account for 24 hours (D-040), and the only supported ways out are a password reset or the emergency command, which would remove the seeded second factor.
* Session timeouts: users are logged out after 30 minutes without a request or 8 hours after login; staff users and system administrators after 15 minutes and 4 hours (D-041). Log in again with a fresh code.

### 9.2 A self-registered test account

1. Open `http://127.0.0.1:8000/register/`, enter an address such as `test.user1@example.org` and submit. The response is always the same page "email sent".
2. The worker produces the email within a few seconds (interval 5 seconds). Extract the link from the worker's output with the helper block below this list (the console email is quoted-printable, so a long link may be split over two lines with `=`; the helper decodes it and prints the recipient and the complete link). The last line for the address is the newest link. The helper was checked against emails produced by Django's console backend (offline, without database).
3. Open the link in the browser. It only stores the token in the session and redirects to the password step (`/register/password/`, step 2 of 3: the rules are listed under the field). The next page (`/register/complete/`, step 3 of 3) asks for the name and the address and the privacy box; the button "Konto erstellen" creates the account and logs you in on the start page (D-058). The link stays valid until this last step is done (24 hours). Choose a password with **at least 15 characters** (docs/SECURITY.md section 3).
4. Limits (D-038): per address 3 requests per 10 minutes, 12 per hour and 24 per 24 hours, per client IP 20 per hour (shared by registration and password reset), instance wide 300 per hour. All requests come from `127.0.0.1`; after about 20 registrations or resets in one hour the page answers "Zu viele Anfragen" (HTTP 429) and nothing is stored. The same browser asking for the same address again within 60 seconds creates no new mail (the page offers "E-Mail erneut senden" after the pause). Clear the counters locally with the snippet in section 14 (row "No email although the worker runs"). A script (curl) must send the form field `form_started` of the page, at least two seconds old, and leave `contact_fax_number` empty; otherwise it gets the normal page without a mail (bot check, D-058).
5. Password reset and email change work the same way: the link appears in the worker output. A request for a new login address or second address (profile card "E-Mail-Adressen") sends the link to the new address and a notice with a "stop" link to the old one. Unverified accounts are deleted by `delete_expired_registrations`, which is part of `run_scheduled_tasks` and not scheduled locally; they stay until then.

Helper for step 2 (the lines of the here-string start in column 0, as PowerShell requires):

```powershell
$log = Join-Path $HOME '.leg-platform-dev\worker.out.log'
$helper = @'
import email, re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
LINK = re.compile(r"https?://\S+?/(?:register|password-reset|two-factor-reset|email-change)/(?:verify|stop)/[A-Za-z0-9_-]+/")
for block in re.split(r"(?m)^-{79}$", text):
    start = block.find("Content-Type:")
    if start < 0:
        continue
    message = email.message_from_string(block[start:])
    body = message.get_payload(decode=True)
    body = body.decode(message.get_content_charset() or "utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    for link in LINK.findall(body):
        print(message["To"], link)
'@
$helper | .\.venv\Scripts\python.exe - $log
```

### 9.3 Authenticator code for an account that is not a design account

For a throw-away account whose setup page (`/profile/two-factor/setup/`) shows the key, compute the code from that key. The key is shown in groups of four characters; spaces are ignored. Pass the key on standard input (not as an argument), then use the code within its 30 seconds:

```powershell
'abcd efgh ijkl mnop qrst uvwx yz23 4567' | .\.venv\Scripts\python.exe -c "import base64, hashlib, hmac, struct, sys, time; k = sys.stdin.readline().strip().replace(' ', '').upper(); k += '=' * (-len(k) % 8); d = hmac.new(base64.b32decode(k), struct.pack('>Q', int(time.time()) // 30), hashlib.sha1).digest(); o = d[-1] & 15; print('%06d' % ((struct.unpack('>I', d[o:o+4])[0] & 0x7FFFFFFF) % 10**6))"
```

Replace the example key with the one on the page. This is RFC 6238 with SHA-1, six digits and 30 seconds, the values the application uses; the formula was checked against the RFC 6238 test vector and against `django_otp.oath.TOTP` for random keys. Use it only for throw-away local accounts. For the design accounts use `seed_design_accounts --code` (section 9.1).

## 10. Synthetic address directory (optional)

The registration (step "Your details") and the profile edit form offer address suggestions from a local copy of the official directory (D-033, D-066). Locally there is no MariaDB directory; without a directory the forms show no suggestions and say nothing about it (the failure is only logged; the operator page "Address directory" of the administration shows the status). To test suggestions, use a throw-away SQLite file with a few invented addresses:

* `leg_platform/local_directory_settings.py` adds the two SQLite aliases `addresses` and `address_import` to the normal settings. It **refuses to be imported unless `APP_ENV=local`**, refuses a file inside the repository or a relative path, and changes no security setting. The file is `LEG_LOCAL_ADDRESS_DIRECTORY_FILE` or, by default, `$HOME\.leg-platform-local-address-directory.sqlite3`.
* `manage.py seed_local_address_directory` loads eight invented addresses (streets "Example Street", "Sample Avenue", "Test Lane", "Throwaway Road", postal codes 9997 to 9999). It refuses outside `APP_ENV=local`, refuses if either alias is not SQLite, and refuses to replace a directory whose checksum is not its synthetic marker (so a file that holds a real import is never overwritten).

Steps (once per file; stop nothing, but the server must be started with the settings module afterwards):

```powershell
.\.venv\Scripts\python manage.py migrate addresses --database=address_import --settings=leg_platform.local_directory_settings
.\.venv\Scripts\python manage.py seed_local_address_directory --settings=leg_platform.local_directory_settings
```

Then start the server with `$Settings = 'leg_platform.local_directory_settings'` in the block of section 6 (the worker does not need it). In the forms, type for example `Exa` in the street field to get "Example Street". The lookup is a CSRF-protected POST to `/addresses/lookup/`, limited to 60 requests per minute per client IP and 4096 bytes of body.

Reset: stop the server, delete only that synthetic file (`Remove-Item "$HOME\.leg-platform-local-address-directory.sqlite3"`), then repeat the two commands. Never point `LEG_LOCAL_ADDRESS_DIRECTORY_FILE` at a file you did not create for this purpose. This replaces the out-of-repository settings module and seed script that were used earlier; the two commands above are all that is needed.

Verification status: the refusals and the module are covered by database-free tests (`leg_platform/addresses/tests/test_local_directory.py`, section 13). The three commands above were **not executed end to end** when this was written (the session that wrote it was not allowed to run migrations or seeds); the first user reports the result to the main session.

## 11. Stop everything

Stopping is done by process ID, in two steps, because the PIDs differ per run:

1. Run the status block of section 8. It prints, for each of `runserver` and `worker`, the child PID and the parent PID.
2. Stop each printed PID with a literal ID, the child first, then the parent, for example:

   ```powershell
   Stop-Process -Id 12345    # child of runserver
   Stop-Process -Id 12344    # parent of runserver
   ```

3. Remove the PID files and confirm that the port is free:

   ```powershell
   Remove-Item (Join-Path $HOME '.leg-platform-dev\runserver.pid'), (Join-Path $HOME '.leg-platform-dev\worker.pid') -ErrorAction SilentlyContinue
   Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue    # must print nothing
   ```

Do not stop processes that the status block did not list as yours, even if they run `manage.py runserver` or `process_mail_queue`. The main session may run its own. The mail worker may be stopped at any time: delivery is at least once; a job is tried three times and unprocessed jobs of requests are deleted after one hour (D-038). The MariaDB service stays running.

The start and stop mechanics (log files readable while the process runs, launcher and child processes, PID check, literal-PID stop) were exercised with a harmless dummy process and the repository's `.venv` interpreter; the same block was not run for `manage.py` because a server was already running.

## 12. Reset the local throw-away data (destructive; only on explicit order)

Do this only when the main session or the project owner orders it and no designer or tester session depends on the data.

1. Stop the server and the worker (section 11).
2. Confirm `APP_ENV` is `local` and `DB_HOST` is `127.0.0.1` (section 3).
3. Remove all rows of the default database and keep the schema: `.\.venv\Scripts\python manage.py flush --noinput`. This deletes all accounts, including the seeded design accounts.
4. Delete the credentials file of the seeded accounts (`Remove-Item "$HOME\.leg-platform-design-accounts.json"`) and create the accounts again (section 9.1). The old file would otherwise make the creation refuse.
5. Delete the logs in `$HOME\.leg-platform-dev\` if wanted, and reset the SQLite directory (section 10) if it was used.
6. Start the server and the worker again (section 6).

`flush` and the commands above were not run (UNVERIFIED in this environment); `flush` is a standard Django command listed by `manage.py help`.

## 13. Tests and checks

Facts about the test database (verified in the installed Django 6.1.2 source and in the project settings):

* The default database of the tests is `test_<DB_NAME>` (`TEST_DATABASE_PREFIX = "test_"` and `_get_test_db_name()` in `django/db/backends/base/creation.py`); the project sets no `TEST["NAME"]`. For the local setup that is `test_leg_platform`. The setup script grants the application account the privileges on this database.
* The runner creates the test database through a connection without a database name, switches the connection to the test name for the run and drops the database afterwards. `--noinput` drops an already existing `test_leg_platform` without asking (`autoclobber`). **The data of `leg_platform` is not read or changed by a test run.** A running development server and a design session are therefore not affected in their data; they only share the CPU (the full suite takes about 3 minutes since D-071; before the fast test hasher it took 14 minutes, see below) and the MariaDB server.
* `leg_platform.test_settings` replaces the address aliases with in-memory SQLite databases (`:memory:`), so tests never touch a directory file or a MariaDB directory.
* Two test runs against the same MariaDB server overlap on the same `test_leg_platform` database: the second run drops the database the first still uses, which corrupts both (D-046). **Run only one test process at a time** and check first:

  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'manage\.py"?\s+test\b' } | Select-Object ProcessId, CommandLine
  ```

  It must print nothing (the running `runserver` and `process_mail_queue` processes do not match; each test run appears twice, parent and child). If it lists a process, wait for it; never stop it. `scripts/test.ps1` also holds a machine-wide named mutex (`Global\LegPlatformTestRun`) for the whole run, so two starts of the script at the same moment cannot both pass; its limits (D-072): it does not see a run started by hand, a run on another machine against the same MariaDB server, or a process of another user that a non-elevated shell cannot read (the script then warns). Exit codes of the script: 0 passed; 1 a test or check failed (the code of Python is passed through); 2 wrong usage or missing virtual environment; 3 another run is active. Nothing runs for 2 and 3.

Scenario runner (the normal way to run tests; D-071). Every change is tested with the tests of the area it touches, not with the whole suite. The areas (source paths to test labels) are defined in one file, `scripts/test_areas.py`; `scripts/test.ps1` resolves them and runs `manage.py test` with the same interpreter and settings as CI (`.venv\Scripts\python.exe`, `leg_platform.test_settings`, `--noinput`, deprecation warnings as errors):

```powershell
.\scripts\test.ps1 -List                              # the areas and the core files
.\scripts\test.ps1 -Changed                           # the areas of the changed files (git status: working tree, staged, untracked)
.\scripts\test.ps1 -Since HEAD~3                      # also the files changed since a revision
.\scripts\test.ps1 -Area registration,mail            # named areas
.\scripts\test.ps1 -Area translations -Timing         # with Django's --timing (database set-up and total time)
.\scripts\test.ps1 -Changed -DryRun                   # print the plan and the commands, run nothing
.\scripts\test.ps1 -Full -Durations 50                # the whole suite, with the 50 slowest tests
```

How the areas are chosen: a changed source file selects every area that maps its path; a changed test module selects that module; a documentation or agent file selects only the repository checks (`docs`); a migration selects `migrations`, which always uses a fresh test database and also runs `makemigrations --check --dry-run`. A **core file** (settings, test settings, the environment module `leg_platform/environment.py` that builds every security setting and the mail backend, urls, middleware, the user model, the authentication backend, `manage.py`, dependencies, shared test helpers) or a file that **no area maps** selects the **full suite** (the safe default). A changed test module whose name is not a plain dotted module name (only letters, digits, underscores and dots, starting with a letter or an underscore: `^[A-Za-z_][A-Za-z0-9_.]*$`) also selects the full suite, because its label is an argument of `manage.py test` (D-072). `-Since <revision>` is checked first: a value that starts with `-` or that is not an existing commit is refused (`git rev-parse --verify --end-of-options`, git 2.24 or later, checked with the installed git 2.44), and only the resolved commit hash reaches `git diff`. A changed file whose name is not valid UTF-8 stops the runner with a message (rename it or run `-Full`). A change of `.github/workflows/` runs the `packaging` area, which includes `test_production_settings`: CI itself cannot run locally, but that module imports the real settings for `stage` and `production` in child processes and `CiDeploymentCheckTests` keeps the stage step of `ci.yml` equal to the production step except for `APP_ENV`; the only part not run locally is the database part of `check --deploy` (section 13, deployment check with synthetic variables, run it by hand with `$env:APP_ENV = 'stage'`). The repository check (`test_repository`, no database) is added to every scoped run. `leg_platform/tests/test_areas.py` fails when a test module belongs to no area, a label or path does not exist, or a file of the repository is mapped to nothing, so the mapping cannot rot; add new files to `scripts/test_areas.py`.

Measured on 2026-10-16 (fast test hasher, a machine with an i7-1265U, 10 cores; full details in D-071): `docs` 0.4 s, `packaging` 0.4 s, `branding` 3.9 s, `static` 8.3 s, `address-directory` 10.1 s, `environment` 18.3 s, `migrations` 21.7 s, `mail` 25.4 s, `translations` 27.6 s, `admin` 29.9 s, `login` 40.2 s, `registration` 41.5 s, `profile` 58.4 s, `templates` 66.6 s, `second-factor` 71.2 s ("Ran N tests in", without the start-up of about 2 s), against 185 s for the full suite (1833 tests). These are the values of step 3a. Step 3b (removal of redundant tests, shared test data, one child-process run for the production settings; D-071, addendum) lowered them further: in a paired measurement on the same day, with the old and the new tests run alternately on the same machine, the areas `admin` (31.6 s to 25.7 s), `templates` (70.6 s to 64.9 s), `profile` (47.7 s to 42.9 s), `mail` (25.9 s to 23.0 s) and `second-factor` (74.7 s to 69.5 s) became 7 to 19 % faster, and `leg_platform.tests.test_production_settings` takes 3.8 s instead of 19.4 s. The machine was slower on that afternoon than during step 3a (creating the test database took 9 to 10 s instead of 3.3 s), so compare only values of the same series.

**Writing fast tests (D-071, step 3b):** most of the time of a test is MariaDB round trips (about 80 SQL statements per test on average; creating an account with a second factor is about 24, `force_login` about 20). A class whose tests only read the same accounts creates them once in `setUpTestData` (Django 6.1 `TestCase.setUpTestData`: the class-level objects are copied for each test, so a test can change them without affecting the next one); the client and its login stay in `setUp`. Use `TransactionTestCase` only where the test needs real commits or threads (concurrency tests, a test that needs no open transaction, the address directory reader that is a second connection); the flush after each such test costs 0.1 to 0.3 s. Child processes (as in `test_production_settings`) are started together once per module, not once per test.

**When the full suite is required:** only for a core change (the runner decides), before a deployment gate, in CI (both jobs always run the full suite), and by the team lead at most once per commit block. It is not run after every small change, and agents do not start it unless the main session orders it. A report always names the area or labels and the number of tests and never presents a scoped run as a full run.

Options of the runner (verified 2026-10-16 with the installed Django 6.1.2 and the local MariaDB 12.3):

* `-KeepDb` (`--keepdb`): reuses `test_leg_platform` between runs. **Local opt-in only.** It saves only about 2.6 s per run (database set-up 3.2 s fresh, 0.7 s with an existing database): the migrations of the project are fast. The runner ignores `-KeepDb`, with a warning, when the plan needs a fresh database: the `migrations` area and the full suite (and an unmapped or core file) always create the database from the migrations, so an old database can never hide a migration problem. A kept `test_leg_platform` stays in MariaDB until a run without `-KeepDb` drops and recreates it (`--noinput`, `autoclobber`, D-046); after a manual change of the schema run any scoped run without `-KeepDb` first.
* `-Parallel N` (`--parallel`): **does not work with the application account of the local setup.** Django clones the test database to `test_leg_platform_1` ... `_N`; the account has privileges only on `leg_platform` and `test_leg_platform` (section 2 and `docs/INSTALLATION.md`, "Database privileges"). Verified: a run with `--parallel 2` stopped with `(1044, "Access denied for user 'leg_platform'@'127.0.0.1' to database 'test_leg_platform_1'")`. Do not widen the privileges for this (security rule: privileges only on the two named databases, checked by the setup script); the option stays in the runner for a MariaDB account that may create these databases. **UNVERIFIED** with a privileged account (also the memory and CPU use: Django starts one process per database clone; the machine of this measurement has 10 cores and 31.7 GB of RAM). D-046 stays: never two test runs at the same time.
* `-Durations N` (`--durations`) and `-Timing` (`--timing`) exist in Django 6.1.2 (`manage.py test --help`).

Full suite (the same as CI; deprecation warnings are errors):

```powershell
.\scripts\test.ps1 -Full
# the same without the script:
.\.venv\Scripts\python -W error::DeprecationWarning -W error::PendingDeprecationWarning manage.py test --settings=leg_platform.test_settings --noinput
```

It prints `Ran N tests` and `OK`. On 2026-10-16 the full run of step 3a had 1833 tests and took 185 seconds of test time (193 seconds wall clock); before the fast hasher (the same suite minus 19 tests added in this unit) it took 830 seconds, of which about 80 % was password hashing: the test settings used Django's default PBKDF2 with 1,500,000 iterations (0.24 s per hash). After step 3b the suite has 1830 tests (three redundant tests removed) and ran in 153 to 195 seconds of test time depending on the load of the machine on that afternoon (the same suite is that variable: the old tests took 151 to 201 s in the alternating runs; the paired run that counted SQL statements, CPU and wall time gave 9 % fewer seconds without the production-settings module and 12 % fewer SQL statements; the production-settings module itself 19.4 s to 3.8 s). `leg_platform.test_settings` now sets `PASSWORD_HASHERS` to the MD5 hasher for the tests only; `leg_platform/tests/test_production_settings.py` proves in a child process that the real settings still use Django's default hasher with at least 600,000 iterations. If a tool call cannot wait that long, run it as a background process and poll the log:

```powershell
$State = Join-Path $HOME '.leg-platform-dev'; New-Item -ItemType Directory -Force -Path $State | Out-Null
$test = Start-Process -FilePath (Join-Path (Get-Location).Path '.venv\Scripts\python.exe') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru `
    -ArgumentList '-W', 'error::DeprecationWarning', '-W', 'error::PendingDeprecationWarning', 'manage.py', 'test', '--settings=leg_platform.test_settings', '--noinput' `
    -RedirectStandardOutput "$State\test.out.log" -RedirectStandardError "$State\test.err.log"
$test.Id | Set-Content "$State\test.pid"
# Poll (the test runner writes to standard error):
Get-Content "$State\test.err.log" -Tail 5
```

Targeted runs without the script (each needs MariaDB unless stated; the same one-run-at-a-time rule applies; the scenario runner above is the normal way):

```powershell
# One module, one class or one test
.\.venv\Scripts\python manage.py test leg_platform.accounts.tests.test_registration --settings=leg_platform.test_settings --noinput
.\.venv\Scripts\python manage.py test leg_platform.accounts.tests.test_registration.RegistrationTests.test_the_link_is_built_from_the_configuration_not_from_the_request --settings=leg_platform.test_settings --noinput
# One app
.\.venv\Scripts\python manage.py test leg_platform.addresses --settings=leg_platform.test_settings --noinput
# Database-free only (no MariaDB needed; for example the local directory tests, the settings tests, the repository checks)
.\.venv\Scripts\python manage.py test leg_platform.addresses.tests.test_local_directory --settings=leg_platform.test_settings --noinput
.\.venv\Scripts\python manage.py test --exclude-tag mariadb --settings=leg_platform.test_settings --noinput
```

Test modules: `leg_platform/accounts/tests/` (account lifecycle, admin, abuse protection, mail queue, password reset, portal views, registration, scheduled tasks, second factor, security log, session timeout, translations), `leg_platform/tests/` (environment, settings, production settings, responses, request body limit, presentation, instance configuration, Graph mail, repository, test areas) and `leg_platform/addresses/tests/`. A run of only database-free `SimpleTestCase` classes prints `Skipping setup of unused database(s)` and creates no database.

Other checks (they need the running MariaDB because Django 6.1 checks all databases):

```powershell
.\.venv\Scripts\python manage.py check --fail-level WARNING
.\.venv\Scripts\python manage.py makemigrations --check --dry-run          # must report "No changes detected"
```

Deployment check with synthetic production-style variables. These contain no real secrets; the secret key is a random throw-away value generated for this call, and the variables exist only in this PowerShell process:

```powershell
$env:APP_ENV = 'production'; $env:EMAIL_MODE = 'graph'
$env:DJANGO_ALLOWED_HOSTS = 'portal.example.org'
$env:INSTANCE_BASE_URL = 'https://portal.example.org'
$env:INSTANCE_PRIVACY_URL = 'https://www.example.org/privacy'
$env:DJANGO_SECRET_KEY = (.\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(50))")
Remove-Item Env:DJANGO_DEBUG, Env:EMAIL_ALLOWED_RECIPIENTS -ErrorAction SilentlyContinue
.\.venv\Scripts\python manage.py check --deploy --fail-level WARNING
```

Expected: `System check identified no issues (3 silenced).` These are the same values as the CI step "Deployment checks with production settings"; the same call with `$env:APP_ENV = 'stage'` gives the same result and is the CI step "Deployment checks with stage settings" (stage does not differ from production, D-070). The three silenced checks are `security.W005`, `security.W021` and `axes.W006` (D-028, `leg_platform/settings.py`).

Translations (the `.po` file is maintained by hand, the `.mo` file is committed, D-034; GNU gettext is in `C:\Program Files\gettext-iconv\bin`, which is on `PATH`):

```powershell
$po = 'locale\de_CH\LC_MESSAGES\django.po'; $mo = 'locale\de_CH\LC_MESSAGES\django.mo'
$tmp = Join-Path $env:TEMP 'leg-compiled.mo'
msgfmt --check -o $tmp $po; "msgfmt exit code: $LASTEXITCODE"                  # 0
"differences to the committed catalogue: " + @(Compare-Object (msgunfmt $mo) (msgunfmt $tmp)).Count    # 0
Remove-Item $tmp
"lines with the forbidden letter: " + @(Select-String -Path $po -Pattern ([string][char]0x00DF) -SimpleMatch).Count    # 0
```

After a change to the `.po` file, regenerate the catalogue with `.\.venv\Scripts\python manage.py compilemessages` and repeat the check above. These commands were run on 2026-10-14 against the current files with the result 0, 0, 0.

## 14. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| `CommandError: You must set settings.ALLOWED_HOSTS if DEBUG is False.` when starting `runserver` | `DJANGO_ALLOWED_HOSTS` is not set in the process environment (Django `runserver`, `core/management/commands/runserver.py`) | Use the start block of section 6 (it sets `DJANGO_ALLOWED_HOSTS` for the process). Alternative: `DJANGO_DEBUG=True` for that process (only with `APP_ENV=local`). |
| `ImproperlyConfigured: DJANGO_DEBUG=True is only allowed with APP_ENV=local.` | `APP_ENV` is not `local` in this process | Check section 3. Never set `APP_ENV` to another value to get around a message. |
| `ImproperlyConfigured: APP_ENV must be one of ...`, `DJANGO_SECRET_KEY must be set`, `Missing database settings` | The process did not inherit the user environment variables (terminal or VS Code opened before the setup script ran) | Section 3 shows which names are `NOT SET`. Report to the main session; a restart of VS Code and the terminals is needed. |
| Pages without styling | The server runs without `--insecure` and debug is off | Restart it with the block of section 6. |
| 400 "Bad Request" or `DisallowedHost` | The URL host is not in `DJANGO_ALLOWED_HOSTS` (for example `127.0.0.2` or a LAN name) | Use `http://127.0.0.1:8000/`. |
| 403 on a form ("CSRF verification failed") | Mixed hosts or ports (`localhost` vs `127.0.0.1`), or old cookies of another server | Use one host consistently and delete the cookies of the site; session and CSRF cookies are host-only. |
| Port 8000 is in use | Another session's server runs there | Do not kill it. Use it, or start your own on another port with `$Port` and `INSTANCE_BASE_URL` (section 6) and tell the main session. |
| "Email sent" page, but no email | The mail worker is not running, crashed, or the job was given up (three tries) or deleted (one hour for requests) | Run the status block (section 8). Read `worker.err.log` and `worker.out.log`. Start the worker (section 6). Request the email again. |
| No email although the worker runs | The request limits are used up (12 per address per hour, 20 per client IP per hour; all local requests share the IP `127.0.0.1`) | Wait an hour, or clear the local counters (below the table). |
| HTTP 429 and the lockout page after wrong logins | Five wrong passwords for an address (or five failed logins from `127.0.0.1`) lock it for 15 minutes (`leg_platform/accounts/login_protection.py`; Axes plus the separate IP counter) | Wait 15 minutes, or clear the local records (below the table). A password reset still works during the lockout. |
| Logged out unexpectedly | 30 minutes idle or 8 hours absolute (staff users and system administrators: 15 minutes and 4 hours) | Log in again; for staff users and administrators get a new code with `seed_design_accounts --code`. |
| A staff user or administrator is sent to the two-factor setup page and cannot reach anything else | Privileged accounts must have a second factor (D-042). The seeded accounts already have one; a new or reset privileged account does not | For the seeded accounts nothing is needed. For others complete `/profile/two-factor/setup/` (section 9.3 computes the code). |
| The code is refused | The code was already used in this 30-second step, the clock is off by more than one step, or the wrong account's key was used | Wait for the next step and compute a new code. Do not repeat wrong codes (15 pause code entry for 24 hours). |
| `ImportError: cannot import name ... (most likely due to a circular import)` or other import errors at startup | The worktree is in the middle of a change (D-051 recorded circular imports during integration), or an unfinished edit | Run `manage.py check` and read the first traceback line to see the module. Do not edit code; report to the main session with the output and `git status`. |
| `DatabaseOperationForbidden` for the `addresses` alias in a test | A test class touches the address alias but does not declare it | Add the alias to the test class: `databases = {"default", "addresses", "address_import"}` (as in `leg_platform/addresses/tests/test_directory.py`). |
| `IntegrityError (1062, "Duplicate entry ...")` in the test setup | Two test runs overlapped on `test_leg_platform` (D-046) | Wait until no other test process runs (section 13), then run again. |
| `OperationalError (2003, "Can't connect to MySQL server on '127.0.0.1'")` | The MariaDB service is not running | Section 3. Starting it needs administrator rights; report it. |
| `OperationalError (1045, "Access denied for user ...")` | The values of `DB_USER` and `DB_PASSWORD` no longer match the database account | Report to the main session; the project owner re-runs the setup script. Never print the variables. |
| `ImproperlyConfigured: Address importer credentials must not be present in the web process.` | `ADDRESS_IMPORT_DB_USER` or `ADDRESS_IMPORT_DB_PASSWORD` is set in the user environment | Report it; these variables must not exist on a development machine. |
| `ImproperlyConfigured: leg_platform.local_directory_settings can only be used with APP_ENV=local.` or `... must point outside the repository` | The guard of the local directory settings (section 10) | Fix the cause (environment or path); never weaken the guard. |
| `seed_local_address_directory`: "The directory tables do not exist yet" | The directory migration was not applied to the SQLite file | Run the `migrate addresses --database=address_import` command of section 10. |
| `seed_local_address_directory`: "may contain imported data; refusing to replace it" | The file holds data that is not the synthetic marker | Use another file through `LEG_LOCAL_ADDRESS_DIRECTORY_FILE`; do not delete a file you did not create. |
| `seed_design_accounts`: "A design account already exists" or "exists. Delete it first" | The accounts and the credentials file already exist | Use them. Do not delete anything (section 1). |
| `msgfmt` or `msgunfmt` is not found | `C:\Program Files\gettext-iconv\bin` is not on `PATH` of this shell | Use the full path of the tool, or report it. |

Clearing the local login lockouts and request counters (throw-away database only; run in a shell whose `APP_ENV` is `local`; the output is only counts):

```powershell
.\.venv\Scripts\python manage.py shell -c "from axes.models import AccessAttempt; from leg_platform.accounts.models import LoginIpBucket, RateLimitBucket; print(AccessAttempt.objects.all().delete()[0], LoginIpBucket.objects.all().delete()[0], RateLimitBucket.objects.all().delete()[0])"
```

This deletes the Axes failure records, the login IP counters (their attempts follow by cascade) and the request counters (events follow by cascade). It deletes no account, no mail job and no security log entry. It was not run in this environment (UNVERIFIED); the models and relations were read in `leg_platform/accounts/models.py` and `axes_handler.py`.

## 15. What was verified and how

| Statement | Verification |
| --- | --- |
| Versions Python 3.14.8, Django 6.1.2; the service `MySQL` runs; MariaDB listens on `127.0.0.1:3306` only | Commands of section 3 run on 2026-10-14 (read-only). |
| `manage.py check` and the production-style `check --deploy` (3 silenced) pass; `showmigrations --plan` shows no unapplied migration | Run on 2026-10-14 with the commands of sections 5 and 13. |
| Environment variable names, defaults and refusals | Read `leg_platform/environment.py`, `leg_platform/settings.py`, `leg_platform/address_configuration.py`, `leg_platform/import_settings.py`, `leg_platform/test_settings.py`. |
| `runserver` needs `DJANGO_ALLOWED_HOSTS` or debug, and serves static files only with debug or `--insecure` | Read the installed Django 6.1.2 sources `core/management/commands/runserver.py` and `contrib/staticfiles/management/commands/runserver.py`; `manage.py help runserver`. |
| Venv launcher plus child process; hidden console host | `Get-CimInstance Win32_Process` on the running processes and on a dummy process (read-only; nothing was stopped except the dummy). |
| Console email format (quoted-printable, soft line breaks) and the link helper | Offline run with Django's console backend and a sample log; no database. |
| TOTP helper | RFC 6238 test vector and comparison with `django_otp.oath.TOTP` for random keys; offline. |
| Login lockout (5 attempts, 15 minutes), request limits, session timeouts, 15 wrong codes pause | Read `login_protection.py`, `session_timeout` settings in `settings.py`, `models.py`, D-038, D-040, D-041. |
| Test database name, runner behaviour | Read installed Django `db/backends/base/creation.py` and `test/runner.py`; D-046. |
| Scenario runner, areas, timing, `--keepdb` and `--parallel` (section 13) | Run on 2026-10-16: every area once with `scripts/test.ps1`, the full suite with `--timing --durations 50` (1833 tests, OK), `--keepdb` twice, `--parallel 2` (stopped with MariaDB error 1044, as stated); `manage.py test --help` lists `--durations`, `--timing`, `--keepdb`, `--parallel`; the numbers are in D-071. |
| `leg_platform.local_directory_settings` and `seed_local_address_directory` refusals | Database-free tests in `leg_platform/addresses/tests/test_local_directory.py`: 15 tests OK with deprecation warnings as errors. |
| Translation check commands | Run on 2026-10-14 (results 0, 0, 0). |
| Not verified | End-to-end run of the directory migration and seed, `flush`, the lockout-clearing snippet, a start of the block of section 6 for `manage.py` (a server was already running) and `--parallel` with a MariaDB account that may create `test_leg_platform_N`. |
