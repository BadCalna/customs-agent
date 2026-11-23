"""Minimal, isolated Django settings for the Agent Debug acceptance suite.

This module intentionally does not import ``config.settings``: that settings module
requires a deployment secret and may select a live provider. The suite defaults to
SQLite, uses an isolated test database, and makes no external network requests.
"""
from pathlib import Path
from secrets import token_urlsafe

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = token_urlsafe(48)
DEBUG = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
ROOT_URLCONF = "optional_tests.ops_debug.urls"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.reviews",
    "apps.runs",
    "apps.notifications",
    "apps.history_reference",
    "ninja",
    "apps.api",
    "django_rq",
    "apps.agent_debug",
]
AUTH_USER_MODEL = "accounts.User"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
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
WSGI_APPLICATION = None
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / ".local" / "ops-debug-test.sqlite3",
    "TEST": {"NAME": ":memory:"},
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
STATIC_URL = "/static/"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Outbound policies and independent ephemeral keys are supplied by test fixtures.
OPS_DEBUG_ENABLED = True
OPS_DEBUG_LIVE_ENABLED = False
OPS_DEBUG_PUBLISHED_DIR = ""
OPS_DEBUG_EVIDENCE_MAP_ROOT = ""

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
CSRF_TRUSTED_ORIGINS = []
