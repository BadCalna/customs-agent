"""P23 真实来源清单口径：仓库里提交的 reference-sources.json 必须自洽、可逐字核对。

守的规则：清单登记的每个 ``repository_path`` 都要真的存在，实测 SHA-256 / 字节数必须与
清单声明一致；未提供的数据类别要写明 ``not_provided`` 与原因，不能用空白补齐。
本测试只做哈希核对，不解析工作簿，因此始终运行且很快。
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

from adapters.sources.reference_import.model import (
    DatasetId,
    DatasetKind,
    DatasetStatus,
    sha256_file,
)
from adapters.sources.reference_import.sources import load_source_manifest
from tests.p23.support import P23TestCase

REPO_ROOT = Path(settings.BASE_DIR)
MANIFEST_PATH = REPO_ROOT / "data" / "import-basis" / "2026-11-17" / "reference-sources.json"


class RealSourceManifestTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.manifest = load_source_manifest(MANIFEST_PATH)

    def test_manifest_parses(self):
        self.assertIs(self.manifest.kind, DatasetKind.REAL, "提交的清单必须是真实批次")
        self.assertEqual(self.manifest.batch_id, "customs-reference-2026-11-17")
        self.assertEqual(
            [item.dataset.value for item in self.manifest.declared],
            [
                "p14_tariff",
                "p14_elements",
                "p17_tax_rates",
                "regulatory_code_ledger",
                "p17_regulatory",
                "tariff_commentary",
            ],
        )
        self.assertEqual(self.manifest.defaults["tariff_version"], "2026")
        self.assertEqual(self.manifest.defaults["tax_version"], "2026C")
        resolved = {item.source_id for item in self.manifest.sources}
        self.assertTrue(
            {"M01", "M02"} <= resolved,
            "随仓库分发的两个工作簿必须都能取到；实际解析到 " + "、".join(sorted(resolved)),
        )
        for source in self.manifest.sources:
            self.assertEqual(source.path.name, source.original_filename,
                             "解析到的原件文件名必须与清单声明一致")

    def test_every_repository_path_exists_with_declared_hash(self):
        checked = 0
        for item in self.raw["inputs"]:
            repository_path = item.get("repository_path")
            if not repository_path:
                # 未随仓库分发的原件用 local_path_hint 指向工作区外的路径，不做存在性断言。
                self.assertTrue(item.get("local_path_hint"), "没有 repository_path 就必须有本地提示路径")
                continue
            path = REPO_ROOT / repository_path
            with self.subTest(source=item["source_id"]):
                self.assertTrue(path.is_file(), f"清单登记的仓库路径不存在：{repository_path}")
                self.assertEqual(
                    sha256_file(path), item["sha256"],
                    f"{item['source_id']} 的实测 SHA-256 与清单声明不一致",
                )
                self.assertEqual(path.stat().st_size, item["bytes"],
                                 f"{item['source_id']} 的实测字节数与清单声明不一致")
                checked += 1
        self.assertEqual(checked, 2, "本批应有两个随仓库分发的原件（M01/M02）")

    def test_resolved_sources_are_verified(self):
        for source in self.manifest.sources:
            with self.subTest(source=source.source_id):
                self.assertTrue(source.verified, f"{source.source_id} 的哈希/字节数与清单不一致")
                self.assertEqual(source.declared_sha256, source.sha256)
                self.assertEqual(source.declared_bytes, source.size)

    def test_regulatory_dataset_is_not_provided_with_reason(self):
        declared = self.manifest.dataset(DatasetId.P17_REGULATORY)
        self.assertIsNotNone(declared)
        self.assertIs(declared.status, DatasetStatus.NOT_PROVIDED)
        self.assertTrue(declared.reason.strip(), "未提供必须写明原因，不能留空")
        self.assertIn("监管条件", declared.reason)
        self.assertEqual(declared.inputs, (), "未提供的数据类别不应声明输入")
