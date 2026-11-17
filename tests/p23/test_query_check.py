"""P23 查询验证口径：用真实 JSON 存储类读快照，缺数据类别记 skip 而不是 pass。

守的规则：验证报告是发布门禁的输入，不能“文件生成了”就写“查询通过”。
快照里缺某类文件时必须显式 ``skip``；编码被改坏时必须显式 ``fail``，而不是崩掉或静默。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from adapters.sources.reference_import.query_check import run_query_checks
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_core_batch
from tests.p23.support import P23TestCase


class QueryCheckTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.batch = build_core_batch(self.root / "src")
        self.build = build_snapshot(
            SnapshotRequest(manifest_path=self.batch.manifest, out_root=self.batch.out_root),
            verify_hook=run_query_checks,
        )
        self.verification = self.build.report["verification"]
        self.checks = {item["id"]: item for item in self.verification["checks"]}

    def copy_snapshot(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(self.build.snapshot_dir, target)
        return target

    def test_all_checks_pass_on_synthetic_snapshot(self):
        self.assertTrue(self.verification["all_passed"], "干净合成快照的查询验证必须全部通过")
        self.assertEqual(self.verification["counts"]["fail"], 0)
        self.assertEqual(self.verification["counts"]["skip"], 0)
        self.assertGreaterEqual(self.verification["counts"]["pass"], 10)
        self.assertEqual(self.verification["schema_version"], "p23.query-check.1")
        self.assertEqual(self.verification["snapshot_dir"], self.build.snapshot_dir.name)
        self.assertEqual(self.build.report["status"], "success")
        self.assertEqual(int(self.build.exit_code), 0)
        for identifier in (
            "p14.tariff.leading_zero_hit",
            "p14.tariff.subheading_only",
            "p14.elements.full_but_unreviewed",
            "p17.tax.hit_but_conditions_undeclared",
            "p17.tax.zero_rate_is_not_blank",
            "p17.regulatory.not_available",
            "p23.commentary.heading_hit",
            "p23.commentary.rejects_declaration_code",
        ):
            with self.subTest(check=identifier):
                self.assertIn(identifier, self.checks)
                self.assertEqual(self.checks[identifier]["status"], "pass",
                                 self.checks[identifier]["detail"])
                self.assertTrue(self.checks[identifier]["request"])
                self.assertTrue(self.checks[identifier]["detail"])
        # 验证报告同时落盘到 verification/ 目录，供发布门禁读取
        saved = json.loads(
            (self.batch.out_root / "verification" / f"{self.build.snapshot_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(saved["all_passed"])

    def test_missing_declared_dataset_fails_verification(self):
        """报告说“已提供”却缺了输出文件：既不能记 pass，也不能整体算通过。"""

        target = self.copy_snapshot("no-elements")
        (target / "elements.json").unlink()
        verification = run_query_checks(target)
        statuses = {item["id"]: item["status"] for item in verification["checks"]}
        self.assertEqual(statuses["p14.elements"], "skip", "缺 elements.json 必须记 skip")
        self.assertEqual(
            [identifier for identifier in statuses if identifier.startswith("p14.elements.")],
            [],
            "缺文件时不应给出任何 p14.elements.* 的“通过”结论",
        )
        self.assertEqual(statuses["p23.snapshot.provided_datasets_present"], "fail")
        self.assertEqual(verification["counts"]["fail"], 1)
        self.assertFalse(
            verification["all_passed"], "声明已提供但缺文件时不能 all_passed"
        )
        detail = next(
            item["detail"]
            for item in verification["checks"]
            if item["id"] == "p23.snapshot.provided_datasets_present"
        )
        self.assertIn("p14_elements", detail)

    def test_corrupted_tariff_code_reports_fail(self):
        target = self.copy_snapshot("corrupted")
        path = target / "tariff.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        victim = next(item for item in payload["codes"] if item["code"].startswith("0"))
        code = victim["code"]
        victim["source"].pop("document")
        victim["source"].pop("locator")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        verification = run_query_checks(target)
        self.assertFalse(verification["all_passed"], "编码证据被改坏时验证必须失败")
        failed = [item for item in verification["checks"] if item["status"] == "fail"]
        self.assertGreaterEqual(verification["counts"]["fail"], 1)
        leading_zero = [
            item for item in failed if item["id"] == "p14.tariff.leading_zero_hit"
        ]
        self.assertEqual(len(leading_zero), 1)
        self.assertIn(code, leading_zero[0]["detail"])
        self.assertIn("不符合预期", leading_zero[0]["detail"])
        # 报告结构仍然完整，能直接写盘
        self.assertEqual(verification["schema_version"], "p23.query-check.1")
        self.assertGreaterEqual(len(verification["checks"]), 10)
        self.assertEqual(verification["counts"]["pass"] + verification["counts"]["fail"]
                         + verification["counts"]["skip"], len(verification["checks"]))
