"""Validation of the environment configuration of an instance.

The settings module reads its configuration only from environment variables, the method
shown in Django's deployment checklist (docs/DECISIONS.md, D-027). This module validates
those variables in one place and refuses to start on missing or unsafe values, so that
security settings are secure by default and are only relaxed explicitly for
``APP_ENV=local`` (docs/SECURITY.md, section 3).

The functions take the environment as a mapping, so that tests can call them with any
values without reloading Django's settings.

Error messages never contain the values of secret variables.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit

from django.core.exceptions import ImproperlyConfigured

APP_ENV_LOCAL = "local"
APP_ENV_STAGE = "stage"
APP_ENV_PRODUCTION = "production"
APP_ENVS = (APP_ENV_LOCAL, APP_ENV_STAGE, APP_ENV_PRODUCTION)

EMAIL_MODE_CONSOLE = "console"
EMAIL_MODE_ALLOWLIST = "allowlist"
EMAIL_MODE_GRAPH = "graph"
EMAIL_MODES = (EMAIL_MODE_CONSOLE, EMAIL_MODE_ALLOWLIST, EMAIL_MODE_GRAPH)

# One year, the value Django's documentation calls common for SECURE_HSTS_SECONDS.
DEFAULT_HSTS_SECONDS = 31536000

# The same criteria as Django's deployment check security.W009 (django/core/checks/
# security/base.py), enforced at startup outside APP_ENV=local.
SECRET_KEY_MIN_LENGTH = 50
SECRET_KEY_MIN_UNIQUE_CHARACTERS = 5
SECRET_KEY_INSECURE_PREFIX = "django-insecure-"

PORT_MIN = 1
PORT_MAX = 65535

# Neutral platform default; instance values belong to the configuration (D-013, D-014).
DEFAULT_INSTANCE_NAME = "LEG Platform"
INSTANCE_NAME_MAX_LENGTH = 100
# Used only with APP_ENV=local, where the development server listens on this origin.
LOCAL_INSTANCE_BASE_URL = "http://127.0.0.1:8000"

_TRUE_VALUES = ("true",)
_FALSE_VALUES = ("false",)

# The delivery modes each APP_ENV allows (docs/SECURITY.md, sections 8 and 11;
# docs/ARCHITECTURE.md, "Email"; D-028): local development allows console output (the default)
# and delivery restricted to allowed recipients, so that Microsoft Graph delivery can be
# tested locally with a dedicated development app registration; stage delivery is restricted
# to allowed recipients; unrestricted delivery is allowed only in production, and production
# allows nothing else, so that security links never end up in console or server logs.
ALLOWED_EMAIL_MODES = {
    APP_ENV_LOCAL: (EMAIL_MODE_CONSOLE, EMAIL_MODE_ALLOWLIST),
    APP_ENV_STAGE: (EMAIL_MODE_ALLOWLIST,),
    APP_ENV_PRODUCTION: (EMAIL_MODE_GRAPH,),
}


@dataclass(frozen=True)
class EnvironmentConfig:
    """Validated configuration read from the environment."""

    app_env: str
    debug: bool
    secret_key: str
    secret_key_fallbacks: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    csrf_trusted_origins: tuple[str, ...]
    secure_proxy_ssl_header: tuple[str, str] | None
    axes_ipware_proxy_count: int
    hsts_seconds: int
    hsts_include_subdomains: bool
    email_mode: str
    email_allowed_recipients: tuple[str, ...]
    instance_name: str
    instance_base_url: str
    instance_privacy_url: str
    instance_website_url: str
    email_sender_address: str
    db_name: str
    db_user: str
    db_password: str
    db_host: str
    db_port: str

    @property
    def is_local(self) -> bool:
        return self.app_env == APP_ENV_LOCAL

    def __repr__(self) -> str:
        # Never expose secrets through repr(), e.g. in tracebacks or debug output.
        return f"EnvironmentConfig(app_env={self.app_env!r}, debug={self.debug!r})"


def _get(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _is_ascii_number(value: str) -> bool:
    # str.isdigit() also accepts characters such as "²" that int() rejects.
    return re.fullmatch(r"[0-9]+", value) is not None


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name).lower()
    if not value:
        return default
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ImproperlyConfigured(f"{name} must be 'True' or 'False'.")


def parse_app_env(env: Mapping[str, str]) -> str:
    value = _get(env, "APP_ENV")
    if value not in APP_ENVS:
        raise ImproperlyConfigured(
            f"APP_ENV must be one of {', '.join(APP_ENVS)}; got {value!r}."
        )
    return value


def parse_debug(env: Mapping[str, str], app_env: str) -> bool:
    value = _get(env, "DJANGO_DEBUG").lower()
    if value in ("",) + _FALSE_VALUES:
        return False
    if value not in _TRUE_VALUES:
        raise ImproperlyConfigured("DJANGO_DEBUG must be 'True' or 'False'.")
    if app_env != APP_ENV_LOCAL:
        raise ImproperlyConfigured("DJANGO_DEBUG=True is only allowed with APP_ENV=local.")
    return True


def _check_secret_key_strength(name: str, value: str) -> None:
    if (
        len(value) < SECRET_KEY_MIN_LENGTH
        or len(set(value)) < SECRET_KEY_MIN_UNIQUE_CHARACTERS
        or value.startswith(SECRET_KEY_INSECURE_PREFIX)
    ):
        raise ImproperlyConfigured(
            f"{name} must be a random value with at least {SECRET_KEY_MIN_LENGTH} "
            f"characters and at least {SECRET_KEY_MIN_UNIQUE_CHARACTERS} unique characters, "
            f"not prefixed with {SECRET_KEY_INSECURE_PREFIX!r}."
        )


def parse_secret_keys(env: Mapping[str, str], app_env: str) -> tuple[str, tuple[str, ...]]:
    secret_key = _get(env, "DJANGO_SECRET_KEY")
    if not secret_key:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")
    fallbacks = _split_list(_get(env, "DJANGO_SECRET_KEY_FALLBACKS"))
    if app_env != APP_ENV_LOCAL:
        _check_secret_key_strength("DJANGO_SECRET_KEY", secret_key)
        for fallback in fallbacks:
            _check_secret_key_strength("Each value of DJANGO_SECRET_KEY_FALLBACKS", fallback)
    return secret_key, fallbacks


def parse_allowed_hosts(env: Mapping[str, str], app_env: str) -> tuple[str, ...]:
    hosts = _split_list(_get(env, "DJANGO_ALLOWED_HOSTS"))
    for host in hosts:
        # "*" allows every host; a leading dot allows all subdomains. ALLOWED_HOSTS lists
        # only the host names of the application (docs/SECURITY.md, section 4; D-025).
        if "*" in host or host.startswith("."):
            raise ImproperlyConfigured(
                f"DJANGO_ALLOWED_HOSTS must list exact host names; wildcards are not "
                f"allowed (got {host!r})."
            )
    if not hosts and app_env != APP_ENV_LOCAL:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set outside APP_ENV=local.")
    return hosts


def parse_csrf_trusted_origins(env: Mapping[str, str], app_env: str) -> tuple[str, ...]:
    origins = _split_list(_get(env, "DJANGO_CSRF_TRUSTED_ORIGINS"))
    allowed_schemes = ("https",) if app_env != APP_ENV_LOCAL else ("https", "http")
    for origin in origins:
        if "*" in origin:
            # Django accepts "https://*.example.com"; the project never trusts a wildcard
            # (docs/SECURITY.md, section 3).
            raise ImproperlyConfigured(
                f"DJANGO_CSRF_TRUSTED_ORIGINS must not contain wildcards (got {origin!r})."
            )
        parts = urlsplit(origin)
        try:
            # Accessing port raises ValueError for a non-numeric or out-of-range port.
            port = parts.port
        except ValueError:
            port = -1
        if (
            parts.scheme not in allowed_schemes
            or not parts.hostname
            or (port is not None and not PORT_MIN <= port <= PORT_MAX)
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ImproperlyConfigured(
                f"Each value of DJANGO_CSRF_TRUSTED_ORIGINS must be an origin of the form "
                f"{' or '.join(s + '://host[:port]' for s in allowed_schemes)} "
                f"(got {origin!r})."
            )
    return origins


def parse_secure_proxy_ssl_header(env: Mapping[str, str]) -> tuple[str, str] | None:
    value = _get(env, "DJANGO_SECURE_PROXY_SSL_HEADER")
    if not value:
        return None
    name, separator, header_value = (part.strip() for part in value.partition(","))
    if not separator or not name.startswith("HTTP_") or not header_value or "," in header_value:
        raise ImproperlyConfigured(
            "DJANGO_SECURE_PROXY_SSL_HEADER must have the form 'HTTP_<HEADER>,<value>', "
            "e.g. 'HTTP_X_FORWARDED_PROTO,https'."
        )
    return name, header_value


def parse_axes_ipware_proxy_count(env: Mapping[str, str]) -> int:
    """Return the trusted proxy count; zero trusts no forwarded client-IP headers."""
    value = _get(env, "AXES_IPWARE_PROXY_COUNT")
    if not value:
        return 0
    if not _is_ascii_number(value):
        raise ImproperlyConfigured(
            "AXES_IPWARE_PROXY_COUNT must be a non-negative ASCII integer."
        )
    try:
        return int(value)
    except ValueError:
        raise ImproperlyConfigured(
            "AXES_IPWARE_PROXY_COUNT must be a non-negative ASCII integer."
        ) from None


def parse_hsts_seconds(env: Mapping[str, str]) -> int:
    value = _get(env, "DJANGO_SECURE_HSTS_SECONDS")
    if not value:
        return DEFAULT_HSTS_SECONDS
    if not _is_ascii_number(value) or int(value) < 1:
        raise ImproperlyConfigured("DJANGO_SECURE_HSTS_SECONDS must be a positive integer.")
    return int(value)


def parse_hsts_include_subdomains(env: Mapping[str, str]) -> bool:
    # Off by default: the policy would also apply to every host below the application's host
    # name; enabling it is a decision of the operator for its domain (D-028).
    return _parse_bool(env, "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)


def parse_email(env: Mapping[str, str], app_env: str) -> tuple[str, tuple[str, ...]]:
    mode = _get(env, "EMAIL_MODE")
    if not mode:
        # Only local development has a default; elsewhere the mode must be chosen explicitly.
        if app_env != APP_ENV_LOCAL:
            raise ImproperlyConfigured("EMAIL_MODE must be set outside APP_ENV=local.")
        mode = EMAIL_MODE_CONSOLE
    if mode not in EMAIL_MODES:
        raise ImproperlyConfigured(
            f"EMAIL_MODE must be one of {', '.join(EMAIL_MODES)}; got {mode!r}."
        )
    allowed = ALLOWED_EMAIL_MODES[app_env]
    if mode not in allowed:
        raise ImproperlyConfigured(
            f"APP_ENV={app_env} allows only EMAIL_MODE={' or '.join(allowed)}; got {mode!r}."
        )
    recipients = parse_email_allowed_recipients(env)
    if mode == EMAIL_MODE_ALLOWLIST and not recipients:
        # An allowlist without entries would deliver to nobody without notice; refuse the
        # start instead (D-028, re-review of 2026-10-11, N-5).
        raise ImproperlyConfigured("EMAIL_MODE=allowlist requires EMAIL_ALLOWED_RECIPIENTS.")
    return mode, recipients


# One complete address: exactly one "@", a non-empty local part and a domain with at least two
# labels; no white space, wildcards or list separators. Deliberately simple: the list contains
# only addresses the operator or developer chose, and it only restricts delivery.
_ALLOWED_RECIPIENT = re.compile(
    r"[^@\s,*]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)


def parse_email_allowed_recipients(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return the normalised (lower-case) allowed recipients; refuse invalid entries.

    Error messages name only the position of an invalid entry, never the addresses, which
    are personal data.
    """
    recipients = []
    for position, entry in enumerate(_split_list(_get(env, "EMAIL_ALLOWED_RECIPIENTS")), 1):
        if not _ALLOWED_RECIPIENT.fullmatch(entry):
            raise ImproperlyConfigured(
                f"Entry {position} of EMAIL_ALLOWED_RECIPIENTS is not a single complete email "
                f"address (exactly one '@', no wildcards, no domain-only entries)."
            )
        recipients.append(entry.lower())
    return tuple(recipients)


