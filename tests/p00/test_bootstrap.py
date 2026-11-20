import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase


class BootstrapTests(SimpleTestCase):
    def startup_environment(self, **overrides):
        # Each subprocess explicitly chooses its mode instead of inheriting the
        # database/executor selected for the outer regression run.
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("CUSTOMS_", "DJANGO_"))}
        environment.update(
            PYTHON_DOTENV_DISABLED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
            DJANGO_SECRET_KEY="p00-isolated-configuration-probe",
            CUSTOMS_DB_BACKEND="sqlite", CUSTOMS_ENGINE_MODE="mock",
            CUSTOMS_EXECUTOR_MODE="inline", CUSTOMS_NOTIFICATION_MODE="mock",
        )
        environment.update(overrides)
        return environment

    def test_local_entry_points(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        schema = self.client.get("/api/openapi.json").json()
        self.assertIn("get", schema["paths"]["/api/health"])
        self.assertContains(self.client.get("/api/docs"), "swagger", html=False)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/")
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_unsupported_modes_fail_on_startup(self):
        for name, value in [("CUSTOMS_ENGINE_MODE", "real"), ("CUSTOMS_EXECUTOR_MODE", "unsupported"),
                            ("DJANGO_SECRET_KEY", "")]:
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, "manage.py", "check"], cwd=settings.BASE_DIR,
                    env=self.startup_environment(**{name: value}),
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)

    def test_rq_rejects_explicit_sqlite_configuration(self):
        result = subprocess.run(
            [sys.executable, "manage.py", "check"], cwd=settings.BASE_DIR,
            env=self.startup_environment(CUSTOMS_DB_BACKEND="sqlite", CUSTOMS_EXECUTOR_MODE="rq"),
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RQ", result.stderr)
        self.assertIn("PostgreSQL", result.stderr)

    def test_rq_accepts_explicit_postgresql_settings_without_connecting(self):
        # Import configuration only: this positive control needs no live server
        # and cannot read/write a developer's PostgreSQL database.
        code = ("import config.settings as s\n"
                "if s.EXECUTOR_MODE != 'rq' or "
                "s.DATABASES['default']['ENGINE'] != 'django.db.backends.postgresql':\n"
                "    raise SystemExit('P00 rq PostgreSQL configuration rejected')\n"
                "print('P00 rq PostgreSQL configuration accepted')\n")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR,
            env=self.startup_environment(
                CUSTOMS_DB_BACKEND="postgres", CUSTOMS_EXECUTOR_MODE="rq",
                CUSTOMS_PG_NAME="p00_configuration_probe", CUSTOMS_PG_USER="p00_configuration_probe",
                CUSTOMS_PG_HOST="127.0.0.1", CUSTOMS_PG_PORT="65432",
            ),
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("P00 rq PostgreSQL configuration accepted", result.stdout)
