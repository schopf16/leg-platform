"""Django settings of LEG-Platform.

Configuration comes only from environment variables (D-027), validated in
``leg_platform.environment``; see ``.env.example``. No instance-specific value is set here (D-013).
"""

import os
from datetime import timedelta
from pathlib import Path

import django
from django.utils.csp import CSP

from leg_platform.environment import (
    email_backend,
    load_environment,
    security_settings,
    silenced_system_checks,
)
from leg_platform.branding import instance_static_directory, load_branding
from leg_platform.addresses.storage import DIRECTORY_ALIAS, directory_database

BASE_DIR = Path(__file__).resolve().parent.parent

ENVIRONMENT = load_environment(os.environ)

# Environment

APP_ENV = ENVIRONMENT.app_env
DEBUG = ENVIRONMENT.debug
SECRET_KEY = ENVIRONMENT.secret_key
SECRET_KEY_FALLBACKS = list(ENVIRONMENT.secret_key_fallbacks)
ALLOWED_HOSTS = list(ENVIRONMENT.allowed_hosts)
CSRF_TRUSTED_ORIGINS = list(ENVIRONMENT.csrf_trusted_origins)

# Instance configuration (D-013). INSTANCE_BASE_URL is the canonical origin for links in emails and for passkeys
# (D-116). The sender and the contact address are in the E-Mail chapter of the administration (D-116).

INSTANCE_BASE_URL = ENVIRONMENT.instance_base_url
INSTANCE_NAME = ENVIRONMENT.instance_name
INSTANCE_PRIVACY_URL = ENVIRONMENT.instance_privacy_url
INSTANCE_WEBSITE_URL = ENVIRONMENT.instance_website_url
INSTANCE_BRANDING = load_branding(os.environ)

# Application definition

INSTALLED_APPS = [
    # First, so that its createsuperuser command replaces Django's (D-005, D-034).
    "leg_platform.accounts",
    "leg_platform.addresses.apps.AddressesConfig",
    "leg_platform.community",
    "leg_platform.customer",
    "leg_platform.management",
    # Replaces django.contrib.admin; checks the second factor (D-040).
    "leg_platform.admin_site.PortalAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_countries",
    "phonenumber_field",
    "axes",
    # Only its middleware and device functions; the devices are models of this project (D-040).
    "django_otp",
]

AUTH_USER_MODEL = "accounts.User"  # D-021
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "leg_platform.accounts.backends.EmailModelBackend",
]

# Login abuse protection (D-017): independent normalised-email locking in Axes and a separate IP
# counter, so that resetting one account preserves IP locks.
from leg_platform.accounts.login_protection import LOGIN_COOLOFF, LOGIN_FAILURE_LIMIT

AXES_COOLOFF_TIME = LOGIN_COOLOFF
AXES_HANDLER = "leg_platform.accounts.axes_handler.LoginProtectionHandler"
AXES_ENABLE_ADMIN = False
AXES_DISABLE_ACCESS_LOG = True
AXES_FAILURE_LIMIT = LOGIN_FAILURE_LIMIT
AXES_CLIENT_IP_CALLABLE = "leg_platform.accounts.login_protection.axes_client_ip"
AXES_IPWARE_META_PRECEDENCE_ORDER = ("HTTP_X_FORWARDED_FOR", "REMOTE_ADDR")
AXES_IPWARE_PROXY_COUNT = ENVIRONMENT.axes_ipware_proxy_count
AXES_HTTP_RESPONSE_CODE = 429
AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_RESET_ON_SUCCESS = False
AXES_SENSITIVE_PARAMETERS = [
    "username",
    "ip_address",
    "otp_token",
    "otp_device",
    "current_password",
    "new_password1",
    "new_password2",
    # The CSRF token of a request holds its secret in masked form.
    "csrfmiddlewaretoken",
]
AXES_USERNAME_CALLABLE = "leg_platform.accounts.login_protection.axes_username"
AXES_USERNAME_FORM_FIELD = "username"
AXES_USE_ATTEMPT_EXPIRATION = True
AXES_VERBOSE = False
# The lockout page is the same for every cause, never shows the submitted value and is not
# stored by a browser or proxy (D-067).
AXES_LOCKOUT_CALLABLE = "leg_platform.accounts.axes_handler.lockout_response"