def parse_instance_name(env: Mapping[str, str]) -> str:
    value = _get(env, "INSTANCE_NAME") or DEFAULT_INSTANCE_NAME
    # The name appears in email subjects; control characters would allow header injection.
    if len(value) > INSTANCE_NAME_MAX_LENGTH or any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in ("Zl", "Zp")
        for character in value
    ):
        raise ImproperlyConfigured(
            f"INSTANCE_NAME must have at most {INSTANCE_NAME_MAX_LENGTH} characters and "
            "no control or line separator characters."
        )
    return value


def _parse_url(name: str, value: str, allowed_schemes: tuple[str, ...]) -> SplitResult:
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in allowed_schemes
        or not parts.hostname
        or any(character.isspace() for character in value)
        or (port is not None and not PORT_MIN <= port <= PORT_MAX)
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ImproperlyConfigured(
            f"{name} must be an absolute {' or '.join(s + '://' for s in allowed_schemes)} URL "
            "without credentials or fragment."
        )
    return parts


def parse_instance_base_url(
    env: Mapping[str, str], app_env: str, allowed_hosts: tuple[str, ...]
) -> str:
    """Return the canonical origin of the instance, used to build links in emails (D-013).

    Links in emails never come from the request (docs/SECURITY.md, section 4). The host must be
    one of ALLOWED_HOSTS, so that the configuration cannot point links at another host.
    """
    value = _get(env, "INSTANCE_BASE_URL")
    if not value:
        if app_env != APP_ENV_LOCAL:
            raise ImproperlyConfigured("INSTANCE_BASE_URL must be set outside APP_ENV=local.")
        return LOCAL_INSTANCE_BASE_URL
    schemes = ("https",) if app_env != APP_ENV_LOCAL else ("https", "http")
    parts = _parse_url("INSTANCE_BASE_URL", value, schemes)
    if parts.path not in ("", "/") or parts.query:
        # The application runs on its own host name, never under a path (D-025).
        raise ImproperlyConfigured("INSTANCE_BASE_URL must be an origin without a path.")
    if allowed_hosts and parts.hostname.lower() not in (host.lower() for host in allowed_hosts):
        raise ImproperlyConfigured("The host of INSTANCE_BASE_URL must be in DJANGO_ALLOWED_HOSTS.")
    return f"{parts.scheme}://{parts.netloc}"


