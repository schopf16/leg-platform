"""Django settings of LEG-Platform.

The configuration is read only from environment variables (docs/DECISIONS.md, D-027) and
validated in ``leg_platform.environment``; see ``.env.example`` for all variables. No
instance-specific value is set in this file (D-013).

References:
https://docs.djangoproject.com/en/6.0/ref/settings/
https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/
"""

import os
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

BASE_DIR = Path(__file__).resolve().parent.parent

ENVIRONMENT = load_environment(os.environ)

# Environment

APP_ENV = ENVIRONMENT.app_env
DEBUG = ENVIRONMENT.debug
SECRET_KEY = ENVIRONMENT.secret_key
SECRET_KEY_FALLBACKS = list(ENVIRONMENT.secret_key_fallbacks)
ALLOWED_HOSTS = list(ENVIRONMENT.allowed_hosts)
CSRF_TRUSTED_ORIGINS = list(ENVIRONMENT.csrf_trusted_origins)

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "leg_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
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
SILENCED_SYSTEM_CHECKS = silenced_system_checks(ENVIRONMENT)

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

# Email (docs/ARCHITECTURE.md, "Email")
# Django 6.1 replaces EMAIL_BACKEND with MAILERS and deprecates EMAIL_BACKEND; Django 6.0 only
# knows EMAIL_BACKEND (no MAILERS in its global settings or test utilities). The backend is
# always set explicitly, so that Django's default SMTP backend is never used. Django's test
# runner replaces the backend with the locmem backend in both versions (EMAIL_BACKEND in 6.0,
# every alias of MAILERS in 6.1; django/test/utils.py), so automated tests never send email.

EMAIL_MODE = ENVIRONMENT.email_mode
EMAIL_ALLOWED_RECIPIENTS = list(ENVIRONMENT.email_allowed_recipients)
# Masks EMAIL_ALLOWED_RECIPIENTS (personal addresses on development machines) on debug pages
# and in error reports, in addition to Django's default sensitive names (D-028).
DEFAULT_EXCEPTION_REPORTER_FILTER = "leg_platform.debug.ExceptionReporterFilter"

if django.VERSION >= (6, 1):
    MAILERS = {"default": {"BACKEND": email_backend(ENVIRONMENT)}}
else:
    EMAIL_BACKEND = email_backend(ENVIRONMENT)

# Logging
# Warnings and errors go to standard error, where the hosting collects them. Django's default
# configuration sends errors by email to ADMINS; ADMINS is not set and no mail handler is
# configured. Log messages must never contain passwords, tokens, secrets or complete security
# links (docs/SECURITY.md, section 10).

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
