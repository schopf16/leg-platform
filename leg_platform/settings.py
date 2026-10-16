"""Django settings of LEG-Platform.

The configuration is read only from environment variables (docs/DECISIONS.md, D-027) and
validated in ``leg_platform.environment``; see ``.env.example`` for all variables. No
instance-specific value is set in this file (D-013).

References:
https://docs.djangoproject.com/en/6.0/ref/settings/
https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/
"""

import os
from datetime import timedelta
from pathlib import Path

import django
from django.utils.csp import CSP
from django.utils.translation import gettext_lazy as _

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

# Instance configuration (docs/PROJECT.md, "Instance configuration"; D-013). Never hard-coded.
# INSTANCE_BASE_URL is the canonical origin used for links in emails.

INSTANCE_NAME = ENVIRONMENT.instance_name
INSTANCE_BASE_URL = ENVIRONMENT.instance_base_url
INSTANCE_PRIVACY_URL = ENVIRONMENT.instance_privacy_url
INSTANCE_WEBSITE_URL = ENVIRONMENT.instance_website_url
INSTANCE_BRANDING = load_branding(os.environ)

# Application definition

INSTALLED_APPS = [
    # First, so that its createsuperuser command replaces Django's (D-005, D-034).
    "leg_platform.accounts",
    "leg_platform.addresses.apps.AddressesConfig",
    # The administration with the second factor check (D-040); replaces django.contrib.admin.
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

# Custom user model from the first migration (docs/DECISIONS.md, D-021).
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "leg_platform.accounts.backends.EmailModelBackend",
]

# Login abuse protection (docs/DECISIONS.md, D-017): independent normalised-email
# locking in Axes and a separate IP counter ensure resetting one account preserves IP locks.
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
# The lockout page is the same for every cause, never shows the submitted value and is not stored
# by a browser or proxy (D-067): Axes calls this function instead of its own response.
AXES_LOCKOUT_CALLABLE = "leg_platform.accounts.axes_handler.lockout_response"

# Login and logout (D-025: the login page is the entry point of the application).
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Phone numbers (D-034): numbers without an international prefix are read as Swiss numbers;
# stored in E.164, the package's default, set explicitly so that it is visible.
# https://django-phonenumber-field.readthedocs.io/en/latest/reference.html#settings
PHONENUMBER_DEFAULT_REGION = "CH"
PHONENUMBER_DB_FORMAT = "E164"

# Two-factor authentication with an authenticator app (docs/DECISIONS.md, D-004, D-040): the
# device models and their settings (30 seconds, six digits, SHA-1) are in accounts/models.py.

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # Before anything reads a request body (sessions, CSRF): bounded bodies for open endpoints.
    "leg_platform.middleware.RequestBodyLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
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
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "leg_platform.context_processors.instance",
            ],
        },
    },
]

WSGI_APPLICATION = "leg_platform.wsgi.application"

# Database: MariaDB through Django's MySQL backend (docs/DECISIONS.md, D-002, D-030)
# https://docs.djangoproject.com/en/6.0/ref/databases/#mariadb-notes ("To use MariaDB, use the
# MySQL backend") and #mysql-notes.
# Django connects with the character set utf8mb4 by default. "read committed" is Django's
# default isolation level for this backend ("Data loss is possible with repeatable read";
# REPEATABLE-READ is MariaDB's default); it is set explicitly so that it is visible. Strict mode
# is part of MariaDB's default sql_mode; sql_mode is nevertheless set for every connection with
# init_command (Django documentation, "Setting sql_mode"), so that a changed server default
# cannot weaken it, to MariaDB's documented default (MariaDB documentation, "Server System
# Variables"). Django's system check mysql.W002 (run with "check --database default" in CI) and
# a test verify strict mode.
# The test database is test_<DB_NAME> (Django's default TEST NAME). utf8mb4_uca1400_ai_ci exists
# from MariaDB 10.10.1 on and is the default utf8mb4 collation from MariaDB 11.5 on; MySQL's
# utf8mb4_0900_ai_ci exists in MariaDB only as an alias from 11.4.5 on (not in 10.11).

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

# The address directory (D-075) is an own SQLite file, not part of any backup, next to the MariaDB
# database; the web process and the worker open it with the same settings (the web process
# read-only, leg_platform.addresses.storage). The router keeps the directory tables in the file
# and the import queue in "default".
DATABASES[DIRECTORY_ALIAS] = directory_database(ENVIRONMENT.address_directory_file)
DATABASE_ROUTERS = ["leg_platform.addresses.router.AddressRouter"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Password validation and hashing
# docs/SECURITY.md, section 3: minimum length 15, no composition rules, no maximum below 64
# (none of Django's validators sets a maximum length). Hashing uses Django's default hasher
# and work factor (D-023), therefore PASSWORD_HASHERS is not set.

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 15},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Sessions, cookies and HTTPS (docs/SECURITY.md, sections 3 and 6; D-025, D-028)
# SESSION_COOKIE_DOMAIN and CSRF_COOKIE_DOMAIN are deliberately not set (Django's default
# None), so that the cookies are host-only. SameSite stays at Django's default "Lax".

SESSION_COOKIE_HTTPONLY = True