def parse_instance_link(env: Mapping[str, str], name: str, required: bool) -> str:
    """Return an absolute https:// URL of the instance configuration, or an empty string."""
    value = _get(env, name)
    if not value:
        if required:
            raise ImproperlyConfigured(f"{name} must be set outside APP_ENV=local.")
        return ""
    _parse_url(name, value, ("https",))
    return value


# The sender address is stricter than a recipient: ASCII letters, digits and a few safe
# characters in the local part, and a host name with at least two labels.
_SENDER_ADDRESS = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)


def parse_email_sender_address(env: Mapping[str, str], base_url: str) -> str:
    """Return the sender address of outgoing email.

    The administrator configures the sender (later in the Microsoft Graph configuration, D-006);
    until then ``EMAIL_SENDER_ADDRESS`` or, as a default, ``noreply@<host of the base URL>``.
    """
    value = _get(env, "EMAIL_SENDER_ADDRESS")
    if value:
        if not _SENDER_ADDRESS.fullmatch(value):
            raise ImproperlyConfigured(
                "EMAIL_SENDER_ADDRESS must be one complete email address."
            )
        return value.lower()
    host = urlsplit(base_url).hostname or "localhost"
    try:
        ip_address(host)
    except ValueError:
        if "." in host:
            return f"noreply@{host.lower()}"
    return "noreply@localhost"


