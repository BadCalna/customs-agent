"""Development settings for one local checkout."""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DIR = BASE_DIR / ".local"
load_dotenv(BASE_DIR / ".env", override=False, interpolate=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if SECRET_KEY is None:
    try:
        SECRET_KEY = (LOCAL_DIR / "dev-secret").read_text().strip()
    except FileNotFoundError as exc:
        raise ImproperlyConfigured("Run make setup or set DJANGO_SECRET_KEY.") from exc
if not SECRET_KEY.strip():
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must not be empty.")

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]


def mode(name, default, choices):
    value = os.environ.get(name, default)
    if value not in choices:
        raise ImproperlyConfigured(f"{name}={value!r} is unsupported. Choose from {choices}.")
    return value


PAGE_DATA_MODE = mode("CUSTOMS_PAGE_MODE", "live", ("live", "fixture"))
ENGINE_MODE = mode("CUSTOMS_ENGINE_MODE", "mock", ("mock",))
EXECUTOR_MODE = mode("CUSTOMS_EXECUTOR_MODE", "inline", ("inline",))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.accounts",
    "apps.reviews",
    "apps.runs",
    "apps.notifications",
    "ninja",
]
AUTH_USER_MODEL = "accounts.User"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / os.environ.get("CUSTOMS_SQLITE_PATH", ".local/db.sqlite3"),
}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{name}"}
    for name in ("UserAttributeSimilarityValidator", "MinimumLengthValidator",
                 "CommonPasswordValidator", "NumericPasswordValidator")
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
TEST_RUNNER = "config.test_runner.TaskRunner"
# CSRF 失败返回 JSON 错误体，而不是 Django 默认 HTML 页面（D3 第 1 节）。
CSRF_FAILURE_VIEW = "apps.accounts.csrf.csrf_failure"
