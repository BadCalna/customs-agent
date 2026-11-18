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
# 历史案例只作为参考证据；默认保持 JSON 兼容，实际部署显式选择 database。
HISTORY_BACKEND = mode("CUSTOMS_HISTORY_BACKEND", "json", ("json", "database"))
HISTORY_DATASET_KEY = os.environ.get("CUSTOMS_HISTORY_DATASET_KEY", "historical-success").strip()
HISTORY_REFERENCE_ENABLED = os.environ.get("CUSTOMS_HISTORY_REFERENCE_ENABLED", "0").strip() == "1"
if HISTORY_BACKEND == "database" and not HISTORY_DATASET_KEY:
    raise ImproperlyConfigured("CUSTOMS_HISTORY_DATASET_KEY must not be empty for database history.")


def byte_setting(name: str, default: int) -> int:
    """读取字节数配置；未设置用默认值，设置了但不是正整数时明确失败。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{name}={raw!r} 必须是正整数（字节数）。") from exc
    if value <= 0:
        raise ImproperlyConfigured(f"{name}={raw!r} 必须是正整数（字节数），当前不是正数。")
    return value


def path_setting(name: str, default: Path) -> Path:
    """读取路径配置；相对路径按仓库根目录解析，空值使用默认值。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


#: P05 受控存储根与上限；原件、图片、导出副本都按版本落在该目录下（D6）。
CUSTOMS_FILE_ROOT = path_setting("CUSTOMS_FILE_ROOT", LOCAL_DIR / "files")
CUSTOMS_FILE_MAX_BYTES = byte_setting("CUSTOMS_FILE_MAX_BYTES", 100 * 1024 * 1024)
CUSTOMS_IMAGE_MAX_BYTES = byte_setting("CUSTOMS_IMAGE_MAX_BYTES", 20 * 1024 * 1024)
CUSTOMS_FILE_READ_MAX_BYTES = byte_setting("CUSTOMS_FILE_READ_MAX_BYTES", 200 * 1024 * 1024)
# P06 认领池：空值表示试用空间全体业务员；策略接收 (user, queryset) 并返回收窄集合。
CUSTOMS_CLAIM_SCOPE_POLICY = os.environ.get("CUSTOMS_CLAIM_SCOPE_POLICY", "")
# 没有任务专家时，提交同事务授权此已有活跃专家；不创建账号、不授权所有专家。
CUSTOMS_DEFAULT_EXPERT_USERNAME = os.environ.get("CUSTOMS_DEFAULT_EXPERT_USERNAME", "expert").strip()

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
    "apps.history_reference",
    "ninja",
    "apps.api",
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
DB_BACKEND = mode("CUSTOMS_DB_BACKEND", "sqlite", ("sqlite", "postgres"))


def _postgres_database():
    """PostgreSQL 连接配置：缺变量时明确失败，不回退到 SQLite 或默认账号（D6 第 3 节）。"""
    required = {"CUSTOMS_PG_NAME": "数据库名", "CUSTOMS_PG_USER": "登录角色"}
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ImproperlyConfigured(
            "CUSTOMS_DB_BACKEND=postgres 需要设置 "
            + "、".join(f"{name}（{required[name]}）" for name in missing)
            + "；本机初始化见 scripts/database/README.md。"
        )
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["CUSTOMS_PG_NAME"],
        "USER": os.environ["CUSTOMS_PG_USER"],
        "PASSWORD": os.environ.get("CUSTOMS_PG_PASSWORD", ""),
        "HOST": os.environ.get("CUSTOMS_PG_HOST", "127.0.0.1"),
        "PORT": os.environ.get("CUSTOMS_PG_PORT", "5432"),
        # 连不上时尽快失败，不要让 make check-postgres 卡在默认超时。
        "OPTIONS": {"connect_timeout": 5},
        "CONN_MAX_AGE": 0,
    }


if DB_BACKEND == "postgres":
    DATABASES = {"default": _postgres_database()}
else:
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
