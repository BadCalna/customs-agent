"""P22：``deploy.settings_cloud`` 的强制约束。

用子进程加载设置模块，因为“缺变量就启动失败”本身要在导入期发生；在测试进程里 import
会把错误变成不可重复的状态。每个用例都用干净的环境变量，避免开发机自己的 .env 干扰。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 读取设置的核心片段：只在设置成功加载后才输出 JSON。
PROBE = """
import json
import django

django.setup()
from django.conf import settings

print(json.dumps({
    "DEBUG": settings.DEBUG,
    "ALLOWED_HOSTS": settings.ALLOWED_HOSTS,
    "CSRF_TRUSTED_ORIGINS": list(getattr(settings, "CSRF_TRUSTED_ORIGINS", [])),
    "STATIC_ROOT": str(settings.STATIC_ROOT),
    "SESSION_COOKIE_SECURE": getattr(settings, "SESSION_COOKIE_SECURE", False),
    "CSRF_COOKIE_SECURE": getattr(settings, "CSRF_COOKIE_SECURE", False),
    "SECURE_PROXY_SSL_HEADER": list(getattr(settings, "SECURE_PROXY_SSL_HEADER", ())),
    "DB_BACKEND": settings.DATABASES["default"]["ENGINE"],
    "FILE_ROOT": str(settings.CUSTOMS_FILE_ROOT),
}))
"""


def run_settings(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """在干净环境里加载 deploy.settings_cloud。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DJANGO_SETTINGS_MODULE": "deploy.settings_cloud",
        "DJANGO_SECRET_KEY": "test-only-secret-not-a-real-key",
        # 显式用 SQLite：这些用例只验证部署开关，不依赖 PostgreSQL。
        "CUSTOMS_DB_BACKEND": "sqlite",
        "CUSTOMS_ALLOWED_HOSTS": "127.0.0.1",
        "CUSTOMS_FILE_ROOT": "/tmp/p22-settings-test-files",
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class AllowedHostsGuardTests(unittest.TestCase):
    def test_missing_allowed_hosts_fails_loudly(self):
        result = run_settings({"CUSTOMS_ALLOWED_HOSTS": ""})
        self.assertNotEqual(result.returncode, 0, "缺少 ALLOWED_HOSTS 时必须启动失败")
        self.assertIn("CUSTOMS_ALLOWED_HOSTS", result.stderr)

    def test_allowed_hosts_is_parsed_as_csv(self):
        result = run_settings({"CUSTOMS_ALLOWED_HOSTS": "203.0.113.10, test.example.com"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ALLOWED_HOSTS"], ["203.0.113.10", "test.example.com"])


class DebugTests(unittest.TestCase):
    def test_debug_is_off_by_default(self):
        result = run_settings({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["DEBUG"], "云端默认必须关闭 DEBUG")

    def test_debug_can_only_be_opened_explicitly(self):
        result = run_settings({"CUSTOMS_DEBUG": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["DEBUG"])

    def test_invalid_debug_value_fails(self):
        result = run_settings({"CUSTOMS_DEBUG": "maybe"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUSTOMS_DEBUG", result.stderr)


class CsrfAndCookieTests(unittest.TestCase):
    def test_csrf_origin_requires_scheme(self):
        result = run_settings({"CUSTOMS_CSRF_TRUSTED_ORIGINS": "test.example.com"})
        self.assertNotEqual(result.returncode, 0, "不带协议的来源必须启动失败")
        self.assertIn("CUSTOMS_CSRF_TRUSTED_ORIGINS", result.stderr)

    def test_csrf_origin_accepted_with_scheme(self):
        result = run_settings({"CUSTOMS_CSRF_TRUSTED_ORIGINS": "https://test.example.com"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["CSRF_TRUSTED_ORIGINS"], ["https://test.example.com"]
        )

    def test_secure_cookies_only_when_https_enabled(self):
        plain = run_settings({})
        self.assertEqual(plain.returncode, 0, plain.stderr)
        payload = json.loads(plain.stdout)
        self.assertFalse(payload["SESSION_COOKIE_SECURE"], "纯 HTTP 测试不能开 Secure cookie")

        secure = run_settings({"CUSTOMS_HTTPS": "1"})
        self.assertEqual(secure.returncode, 0, secure.stderr)
        payload = json.loads(secure.stdout)
        self.assertTrue(payload["SESSION_COOKIE_SECURE"])
        self.assertTrue(payload["CSRF_COOKIE_SECURE"])
        # 反代终止 TLS 时靠这个头判断 https。
        self.assertEqual(payload["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])


class StaticAndStorageTests(unittest.TestCase):
    def test_static_root_is_configurable(self):
        result = run_settings({"CUSTOMS_STATIC_ROOT": "/tmp/p22-static-root"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["STATIC_ROOT"], "/tmp/p22-static-root")

    def test_file_root_and_backend_come_from_env(self):
        result = run_settings({"CUSTOMS_FILE_ROOT": "/srv/customs/files"})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["FILE_ROOT"], "/srv/customs/files")
        self.assertIn("sqlite", payload["DB_BACKEND"])

    def test_postgres_backend_requires_explicit_variables(self):
        """切 postgres 却缺库名/用户时必须失败，不能悄悄回退 SQLite（P12 口径）。"""
        result = run_settings({"CUSTOMS_DB_BACKEND": "postgres"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CUSTOMS_PG_NAME", result.stderr)


if __name__ == "__main__":
    unittest.main()
