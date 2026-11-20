"""P05 存储配置：settings 落地、环境变量覆盖与解析失败明确报错。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.settings import byte_setting, path_setting


class StorageSettingTests(unittest.TestCase):
    def test_settings_expose_storage_root_and_limits(self):
        self.assertIsInstance(settings.CUSTOMS_FILE_ROOT, Path)
        # Test defaults independently of a caller's explicit storage/limit overrides.
        # Import settings only: do not connect to a database or read the checkout's .env.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CUSTOMS_", "DJANGO_"))}
        env.update(PYTHON_DOTENV_DISABLED="1", DJANGO_SECRET_KEY="p05-default-settings-test-only")
        script = """
import json
from config import settings
print(json.dumps({
    "file_root": str(settings.CUSTOMS_FILE_ROOT),
    "file_max_bytes": settings.CUSTOMS_FILE_MAX_BYTES,
    "image_max_bytes": settings.CUSTOMS_IMAGE_MAX_BYTES,
    "file_read_max_bytes": settings.CUSTOMS_FILE_READ_MAX_BYTES,
}))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script], cwd=settings.BASE_DIR, env=env,
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            {
                "file_root": str(Path(settings.BASE_DIR) / ".local" / "files"),
                "file_max_bytes": 100 * 1024 * 1024,
                "image_max_bytes": 20 * 1024 * 1024,
                "file_read_max_bytes": 200 * 1024 * 1024,
            },
        )

    def test_byte_setting_accepts_env_override_and_default(self):
        with patch.dict(os.environ, {"CUSTOMS_FILE_MAX_BYTES": "2048"}):
            self.assertEqual(byte_setting("CUSTOMS_FILE_MAX_BYTES", 1), 2048)
        with patch.dict(os.environ, {"CUSTOMS_FILE_MAX_BYTES": ""}):
            self.assertEqual(byte_setting("CUSTOMS_FILE_MAX_BYTES", 3), 3)

    def test_byte_setting_rejects_invalid_values_with_clear_error(self):
        for raw in ("abc", "0", "-1", "1.5"):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {"CUSTOMS_FILE_MAX_BYTES": raw}):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        byte_setting("CUSTOMS_FILE_MAX_BYTES", 1)
                self.assertIn("CUSTOMS_FILE_MAX_BYTES", str(caught.exception))

    def test_path_setting_resolves_relative_to_repository_root(self):
        with patch.dict(os.environ, {"CUSTOMS_FILE_ROOT": "var/files"}):
            self.assertEqual(
                path_setting("CUSTOMS_FILE_ROOT", Path("/default")),
                Path(settings.BASE_DIR) / "var" / "files",
            )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CUSTOMS_FILE_ROOT": directory}):
                self.assertEqual(
                    path_setting("CUSTOMS_FILE_ROOT", Path("/default")), Path(directory),
                )


if __name__ == "__main__":
    unittest.main()
