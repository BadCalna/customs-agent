"""P23 测试公共基类：每个测试一个临时目录，测试结束清理工作簿缓存。

不写 ``.local/``、不写仓库里的任何生产目录：所有输出都落在
``tempfile.TemporaryDirectory()`` 里。
"""

from __future__ import annotations

import tempfile
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from adapters.sources.reference_import.sources import clear_workbook_cache
from adapters.sources.reference_import import publish as publishing


def active_pointer(out: Path) -> Path:
    return out / "published" / ("current.json" if publishing.POINTER_BACKEND == "atomic_json" else "current")


def force_current(out: Path, release) -> None:
    """Install a deliberately unchecked test pointer to a real immutable release."""
    if publishing.POINTER_BACKEND == "atomic_json":
        payload = json.loads((release.release_dir / ".current.json").read_text(encoding="utf-8"))
        publishing._write_current_pointer(out / "published" / "current.json", payload)
    else:
        temp = out / "published" / ".test-current"
        temp.unlink(missing_ok=True)
        os.symlink(os.path.join("releases", release.release_id), temp)
        os.replace(temp, out / "published" / "current")


@contextmanager
def fail_activation_commit():
    """Fail the actual native commit mechanism, without emulating another OS."""
    target, name = ((publishing, "_write_current_pointer") if publishing.POINTER_BACKEND == "atomic_json"
                    else (publishing.os, "symlink"))
    with patch.object(target, name, side_effect=OSError("模拟原子激活提交失败")):
        yield


class P23TestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self._tempdir = tempfile.TemporaryDirectory(prefix="p23-tests-")
        self.addCleanup(self._tempdir.cleanup)
        self.addCleanup(clear_workbook_cache)
        self.root = Path(self._tempdir.name)

    def path(self, *parts: str) -> Path:
        """临时目录内的路径（父目录按需创建）。"""

        target = self.root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def assert_current_layout(self, out: Path, release) -> None:
        pointer = out / "published" / "current.json"
        if publishing.POINTER_BACKEND == "atomic_json":
            self.assertTrue(pointer.is_file())
            self.assertFalse(pointer.is_symlink())
            self.assertFalse((out / "published" / "current").exists())
            meta = json.loads(pointer.read_text(encoding="utf-8"))
            self.assertEqual(meta["schema_version"], publishing.CURRENT_SCHEMA_VERSION)
            self.assertEqual(meta["pointer_backend"], "atomic_json")
            self.assertEqual(meta["release_id"], release.release_id)
        else:
            link = out / "published" / "current"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), release.release_dir.resolve())
            self.assertEqual(pointer.resolve(), (release.release_dir / ".current.json").resolve())
        self.assertEqual(publishing.current_release_dir(out), release.release_dir)