# Login and logout (D-025: the login page is the entry point of the application).
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Phone numbers (D-034): numbers without an international prefix are Swiss; stored in E.164.
PHONENUMBER_DEFAULT_REGION = "CH"
PHONENUMBER_DB_FORMAT = "E164"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # No response under /management/ is cached, also the 403 and 404 pages.
    "leg_platform.middleware.NoStoreForManagementMiddleware",
    # Before anything reads a request body (sessions, CSRF): bounded bodies for open endpoints.
    "leg_platform.middleware.RequestBodyLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # The language comes from the language cookie only, never from Accept-Language (D-010, D-056).
    # After the session middleware, before the common middleware (Django's documented place).
    "leg_platform.language.ChosenLanguageMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # After the messages middleware: ends sessions that are idle for too long or too old (D-041).
    "leg_platform.accounts.session_timeout.SessionTimeoutMiddleware",
    # After OTPMiddleware and MessageMiddleware: no privileges without the second factor.
    "leg_platform.accounts.second_factor.SecondFactorMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "leg_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "leg_platform" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                # LANGUAGE_CODE and LANGUAGE_BIDI for the administration's <html lang> and dir.
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "leg_platform.context_processors.instance",
            ],
        },
    },
]

WSGI_APPLICATION = "leg_platform.wsgi.application"

# Database: MariaDB through Django's MySQL backend (D-002, D-030). "read committed" is Django's
# default isolation level for this backend (repeatable read can lose data), set explicitly.
# sql_mode is set for every connection with init_command so that a changed server default cannot
# weaken strict mode; Django check mysql.W002 and a test verify it. The test database is
# test_<DB_NAME>; utf8mb4_uca1400_ai_ci exists from MariaDB 10.10.1 on.

DATABASE_SQL_MODE = (
    "STRICT_TRANS_TABLES",
    "ERROR_FOR_DIVISION_BY_ZERO",
    "NO_AUTO_CREATE_USER",
    "NO_ENGINE_SUBSTITUTION",
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": ENVIRONMENT.db_name,
        "USER": ENVIRONMENT.db_user,
        "PASSWORD": ENVIRONMENT.db_password,
        "HOST": ENVIRONMENT.db_host,
        "PORT": ENVIRONMENT.db_port,
        "OPTIONS": {
            "charset": "utf8mb4",
            "isolation_level": "read committed",
            "init_command": "SET sql_mode='" + ",".join(DATABASE_SQL_MODE) + "'",
        },
        "TEST": {
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_uca1400_ai_ci",
        },
    }
}

# The address directory (D-075) is an own SQLite file next to the MariaDB database (the web
# process opens it read-only, see leg_platform.addresses.storage); the router keeps the directory
# tables in the file and the import queue in "default".
DATABASES[DIRECTORY_ALIAS] = directory_database(ENVIRONMENT.address_directory_file)
DATABASE_ROUTERS = ["leg_platform.addresses.router.AddressRouter"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Password validation: minimum length 15, no composition rules (docs/SECURITY.md, section 3).
# Hashing uses Django's default hasher and work factor (D-023), so PASSWORD_HASHERS is not set.

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 15},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Cookies are host-only: SESSION_COOKIE_DOMAIN and CSRF_COOKIE_DOMAIN stay unset (D-025, D-028).

SESSION_COOKIE_HTTPONLY = True

# Idle session timeouts (D-041, D-088), enforced on the server by leg_platform.accounts.session_timeout;
# staff users and system administrators have the shorter value. Activity extends the idle timeout;
# there is no absolute limit for an ordinary session (owner decision, 2026-10-17). A session ends at
# the idle timeout or at the logout.
SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_PRIVILEGED_IDLE_TIMEOUT = timedelta(minutes=15)
# The stored session slides with every save (at most once a minute while active); the value only
# bounds how long the cookie and the row of an abandoned session live. It must be longer than the
# longest idle timeout; the idle timeouts decide.
SESSION_COOKIE_AGE = int(timedelta(hours=8).total_seconds())
# "Stay signed in" (D-056, D-088): a persistent, non-sliding session for every account, with its own
# absolute limit; the second factor is asked once, at the login.
STAY_SIGNED_IN_DURATION = timedelta(days=14)
# No JavaScript reads the CSRF cookie (docs/SECURITY.md, section 3); scripts use the form field.
CSRF_COOKIE_HTTPONLY = True

