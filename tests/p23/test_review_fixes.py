"""P23 复核轮修复的回归测试（B1–B7）。

这些用例对应独立审查发现的实质问题：冲突被重复计数导致快照无法构建、
合成批次的数据文件自称官方权威、复用快照时把“验证未通过”写成成功、
注释关键词检查写死某个词、未知 ``dataset_kind`` 抛错类型不一致。
每条都先在修复前复现过，再在此固定行为，避免回退。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from adapters.sources.hs_lookup import JsonTariffStore
from adapters.sources.reference_import.model import DatasetKind
from adapters.sources.reference_import.query_check import run_query_checks
from adapters.sources.reference_import.snapshot import (
    SnapshotRequest,
    build_snapshot,
)
from adapters.sources.reference_import.sources import (
    SourceError,
    load_source_manifest,
    sha256_file,
)
from adapters.sources.tariff_commentary import (
    CommentaryStoreError,
    JsonCommentaryStore,
)
from adapters.sources.tax_lookup import JsonTaxRateStore
from tests.p23.builders import (
    ElementRow,
    IndexRow,
    TaxRow,
    build_batch,
    commentary_item,
    core_element_rows,
    core_index_rows,
    mutate_manifest,
    write_catalog_workbook,
)
from tests.p23.support import P23TestCase

CODE = "0999999981"


def _single_code_rows(*, reg_codes: str = "A", name: str = "合成样本甲"):
    index_rows = [
        IndexRow(CODE, name, unit="001", reg_codes=reg_codes, element_count=1, required_count=1)
    ]
    element_rows = [ElementRow(CODE, name, "合成要素甲", "1")]
    return index_rows, element_rows


def _rewrite_workbooks(batch, *, m01, m02) -> Path:
    """按 ``(index_rows, element_rows, tax_rows)`` 分别重写两份工作簿并同步清单哈希。

    两份工作簿必须能写成**不同**内容，才能构造“同业务键内容不同”的冲突场景。
    """

    for path, (index_rows, element_rows, tax_rows) in zip(batch.workbooks, (m01, m02)):
        write_catalog_workbook(
            path,
            index_rows=index_rows,
            element_rows=element_rows,
            tax_rows=tax_rows,
            index_header_row=9,
            element_header_row=3,
        )

    def fix_hash(payload: dict) -> None:
        for entry in payload["inputs"]:
            if entry["source_id"] not in ("M01", "M02"):
                continue
            target = batch.workbooks[0] if entry["source_id"] == "M01" else batch.workbooks[1]
            entry["sha256"] = sha256_file(target)
            entry["bytes"] = target.stat().st_size

    return mutate_manifest(batch, fix_hash, name="reference-sources-rewritten.json")


class ConflictCountingTest(P23TestCase):
    """B1/B2/B3：冲突行只能记一个状态，且必须能在不触发对账失败的前提下生成快照。"""

    def counts(self, build, dataset: str) -> dict:
        return next(
            item["counts"] for item in build.report["datasets"] if item["dataset"] == dataset
        )

    def test_tax_conflict_is_counted_once(self):
        index_rows, element_rows = _single_code_rows()
        batch = build_batch(
            self.root / "tax",
            index_rows=index_rows,
            element_rows=element_rows,
            tax_rows=(TaxRow(CODE, "合成样本甲", "0.13"),),
            tax_in_both=True,
        )
        # 第二份工作簿给出不同退税率 → 同业务键内容不同，必须是 conflict 而不是 duplicate。
        manifest = _rewrite_workbooks(
            batch,
            m01=(index_rows, element_rows, (TaxRow(CODE, "合成样本甲", "0.13"),)),
            m02=(
                index_rows,
                element_rows,
                (TaxRow(CODE, "合成样本甲", "0.09", tax_code="9999999981002"),),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=manifest, out_root=batch.out_root)
        )
        counts = self.counts(build, "p17_tax_rates")
        self.assertEqual(counts["input_total"], 2)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["conflict"], 1, "内容不同必须记 conflict")
        self.assertEqual(counts["duplicate"], 0, "冲突行不能再记一次 duplicate")
        self.assertEqual(counts["output_records"], 1, "冲突不得产生第二条记录")
        self.assertEqual(build.report["status"], "partial")
        self.assertEqual(int(build.exit_code), 1)
        rates = json.loads(
            (build.snapshot_dir / "tax_rates.json").read_text(encoding="utf-8")
        )["rates"]
        self.assertEqual(rates[0]["rate_percent"], 13.0, "保留先出现的退税率")

    def test_ledger_conflict_is_counted_once(self):
        rows_a, element_rows = _single_code_rows(reg_codes="A")
        rows_b = [replace(rows_a[0], reg_codes="B")]
        batch = build_batch(
            self.root / "ledger",
            index_rows=rows_a,
            element_rows=element_rows,
        )
        manifest = _rewrite_workbooks(
            batch,
            m01=(rows_a, element_rows, ()),
            m02=(rows_b, element_rows, ()),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=manifest, out_root=batch.out_root)
        )
        counts = self.counts(build, "regulatory_code_ledger")
        self.assertEqual(counts["input_total"], 2)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["conflict"], 1, "监管代码不同必须记 conflict")
        self.assertEqual(counts["duplicate"], 0)
        ledger = json.loads(
            (build.snapshot_dir / "regulatory_codes.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["records"][0]["codes_raw"], "A", "保留先出现的原文")

    def test_commentary_conflict_is_counted_once(self):
        batch = build_batch(
            self.root / "commentary",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            commentary_items=(
                commentary_item(
                    tariff_no="9992",
                    kind="品目",
                    sections=(("品目注释", "合成样本：第一版正文。"),),
                ),
                commentary_item(
                    tariff_no="9992",
                    kind="品目",
                    sections=(("品目注释", "合成样本：第二版正文（不同）。"),),
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        counts = self.counts(build, "tariff_commentary")
        self.assertEqual(counts["input_total"], 2)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["conflict"], 1)
        self.assertEqual(counts["duplicate"], 0)
        payload = json.loads(
            (build.snapshot_dir / "commentary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(payload["records"]), 1, "冲突不得产出两条注释")
        self.assertIn("第一版", payload["records"][0]["text_all"], "保留先出现的正文")


class SyntheticIsolationTest(P23TestCase):
    """B4：合成批次的数据文件必须自称合成，不能被读成官方权威来源。"""

    def test_synthetic_payloads_are_marked_synthetic(self):
        batch = build_batch(
            self.root / "synthetic",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            tax_rows=(
                TaxRow("0999999991", "合成样本甲", "0.13"),
                TaxRow("9999999911", "合成样本乙", "0"),
            ),
            commentary_items=(
                commentary_item(
                    tariff_no="9991",
                    kind="品目",
                    sections=(("品目注释", "合成样本正文。"),),
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        self.assertEqual(build.report["dataset_kind"], "synthetic")
        for name in (
            "tariff.json",
            "elements.json",
            "tax_rates.json",
            "commentary.json",
            "regulatory_codes.json",
        ):
            payload = json.loads((build.snapshot_dir / name).read_text(encoding="utf-8"))
            dataset = payload["dataset"]
            self.assertEqual(
                dataset["source_type"], "synthetic", f"{name} 的 source_type 必须是 synthetic"
            )
            self.assertEqual(
                dataset["authority"], "synthetic", f"{name} 的 authority 必须是 synthetic"
            )
            self.assertIs(
                dataset["synthetic"], True, f"{name} 的 synthetic 必须为 true"
            )
        # 反过来也要成立：加载器仍能读合成快照，但导出的 source 不会被当成官方。
        snapshot = JsonTariffStore(build.snapshot_dir / "tariff.json").load()
        self.assertFalse(snapshot.dataset.authoritative)
        self.assertEqual(snapshot.dataset.source_type.value, "synthetic")
        tax_snapshot = JsonTaxRateStore(build.snapshot_dir / "tax_rates.json").load()
        self.assertFalse(tax_snapshot.dataset.authoritative)

    def test_real_descriptor_is_not_inverted(self):
        """对照组：真实批次仍是 official_* + authoritative，但未复核。"""

        batch = build_batch(
            self.root / "real",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            kind="real",
        )
        build = build_snapshot(
            SnapshotRequest(
                manifest_path=batch.manifest,
                out_root=batch.out_root,
                dataset_kind=DatasetKind.REAL,
            )
        )
        dataset = json.loads(
            (build.snapshot_dir / "tariff.json").read_text(encoding="utf-8")
        )["dataset"]
        self.assertEqual(dataset["source_type"], "official_tariff")
        self.assertEqual(dataset["authority"], "authoritative")
        self.assertIs(dataset["synthetic"], False)
        self.assertEqual(dataset["review_status"], "unreviewed")


class ReuseVerificationTest(P23TestCase):
    """B5：复用既有快照时，“本次验证未通过”不能被覆盖成成功。"""

    def test_reused_run_keeps_partial_status_when_verification_fails(self):
        batch = build_batch(
            self.root / "reuse",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        request = SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)

        def failing_verification(_snapshot_dir: Path) -> dict:
            return {
                "schema_version": "p23.query-check.1",
                "all_passed": False,
                "checks": [
                    {"id": "p14.tariff.leading_zero_hit", "status": "fail", "detail": "合成：故意失败"}
                ],
                "counts": {"pass": 0, "fail": 1, "skip": 0},
            }

        first = build_snapshot(request, verify_hook=failing_verification)
        second = build_snapshot(request, verify_hook=failing_verification)
        self.assertTrue(second.reused)
        self.assertFalse(second.report["verification"]["all_passed"])
        self.assertEqual(first.report["status"], "partial")
        self.assertEqual(
            second.report["status"], "partial", "复用路径也必须反映验证未通过"
        )
        self.assertEqual(int(second.exit_code), 1, "复用路径的退出码不能是成功")


class KeywordCheckTest(P23TestCase):
    """B6：注释关键词检查不能写死某个词，否则换一批数据就会误判失败。"""

    def test_keyword_check_works_without_hardcoded_phrase(self):
        batch = build_batch(
            self.root / "keyword",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            commentary_items=(
                commentary_item(
                    tariff_no="9993",
                    kind="品目",
                    sections=(("品目注释", "完全不同的一批合成正文，用于验证关键词自适应。"),),
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        verification = run_query_checks(build.snapshot_dir)
        self.assertTrue(verification["all_passed"], "任何正常注释快照的验证都应通过")
        keyword_checks = [
            item for item in verification["checks"] if item["id"] == "p23.commentary.keyword_search"
        ]
        self.assertEqual(len(keyword_checks), 1)
        self.assertEqual(keyword_checks[0]["status"], "pass")
        self.assertNotEqual(
            keyword_checks[0]["request"].get("keyword"), "改良种用", "关键词必须来自数据本身"
        )


class ManifestKindTest(P23TestCase):
    """B7：未知 dataset_kind 必须像其它枚举错误一样抛 SourceError。"""

    def test_unknown_dataset_kind_is_source_error(self):
        batch = build_batch(
            self.root / "kind",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        payload = json.loads(batch.manifest.read_text(encoding="utf-8"))
        payload["dataset_kind"] = "demo"
        broken = self.path("kind", "broken-sources.json")
        broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(SourceError) as caught:
            load_source_manifest(broken)
        self.assertIn("dataset_kind", str(caught.exception))


class CommentaryIntegrityTest(P23TestCase):
    """补充：注释快照正文被改写时，读取期必须报存储不可读（不是静默降级）。"""

    def test_tampered_text_is_rejected_on_load(self):
        batch = build_batch(
            self.root / "tamper",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            commentary_items=(
                commentary_item(
                    tariff_no="9994",
                    kind="品目",
                    sections=(("品目注释", "合成样本：原始正文。"),),
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        path = build.snapshot_dir / "commentary.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][0]["text_all"] = payload["records"][0]["text_all"] + "被追加的一句"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CommentaryStoreError):
            JsonCommentaryStore(path).load()