# Idle and absolute session timeouts (docs/SECURITY.md, section 3; D-041). OWASP Session
# Management Cheat Sheet: idle timeouts of 15-30 minutes for low-risk applications (2-5 minutes
# for high-value ones) and 4-8 hours absolute for an application that is used for a working
# day. Staff users and system administrators have the shorter values. The cookie lives as long
# as the longest absolute timeout; the timeouts themselves are enforced on the server by
# leg_platform.accounts.session_timeout.
SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)
SESSION_PRIVILEGED_IDLE_TIMEOUT = timedelta(minutes=15)
SESSION_PRIVILEGED_ABSOLUTE_TIMEOUT = timedelta(hours=4)
SESSION_COOKIE_AGE = int(SESSION_ABSOLUTE_TIMEOUT.total_seconds())
# "Stay signed in" (D-056, owner decision of 2026-10-14; team lead decision K21): a person who
# ticks the box at login gets a persistent session that ends after this time. It does not slide
# (the idle and the absolute timeout are both this long) and it is ignored for staff users and
# system administrators, whose shorter timeouts above stay in force. The value is subject to
# the security review.
STAY_SIGNED_IN_DURATION = timedelta(days=14)
# No JavaScript reads the CSRF cookie. Django's documentation states that HttpOnly offers
# little practical protection for this cookie; it is set because docs/SECURITY.md (section 3)
# requires HttpOnly where possible. Scripts that need the token must read it from the form
# field rendered by {% csrf_token %}, not from the cookie.
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

# Content Security Policy (docs/SECURITY.md, section 6; D-028)
# Django's built-in ContentSecurityPolicyMiddleware (Django 6.0 and later) sends this policy
# on every response in all environments. Scripts, styles, images and fonts are loaded only
# from the application's own origin (static assets are self-hosted); no inline scripts or
# styles, no 'unsafe-inline', no nonces. object-src, base-uri, form-action and
# frame-ancestors are set explicitly, because base-uri, form-action and frame-ancestors do
# not fall back to default-src (W3C Content Security Policy Level 3). frame-ancestors 'none'
# complements X-Frame-Options DENY. Django's debug pages (DEBUG=True, local only) disable the
# policy for themselves with csp_override({}).
# https://docs.djangoproject.com/en/6.0/ref/csp/

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

# HSTS preload is never set and includeSubDomains is left to the operator (D-028); the
# corresponding deployment warnings are silenced (security.W005 only while includeSubDomains
# is off), so that "check --deploy --fail-level WARNING" reports every other warning.
# Axes' W006 assumes its own IP counter is enabled. This project deliberately keeps IP
# failures in LoginIpAttempt so account resets cannot weaken IP lockouts; integration tests
# exercise that independent counter and the trusted-proxy resolver.
SILENCED_SYSTEM_CHECKS = [*silenced_system_checks(ENVIRONMENT), "axes.W006"]

# Internationalization (D-010)

LANGUAGE_CODE = "de-ch"
LANGUAGES = [("de-ch", _("Swiss German"))]
USE_I18N = True
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Zurich"
USE_TZ = True

# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "leg_platform" / "static"]
_instance_static_directory = instance_static_directory(os.environ)
if _instance_static_directory is not None:
    STATICFILES_DIRS.append(_instance_static_directory)

# Email (docs/ARCHITECTURE.md, "Email")
# Django 6.1 replaces EMAIL_BACKEND with MAILERS and deprecates EMAIL_BACKEND; Django 6.0 only
# knows EMAIL_BACKEND (no MAILERS in its global settings or test utilities). The backend is
# always set explicitly, so that Django's default SMTP backend is never used. Django's test
# runner replaces the backend with the locmem backend in both versions (EMAIL_BACKEND in 6.0,
# every alias of MAILERS in 6.1; django/test/utils.py), so automated tests never send email.

EMAIL_MODE = ENVIRONMENT.email_mode
EMAIL_ALLOWED_RECIPIENTS = list(ENVIRONMENT.email_allowed_recipients)
# The sender of every email; the Microsoft Graph configuration of the administrator will take
# over this role (D-006).
DEFAULT_FROM_EMAIL = ENVIRONMENT.email_sender_address
# Masks EMAIL_ALLOWED_RECIPIENTS (personal addresses on development machines) on debug pages
# and in error reports, in addition to Django's default sensitive names (D-028).
DEFAULT_EXCEPTION_REPORTER_FILTER = "leg_platform.debug.ExceptionReporterFilter"

if django.VERSION >= (6, 1):
    MAILERS = {"default": {"BACKEND": email_backend(ENVIRONMENT)}}
else:
    EMAIL_BACKEND = email_backend(ENVIRONMENT)

# Security log retention in days (docs/SECURITY.md, section 10; D-018, D-041).
SECURITY_LOG_RETENTION_DAYS = ENVIRONMENT.security_log_retention_days
SECURITY_LOG_LONG_RETENTION_DAYS = ENVIRONMENT.security_log_long_retention_days

# Logging
# Warnings and errors go to standard error, where the hosting collects them. Django's default
# configuration sends errors by email to ADMINS; ADMINS is not set and no mail handler is
# configured. Log messages must never contain passwords, tokens, secrets or complete security
# links (docs/SECURITY.md, section 10).

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # Removes tokens of verification links from the request logs (docs/SECURITY.md,
        # section 10).
        "redact_tokens": {"()": "leg_platform.log_filters.TokenRedactionFilter"},
    },
    "formatters": {
        "default": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "filters": ["redact_tokens"],
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        # The development server's request log names every requested path.
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # Security events (docs/SECURITY.md, section 10). Until the security log of D-018 exists,
        # they are written only here, with the internal number of the account and nothing else.
        "leg_platform.security": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        # MSAL logs a token response that is not JSON at ERROR level, with the response body
        # (msal/oauth2cli/oauth2.py, self.logger.exception). The body can contain tenant details,
        # so only CRITICAL is written for this logger (D-048).
        "msal.oauth2cli.oauth2": {
            "handlers": ["console"],
            "level": "CRITICAL",
            "propagate": False,
        },
        "msal.oauth2cli.assertion": {
            "handlers": ["console"],
            "level": "CRITICAL",
            "propagate": False,
        },
    },
}