def parse_database(env: Mapping[str, str]) -> dict[str, str]:
    values = {name: _get(env, name) for name in ("DB_NAME", "DB_USER", "DB_PASSWORD")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"Missing database settings: {', '.join(missing)}.")
    port = _get(env, "DB_PORT") or "3306"
    if not _is_ascii_number(port) or not PORT_MIN <= int(port) <= PORT_MAX:
        raise ImproperlyConfigured(f"DB_PORT must be a number from {PORT_MIN} to {PORT_MAX}.")
    values["DB_HOST"] = _get(env, "DB_HOST") or "127.0.0.1"
    values["DB_PORT"] = port
    return values


def load_environment(env: Mapping[str, str]) -> EnvironmentConfig:
    """Validate the environment and return the configuration; raise ImproperlyConfigured."""
    app_env = parse_app_env(env)
    debug = parse_debug(env, app_env)
    secret_key, secret_key_fallbacks = parse_secret_keys(env, app_env)
    email_mode, email_allowed_recipients = parse_email(env, app_env)
    database = parse_database(env)
    allowed_hosts = parse_allowed_hosts(env, app_env)
    base_url = parse_instance_base_url(env, app_env, allowed_hosts)
    return EnvironmentConfig(
        app_env=app_env,
        debug=debug,
        secret_key=secret_key,
        secret_key_fallbacks=secret_key_fallbacks,
        allowed_hosts=allowed_hosts,
        csrf_trusted_origins=parse_csrf_trusted_origins(env, app_env),
        secure_proxy_ssl_header=parse_secure_proxy_ssl_header(env),
        axes_ipware_proxy_count=parse_axes_ipware_proxy_count(env),
        hsts_seconds=parse_hsts_seconds(env),
        hsts_include_subdomains=parse_hsts_include_subdomains(env),
        email_mode=email_mode,
        email_allowed_recipients=email_allowed_recipients,
        instance_name=parse_instance_name(env),
        instance_base_url=base_url,
        # The privacy notice must be linked at registration (D-022), so it is required
        # wherever real persons register.
        instance_privacy_url=parse_instance_link(
            env, "INSTANCE_PRIVACY_URL", required=app_env != APP_ENV_LOCAL
        ),
        instance_website_url=parse_instance_link(env, "INSTANCE_WEBSITE_URL", required=False),
        email_sender_address=parse_email_sender_address(env, base_url),
        db_name=database["DB_NAME"],
        db_user=database["DB_USER"],
        db_password=database["DB_PASSWORD"],
        db_host=database["DB_HOST"],
        db_port=database["DB_PORT"],
    )


