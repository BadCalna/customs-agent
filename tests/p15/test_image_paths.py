"""历史导入的图片相对路径契约：Windows 也输出 POSIX，两个格式共用。"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from adapters.sources.history_import import (
    FORMAT_CONSOLIDATED,
    FORMAT_LEGACY,
    ImportRequest,
    _copy_row_image,
    import_history_templates,
)
from tests.p15.consolidated_fixtures import BASE_ROW, build_consolidated_workbook
from tests.p15.history_import_fixtures import build_synthetic_workbook


class ImageOutputPathTest(SimpleTestCase):
    def test_windows_relative_path_is_serialized_as_posix(self):
        # 用真正的 Windows 纯路径作为 relative_to 结果；str(...) 在 macOS/Linux
        # 上也会产生反斜杠，因此可捕获把 as_posix() 改回 str(...) 的回归。
        image_dir = mock.MagicMock(spec=Path)
        target = image_dir.__truediv__.return_value
        target.exists.return_value = False
        target.relative_to.return_value = PureWindowsPath(
            "images", "HIST-WINDOWS.12345678.png"
        )
        image = SimpleNamespace(
            status="available", data=b"synthetic image", sha256="12345678abcdef",
            image_format="png",
        )

        result = _copy_row_image(image, "HIST-WINDOWS", image_dir)

        self.assertEqual(result, "images/HIST-WINDOWS.12345678.png")
        self.assertNotIn("\\", result)
        image_dir.__truediv__.assert_called_once_with("HIST-WINDOWS.12345678.png")
        target.relative_to.assert_called_once_with(image_dir.parent)
        target.write_bytes.assert_called_once_with(image.data)

    def _assert_imported_paths(self, input_format):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            if input_format == FORMAT_LEGACY:
                workbook = build_synthetic_workbook(tmp / "legacy.xlsx")
            else:
                workbook = build_consolidated_workbook(
                    tmp / "consolidated.xlsx", rows=[dict(BASE_ROW)], with_image=True,
                )
            request = ImportRequest(
                files=[workbook], out_dir=tmp / "out", dataset_kind="synthetic",
                input_format=input_format, copy_images=True,
            )
            # 保证两种格式实际经过同一条已覆盖 Windows 的路径序列化逻辑。
            with mock.patch(
                "adapters.sources.history_import._copy_row_image", wraps=_copy_row_image,
            ) as copy_image:
                outcome = import_history_templates(request)
            payload = json.loads(outcome.records_path.read_text(encoding="utf-8"))
            copied = [
                record for record in payload["records"]
                if record["attributes"]["image_output_path"]
            ]
            self.assertEqual(len(copied), 1)
            self.assertEqual(copy_image.call_count, 1)
            self.assertEqual(outcome.report["files"][0]["format"], input_format)
            self.assertEqual(outcome.report["totals"]["parse_failed_files"], 0)
            self.assertTrue(outcome.report["totals"]["reconciliation_ok"])
            for record in copied:
                attributes = record["attributes"]
                relative = attributes["image_output_path"]
                self.assertTrue(relative.startswith("images/"))
                self.assertNotIn("\\", relative)
                self.assertNotIn("..", PurePosixPath(relative).parts)
                self.assertNotIn(".staging-", relative)
                image_file = outcome.out_dir / relative
                self.assertTrue(image_file.is_file())
                self.assertEqual(
                    hashlib.sha256(image_file.read_bytes()).hexdigest(),
                    attributes["image_sha256"],
                )

    def test_legacy_template_publishes_posix_image_path(self):
        self._assert_imported_paths(FORMAT_LEGACY)

    def test_consolidated_publishes_posix_image_path(self):
        self._assert_imported_paths(FORMAT_CONSOLIDATED)
