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

   If a local MySQL installation already exists, the first command stops without changing anything and shows the second command. Before the second command deletes the MySQL data directory, it asks you to type `DELETE`; all local MySQL databases are then lost. Details in "Recovery after a failed setup" below. Chocolatey 2 or later is required.

   The script:

   * first asks for the new password of the local MySQL `root` account, before anything is installed. The password needs at least 15 characters, only printable ASCII characters, none of `#`, `"`, `'`, `\` and `;`, and no leading or trailing space, because it is written into an SQL string and into a MySQL option file, where these characters have a special meaning (MySQL Reference Manual, "Using Option Files"). Keep it in your password manager; the script does not store it;
   * installs Python 3.14 with Chocolatey, unless the package `python314` is already installed, and then MySQL 9.7 LTS;
   * finds the MySQL server program from the Windows service, also when the service command line has no `.exe` extension and no quotes, as the Chocolatey package registers it (`C:\tools\mysql\current\bin\mysqld MySQL`), and reads the option file `my.ini` in the MySQL base directory;
   * binds MySQL to `127.0.0.1` and disables X Plugin (`mysqlx=OFF`) in `my.ini`, restarts the service and verifies with `Get-NetTCPConnection` that the server listens only on `127.0.0.1:3306` and that nothing listens on the X Plugin port 33060. X Plugin is enabled by default and listens on all interfaces (`mysqlx_bind_address` default `*`); Django does not use the X Protocol. A copy of the original option file is kept as `my.ini.before-leg-setup`;
   * sets the `root` password, verifies a login with it, and verifies inside the server that `@@GLOBAL.bind_address` is `127.0.0.1` and that the plugin `mysqlx` is not `ACTIVE` (`information_schema.PLUGINS`), because X Plugin may open its port only after the socket check;
   * keeps the service start type `Manual` until the setup has succeeded and sets it to `Automatic` at the end;
   * creates the database `leg_platform` and the account `leg_platform`@`127.0.0.1` with the privileges described in "Database privileges" below, on `leg_platform` and `test_leg_platform` only;
   * stores the local configuration (`APP_ENV=local`, `DB_*`, `DJANGO_SECRET_KEY`) as user environment variables without printing generated values.

   Restart VS Code and the terminal afterwards so that they receive the variables. All variables are described in `.env.example`.

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

If a step after the MySQL installation fails or the script is interrupted with Ctrl+C, the script tries to stop the `MySQL` service and set its start type to `Disabled`, checks the result and prints the paths it found. If it reports that this did not work, or if the console window was closed during the setup (the cleanup may then not run), stop and disable the service yourself as administrator (`sc.exe stop MySQL`, `sc.exe config MySQL start= disabled`) and check with `Get-Service MySQL | Select-Object Status, StartType`.

To start over, fix the cause shown by the script and run as administrator:

```powershell
.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation
```

Before it changes anything, the script lists what it will remove and asks for the new `root` password. It then:

* stops the `MySQL` service, uninstalls the Chocolatey package `mysql` and deletes the service if it still exists (`sc.exe delete MySQL`);
* deletes the data directory `C:\ProgramData\MySQL\data` only after you have typed `DELETE`, and only if it looks like a MySQL data directory (it contains `mysql.ibd` and the folder `mysql`) or is empty. It never deletes another directory: if `my.ini` names a different `datadir`, the script reports it and leaves it in place;
* deletes the leftover `my.ini` and `my.ini.before-leg-setup`, checks that nothing of the old installation remains, and continues with the normal setup.

If the script reports that the service still exists after `sc.exe delete`, close the Services console and Task Manager or restart Windows, and run the command again; every step checks the current state and can be repeated. User environment variables of an earlier run are overwritten by a successful run.

Manual steps, if you prefer them or the script cannot complete them:

1. Uninstall MySQL: `choco uninstall mysql -y`.
2. If the Windows service still exists (`Get-Service MySQL`), remove it: `sc.exe delete MySQL`.
3. Delete the data directory (`datadir` in `my.ini`; the Chocolatey package uses `C:\ProgramData\MySQL\data` unless `/dataLocation` was given). This is required: the package initialises a new data directory only if none exists, and an old one may already contain the new `root` password or the application account.
4. Delete `my.ini` and `my.ini.before-leg-setup` in the MySQL installation directory if they remain; the package creates `my.ini` only if it does not exist.
5. Remove user environment variables the script may have set (`APP_ENV`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DJANGO_SECRET_KEY`), e.g. with `[Environment]::SetEnvironmentVariable('DB_PASSWORD', $null, 'User')`.
6. Fix the cause shown by the script and run it again. Without `-RemoveFailedInstallation`, the script refuses to run while the `MySQL` service, the Chocolatey package `mysql`, the data directory `C:\ProgramData\MySQL\data` or a leftover `my.ini` exists.

The Chocolatey package `mysql` 9.7.2 has no uninstall script (its `tools` folder contains only `chocolateyInstall.ps1`; checked on the development machine on 2026-10-11), so `choco uninstall` does not remove the service; the program files under `C:\tools\mysql` may also remain and are overwritten by the next installation. Source: https://community.chocolatey.org/packages/mysql (install script, checked on 2026-10-11).

The helper functions of the setup script are tested without changing the system (no administrator rights needed):

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
