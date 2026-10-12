# Installation

This guide grows with the project. Installation of stage and production instances is documented once the hosting details are decided (D-008).

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

Email: locally, emails are written to the console by default (`EMAIL_MODE=console`). To test delivery via Microsoft Graph locally, use `EMAIL_MODE=allowlist` with a dedicated development app registration and sender mailbox, never the credentials of stage or production, and set the allowed recipient addresses only as the user environment variable `EMAIL_ALLOWED_RECIPIENTS` (D-028). Delivery via Microsoft Graph is not implemented yet; until it is, every send attempt in this mode fails with an error. Automated tests never send real email.

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

The privileges were taken from the sources above; whether Django's test suite and later migrations run with exactly these privileges is verified when the script is first run on a development machine (not yet done).

Do not run `manage.py migrate` against the local database yet: the custom user model (D-021) must exist before the first migration of a persistent database. The test runner creates and removes its own test database.

Continuous integration (`.github/workflows/ci.yml`) runs the same checks and tests with the minimum versions (Python 3.12, Django 6.0, MariaDB 10.11) and with the newest versions (Python 3.14, Django 6.1, MariaDB 12.3), with an application account that has exactly the privileges above (created by root in a CI step).
