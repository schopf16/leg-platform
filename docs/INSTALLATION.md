# Installation

This guide grows with the project. Installation of stage and production instances is documented once hosting has been decided (D-008).

## Local Development

For Windows development machines (D-027). Never use these steps or their credentials for stage or production.

1. Run the setup script once in PowerShell **as administrator**:

   ```powershell
   .\scripts\local\setup-dev-windows.ps1
   ```

   It installs MySQL 9.7 LTS and Python 3.14 with Chocolatey, binds MySQL to `127.0.0.1`, sets a `root` password (entered interactively), creates the database `leg_platform` and an application account with access only to `leg_platform` and `test_leg_platform`, and stores the local configuration (`APP_ENV=local`, `DB_*`, `DJANGO_SECRET_KEY`) as user environment variables without printing generated values. Restart VS Code and the terminal afterwards so that they receive the variables. All variables are described in `.env.example`.

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

Do not run `manage.py migrate` against the local database yet: the custom user model (D-021) must exist before the first migration of a persistent database. The test runner creates and removes its own test database.

Continuous integration (`.github/workflows/ci.yml`) runs the same checks and tests with the minimum versions (Python 3.11, Django 5.2, MySQL 8.4) and with the newest versions (Python 3.14, Django 6.1, MySQL 9.7).
