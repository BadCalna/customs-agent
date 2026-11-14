import os
import subprocess
import sys
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase


class BootstrapTests(SimpleTestCase):
    def test_local_entry_points(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        schema = self.client.get("/api/openapi.json").json()
        self.assertIn("get", schema["paths"]["/api/health"])
        self.assertContains(self.client.get("/api/docs"), "swagger", html=False)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/")
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_unsupported_modes_fail_on_startup(self):
        for name, value in [("CUSTOMS_ENGINE_MODE", "real"), ("CUSTOMS_EXECUTOR_MODE", "rq"),
                            ("DJANGO_SECRET_KEY", "")]:
            with self.subTest(name=name), patch.dict(os.environ, {name: value}):
                result = subprocess.run(
                    [sys.executable, "manage.py", "check"], cwd=settings.BASE_DIR,
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
