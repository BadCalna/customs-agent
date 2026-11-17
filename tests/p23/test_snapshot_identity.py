"""P23 快照身份口径：同一输入只产生一份数据，业务指纹与生成时间分离。

守的规则：快照目录名由**业务内容指纹**决定。输入相同、转换器版本相同 → 复用既有快照
（``reused=True``），不产生第二份“看起来不同”的数据；``generated_at`` 只写报告，
不参与业务指纹，所以同一输入在不同时间生成的指纹必须一致。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import adapters.sources.reference_import.snapshot as snapshot_module
from adapters.sources.reference_import.model import CONVERTER_VERSION
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_core_batch, build_publish_batch
from tests.p23.support import P23TestCase


class ReuseTest(P23TestCase):
    def test_second_run_reuses_snapshot(self):
        batch = build_core_batch(self.root / "src")
        out_root = batch.out_root
        with patch.object(
            snapshot_module,
            "utc_now",
            side_effect=["2026-11-18T01:00:00+00:00", "2026-11-18T01:00:01+00:00"],
        ):
            first = build_snapshot(
                SnapshotRequest(manifest_path=batch.manifest, out_root=out_root)
            )
            second = build_snapshot(
                SnapshotRequest(manifest_path=batch.manifest, out_root=out_root)
            )
        self.assertFalse(first.reused, "第一次必须真算一遍")
        self.assertTrue(second.reused, "输入未变时必须复用既有快照")
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.snapshot_dir, second.snapshot_dir)
        self.assertEqual(
            first.report["business_fingerprint"],
            second.report["business_fingerprint"],
            "业务指纹必须一致",
        )
        candidates = sorted(entry.name for entry in (out_root / "candidates").iterdir())
        self.assertEqual(candidates, [first.snapshot_dir.name], "只能有一个候选快照目录")
        # 复用时不再重写数据文件，只追加一条运行记录。
        self.assertTrue(second.report.get("last_seen_at"))
        runs = sorted((out_root / "runs").glob("*.json"))
        self.assertEqual(len(runs), 2, "每次运行都留一条运行报告（含复用）")
        # 复用的结论写在运行报告里；快照内的数据报告保持首次生成的原文（不改写数据面）。
        run_reports = [
            json.loads(path.read_text(encoding="utf-8")) for path in runs
        ]
        self.assertTrue(any(item["reused"] for item in run_reports),
                        "复用必须在运行报告里标出来")
        self.assertEqual(
            {item["snapshot_id"] for item in run_reports}, {first.snapshot_id}
        )
        snapshot_report = json.loads(
            (first.snapshot_dir / "report.json").read_text(encoding="utf-8")
        )
        # 业务数据文件不重写，但“验证/复用”这类每次运行的结论会刷新到快照报告里，
        # 否则报告会一直停留在上一次运行的状态（复核轮 B8）。
        self.assertTrue(snapshot_report["reused"], "复用后快照报告应标出本次是复用")
        self.assertEqual(snapshot_report["snapshot_id"], first.snapshot_id)
        self.assertEqual(
            [item["counts"] for item in snapshot_report["datasets"]],
            [item["counts"] for item in first.report["datasets"]],
            "复用只刷新运行结论，业务计数必须与首次一致",
        )

    def test_business_fingerprint_ignores_generated_at(self):
        batch = build_core_batch(self.root / "src")
        with patch.object(
            snapshot_module,
            "local_now",
            side_effect=["2026-11-18T01:00:00+08:00", "2026-11-18T05:00:00+08:00"],
        ):
            first = build_snapshot(
                SnapshotRequest(manifest_path=batch.manifest, out_root=self.root / "out1")
            )
            second = build_snapshot(
                SnapshotRequest(manifest_path=batch.manifest, out_root=self.root / "out2")
            )
        self.assertNotEqual(
            first.report["generated_at"],
            second.report["generated_at"],
            "夹具前提：两次生成的报告时间不同",
        )
        self.assertEqual(
            first.report["business_fingerprint"],
            second.report["business_fingerprint"],
            "generated_at 不得参与业务指纹",
        )
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        first_manifest = json.loads((first.snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        second_manifest = json.loads((second.snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first_manifest["business_fingerprint"], second_manifest["business_fingerprint"])
        self.assertEqual(first_manifest["input_fingerprint"], second_manifest["input_fingerprint"])
        self.assertEqual(first_manifest["converter_version"], CONVERTER_VERSION)
        self.assertNotEqual(first_manifest["generated_at"], second_manifest["generated_at"])
        self.assertTrue(first.snapshot_dir.name.endswith(first.report["business_fingerprint"][:12]))

    def test_changed_content_creates_new_snapshot(self):
        first_batch = build_publish_batch(self.root / "a", label="A")
        second_batch = build_publish_batch(
            self.root / "b", label="B", name_suffix="（内容变更 B）"
        )
        out_root = self.root / "out"
        first = build_snapshot(
            SnapshotRequest(manifest_path=first_batch.manifest, out_root=out_root)
        )
        second = build_snapshot(
            SnapshotRequest(manifest_path=second_batch.manifest, out_root=out_root)
        )
        self.assertFalse(first.reused)
        self.assertFalse(second.reused, "内容不同的批次不得复用快照")
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertNotEqual(
            first.report["business_fingerprint"], second.report["business_fingerprint"]
        )
        candidates = sorted(entry.name for entry in (out_root / "candidates").iterdir())
        self.assertEqual(len(candidates), 2, "内容变化必须留下两份候选快照")
        # 旧快照仍然完整可读
        old_manifest = json.loads((first.snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(old_manifest["snapshot_id"], first.snapshot_id)
        self.assertEqual(
            json.loads((first.snapshot_dir / "tariff.json").read_text(encoding="utf-8"))["codes"][0][
                "description"
            ],
            "合成样本甲",
        )
