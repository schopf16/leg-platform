"""Disposable local browser data using Django's test DB and live-server infrastructure.

Start only through d106-browser-fixture.ps1 (the D-046 test mutex). No shared
application data is queried or changed. Stop gracefully by creating STOP in the
state directory. The test database is dropped on normal exit. This is a manual
browser harness, not an automated test or an acceptance claim.
"""

import argparse
import json
import os
import secrets
import socket
import sys
import time
from base64 import b32encode
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--administrator-state", choices=("first-login", "ready"), default="first-login")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 8000:
        parser.error("Use an unprivileged local port other than the shared port 8000.")
    directory = args.state_directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Never overwrite credentials or adopt an earlier run's stop/status files.
    if any(directory.iterdir()):
        parser.error("The fixture state directory must be empty.")
    origin = f"http://localhost:{args.port}"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))

    # Only synthetic/local overrides; DB authentication is consumed by the normal
    # Django configuration, never inspected, copied or printed by this harness.
    os.environ.update(
        APP_ENV="local",
        DB_NAME="leg_platform",
        DB_HOST="127.0.0.1",
        DJANGO_SETTINGS_MODULE="leg_platform.test_settings",
        DJANGO_SECRET_KEY=secrets.token_urlsafe(64),
        DJANGO_ALLOWED_HOSTS="localhost",
        DJANGO_DEBUG="False",
        INSTANCE_BASE_URL=origin,
        INSTANCE_NAME="Disposable Browser Fixture",
        INSTANCE_WEBSITE_URL="https://operator.example.org",
        EMAIL_MODE="console",
        ADDRESS_IMPORT_AUTOMATIC="False",
        LOG_DIRECTORY="",
    )
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth.models import Permission
    from django.contrib.staticfiles.handlers import StaticFilesHandler
    from django.core import mail
    from django.db import connections
    from django.test.runner import DiscoverRunner
    from django.test.testcases import LiveServerThread
    from django_otp.oath import TOTP

    from leg_platform.accounts.mail_queue import process_pending
    from leg_platform.accounts.models import AuthenticatorDevice, User

    # Cookies are host-wide, not port-wide. Do not replace the shared localhost
    # instance's session, CSRF or language cookies in the designer's browser.
    settings.SESSION_COOKIE_NAME = "d106_fixture_session"
    settings.CSRF_COOKIE_NAME = "d106_fixture_csrf"
    settings.LANGUAGE_COOKIE_NAME = "d106_fixture_language"
    runner = DiscoverRunner(verbosity=0, interactive=False)
    database_config = None
    server = None
    started = False
    exported = 0
    try:
        runner.setup_test_environment()
        # Both aliases are test databases: MariaDB test_leg_platform and the
        # test_settings temporary SQLite directory, never the shared directory.
        database_config = runner.setup_databases()
        if connections["default"].settings_dict["NAME"] != "test_leg_platform":
            raise RuntimeError("Refusing fixtures outside test_leg_platform.")
        if User.objects.exists():
            raise RuntimeError("Refusing fixtures in a nonempty database.")

        credentials = {}
        for role in ("password_user", "app_user", "staff", "system_admin"):
            password = secrets.token_urlsafe(30)
            email = f"browser.{role}@example.org"
            profile = {
                "first_name": "Synthetic",
                "last_name": role.replace("_", " ").title(),
                "salutation": "mrs",
                "street": "Example Street",
                "house_number": "12",
                "swiss_postal_code": 3000,
                "town": "Example Town",
            }
            if role == "system_admin":
                user = User.objects.create_superuser(
                    email, password,
                    password_change_required=args.administrator_state == "first-login",
                    **profile,
                )
            else:
                user = User.objects.create_user(email, **profile)
                user.complete_registration(password)
            if role == "staff":
                user.set_privileges(is_staff=True, is_superuser=False)
                user.user_permissions.set(
                    Permission.objects.filter(
                        content_type__app_label="accounts",
                        codename__in=("view_user", "change_user"),
                    )
                )
            entry = {"email": email, "password": password, "user_id": user.pk}
            if role in ("app_user", "staff") or (
                role == "system_admin" and args.administrator_state == "ready"
            ):
                device = AuthenticatorDevice.objects.create_device(user, confirmed=True)
                entry["authenticator_secret"] = b32encode(device.bin_key).decode("ascii")
            credentials[role] = entry
        write_json(directory / "credentials.json", credentials)
        (directory / "mail.jsonl").touch()
        if User.objects.filter(is_superuser=True, is_active=True).count() != 1:
            raise RuntimeError("The fixture must contain exactly one system administrator.")
        # Real middleware, CSRF, auth and WebAuthn verification remain enabled.
        # The only substitutions are test DBs, weak test hashing, static serving
        # and Django's in-memory mail backend. Browser WebAuthn is not mocked.
        runner.run_checks({"default", "addresses"})
        server = LiveServerThread("localhost", StaticFilesHandler, port=args.port)
        server.daemon = True
        server.start()
        server.is_ready.wait(timeout=30)
        if not server.is_ready.is_set() or server.error:
            raise RuntimeError("The disposable live server could not start.")
        started = True
        write_json(
            directory / "status.json",
            {
                "running": True,
                "pid": os.getpid(),
                "url": origin,
                "database": "test_leg_platform",
                "credentials": str(directory / "credentials.json"),
                "totp": str(directory / "totp.json"),
                "mail_sink": str(directory / "mail.jsonl"),
                "stop_file": str(directory / "STOP"),
                "system_admin_state": args.administrator_state,
                "sole_active_system_administrator": True,
            },
        )
        print(f"Disposable browser fixture ready: {origin}", flush=True)
        print("Test database reserved; no Changed/Full/manual test runs until stopped.", flush=True)
        while not (directory / "STOP").exists():
            done, failed = process_pending()
            if failed:
                raise RuntimeError("A disposable mail job failed.")
            # The sink contains synthetic test mail only, including short-lived
            # test links. Never echo it to the terminal or shared logs.
            outbox = mail.outbox
            with (directory / "mail.jsonl").open("a", encoding="utf-8") as sink:
                while exported < len(outbox):
                    message = outbox[exported]
                    sink.write(json.dumps({
                        "to": message.to,
                        "subject": message.subject,
                        "body": message.body,
                    }) + "\n")
                    exported += 1
            codes = {}
            for role, entry in credentials.items():
                device = AuthenticatorDevice.objects.filter(
                    user_id=entry["user_id"], confirmed=True
                ).first()
                if device:
                    generator = TOTP(device.bin_key, device.STEP, 0, device.DIGITS, device.drift)
                    generator.time = time.time()
                    codes[role] = {
                        "code": str(generator.token()).zfill(device.DIGITS),
                        "seconds_remaining": device.STEP - int(time.time()) % device.STEP,
                    }
            write_json(directory / "totp.json", codes)
            connections.close_all()
            time.sleep(1)
    finally:
        if server:
            server.terminate()
        if database_config is not None:
            runner.teardown_databases(database_config)
        runner.teardown_test_environment()
        if started:
            write_json(directory / "status.json", {"running": False, "database_dropped": True})
            print("Disposable fixture stopped; test database dropped.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not include exception text or locals which may contain test proofs.
        print(f"Disposable fixture failed ({type(error).__name__}); no secret details printed.",
              file=sys.stderr, flush=True)
        sys.exit(1)
