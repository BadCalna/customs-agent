"""P05 存储配置：settings 落地、环境变量覆盖与解析失败明确报错。"""

import os
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
        self.assertEqual(
            settings.CUSTOMS_FILE_ROOT,
            Path(settings.BASE_DIR) / ".local" / "files",
        )
        self.assertEqual(settings.CUSTOMS_FILE_MAX_BYTES, 100 * 1024 * 1024)
        self.assertEqual(settings.CUSTOMS_IMAGE_MAX_BYTES, 20 * 1024 * 1024)
        self.assertEqual(settings.CUSTOMS_FILE_READ_MAX_BYTES, 200 * 1024 * 1024)

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
