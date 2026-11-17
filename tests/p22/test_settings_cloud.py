"""P22 部署设置：隔离 dotenv 的启动约束与无数据库 Django client 行为测试。"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = """
import json
import django
django.setup()
from django.conf import settings
print(json.dumps({
    "DEBUG": settings.DEBUG,
    "ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
    "CSRF_TRUSTED_ORIGINS": settings.CSRF_TRUSTED_ORIGINS,
    "STATIC_ROOT": str(settings.STATIC_ROOT),
    "SESSION_COOKIE_SECURE": settings.SESSION_COOKIE_SECURE,
    "CSRF_COOKIE_SECURE": settings.CSRF_COOKIE_SECURE,
    "SECURE_PROXY_SSL_HEADER": list(settings.SECURE_PROXY_SSL_HEADER),
    "DB_BACKEND": settings.DATABASES["default"]["ENGINE"],
    "FILE_ROOT": str(settings.CUSTOMS_FILE_ROOT),
}))
"""


def run_settings(extra_env: dict[str, str], probe: str = PROBE) -> subprocess.CompletedProcess:
    """既不读取也不输出运维机器上的真实配置；密钥只在内存中生成。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DJANGO_SETTINGS_MODULE": "deploy.settings_cloud",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "CUSTOMS_DB_BACKEND": "sqlite",
        "CUSTOMS_ALLOWED_HOSTS": "203.0.113.10",
        "CUSTOMS_FILE_ROOT": "/srv/customs/files",
    }
    env.update(extra_env)
    # config.settings 默认加载根 .env，不能让本机数据库/业务模式影响隔离测试。
    bootstrap = "from unittest.mock import patch\nwith patch('dotenv.load_dotenv'):\n"
    bootstrap += "\n".join("    " + line for line in probe.splitlines())
    return subprocess.run(
        [sys.executable, "-c", bootstrap], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )


