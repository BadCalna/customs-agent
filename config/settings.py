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
ENGINE_MODE = mode("CUSTOMS_ENGINE_MODE", "mock", ("mock", "openai"))
EXECUTOR_MODE = mode("CUSTOMS_EXECUTOR_MODE", "inline", ("inline", "rq"))
NOTIFICATION_MODE = mode("CUSTOMS_NOTIFICATION_MODE", "mock", ("mock", "disabled"))
CUSTOMS_AI_API_BASE = os.environ.get("CUSTOMS_AI_API_BASE", "").strip().rstrip("/")
CUSTOMS_AI_API_KEY = os.environ.get("CUSTOMS_AI_API_KEY", "").strip()
CUSTOMS_AI_MODEL = os.environ.get("CUSTOMS_AI_MODEL", "").strip()
CUSTOMS_AI_TIMEOUT_SECONDS = 60
if ENGINE_MODE == "openai":
    from urllib.parse import urlsplit

    endpoint = urlsplit(CUSTOMS_AI_API_BASE)
    if (not endpoint.hostname or endpoint.scheme not in ("https", "http")
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or (endpoint.scheme == "http" and os.environ.get("CUSTOMS_AI_ALLOW_HTTP") != "1")):
        raise ImproperlyConfigured("CUSTOMS_AI_API_BASE 必须是 HTTPS 服务地址；内网 HTTP 须显式设置 CUSTOMS_AI_ALLOW_HTTP=1")
    if not CUSTOMS_AI_API_KEY or not CUSTOMS_AI_MODEL:
        raise ImproperlyConfigured("openai 引擎需要 CUSTOMS_AI_API_KEY 和 CUSTOMS_AI_MODEL")
    try:
        CUSTOMS_AI_TIMEOUT_SECONDS = int(os.environ.get("CUSTOMS_AI_TIMEOUT_SECONDS", "60"))
    except ValueError as exc:
        raise ImproperlyConfigured("CUSTOMS_AI_TIMEOUT_SECONDS 必须是 1 到 240 的整数") from exc
    if not 1 <= CUSTOMS_AI_TIMEOUT_SECONDS <= 240:
        raise ImproperlyConfigured("CUSTOMS_AI_TIMEOUT_SECONDS 必须是 1 到 240 的整数")

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
    "django_rq",
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

if EXECUTOR_MODE == "rq" and DB_BACKEND != "postgres":
    raise ImproperlyConfigured("RQ 执行器需要 PostgreSQL；SQLite 只用于 inline 顺序验证。")

CUSTOMS_QUEUE_NAME = os.environ.get("CUSTOMS_QUEUE_NAME", "customs")
CUSTOMS_QUEUE_TIMEOUT = byte_setting("CUSTOMS_QUEUE_TIMEOUT", 300)
CUSTOMS_QUEUE_LEASE_SECONDS = byte_setting("CUSTOMS_QUEUE_LEASE_SECONDS", 360)
CUSTOMS_QUEUE_REDELIVERY_SECONDS = byte_setting("CUSTOMS_QUEUE_REDELIVERY_SECONDS", 30)
CUSTOMS_ROW_MAX_ATTEMPTS = byte_setting("CUSTOMS_ROW_MAX_ATTEMPTS", 3)
CUSTOMS_RETRY_BACKOFF_SECONDS = byte_setting("CUSTOMS_RETRY_BACKOFF_SECONDS", 2)
if CUSTOMS_QUEUE_LEASE_SECONDS <= CUSTOMS_QUEUE_TIMEOUT:
    raise ImproperlyConfigured("队列租约必须长于 worker 超时。")
RQ_QUEUES = {CUSTOMS_QUEUE_NAME: {
    "URL": os.environ.get("CUSTOMS_REDIS_URL", "redis://127.0.0.1:6379/0"),
    "DEFAULT_TIMEOUT": CUSTOMS_QUEUE_TIMEOUT,
}}
RQ = {"WORKER_CLASS": "rq.worker.SimpleWorker" if os.name == "nt" else "rq.worker.Worker"}
SESSION_COOKIE_NAME = os.environ.get("CUSTOMS_SESSION_COOKIE_NAME", "sessionid")
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

# Other ops modules supply trusted install manifests; none are installed by default.
INSTALLED_APPS += ['apps.ops_admin']
MIDDLEWARE.insert(MIDDLEWARE.index('django.contrib.auth.middleware.AuthenticationMiddleware') + 1,
                  'apps.ops_admin.middleware.OpsMiddleware')
OPS_ADMIN_MODULE_INSTALLERS = ()
OPS_ADMIN_CRITICAL_EVENTS = frozenset()
OPS_ADMIN_SERVICE_VERSION = os.environ.get('CUSTOMS_SERVICE_VERSION', '0.1.0')[:64]
OPS_ADMIN_SECRET_TTL_SECONDS = 900
OPS_ADMIN_RESERVED_GROUPS = ('ops_system_admin',)
OPS_ADMIN_RESERVED_USERNAMES = tuple(s.strip() for s in os.environ.get('CUSTOMS_OPS_RESERVED_USERS', '').split(',') if s.strip())
OPS_ADMIN_TRUSTED_EMAIL_ENABLED = os.environ.get('CUSTOMS_OPS_TRUSTED_EMAIL') == '1'
OPS_ADMIN_SECRET_PUBLIC_URL = os.environ.get('CUSTOMS_OPS_SECRET_PUBLIC_URL', '')
if OPS_ADMIN_TRUSTED_EMAIL_ENABLED:
    from urllib.parse import urlsplit

    ops_url = urlsplit(OPS_ADMIN_SECRET_PUBLIC_URL)
    if (ops_url.scheme != 'https' or not ops_url.hostname or ops_url.username or ops_url.password or ops_url.query
            or ops_url.fragment or ops_url.path != '/ops/admin/secret/'
            or any(c.isspace() or ord(c) < 32 for c in OPS_ADMIN_SECRET_PUBLIC_URL)):
        raise ImproperlyConfigured('CUSTOMS_OPS_SECRET_PUBLIC_URL must be an HTTPS /ops/admin/secret/ URL')
    EMAIL_HOST = os.environ.get('CUSTOMS_OPS_SMTP_HOST', '')
    EMAIL_PORT = byte_setting('CUSTOMS_OPS_SMTP_PORT', 587)
    EMAIL_HOST_USER = os.environ.get('CUSTOMS_OPS_SMTP_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('CUSTOMS_OPS_SMTP_PASSWORD', '')
    DEFAULT_FROM_EMAIL = os.environ.get('CUSTOMS_OPS_SMTP_FROM', '')
    EMAIL_USE_TLS = True
    EMAIL_TIMEOUT = 5
    if not EMAIL_HOST or not DEFAULT_FROM_EMAIL:
        raise ImproperlyConfigured('Trusted ops mail requires CUSTOMS_OPS_SMTP_HOST and CUSTOMS_OPS_SMTP_FROM')

LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'handlers': {'ops_events': {'class': 'apps.ops_admin.events.OpsEventHandler'}},
    'loggers': {'customs.ops.events': {'handlers': ['ops_events'], 'level': 'INFO', 'propagate': False}},
}
