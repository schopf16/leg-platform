# Local Startup for Agents and Developers

How a **designer agent** (browser), a **tester agent** and developers start, check, use and stop the complete local environment (`APP_ENV=local`, throw-away data) without asking questions. Stage and production: `docs/DEPLOYMENT.md`. One-time installation of Python and MariaDB: `docs/INSTALLATION.md` ("Local Development"). **UNVERIFIED** = never run.

## 1. Rules (read first)

Never (`AGENTS.md` sections 8 and 9, `CONDUCT.md`, D-020, D-027):

* Print, log or screenshot the process environment or any secret: no `Get-ChildItem env:`, `dir env:`, `set`, `printenv`, `[Environment]::GetEnvironmentVariables()`, `manage.py diffsettings`, `manage.py dumpdata`, `manage.py dbshell`.
* Open `.env` or any other secret file. Never write a secret into a file, report or commit. Names of variables may be written, values never.
* Use stage or production credentials, hosts or data. This environment connects only to the local MariaDB on `127.0.0.1` with `APP_ENV=local`.
* Use real personal data. Use addresses under `example.org`, `example.com`, `example.net`, `.test` or `.invalid` (RFC 2606).
* Delete or recreate the seeded design accounts or their credentials file, run `flush`, drop the database, or delete the directory file of section 10, unless the main session or the project owner orders it (section 12).
* Stop a process by name (`Stop-Process -Name`, `taskkill /IM`). Stop only by process ID (`Stop-Process -Id <PID>`) and only processes you started (section 11).
* Run two test runs at the same time against the same MariaDB server (D-046, section 13).
* Commit or push (the main session commits). Never "fix" application code during a design review or test run; report defects to the main session.

## 2. What runs where