def security_settings(config: EnvironmentConfig) -> dict[str, object]:
    """Return the Django security settings that depend on APP_ENV.

    Outside APP_ENV=local, HTTPS-only settings apply. Locally, the development server
    serves plain HTTP, so cookies cannot be Secure and HTTPS is not enforced.
    """
    secure = not config.is_local
    settings: dict[str, object] = {
        "SESSION_COOKIE_SECURE": secure,
        "CSRF_COOKIE_SECURE": secure,
        "SECURE_SSL_REDIRECT": secure,
        "SECURE_HSTS_SECONDS": config.hsts_seconds if secure else 0,
        # includeSubDomains would also cover every host below the application's host name; it
        # is a decision of the operator (DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS). Preload is
        # never set: removal from browsers' preload lists is slow and not under the operator's
        # control (docs/DECISIONS.md, D-028).
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": secure and config.hsts_include_subdomains,
        "SECURE_HSTS_PRELOAD": False,
        "SECURE_PROXY_SSL_HEADER": config.secure_proxy_ssl_header,
    }
    if secure:
        # Browsers accept "__Host-" cookies only if they are Secure, have Path=/ and no
        # Domain attribute (draft-ietf-httpbis-rfc6265bis; D-025).
        settings["SESSION_COOKIE_NAME"] = "__Host-sessionid"
        settings["CSRF_COOKIE_NAME"] = "__Host-csrftoken"
    else:
        settings["SESSION_COOKIE_NAME"] = "sessionid"
        settings["CSRF_COOKIE_NAME"] = "csrftoken"
    return settings


def email_backend(config: EnvironmentConfig) -> str:
    """Return the email backend for EMAIL_MODE.

    The Microsoft Graph backend (modes allowlist and graph) is not implemented yet. Until it
    is, these modes use a placeholder that refuses to send (``leg_platform.mail``); they never
    fall back to Django's default SMTP backend or to the console.
    """
    if config.email_mode == EMAIL_MODE_CONSOLE:
        return "django.core.mail.backends.console.EmailBackend"
    return "leg_platform.mail.GraphNotImplementedEmailBackend"


def silenced_system_checks(config: EnvironmentConfig) -> list[str]:
    """Return the deployment checks that are silenced deliberately (D-028).

    security.W021 (no HSTS preload) is always silenced, because preload is never set.
    security.W005 (no includeSubDomains) is silenced only while the operator has not enabled
    includeSubDomains, so that "check --deploy --fail-level WARNING" reports every other
    warning.
    """
    checks = ["security.W021"]
    if not config.hsts_include_subdomains:
        checks.insert(0, "security.W005")
    return checks