_security = security_settings(ENVIRONMENT)
SESSION_COOKIE_NAME = _security["SESSION_COOKIE_NAME"]
SESSION_COOKIE_SECURE = _security["SESSION_COOKIE_SECURE"]
CSRF_COOKIE_NAME = _security["CSRF_COOKIE_NAME"]
CSRF_COOKIE_SECURE = _security["CSRF_COOKIE_SECURE"]
SECURE_SSL_REDIRECT = _security["SECURE_SSL_REDIRECT"]
SECURE_HSTS_SECONDS = _security["SECURE_HSTS_SECONDS"]
SECURE_HSTS_INCLUDE_SUBDOMAINS = _security["SECURE_HSTS_INCLUDE_SUBDOMAINS"]
SECURE_HSTS_PRELOAD = _security["SECURE_HSTS_PRELOAD"]
SECURE_PROXY_SSL_HEADER = _security["SECURE_PROXY_SSL_HEADER"]

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Content Security Policy (docs/SECURITY.md, section 6; D-028), sent by Django's middleware on
# every response: everything only from the own origin (static assets are self-hosted), no inline
# scripts or styles. base-uri, form-action and frame-ancestors do not fall back to default-src,
# so they are set explicitly. Django's debug pages disable the policy for themselves.

SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF],
    "font-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

# HSTS preload is never set and includeSubDomains is left to the operator (D-028): those
# deployment warnings are silenced. Axes' W006 assumes its own IP counter; this project keeps IP
# failures in LoginIpAttempt so account resets cannot weaken IP lockouts.
SILENCED_SYSTEM_CHECKS = [*silenced_system_checks(ENVIRONMENT), "axes.W006"]

# Internationalization (D-010, D-056). The default language of an instance is Swiss German; the
# person chooses another one with the language menu (the language cookie, leg_platform.language).
# The names are written in their own language and are not translated. English is the language of the
# source texts and needs no catalogue.

LANGUAGE_CODE = "de-ch"
LANGUAGES = [
    ("de-ch", "Deutsch"),
    ("fr", "Français"),
    ("it", "Italiano"),
    ("en", "English"),
]
USE_I18N = True
LOCALE_PATHS = [BASE_DIR / "locale"]
# The language cookie holds the code of the chosen language and nothing else. Name and Secure flag
# follow the other cookies (``__Host-`` prefix outside local, D-025); no Domain, Path "/". JavaScript
# does not read it (the server renders the language); it is sent with a link from another site
# only as a top-level GET (SameSite=Lax); it lives one year, so the choice outlasts the session.
LANGUAGE_COOKIE_NAME = _security["LANGUAGE_COOKIE_NAME"]
LANGUAGE_COOKIE_SECURE = _security["LANGUAGE_COOKIE_SECURE"]
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_AGE = int(timedelta(days=365).total_seconds())
TIME_ZONE = "Europe/Zurich"
USE_TZ = True

# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "leg_platform" / "static"]
_instance_static_directory = instance_static_directory(os.environ)
if _instance_static_directory is not None:
    STATICFILES_DIRS.append(_instance_static_directory)

# Email: the backend is always set explicitly, so that Django's default SMTP backend is never
# used; Django 6.1 replaces EMAIL_BACKEND with MAILERS. The test runner swaps in the locmem
# backend, so tests never send email.

EMAIL_MODE = ENVIRONMENT.email_mode
# Every message names its sender explicitly (the E-Mail chapter, D-116); Django's default is never used.
DEFAULT_EXCEPTION_REPORTER_FILTER = "leg_platform.debug.ExceptionReporterFilter"

if django.VERSION >= (6, 1):
    MAILERS = {"default": {"BACKEND": email_backend(ENVIRONMENT)}}
else:
    EMAIL_BACKEND = email_backend(ENVIRONMENT)

# Security log retention in days (D-018).
SECURITY_LOG_RETENTION_DAYS = ENVIRONMENT.security_log_retention_days
SECURITY_LOG_LONG_RETENTION_DAYS = ENVIRONMENT.security_log_long_retention_days

# Logging: warnings and errors go to standard error (journald in production); no mail handler
# (ADMINS is not set). With a usable ``LOG_DIRECTORY`` the application log and the security log are
# also written to bounded rotating files that the system administrator can read in the administration
# (D-092). Log messages never contain passwords, tokens, secrets or complete security links; the
# filter also removes email addresses, IBANs and the values of database errors.
from leg_platform.logfiles import logging_config, usable_log_directory

LOG_DIRECTORY = usable_log_directory(ENVIRONMENT.log_directory)
LOGGING = logging_config(LOG_DIRECTORY)
# The web application starts the import of the address directory when it is due (D-090).
ADDRESS_IMPORT_AUTOMATIC = ENVIRONMENT.address_import_automatic
