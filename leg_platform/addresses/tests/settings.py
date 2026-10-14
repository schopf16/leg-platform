"""Isolated synthetic-data tests; never load local/production configuration."""

SECRET_KEY = "address-directory-synthetic-test-key-not-for-deployment"
DEBUG = False
USE_TZ = True
TIME_ZONE = "Europe/Zurich"
LANGUAGE_CODE = "en"
ALLOWED_HOSTS = ["testserver"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "leg_platform.addresses.tests.urls"
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.admin", "leg_platform.addresses",
]
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "address_import": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    "addresses": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
        "TEST": {"MIRROR": "address_import"},
    },
}
DATABASE_ROUTERS = ["leg_platform.addresses.router.AddressRouter"]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STATIC_URL = "/static/"
