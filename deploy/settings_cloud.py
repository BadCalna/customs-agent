"""云端测试环境的 Django 设置（P22）。

用法：容器里设 ``DJANGO_SETTINGS_MODULE=deploy.settings_cloud``（compose 已内置）。
本模块只覆盖“部署相关”的开关，不改任何业务语义：数据库、受控文件存储、页面/引擎/执行器
模式、CSRF 失败视图仍然来自 ``config.settings``。

为什么另开一个模块而不是改 ``config/settings.py``：

- ``config/settings.py`` 是主控维护的公共配置，P06 正在开发上传/认领/页面相关代码，
  同一个文件两头改容易冲突；
- 部署侧的 ``DEBUG`` / ``ALLOWED_HOSTS`` / ``CSRF_TRUSTED_ORIGINS`` / ``STATIC_ROOT``
  只在云端需要，开发机读不到这些变量时行为必须完全不变（开发默认仍由 config.settings 决定）。

安全默认值：

- ``DEBUG`` 默认关闭，只能用 ``CUSTOMS_DEBUG=1`` 显式打开；
- ``ALLOWED_HOSTS`` 不给默认值，缺失就启动失败——宁可起不来，也不要悄悄接受任意 Host；
- ``CSRF_TRUSTED_ORIGINS`` 每一项必须带 ``http://`` 或 ``https://``，写错即启动失败；
- 只有 ``CUSTOMS_HTTPS=1`` 时才把会话/CSRF cookie 标成 Secure（没证书的纯 HTTP 测试不打开，
  否则浏览器不发送 cookie，登录会“成功但立刻失效”）。
"""
from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from config.settings import *  # noqa: F401,F403  复用开发设置，再覆盖部署项


def _flag(name: str, default: bool = False) -> bool:
    """读布尔开关；取值不认识时明确失败，不猜。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name}={raw!r} 只能是 1/0、true/false、yes/no、on/off。")


def _csv(name: str) -> list[str]:
    """逗号分隔列表；空项丢弃。"""
    raw = os.environ.get(name, "")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ImproperlyConfigured(f"{name} 不能包含控制字符。")
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = _flag("CUSTOMS_DEBUG", False)
CUSTOMS_PRODUCTION = _flag("CUSTOMS_PRODUCTION", False)
if CUSTOMS_PRODUCTION:
    if DEBUG or ENGINE_MODE != "openai" or EXECUTOR_MODE != "rq" or PAGE_DATA_MODE != "live":
        raise ImproperlyConfigured(
            "CUSTOMS_PRODUCTION=1 需要 CUSTOMS_DEBUG=0、CUSTOMS_ENGINE_MODE=openai、"
            "CUSTOMS_EXECUTOR_MODE=rq 和 CUSTOMS_PAGE_MODE=live"
        )
    if not _flag("CUSTOMS_HTTPS", False):
        raise ImproperlyConfigured("CUSTOMS_PRODUCTION=1 需要 CUSTOMS_HTTPS=1 和已配置的 TLS 入口")

ALLOWED_HOSTS = _csv("CUSTOMS_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "CUSTOMS_ALLOWED_HOSTS 必须显式设置（逗号分隔），填服务器公网 IP、内网 IP 或域名。"
        "留空时 Django 会拒绝所有 Host 头；云端不允许沿用开发默认的 localhost。"
    )

def _valid_host(host: str) -> bool:
    """只接受明确的 IP / DNS Host，不接受通配符、端口或模板域名。"""
    lowered = host.lower()
    if any(token in lowered for token in ("*", "change-me", "changeme", "your-domain", "<", ">", "{", "}")):
        return False
    if any(lowered == domain or lowered.endswith("." + domain)
           for domain in ("example.com", "example.org", "example.net", "example")):
        return False
    try:
        ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
        return ":" not in host or (host.startswith("[") and host.endswith("]"))
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.split(".")
        )


if not all(_valid_host(host) for host in ALLOWED_HOSTS):
    # 不回显配置值，避免运维误把凭据放进 Host 后泄露到日志。
    raise ImproperlyConfigured("CUSTOMS_ALLOWED_HOSTS 只能填明确的 IP / 域名，禁止 *、占位值和端口。")
# 必须先校验显式白名单，随后才加健康检查需要的回环地址；缺配置仍然启动失败。
ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, "127.0.0.1", "[::1]", "localhost"]))

# 只有走域名 + HTTPS（或明确从别的源发写请求）时才需要；纯 IP + HTTP 可留空。
CSRF_TRUSTED_ORIGINS = _csv("CUSTOMS_CSRF_TRUSTED_ORIGINS")
for _origin in CSRF_TRUSTED_ORIGINS:
    try:
        _parsed = urlsplit(_origin)
        _port = _parsed.port  # 非法端口必须启动失败。
        _origin_valid = (
            _parsed.scheme in {"http", "https"} and bool(_parsed.hostname)
            and not _parsed.username and not _parsed.password
            and not _parsed.path and not _parsed.query and not _parsed.fragment
        )
    except ValueError:
        _origin_valid = False
    if not _origin_valid:
        raise ImproperlyConfigured("CUSTOMS_CSRF_TRUSTED_ORIGINS 必须是 http(s)://域名[:端口]，不能带路径或凭据。")


def _persistent_file_root(raw: str, volume: Path = Path("/srv/customs")) -> Path:
    """根必须是命名卷的严格子目录，拒绝非规范路径和现有符号链接。

    应用 UID 无权替换卷根；卷内仍须按运维约定禁止其他进程植入链接。
    此处启动期检查不能代替文件服务自身对每次访问的防穿越检查。
    """
    path = Path(raw)
    if (not path.is_absolute() or os.path.normpath(raw) != raw or ".." in path.parts
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw)):
        raise ImproperlyConfigured("CUSTOMS_FILE_ROOT 必须使用 /srv/customs 内的规范绝对路径。")
    try:
        relative = path.relative_to(volume)
        if not relative.parts or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError
        path.resolve().relative_to(volume.resolve())
    except (ValueError, OSError, RuntimeError):
        raise ImproperlyConfigured("CUSTOMS_FILE_ROOT 必须是 /srv/customs 的严格子目录，且不能包含符号链接。") from None
    return path


CUSTOMS_FILE_ROOT = _persistent_file_root(os.environ.get("CUSTOMS_FILE_ROOT", "/srv/customs/files"))

# collectstatic 的输出目录，由 nginx 以 /static/ 提供（DEBUG 关闭后 Django 不再自己服务静态文件）。
STATIC_ROOT = Path(os.environ.get("CUSTOMS_STATIC_ROOT") or (BASE_DIR / "staticfiles"))

# 反代（nginx）终止 TLS 时，Django 靠这个头判断请求是 https，否则 CSRF 校验与 secure cookie 都会判错。
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CUSTOMS_HTTPS = _flag("CUSTOMS_HTTPS", False)
if CUSTOMS_HTTPS:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

SESSION_COOKIE_HTTPONLY = True
# 前端（P01 手测页、工作台）要读 csrftoken cookie 再放进 X-CSRFToken 头，不能设 HttpOnly。
CSRF_COOKIE_HTTPONLY = False

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# HSTS 故意不打开：测试环境可能只有 IP 或临时证书，开了会让浏览器长期强制 https，
# 后续换回 HTTP 或换域名时很难退回。上线正式域名后再评估（见 deploy/README.md“生产差距”）。
