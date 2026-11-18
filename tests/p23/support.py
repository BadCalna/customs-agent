"""P23 测试公共基类：每个测试一个临时目录，测试结束清理工作簿缓存。

不写 ``.local/``、不写仓库里的任何生产目录：所有输出都落在
``tempfile.TemporaryDirectory()`` 里。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from adapters.sources.reference_import.sources import clear_workbook_cache


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