class AllowedHostsGuardTests(unittest.TestCase):
    def test_missing_allowed_hosts_fails_loudly(self):
        result = run_settings({"CUSTOMS_ALLOWED_HOSTS": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUSTOMS_ALLOWED_HOSTS", result.stderr)

    def test_wildcards_placeholders_and_invalid_hosts_rejected(self):
        for host in ("*", ".customs.invalid", "change-me-server-ip", "test.example.com",
                     "<domain>", "203.0.113.10:8080", "https://customs.invalid", "good.invalid,*",
                     "customs.invalid\nforeign.invalid", "customs.invalid\t"):
            with self.subTest(host=host):
                result = run_settings({"CUSTOMS_ALLOWED_HOSTS": host})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CUSTOMS_ALLOWED_HOSTS", result.stderr)

    def test_allowed_hosts_is_parsed_then_loopback_is_added(self):
        result = run_settings({"CUSTOMS_ALLOWED_HOSTS": "203.0.113.10, customs.invalid"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ALLOWED_HOSTS"],
                         ["203.0.113.10", "customs.invalid", "127.0.0.1", "[::1]", "localhost"])

    def test_default_health_chain_accepts_loopback_but_rejects_foreign_hosts(self):
        # 使用真实 URL、真实中间件与 Django client；任何数据库访问都立即失败。
        probe = '''
import json
import django
django.setup()
from django.test import Client
from django.db.backends.base.base import BaseDatabaseWrapper
from unittest.mock import patch
with patch.object(BaseDatabaseWrapper, 'connect', side_effect=AssertionError('health accessed database')):
    client = Client()
    responses = {host: client.get('/api/health', HTTP_HOST=host)
                 for host in ('127.0.0.1:8000', '203.0.113.10:8080', 'foreign.invalid', '[::1]:8000')}
    assert responses['127.0.0.1:8000'].json() == {'status': 'ok'}
    print(json.dumps({host: response.status_code for host, response in responses.items()}))
'''
        result = run_settings({}, probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "127.0.0.1:8000": 200, "203.0.113.10:8080": 200,
            "foreign.invalid": 400, "[::1]:8000": 200,
        })


class DebugTests(unittest.TestCase):
    def test_debug_is_off_by_default(self):
        result = run_settings({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["DEBUG"])

    def test_debug_can_only_be_opened_explicitly(self):
        result = run_settings({"CUSTOMS_DEBUG": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["DEBUG"])

    def test_invalid_debug_value_fails(self):
        result = run_settings({"CUSTOMS_DEBUG": "maybe"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUSTOMS_DEBUG", result.stderr)


class CsrfAndCookieTests(unittest.TestCase):
    def test_csrf_origins_must_be_http_origins(self):
        for origin in ("customs.invalid", "ftp://customs.invalid", "https://user@customs.invalid",
                       "https://customs.invalid/path", "https://customs.invalid:invalid"):
            with self.subTest(origin=origin):
                result = run_settings({"CUSTOMS_CSRF_TRUSTED_ORIGINS": origin})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CUSTOMS_CSRF_TRUSTED_ORIGINS", result.stderr)

    def test_csrf_origin_accepted_with_scheme_and_nondefault_port(self):
        result = run_settings({"CUSTOMS_CSRF_TRUSTED_ORIGINS": "https://customs.invalid:8443"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["CSRF_TRUSTED_ORIGINS"], ["https://customs.invalid:8443"])

    def test_secure_cookies_only_when_https_enabled(self):
        for enabled in (False, True):
            result = run_settings({"CUSTOMS_HTTPS": str(int(enabled))})
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["SESSION_COOKIE_SECURE"], enabled)
            self.assertEqual(payload["CSRF_COOKIE_SECURE"], enabled)
            self.assertEqual(payload["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])

    def test_proxy_host_port_preserves_same_origin_csrf(self):
        probe = '''
import json
import django
django.setup()
from django.http import HttpResponse
from django.test import RequestFactory
from django.middleware.csrf import CsrfViewMiddleware, get_token
request = RequestFactory().post('/', HTTP_HOST='customs.invalid:8443',
    HTTP_ORIGIN='https://customs.invalid:8443', HTTP_X_FORWARDED_PROTO='https')
token = get_token(request)
from django.conf import settings
request.COOKIES[settings.CSRF_COOKIE_NAME] = request.META['CSRF_COOKIE']
request.META['HTTP_X_CSRFTOKEN'] = token
middleware = CsrfViewMiddleware(lambda request: HttpResponse())
response = middleware.process_view(request, lambda request: HttpResponse(), (), {})
print(json.dumps({'accepted': response is None, 'secure': request.is_secure(), 'host': request.get_host()}))
'''
        result = run_settings({"CUSTOMS_ALLOWED_HOSTS": "customs.invalid", "CUSTOMS_HTTPS": "1"}, probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"accepted": True, "secure": True, "host": "customs.invalid:8443"})


class StaticAndStorageTests(unittest.TestCase):
    def test_static_root_is_configurable(self):
        result = run_settings({"CUSTOMS_STATIC_ROOT": "/tmp/p22-static-root"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["STATIC_ROOT"], "/tmp/p22-static-root")

    def test_file_root_and_backend_come_from_env(self):
        result = run_settings({"CUSTOMS_FILE_ROOT": "/srv/customs/release-files"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["FILE_ROOT"], "/srv/customs/release-files")
        self.assertIn("sqlite", json.loads(result.stdout)["DB_BACKEND"])

    def test_file_root_must_be_canonical_strict_volume_child(self):
        for root in ("/srv/customs", "/srv/customs/", "/srv/customs/../escape", "/srv/customs/files/../other",
                     "/tmp/files", "files", "/srv/customs//files", "/srv/customs/./files", "",
                     "/srv/customs/files\nother", "/srv/customs/file\troot", "/srv/customs/file root"):
            with self.subTest(root=root):
                result = run_settings({"CUSTOMS_FILE_ROOT": root})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CUSTOMS_FILE_ROOT", result.stderr)

    def test_file_root_rejects_existing_symlink_even_inside_volume(self):
        # helper 的 volume 参数仅供隔离测试：不需要在宿主创建 /srv/customs。
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory).resolve() / "volume"
            volume.mkdir()
            (volume / "real").mkdir()
            (volume / "link").symlink_to(volume / "real", target_is_directory=True)
            probe = f'''
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from deploy.settings_cloud import _persistent_file_root
try:
    _persistent_file_root({str(volume / 'link' / 'files')!r}, Path({str(volume)!r}))
except ImproperlyConfigured:
    print('rejected')
else:
    raise AssertionError('symlink accepted')
'''
            result = run_settings({}, probe)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "rejected")

    def test_postgres_backend_requires_explicit_variables(self):
        result = run_settings({"CUSTOMS_DB_BACKEND": "postgres"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUSTOMS_PG_NAME", result.stderr)


if __name__ == "__main__":
    unittest.main()
