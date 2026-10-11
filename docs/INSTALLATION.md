# Installation

This guide grows with the project. Installation of stage and production instances is documented once hosting has been decided (D-008).

## Local Development

For Windows development machines (D-027). Never use these steps or their credentials for stage or production.

1. Open PowerShell **as administrator** in the project directory and run the setup script.

   First setup:

   ```powershell
   .\scripts\local\setup-dev-windows.ps1
   ```

   Retry after a failed setup (removes the local MySQL installation of the failed run first, then sets everything up again):

   ```powershell
   .\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
   ```

   If a local MySQL installation already exists, the first command stops without changing anything. For an installation in `C:\LEG-MySQL` made by this script it shows the second command; before that command removes anything, it lists everything it will remove and asks you to type `DELETE`; all local MySQL databases are then lost. For an installation made by the earlier version of the script (in `C:\tools\mysql`, as on the current development machine), it prints a few commands to remove it by hand instead. Details in "Recovery after a failed setup" below. Chocolatey 2 or later is required.

   Short phase without a root password: the Chocolatey package initialises MySQL with a `root` account without password and starts the service right away. The script writes the server settings beforehand, so the server listens only on `127.0.0.1` from its first start and X Plugin is off; the script sets the password a few seconds later. During these seconds, any program on the same computer could log in as `root`. Run the script only when no untrusted user or program is active on the computer.

   As an additional safeguard, check as administrator that the Windows Firewall is on and blocks inbound connections for all profiles, and that no rule allows inbound connections to MySQL:

   ```powershell
   Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction
   Get-NetFirewallPortFilter -PolicyStore ActiveStore | Where-Object { $_.LocalPort -in '3306', '33060' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallApplicationFilter -PolicyStore ActiveStore | Where-Object { $_.Program -like '*mysqld*' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' -and $_.Enabled -eq 'True' }
   Get-NetFirewallRule -PolicyStore ActiveStore -Direction Inbound -Action Allow -Enabled True | Where-Object { $port = $_ | Get-NetFirewallPortFilter; $app = $_ | Get-NetFirewallApplicationFilter; $svc = $_ | Get-NetFirewallServiceFilter; -not $_.Owner -and -not $app.Package -and $app.Program -eq 'Any' -and $svc.Service -eq 'Any' -and $port.Protocol -in 'Any', 'TCP' -and $port.LocalPort -eq 'Any' } | Select-Object DisplayName, Profile
   ```

   The first command must show `Enabled True` and `DefaultInboundAction Block` for every profile; the other three must show nothing. The fourth lists enabled inbound allow rules that are not restricted to a port, a program, a service or a Windows Store app; such a rule would also allow connections to MySQL. Rules with an owner or a package are network isolation rules of Windows Store apps and match only traffic of that app (Microsoft Learn, "New-NetFirewallRule", parameters `-Owner` and `-Package`), so they are not listed. Limit: the second command finds only rules whose local port is exactly 3306 or 33060, not port ranges or lists that contain them; the commands do not evaluate remote address restrictions, rules for other protocols or the order of block and allow rules. If in doubt, run the script only on a trusted network. If Windows asks whether `mysqld` may communicate on networks, deny it. Sources: Microsoft Learn, "Get-NetFirewallProfile" (`-PolicyStore ActiveStore` "retrieves the active profile conditions"), "Get-NetFirewallPortFilter", "Get-NetFirewallApplicationFilter", "Get-NetFirewallServiceFilter", "Get-NetFirewallRule" and "New-NetFirewallRule" (NetSecurity module).

   The script:

   * first asks for the new password of the local MySQL `root` account, before anything is installed. The password needs at least 15 characters, only printable ASCII characters, none of `#`, `"`, `'`, `\` and `;`, and no leading or trailing space, because it is written into an SQL string and into a MySQL option file, where these characters have a special meaning (MySQL Reference Manual, "Using Option Files"). Keep it in your password manager; the script does not store it;
   * removes the entry `C:\tools\mysql\current\bin` of the earlier version from your user `PATH` (other entries and the value type are kept) and warns about MySQL folders in the machine-wide `PATH`, which it does not change; open a new terminal afterwards to use the new `PATH`;
   * stops if the MySQL login path file `%APPDATA%\MySQL\.mylogin.cnf` exists, because the MySQL client would read it in addition to the script's own settings;
   * installs Python 3.14 with Chocolatey, unless the package `python314` is already installed;
   * creates the folder `C:\LEG-MySQL` with the subfolders `server` (MySQL programs) and `data` (databases). The script stops if `C:\LEG-MySQL` already exists. Each folder is created with its permissions already set: nothing is inherited from `C:\`, only Administrators and SYSTEM have full control, ordinary users may only read and run the programs in `server` and have no access to `data`, and the owner is Administrators. This matters because the MySQL service runs with full system rights (`LocalSystem`): anybody who could change its files could take over the computer. Folders such as `C:\tools` or `C:\ProgramData`, where the package installs by default, let ordinary users create or change files;
   * writes the MySQL settings file `C:\LEG-MySQL\server\mysql\current\my.ini` before the installation: MySQL listens only on `127.0.0.1` (`bind-address`), X Plugin is off (`mysqlx=OFF`; it would listen on all interfaces on port 33060, and Django does not use it), and the program and data folders are below `C:\LEG-MySQL`. The package keeps an existing `my.ini`, so MySQL uses these settings from its very first start. The script stops if one of the global settings files `C:\my.ini`, `C:\my.cnf`, `%WINDIR%\my.ini` or `%WINDIR%\my.cnf` exists, because MySQL would read them as well;
   * installs MySQL 9.7 LTS with Chocolatey into these folders (package parameters `/installLocation` and `/dataLocation`);
   * checks after the installation that `my.ini` is unchanged and that the data folder was initialised, stops MySQL for the following checks, makes Administrators the owner of every file and folder below `C:\LEG-MySQL` (depending on a Windows policy, the files could otherwise belong to your own user account, and an owner can always change the permissions), and then checks that everything there has only the permissions and owners listed above; any other entry, owner or symbolic link, or an additional settings file `my.cnf` or `mysqld-auto.cnf`, makes the setup fail; MySQL is then started again;
   * verifies with `Get-NetTCPConnection` that the server listens only on `127.0.0.1:3306` and that nothing listens on port 33060;
   * calls the MySQL client only with its own settings (`--defaults-file` or `--no-defaults`, TCP to `127.0.0.1` on port 3306 given on the command line) after removing MySQL client environment variables such as `MYSQL_HOST` or `MYSQL_PWD` from its own process;
   * sets the `root` password, verifies a login with it, checks that only the accounts MySQL creates during the initialisation exist (`root`, `mysql.sys`, `mysql.session` and `mysql.infoschema`, all `@localhost`), and verifies inside the server that `@@GLOBAL.bind_address` is `127.0.0.1` and that the plugin `mysqlx` is not `ACTIVE` (`information_schema.PLUGINS`);
   * keeps the service start type `Manual` until the setup has succeeded and sets it to `Automatic` at the end;
   * creates the database `leg_platform` and the account `leg_platform`@`127.0.0.1` with the privileges described in "Database privileges" below, on `leg_platform` and `test_leg_platform` only;
   * stores the local configuration (`APP_ENV=local`, `DB_*`, `DJANGO_SECRET_KEY`) as user environment variables without printing generated values.

   The package adds `C:\LEG-MySQL\server\mysql\current\bin` to your user `PATH`, so that the MySQL command line tools can be used. Restart VS Code and the terminal afterwards so that they receive the variables. All variables are described in `.env.example`.

   User environment variables are inherited by every process of the Windows user, including AI tools and their subprocesses. Never print the environment (e.g. `Get-ChildItem env:`, `set`, `env`); it contains `DB_PASSWORD` and `DJANGO_SECRET_KEY` (D-027).

2. Create a virtual environment in the project directory and install the project with the newest compatible packages (D-015):

   ```powershell
   py -3.14 -m venv .venv
   .venv\Scripts\python -m pip install --upgrade pip
   .venv\Scripts\python -m pip install --upgrade -e .
   ```

   `.venv` is ignored by Git.

3. Run the checks and the tests:

   ```powershell
   .venv\Scripts\python manage.py check --database default
   .venv\Scripts\python manage.py makemigrations --check --dry-run
   .venv\Scripts\python manage.py test
   ```

   Without a running MySQL server, only the tests that need no database can run: `manage.py test --exclude-tag mysql`. With Django 6.1, the system checks that `check` and other commands such as `makemigrations` run also access the database ("The check management command now supplies all databases if not specified", Django 6.1 release notes), so these commands need a running MySQL server.

Email: locally, emails are written to the console by default (`EMAIL_MODE=console`). To test delivery via Microsoft Graph locally, use `EMAIL_MODE=allowlist` with a dedicated development app registration and sender mailbox, never the credentials of stage or production, and set the allowed recipient addresses only as the user environment variable `EMAIL_ALLOWED_RECIPIENTS` (D-028). Delivery via Microsoft Graph is not implemented yet; until it is, every send attempt in this mode fails with an error. Automated tests never send real email.

### Recovery after a failed setup

If a step after the MySQL installation fails or the script is interrupted with Ctrl+C, the script tries to stop the `MySQL` service and set its start type to `Disabled` and checks the result. If it reports that this did not work, or if the console window was closed during the setup (the cleanup may then not run), stop and disable the service yourself as administrator (`sc.exe stop MySQL`, `sc.exe config MySQL start= disabled`) and check with `Get-Service MySQL | Select-Object Status, StartType`.

#### Installation made by this script (`C:\LEG-MySQL`)

Fix the cause shown by the script and run as administrator:

```powershell
.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
```

The script removes the installation automatically only if `C:\LEG-MySQL` still has exactly the protection it was created with: owner Administrators or SYSTEM, access only for Administrators, SYSTEM and (read only, below `server`) Users, only the folders `server` and `data`, and no symbolic links or junctions; and only if the `MySQL` service runs the program in `C:\LEG-MySQL\server\mysql`. It lists what it will remove, asks you to type `DELETE` and asks for the new `root` password. It then checks everything again, stops the service and sets it to `Disabled`, uninstalls the Chocolatey package `mysql`, deletes the service (`sc.exe delete MySQL`), removes the MySQL folders from your user `PATH`, deletes `C:\LEG-MySQL` and continues with the normal setup.

If the script reports that the service still exists after `sc.exe delete`, close the Services console and Task Manager or restart Windows, and run the command again; every step checks the current state and can be repeated. User environment variables of an earlier run are overwritten by a successful run.

#### Installation made by the earlier version of the script, or anything else

The earlier version installed the programs in Chocolatey's tools folder (`C:\tools\mysql`; the machine-wide variable `ChocolateyToolsLocation` if it is set, otherwise `C:\tools`) and the databases in `C:\ProgramData\MySQL`. Ordinary users could change both folders, so the script does not delete them itself; it also refuses a `C:\LEG-MySQL` that fails the check above. In these cases it changes nothing and prints the commands for the manual removal. For the earlier layout they are:

```powershell
Stop-Service -Name MySQL -Force
sc.exe config MySQL start= disabled
choco uninstall mysql -y
sc.exe delete MySQL
[IO.Directory]::Delete('C:\tools\mysql', $true)
[IO.Directory]::Delete('C:\ProgramData\MySQL', $true)
```

Run them in a PowerShell window opened as administrator, while no other user is signed in to the computer. Commands for parts that no longer exist report an error; that is expected. `[IO.Directory]::Delete` with `$true` deletes the folder with its content and does not follow symbolic links or junctions inside it ("This method does not recurse through the reparse point", Microsoft Learn, "Directory.Delete Method"). All local MySQL databases are lost. Then run the setup script again (first command above, without `-RemoveFailedInstallation`); it removes the old entry `C:\tools\mysql\current\bin` from your user `PATH` automatically. The folder `C:\tools` itself may remain; it is no longer used by this project.

For a `C:\LEG-MySQL` that the script refuses, check its content and owner first (`Get-Acl C:\LEG-MySQL | Format-List`), then remove it the same way (`[IO.Directory]::Delete('C:\LEG-MySQL', $true)`), together with the service and package as above.

The `PATH` cleanup and the user environment variables apply to the Windows account that runs the script. Run it elevated with your own account ("Run as administrator"); the script warns if the account differs from the signed-in user.

The Chocolatey package `mysql` 9.7.2 has no uninstall script (its `tools` folder contains only `chocolateyInstall.ps1`; checked on the development machine on 2026-10-11), so `choco uninstall` removes neither the service nor the program files. It installs to `<installLocation>\mysql\current`, writes `my.ini` there only if none exists, initialises `<dataLocation>\mysql\data` with `--initialize-insecure` using that `my.ini`, registers the service with `mysqld --install MySQL` (an unquoted path; therefore the folder names contain no spaces) and starts it. Source: https://community.chocolatey.org/packages/mysql and its `tools\chocolateyInstall.ps1` (checked on 2026-10-11).

The helper functions of the setup script are tested without changing the system (no administrator rights needed; the creation of folders with the owner Administrators is tested only in an elevated PowerShell):

```powershell
powershell -NoProfile -File scripts\local\test-setup-dev-windows.ps1
```

### Database privileges

The application account receives only `SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES`, granted on the database level, and no global privileges:

* `SELECT`, `INSERT`, `UPDATE`, `DELETE`: data access of the application.
* `CREATE`, `DROP`, `ALTER`, `INDEX`: schema changes by Django migrations. `REFERENCES`: "Creation of a foreign key constraint requires the `REFERENCES` privilege for the parent table" (MySQL Reference Manual, "Privileges Provided by MySQL").
* Locally also on `test_leg_platform`: "The test database is created by the user specified by `USER`" (Django documentation, "Writing and running tests"); Django's MySQL backend runs `CREATE DATABASE` and `DROP DATABASE` for it (source `django/db/backends/base/creation.py`). MySQL allows granting privileges on a database that does not exist yet (MySQL Reference Manual, "GRANT Statement").
* In database-level grants, `_` is a wildcard; the script escapes it (`leg\_platform`), so that the grant covers exactly the named databases (MySQL Reference Manual, "GRANT Statement").
* Parallel test runs (`manage.py test --parallel`) are not supported with this account: they clone the test database into further databases (`test_leg_platform_1`, ...) with `mysqldump`, which needs other privileges and databases.

Pattern for stage and production: the same privileges, only on the instance's own database, no test database and no global privileges, for an account that can connect only from the application host. `DROP` on the database level also allows dropping the whole database; a stricter split into a migration account (schema privileges) and a runtime account (`SELECT`, `INSERT`, `UPDATE`, `DELETE` only) is decided together with the hosting (D-008).

The privileges were taken from the sources above; whether Django's test suite and later migrations run with exactly these privileges is verified when the script is first run on a development machine (not yet done).

Do not run `manage.py migrate` against the local database yet: the custom user model (D-021) must exist before the first migration of a persistent database. The test runner creates and removes its own test database.

Continuous integration (`.github/workflows/ci.yml`) runs the same checks and tests with the minimum versions (Python 3.12, Django 6.0, MySQL 8.4) and with the newest versions (Python 3.14, Django 6.1, MySQL 9.7).
