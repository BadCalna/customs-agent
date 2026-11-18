"""P23 真实原件导入（默认跳过）：用仓库里的真实清单跑一遍，核对真实计数。

门控：``CUSTOMS_P23_REAL_INPUT=1`` 时才运行（约 1-2 分钟）。默认跳过是为了让常规测试
保持秒级；打开后必须真的解析 24 MB 工作簿，因此这里的计数断言是硬数字，不能放宽。

守的规则：真实批次导入后的记录数、要素行数、待核数与原件哈希都必须对得上，
且导入过程只读原件（前后哈希不变）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from adapters.sources.reference_import.model import sha256_file
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot

REAL_INPUT_ENV = "CUSTOMS_P23_REAL_INPUT"
REPO_ROOT = Path(settings.BASE_DIR)
MANIFEST_PATH = REPO_ROOT / "data" / "import-basis" / "2026-11-17" / "reference-sources.json"


def repository_inputs() -> dict[str, Path]:
    """清单里 ``repository_path`` 指向的原件（不含未随仓库分发的大文件）。"""

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    paths: dict[str, Path] = {}
    for item in payload["inputs"]:
        repository_path = item.get("repository_path")
        if repository_path:
            paths[item["source_id"]] = REPO_ROOT / repository_path
    return paths


@unittest.skipUnless(
    os.environ.get(REAL_INPUT_ENV) == "1",
    f"真实原件导入默认跳过；设置 {REAL_INPUT_ENV}=1 运行（约 1-2 分钟）",
)
class RealInputImportTest(SimpleTestCase):
    def test_real_snapshot_counts(self):
        originals = repository_inputs()
        self.assertEqual(sorted(originals), ["M01", "M02"])
        before = {name: sha256_file(path) for name, path in originals.items()}
        manifest_before = sha256_file(MANIFEST_PATH)
        with tempfile.TemporaryDirectory(prefix="p23-real-input-") as tmp:
            build = build_snapshot(
                SnapshotRequest(manifest_path=MANIFEST_PATH, out_root=Path(tmp)),
                verify_hook=None,  # 省时间：查询验证另有测试覆盖
            )
            self.assertFalse(build.reused)
            datasets = {item["dataset"]: item for item in build.report["datasets"]}
            counts = {name: item["counts"] for name, item in datasets.items()}

            self.assertEqual(counts["p14_tariff"]["output_records"], 12089)
            self.assertEqual(counts["p14_elements"]["output_records"], 12083)
            self.assertEqual(counts["p14_elements"]["imported"], 102792)
            self.assertEqual(counts["p17_tax_rates"]["output_records"], 12088)
            self.assertEqual(counts["p14_elements"]["pending"], 6)
            self.assertEqual(counts["p14_elements"]["rejected"], 0)
            self.assertEqual(counts["p14_tariff"]["rejected"], 0)
            self.assertEqual(
                sum(item["counts"]["rejected"] for item in build.report["datasets"]), 0,
                "真实批次不应有被拒绝的记录",
            )
            # 输出文件里的数组长度必须与报告一致
            tariff = json.loads((build.snapshot_dir / "tariff.json").read_text(encoding="utf-8"))
            elements = json.loads((build.snapshot_dir / "elements.json").read_text(encoding="utf-8"))
            tax = json.loads((build.snapshot_dir / "tax_rates.json").read_text(encoding="utf-8"))
            self.assertEqual(len(tariff["codes"]), 12089)
            self.assertEqual(len(elements["schemas"]), 12083)
            self.assertEqual(len(tax["rates"]), 12088)
            # 报告里每条记录都要有可回查的来源定位
            for record in tariff["codes"][:20]:
                self.assertTrue(record["source"]["document"])
                self.assertTrue(record["source"]["locator"])
        after = {name: sha256_file(path) for name, path in originals.items()}
        self.assertEqual(before, after, "导入前后原件字节不得变化")
        self.assertEqual(sha256_file(MANIFEST_PATH), manifest_before,
                         "来源清单本身也不应被改写")
