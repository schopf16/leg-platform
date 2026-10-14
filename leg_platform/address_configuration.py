"""Separate read-only address settings from isolated importer credentials."""

from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured


def validate_process_credentials(env: Mapping[str, str]) -> None:
    if any(env.get(name, "").strip() for name in ("ADDRESS_IMPORT_DB_USER", "ADDRESS_IMPORT_DB_PASSWORD")):
        if env.get("DJANGO_SETTINGS_MODULE") != "leg_platform.import_settings":
            raise ImproperlyConfigured("Address importer credentials must not be present in the web process.")


def address_database(env: Mapping[str, str], sql_mode: tuple[str, ...]) -> dict | None:
    names = ("ADDRESS_DB_NAME", "ADDRESS_DB_USER", "ADDRESS_DB_PASSWORD")
    values = {name: env.get(name, "").strip() for name in names}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ImproperlyConfigured("ADDRESS_DB_NAME, ADDRESS_DB_USER and ADDRESS_DB_PASSWORD are required together.")
    port = env.get("ADDRESS_DB_PORT", "").strip() or "3306"
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ImproperlyConfigured("ADDRESS_DB_PORT must be a port from 1 to 65535.")
    if values["ADDRESS_DB_NAME"] == env.get("DB_NAME", "").strip():
        raise ImproperlyConfigured("The address directory must use a separate database.")
    if values["ADDRESS_DB_USER"] == env.get("DB_USER", "").strip():
        raise ImproperlyConfigured("The address directory requires its own read-only account.")
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": values["ADDRESS_DB_NAME"],
        "USER": values["ADDRESS_DB_USER"],
        "PASSWORD": values["ADDRESS_DB_PASSWORD"],
        "HOST": env.get("ADDRESS_DB_HOST", "").strip() or "127.0.0.1",
        "PORT": port,
        "OPTIONS": {
            "charset": "utf8mb4",
            "isolation_level": "read committed",
            "init_command": "SET sql_mode='" + ",".join(sql_mode) + "'",
        },
        "TEST": {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_uca1400_ai_ci"},
    }


def importer_database(env: Mapping[str, str], reader: dict | None) -> dict:
    if reader is None:
        raise ImproperlyConfigured("The address reader configuration is required for the importer.")
    user = env.get("ADDRESS_IMPORT_DB_USER", "").strip()
    password = env.get("ADDRESS_IMPORT_DB_PASSWORD", "").strip()
    if not user or not password:
        raise ImproperlyConfigured("The isolated importer requires ADDRESS_IMPORT_DB_USER and ADDRESS_IMPORT_DB_PASSWORD.")
    if user in {reader["USER"], env.get("DB_USER", "").strip()}:
        raise ImproperlyConfigured("The isolated importer requires its own database account.")
    return {**reader, "USER": user, "PASSWORD": password}