| Part | Where | Notes |
| --- | --- | --- |
| MariaDB 12.3 | Windows service `MySQL` (name given by the setup script), `127.0.0.1:3306` only | Databases `leg_platform` (data) and `test_leg_platform` (created and dropped by the test runner). |
| Python environment | `.venv` in the repository root | `.venv\Scripts\python.exe` |
| Development server | `http://127.0.0.1:8000/` (designer and acceptance); `http://127.0.0.1:8001/` (the developer's own server, `$Port = 8001` in section 6) | `manage.py runserver 127.0.0.1:8000 --noreload --insecure`. Restart after a template change (no reload). Use one canonical host (host-only cookies). **Passkey checks (D-106) need `localhost`: set `INSTANCE_BASE_URL=http://localhost:<port>` for server and worker and browse only that address.** |
| Mail worker | background process | `manage.py process_mail_queue --loop`. Required (section 6). |
| Address directory (optional) | SQLite file `$HOME\.leg-platform-dev\address-directory.sqlite3` | Synthetic data only (section 10). |
| Logs and process IDs | `$HOME\.leg-platform-dev\` | Outside the repository. |

All commands are PowerShell, run from the repository root. Every tool call starts a fresh process: `$env:NAME = ...` lives only for that call. A block that starts a process sets what the process needs in the same call.

## 3. Prerequisites check (read-only)

```powershell
Test-Path .\manage.py                                       # True
Test-Path .\.venv\Scripts\python.exe                        # True
.\.venv\Scripts\python.exe --version                        # Python 3.13 or newer (3.12 is the minimum)
.\.venv\Scripts\python.exe -c "import django; print(django.get_version())"   # 6.1.x
Get-Service -Name MySQL | Select-Object Name, Status, StartType               # Running, Automatic
Get-NetTCPConnection -State Listen -LocalPort 3306 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort   # LocalAddress 127.0.0.1 only
Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, OwningProcess   # empty = port free
```

Check that the configuration exists without printing any value:

```powershell
foreach ($name in 'APP_ENV', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_PORT', 'DJANGO_SECRET_KEY') {
    '{0}: {1}' -f $name, $(if ([Environment]::GetEnvironmentVariable($name, 'User')) { 'set' } else { 'NOT SET' })
}
'APP_ENV is local: ' + ([Environment]::GetEnvironmentVariable('APP_ENV', 'User') -eq 'local')
'DB_HOST is 127.0.0.1: ' + (([Environment]::GetEnvironmentVariable('DB_HOST', 'User') -in @('127.0.0.1', $null, '')))
```

Required: everything `set`, `APP_ENV is local: True`, `DB_HOST is 127.0.0.1: True`. Otherwise stop and report to the main session. Never "repair" it by printing or editing the variables. MariaDB not running: starting it needs administrator rights; report it.

## 4. Configuration

Configuration comes only from environment variables (`leg_platform/settings.py`, D-027), never from `.env`. The application refuses to start on missing or unsafe values.

| Name | Source | Meaning locally |
| --- | --- | --- |
| `APP_ENV` | user variable (setup script) | `local`. Required. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DJANGO_SECRET_KEY` | user variables (setup script) | Local MariaDB, its application account, the local secret key. |
| `EMAIL_MODE` | not set; default `console` | Emails go to the standard output of the mail worker (section 6). `graph` is refused locally (D-116). |
| `DJANGO_ALLOWED_HOSTS` | per process, start block (section 6) | `127.0.0.1,localhost`. Not a permanent user variable. |
| `INSTANCE_BASE_URL` | per process for passkey work | Canonical origin for email links and WebAuthn. `http://localhost:<port>` for server, worker and browser alike. |
| `INSTANCE_NAME`, `INSTANCE_WEBSITE_URL`, `INSTANCE_STATIC_DIRECTORY`, `INSTANCE_LOGO_LIGHT`, `INSTANCE_LOGO_DARK` | per process, start block (section 6) | **Fallback** for "Portal-Einstellungen" (D-056, D-073). Logos: `$HOME\.leg-platform-dev\instance-static\instance\LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg` (copied from `LEG-Design-Vorgaben\logo\`). Without them the pages show the name as text. |
| `DJANGO_DEBUG` | not set (False) | `True` only with `APP_ENV=local`; not needed. |

Never permanent user variables locally: `BOOTSTRAP_SYSTEM_ADMIN_EMAIL`, `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD`, `EMAIL_ALLOWED_RECIPIENTS`, `EMAIL_SENDER_ADDRESS`, `CONTACT_EMAIL_ADDRESS`; the application refuses to start with them (D-116). The bootstrap variables only for one `setup_instance` run (section 5).

## 5. Database and migrations

The local database `leg_platform` is **throw-away** (D-027, D-030). Check without changing anything:

```powershell
.\.venv\Scripts\python manage.py check
.\.venv\Scripts\python manage.py showmigrations --plan | Select-String -Pattern '\[ \]'
```

* `check` must print `System check identified no issues (1 silenced).` (needs the running MariaDB).
* The second command must print nothing. Any line is an unapplied migration.
* Apply pending migrations only when the main session or a code change requires it, never during a test run:

```powershell
.\.venv\Scripts\python manage.py migrate --database=default
.\.venv\Scripts\python manage.py migrate addresses --database=addresses
```

The second command creates the tables of the address directory (own SQLite file, D-075). Normal setup and schema upgrades (D-075, D-076): prefer `manage.py setup_instance` (`docs/INSTALLATION.md`, "Setup of the instance and initial system administrator"). It never deletes or flushes application data. It creates or rebuilds the disposable address file and queues the official import in MariaDB, without a download during setup. Additive address migrations are applied in place to an existing file (`docs/INSTALLATION.md`, "Address Directory"):

```powershell
.\.venv\Scripts\python manage.py setup_instance
```

* Without the bootstrap variables it creates no administrator (design review accounts: section 9).
* To also create the first administrator on an empty local database: set `BOOTSTRAP_SYSTEM_ADMIN_EMAIL` and `BOOTSTRAP_SYSTEM_ADMIN_PASSWORD` (read with `Read-Host -MaskInput`) **in this PowerShell session only** for that one run, then `Remove-Item Env:...`.
* Do not run this against the local database as a test of "empty database"; `scripts/test.ps1 -Area second-factor` uses its own test database.
* The queued data imports need the worker `manage.py process_import_queue --loop` (D-133) or a `run_scheduled_tasks` run; the web process runs no import. No consumer means no import. Until an import succeeds, address validation and suggestions are unavailable.
* Busy-file and rebuild errors are nonfatal; run setup again later. Never bypass a `.reset` marker.

## 6. Start the development server and the mail worker

Registration, password reset and email change **only queue** an email (D-038); the mail worker sends it. Without the worker nothing is sent (check `worker.err.log` and `worker.out.log`). With `EMAIL_MODE=console` the email goes to the **standard output of the worker**.

One block starts both. It refuses to start if port 8000 is taken (it kills nothing), writes logs and process IDs to `$HOME\.leg-platform-dev\`. One-time logo copy: `New-Item -ItemType Directory -Force "$HOME\.leg-platform-dev\instance-static\instance"; Copy-Item <path of LEG-Design-Vorgaben>\logo\*.svg "$HOME\.leg-platform-dev\instance-static\instance"`:

```powershell
$Repo = (Get-Location).Path
$State = Join-Path $HOME '.leg-platform-dev'
New-Item -ItemType Directory -Force -Path $State | Out-Null
$Port = 8000
$Settings = 'leg_platform.settings'

if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    "Port $Port is in use. Do not kill the process. Use the running server, or choose another port and set INSTANCE_BASE_URL (section 4)."
} elseif ((Test-Path "$State\runserver.pid") -or (Test-Path "$State\worker.pid")) {
    'A PID file exists. Run the status block of section 8 first; remove a PID file only if it is stale.'
} else {
    $env:DJANGO_ALLOWED_HOSTS = '127.0.0.1,localhost'
    # D-090: no automatic download of the real address directory on a development machine.
    $env:ADDRESS_IMPORT_AUTOMATIC = 'False'
    # D-106: open only this canonical hostname; the listener remains loopback-only.
    $env:INSTANCE_BASE_URL = "http://localhost:$Port"
    # Local instance presentation (section 4)
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

* `--insecure` serves `/static/` with debug off; `--noreload` avoids a watcher process.
* The venv `python.exe` is a launcher with the real interpreter as child: each process appears twice. The PID file holds the parent; section 11 stops both.
* The console email in `worker.out.log` holds the **complete link** (hence `EMAIL_MODE=console` is refused in stage and production).
* Server does not come up: read `runserver.err.log` (section 14), wait about five seconds, run section 7.
* After CSS/JavaScript changes, hard-reload the browser (static cache). Restarting refreshes Python and templates only. Do not add duplicate CSS overrides to compensate for a cached file.

### Participant prototyping in VS Code

* Run the task `Local participant prototyping server` instead of the server above. Open `http://localhost:8000/`. Run `D106 local console mail worker` separately if no mail worker is running.
* The task keeps debug off and enables Django's standard autoreloader: Python changes restart the server; template changes reset the template loaders. Refresh the browser after a change; hard-reload for CSS and JavaScript.
* Use the normal authenticated design account (section 9.1). Start at `/participation/buildings/` and use the existing registration wizard. Keep account and object access checks unchanged.
* Use synthetic data only. Do not reset existing drafts or buildings. Mark any later mockup explicitly; it must not pretend to submit a real application.
* Source: installed Django `django/template/autoreload.py` (`watch_for_template_changes`, `template_changed`, `reset_loaders`); `runserver` with its default autoreloader.

## 7. Health checks (read-only)

```powershell
curl.exe -s -o NUL -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8000/            # 302 http://127.0.0.1:8000/login/
curl.exe -s -o NUL -w '%{http_code} %{content_type}\n' http://127.0.0.1:8000/login/      # 200 text/html; charset=utf-8
curl.exe -s -o NUL -w '%{http_code} %{content_type}\n' http://127.0.0.1:8000/static/vendor/tabler/css/tabler.min.css   # 200 text/css
curl.exe -s -o NUL -w '%{http_code}\n' http://127.0.0.1:8000/does-not-exist/              # 404
curl.exe -sI http://127.0.0.1:8000/login/                                                 # Content-Security-Policy, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
```

Use `curl.exe`, not `curl` (alias of `Invoke-WebRequest`). There is no health endpoint. Worker check: `Get-Content "$HOME\.leg-platform-dev\worker.err.log" -Tail 20` shows no repeated errors.

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

## 9. Test accounts

### 9.1 Seeded design accounts

`seed_design_accounts` creates three throw-away accounts under `example.org`: `design.user@example.org` (authenticated user), `design.staff@example.org` (staff user without role), `design.administrator@example.org` (system administrator). It refuses to run unless `APP_ENV=local`.

* Exist? `Test-Path "$HOME\.leg-platform-design-accounts.json"`. If `True`, **use them; never run the creation again**.
* Creation (only if the file does not exist, only when the main session asks): `.\.venv\Scripts\python manage.py seed_design_accounts`. It writes the generated passwords and authenticator keys to the credentials file outside the repository (default `$HOME\.leg-platform-design-accounts.json`, option `--output`; never overwritten). It prints no secret.
* The file is JSON with keys `user`, `staff`, `administrator`; each has `email` and `password`; `staff` and `administrator` also `authenticator_secret`. List the structure without values:

  ```powershell
  (Get-Content -Raw "$HOME\.leg-platform-design-accounts.json" | ConvertFrom-Json).PSObject.Properties.Name
  ```

  Treat emails and passwords like secrets: read only the one you need for a login. Never copy them into reports, screenshots, logs or committed files.
* Staff users and system administrators need a second factor. Print the current codes:

  ```powershell
  .\.venv\Scripts\python manage.py seed_design_accounts --code
  ```

  A code works **once**. A second login within 30 seconds needs a newer code: wait, run the command again. 15 wrong codes pause code entry for the account for 24 hours (D-040); the only ways out are a password reset or the emergency command, which would remove the seeded second factor.
* Session timeouts and "Stay signed in": D-041, D-100.
* Logs (D-099): `$HOME\.leg-platform-dev\logs\` (`application.log`, `security.log`; also in the administration, "Protokolldateien"); the dev server and mail worker write `.err.log` files.
* Data imports (D-090, D-132): the administration page "Datenimporte" has a start button per source and "Alles aktualisieren" (durable queue, one job at a time). **The address and PV buttons download the real files (the address file is about 143 MB; the resulting file is about 1.7 GB for 3.3 million addresses; needs 4 GB free): do not press them casually.** The daily run is off in the tests and in the start block of section 6 (`ADDRESS_IMPORT_AUTOMATIC=False`). Details: `docs/INSTALLATION.md`, "Address Directory". `seed_local_address_directory` loads invented addresses (section 10).

### 9.2 A self-registered test account

1. Open `http://127.0.0.1:8000/register/`, enter an address such as `test.user1@example.org`, submit. The response is always the page "email sent".
2. Extract the link from the worker output with the helper below (it prints recipient and complete link; the last line for the address is the newest).
3. Open the link: password step, then `/register/complete/`; "Konto erstellen" creates the account and logs you in (D-058). The password needs **at least 15 characters** (`docs/SECURITY.md`, section 3).
4. Limits (D-038): all local requests come from `127.0.0.1`. After about 20 registrations or resets in one hour the page answers "Zu viele Anfragen" (HTTP 429). Clear the counters with the snippet of section 14. A script (curl) must send the form field `form_started` of the page, at least two seconds old, and leave `contact_fax_number` empty, otherwise it gets the normal page without a mail (D-058).
5. Password reset and email change work the same way.

Helper for step 2 (the here-string lines start in column 0, as PowerShell requires):

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

For a throw-away account whose setup page (`/profile/two-factor/setup/`) shows the key, compute the code (spaces in the key are ignored). Pass the key on standard input, not as an argument. Use the code within its 30 seconds:

```powershell
'abcd efgh ijkl mnop qrst uvwx yz23 4567' | .\.venv\Scripts\python.exe -c "import base64, hashlib, hmac, struct, sys, time; k = sys.stdin.readline().strip().replace(' ', '').upper(); k += '=' * (-len(k) % 8); d = hmac.new(base64.b32decode(k), struct.pack('>Q', int(time.time()) // 30), hashlib.sha1).digest(); o = d[-1] & 15; print('%06d' % ((struct.unpack('>I', d[o:o+4])[0] & 0x7FFFFFFF) % 10**6))"
```

Replace the example key with the one on the page. Only for throw-away local accounts; for the design accounts use `seed_design_accounts --code`.

## 10. Synthetic address directory (optional)

Without a directory the forms show no suggestions (D-066, D-111). To test suggestions, load invented addresses:

```powershell
.\.venv\Scripts\python manage.py seed_local_address_directory
```

* Loads eight invented addresses (postal codes 9997 to 9999) into the local directory file. The tables must exist (section 5). After a pull that adds an address directory migration, run the second command of section 5 again.
* **Refuses to run unless `APP_ENV=local`.** Refuses to replace a directory whose checksum is not its synthetic marker (a real import is never overwritten). Run it again to replace the synthetic rows. No restart needed.
* Typing `Exa` in the street field gives "Example Street"; `9999` in the postal code gives "Example Town"; `Tes` in the town gives "9997 Test Village". Real street names give nothing.
* The ranking of the LEG's area (D-111) follows the "Invoice sender" address in the settings of the management area: set its postal code to 9999 and its town to "Example Town" to see these addresses first.

Remove the synthetic rows: stop the processes, delete only that file with its `-wal` and `-shm` files (`Remove-Item "$HOME\.leg-platform-dev\address-directory.sqlite3*"`), run the two commands of section 5 again. Do not run the real import casually (section 9.1; `docs/INSTALLATION.md`, "Address Directory").

## 11. Stop everything

Stop by process ID (the PIDs differ per run):

1. Run the status block of section 8. It prints the child and parent PID of `runserver` and `worker`.
2. Stop each printed PID with a literal ID, child first, then parent:

   ```powershell
   Stop-Process -Id 12345    # child of runserver
   Stop-Process -Id 12344    # parent of runserver
   ```

3. Remove the PID files and confirm that the port is free:

   ```powershell
   Remove-Item (Join-Path $HOME '.leg-platform-dev\runserver.pid'), (Join-Path $HOME '.leg-platform-dev\worker.pid') -ErrorAction SilentlyContinue
   Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue    # must print nothing
   ```

Do not stop processes the status block did not list as yours. The mail worker may be stopped at any time (D-038). The MariaDB service stays running.

## 12. Reset the local throw-away data (destructive; only on explicit order)

Only when the main session or the project owner orders it and no designer or tester session depends on the data.

1. Stop the server and the worker (section 11). Confirm `APP_ENV` is `local` and `DB_HOST` is `127.0.0.1` (section 3).
2. `.\.venv\Scripts\python manage.py flush --noinput` removes application data, including the seeded design accounts, and keeps the schema and migration records. It does not reset the separate address directory.
3. `Remove-Item "$HOME\.leg-platform-design-accounts.json"`, then create the accounts again (section 9.1).
4. Start server and worker again (section 6).

## 13. Tests and checks

Test database:

* The tests use `test_<DB_NAME>` (`test_leg_platform` locally). The runner creates and drops it (`--noinput` drops an existing one). **A test run never reads or changes the data of `leg_platform`.**
* The address directory of a test run is a temporary SQLite file. A test class that touches it declares `databases = {"default", "addresses"}`.
* Two test runs against the same MariaDB server overlap on `test_leg_platform` and corrupt both (D-046). **Run only one test process at a time.** Check first:

  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'manage\.py"?\s+test\b' } | Select-Object ProcessId, CommandLine
  ```

  It must print nothing (each run appears twice, parent and child). If it lists a process, wait; never stop it. `scripts/test.ps1` also holds a machine-wide named mutex (`Global\LegPlatformTestRun`) but does not see a run started by hand or on another machine (D-072).
* Exit codes: 0 passed; 1 a test or check failed; 2 wrong usage or missing virtual environment; 3 another run is active.

Scenario runner (D-071): test the area a change touches, not the whole suite. `scripts/test_areas.py` maps source paths to test labels; `scripts/test.ps1` runs `manage.py test` like CI (`--noinput`, deprecation warnings as errors). Area table and core files: `docs/CODEMAP.md`, section 9.

```powershell
.\scripts\test.ps1 -List                              # the areas and the core files
.\scripts\test.ps1 -Changed                           # the areas of the changed files (git status: working tree, staged, untracked)
.\scripts\test.ps1 -Since HEAD~3                      # also the files changed since a revision
.\scripts\test.ps1 -Area registration,mail            # named areas
.\scripts\test.ps1 -Area translations -Timing         # with Django's --timing
.\scripts\test.ps1 -Changed -DryRun                   # print the plan and the commands, run nothing
.\scripts\test.ps1 -Full -Durations 50                # the whole suite, with the 50 slowest tests
```

* A **core file** (settings, test settings, `leg_platform/environment.py`, urls, middleware, the user model, the authentication backend, `manage.py`, dependencies, shared test helpers), a file that **no area maps**, or a test module whose name is not a plain dotted module name selects the **full suite** (D-072). A migration selects `migrations` (fresh test database, also `makemigrations --check --dry-run`). A documentation or agent file selects only `docs`. `.github/workflows/` selects `packaging`.
* `leg_platform/tests/test_areas.py` fails when a test module or repository file is mapped to no area: add new files to `scripts/test_areas.py`.
* **Full suite** (`-Full`, prints `Ran N tests` and `OK`): for a core change (the runner decides), before a deployment gate, in CI, and by the team lead at most once per commit block. Agents start it only when the main session orders it. A report names the area or labels and the number of tests; never present a scoped run as a full run.
* `-KeepDb` (`--keepdb`): reuses `test_leg_platform`; local opt-in. The runner ignores it, with a warning, when the plan needs a fresh database.
* `-Parallel N` (`--parallel`): **does not work with the local application account** (MariaDB error 1044; `docs/INSTALLATION.md`, "Database privileges"). Do not widen the privileges.
* A tool call that cannot wait: start the run with `Start-Process` like the server in section 6 (output to files in `$HOME\.leg-platform-dev\`; the runner writes to standard error) and poll the log.
* The test settings use the MD5 password hasher. One module without the script: `manage.py test <label> --settings=leg_platform.test_settings --noinput` (same one-run rule). `manage.py test --exclude-tag mariadb ...` needs no MariaDB.
* Other checks (need MariaDB): `manage.py check --fail-level WARNING`; `manage.py makemigrations --check --dry-run` (must report "No changes detected").

Deployment check with synthetic production-style variables (no real secrets; the variables exist only in this PowerShell process). It is the CI step "Deployment checks with production settings"; with `$env:APP_ENV = 'stage'` it is the stage step (D-070):

```powershell
$env:APP_ENV = 'production'; $env:EMAIL_MODE = 'graph'
$env:DJANGO_ALLOWED_HOSTS = 'portal.example.org'
$env:INSTANCE_BASE_URL = 'https://portal.example.org'
$env:INSTANCE_PRIVACY_URL = 'https://www.example.org/privacy'
$env:DJANGO_SECRET_KEY = (.\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(50))")
Remove-Item Env:DJANGO_DEBUG, Env:EMAIL_ALLOWED_RECIPIENTS -ErrorAction SilentlyContinue
.\.venv\Scripts\python manage.py check --deploy --fail-level WARNING
```

Expected: `System check identified no issues (3 silenced).` (`security.W005`, `security.W021`, `axes.W006`; D-028).

Translations (the `.po` files are maintained by hand, the `.mo` files are committed, D-034; GNU gettext is in `C:\Program Files\gettext-iconv\bin`, on `PATH`). Swiss German shown; `locale\fr` and `locale\it` (`django.po`, `djangojs.po`) are checked the same way by `accounts/tests/test_translations.py` (`LanguageCatalogueTests`, also when `msgfmt` is installed):

```powershell
$po = 'locale\de_CH\LC_MESSAGES\django.po'; $mo = 'locale\de_CH\LC_MESSAGES\django.mo'
$tmp = Join-Path $env:TEMP 'leg-compiled.mo'
msgfmt --check -o $tmp $po; "msgfmt exit code: $LASTEXITCODE"                  # 0
"differences to the committed catalogue: " + @(Compare-Object (msgunfmt $mo) (msgunfmt $tmp)).Count    # 0
Remove-Item $tmp
"lines with the forbidden letter: " + @(Select-String -Path $po -Pattern ([string][char]0x00DF) -SimpleMatch).Count    # 0
```

After a change to the `.po` file run `.\.venv\Scripts\python manage.py compilemessages`, then repeat the check.

## 14. Troubleshooting

| Symptom | Cause | What to do |
| --- | --- | --- |
| `CommandError: You must set settings.ALLOWED_HOSTS if DEBUG is False.` | `DJANGO_ALLOWED_HOSTS` is not set in the process | Use the start block of section 6. |
| `ImproperlyConfigured` (`DJANGO_DEBUG=True is only allowed with APP_ENV=local`, `APP_ENV must be one of`, `DJANGO_SECRET_KEY must be set`, `Missing database settings`) | The process did not inherit the user variables (terminal or VS Code opened before the setup script ran) | Section 3 shows which names are `NOT SET`. Never set `APP_ENV` to another value to get around a message. Report; restart VS Code and the terminals. |
| No email although the worker runs, or HTTP 429 after wrong logins | Request limits used up (all local requests share `127.0.0.1`), or five wrong passwords for an address locked it for 15 minutes | Wait, or clear the records (snippet below). A password reset still works during a lockout. |
| `OperationalError` 2003 or 1045 | MariaDB is not running (starting it needs administrator rights), or `DB_USER` and `DB_PASSWORD` no longer match the account | Report; the project owner re-runs the setup script. Never print the variables. |

Import errors at startup: the worktree is in the middle of a change. Run `manage.py check`, do not edit code, report with the output and `git status`.

Clear the local login lockouts and request counters (throw-away database only; the output is only counts):

```powershell
.\.venv\Scripts\python manage.py shell -c "from axes.models import AccessAttempt; from leg_platform.accounts.models import LoginIpBucket, RateLimitBucket; print(AccessAttempt.objects.all().delete()[0], LoginIpBucket.objects.all().delete()[0], RateLimitBucket.objects.all().delete()[0])"
```

It deletes the Axes failure records, the login IP counters and the request counters; no account, mail job or security log entry.

## 15. Unverified points (open)

* Not run end to end: the address directory migration and seed (section 10), `flush` (section 12), the lockout-clearing snippet (section 14), `--parallel` with a privileged account (section 13).
