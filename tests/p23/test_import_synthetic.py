"""P23 合成批次导入：计数必须能对账，重复不重复写、冲突保留先出现、坏行按契约拒绝。

守的规则（每条都对应报告里的具体字段）：

- 每个数据类别 ``input_total = imported + duplicate + conflict + pending + rejected
  + not_imported``，且 ``output_records`` 等于输出文件里数组的真实长度；
- 两个来源内容相同 → 第二条记 ``duplicate``，输出里没有重复记录；
- 同业务键内容不同 → 记 ``conflict`` 并保留**先出现**的那条；
- 坏的编码/日期/必填标记只让对应记录（或整份要素清单）拒绝，不污染其它行；
- 合成批次不得发布；失败的构建不留半个快照，既有快照仍可读。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import adapters.sources.reference_import.snapshot as snapshot_module
from adapters.sources.reference_import.model import (
    DatasetId,
    DatasetKind,
    DatasetOutcome,
    DatasetStatus,
    RecordStatus,
)
from adapters.sources.reference_import.publish import (
    PublishError,
    PublishRequest,
    publish_release,
)
from adapters.sources.reference_import.snapshot import (
    SnapshotFailed,
    SnapshotRequest,
    _check_count_reconciliation,
    build_snapshot,
)
from tests.p23.builders import (
    ElementRow,
    IndexRow,
    TaxRow,
    build_batch,
    build_core_batch,
    build_duplicate_batch,
    build_publish_batch,
    core_element_rows,
    core_index_rows,
    mutate_manifest,
    write_verification,
)
from tests.p23.support import P23TestCase

#: 数据类别 → 输出文件里的数组字段（``output_records`` 的口径）。
OUTPUT_KEYS = {
    "p14_tariff": "codes",
    "p14_elements": "schemas",
    "p17_tax_rates": "rates",
    "regulatory_code_ledger": "records",
    "tariff_commentary": "records",
}


class CoreBatchTest(P23TestCase):
    """干净合成批次：全量导入、无拒绝无冲突。"""

    def setUp(self):
        super().setUp()
        self.batch = build_core_batch(self.root / "src")
        self.build = build_snapshot(
            SnapshotRequest(
                manifest_path=self.batch.manifest,
                out_root=self.batch.out_root,
                dataset_kind=DatasetKind.SYNTHETIC,
            )
        )
        self.by_name = {item["dataset"]: item for item in self.build.report["datasets"]}

    def output_payload(self, dataset: str) -> dict:
        path = self.build.snapshot_dir / DatasetId(dataset).filename
        return json.loads(path.read_text(encoding="utf-8"))

    def test_status_is_success(self):
        self.assertEqual(self.build.report["status"], "success", "干净批次不应是 partial")
        self.assertEqual(int(self.build.exit_code), 0)
        self.assertFalse(self.build.reused)
        self.assertEqual(self.build.report["dataset_kind"], "synthetic")
        self.assertEqual(self.build.report["batch_id"], "customs-reference-synthetic-2026-11-18")
        self.assertEqual(len(self.build.report["records"]), 0, "干净批次不应有非 imported 明细")

    def test_counts_reconcile_for_every_dataset(self):
        for name, item in self.by_name.items():
            counts = item["counts"]
            counted = (
                counts["imported"]
                + counts["duplicate"]
                + counts["conflict"]
                + counts["pending"]
                + counts["rejected"]
                + counts["not_imported"]
            )
            self.assertEqual(
                counted,
                counts["input_total"],
                f"{name} 计数对不上：input_total={counts['input_total']}，各状态合计={counted}",
            )

    def test_output_records_match_payload_length(self):
        for name, key in OUTPUT_KEYS.items():
            payload = self.output_payload(name)
            self.assertEqual(
                self.by_name[name]["counts"]["output_records"],
                len(payload[key]),
                f"{name} 的 output_records 必须等于 {key} 数组长度",
            )

    def test_expected_counts(self):
        expected = {
            "p14_tariff": (6, 3, 3, 0, 0, 3),
            "p14_elements": (8, 4, 4, 0, 0, 3),
            "p17_tax_rates": (3, 3, 0, 0, 0, 3),
            "regulatory_code_ledger": (6, 3, 3, 0, 0, 3),
            "tariff_commentary": (4, 4, 0, 0, 0, 4),
        }
        for name, values in expected.items():
            counts = self.by_name[name]["counts"]
            actual = (
                counts["input_total"],
                counts["imported"],
                counts["duplicate"],
                counts["conflict"],
                counts["pending"],
                counts["output_records"],
            )
            self.assertEqual(actual, values, f"{name} 的计数口径不符（input/imported/duplicate/"
                                            "conflict/pending/output）")
        self.assertEqual(self.by_name["p17_regulatory"]["status"], "not_provided")
        self.assertIn("合成样本", self.by_name["p17_regulatory"]["reason"])

    def test_totals_equal_sum_of_datasets(self):
        totals = self.build.report["totals"]
        self.assertEqual(totals["input_total"], 27)
        self.assertEqual(totals["imported"], 17)
        self.assertEqual(totals["duplicate"], 10)
        self.assertEqual(totals["conflict"], 0)
        self.assertEqual(totals["pending"], 0)
        self.assertEqual(totals["rejected"], 0)
        self.assertEqual(totals["output_records"], 16)
        for field in totals:
            summed = sum(
                item["counts"][field] for item in self.build.report["datasets"]
            )
            self.assertEqual(
                totals[field], summed,
                f"汇总 {field} 必须等于各数据类别之和（{totals[field]} != {summed}）",
            )

    def test_every_output_record_has_source_document_and_locator(self):
        for name, key in OUTPUT_KEYS.items():
            payload = self.output_payload(name)
            self.assertGreater(len(payload[key]), 0, f"{name} 不应是空快照")
            for record in payload[key]:
                source = record.get("source") or {}
                with self.subTest(dataset=name, key=record.get("code") or record.get("hs_code")
                                  or record.get("tariff_no")):
                    self.assertTrue(source.get("document"), f"{name} 的记录缺 source.document")
                    self.assertTrue(source.get("locator"), f"{name} 的记录缺 source.locator")
                    self.assertTrue(source.get("retrieved_at"), f"{name} 的记录缺采集时间")
                    if name == "tariff_commentary":
                        # 注释条目按“第 N 条 / tariff_no”定位，不是单元格定位。
                        self.assertIn("tariff_no=", source["locator"])
                        continue
                    self.assertIn("!", source["locator"], "定位必须是 页签!单元格 形式")
                    self.assertEqual(source["publisher"], "合成样本（非真实来源）")

    def test_commentary_and_ledger_are_separate_datasets(self):
        commentary = self.output_payload("tariff_commentary")
        self.assertEqual(commentary["schema_version"], "p23.commentary.1")
        self.assertEqual(commentary["integration"]["p16_engine"], "not_wired")
        ledger = self.output_payload("regulatory_code_ledger")
        self.assertEqual(ledger["schema_version"], "p23.regulatory-codes.1")
        self.assertEqual(ledger["integration"]["p17_regulatory_store"], "not_generated")
        self.assertEqual(self.build.report["datasets"][0]["dataset"], "p14_tariff")

    def test_synthetic_batch_is_rejected_by_publish(self):
        with self.assertRaises(PublishError) as caught:
            publish_release(
                PublishRequest(
                    snapshot_dir=self.build.snapshot_dir, out_root=self.batch.out_root
                )
            )
        message = str(caught.exception)
        self.assertIn("合成批次不允许发布", message)
        self.assertIn("dataset_kind", message)


class DuplicateAndConflictTest(P23TestCase):
    """重复不重复写；冲突保留先出现并转待核。"""

    def test_identical_sources_are_duplicates(self):
        batch = build_duplicate_batch(self.root / "src")
        self.assertEqual(
            batch.workbooks[0].read_bytes(),
            batch.workbooks[1].read_bytes(),
            "夹具前提：两个来源文件字节完全相同",
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        tariff = by_name["p14_tariff"]["counts"]
        self.assertEqual((tariff["input_total"], tariff["imported"], tariff["duplicate"]), (6, 3, 3))
        elements = by_name["p14_elements"]["counts"]
        self.assertEqual((elements["input_total"], elements["imported"], elements["duplicate"]), (8, 4, 4))
        codes = json.loads((build.snapshot_dir / "tariff.json").read_text(encoding="utf-8"))["codes"]
        self.assertEqual([item["code"] for item in codes],
                         ["0999999991", "9999999911", "9999999912"],
                         "输出必须只有唯一业务键，不能写两遍")
        self.assertEqual(len({item["code"] for item in codes}), len(codes))
        schemas = json.loads((build.snapshot_dir / "elements.json").read_text(encoding="utf-8"))["schemas"]
        self.assertEqual(len({item["hs_code"] for item in schemas}), len(schemas))
        self.assertEqual(len(schemas), 3)

    def test_conflicting_sources_keep_first_content(self):
        batch = build_batch(
            self.root / "src",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            second_name_overrides={"0999999991": "合成样本甲（第二来源不同内容）"},
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        tariff = by_name["p14_tariff"]["counts"]
        self.assertEqual(tariff["input_total"], 6)
        self.assertEqual(tariff["imported"], 3)
        self.assertEqual(tariff["duplicate"], 2, "内容一致的另外两条才是 duplicate")
        self.assertEqual(tariff["conflict"], 1, "同业务键内容不同必须记 conflict")
        codes = {item["code"]: item["description"] for item in json.loads(
            (build.snapshot_dir / "tariff.json").read_text(encoding="utf-8")
        )["codes"]}
        self.assertEqual(codes["0999999991"], "合成样本甲", "必须保留先出现（M01）的那条内容")
        self.assertEqual(len(codes), 3, "冲突不得产生第二条记录")
        elements = by_name["p14_elements"]["counts"]
        self.assertEqual((elements["input_total"], elements["imported"]), (8, 4))
        self.assertEqual((elements["duplicate"], elements["conflict"]), (2, 2),
                         "该编码的两行要素清单在两个来源里内容不同")
        conflict_records = [
            item for item in build.report["records"] if item["status"] == "conflict"
        ]
        self.assertEqual(len(conflict_records), 2)
        codes_seen = {item["business_key"] for item in conflict_records}
        self.assertEqual(codes_seen, {"0999999991"})
        self.assertEqual(build.report["status"], "partial", "冲突必须让批次变成 partial")
        self.assertEqual(int(build.exit_code), 1)
        for item in conflict_records:
            self.assertIn("保留先出现", item["reason"])

    def test_reconciliation_guard_stops_double_counted_conflict(self):
        """守卫行为：同一行若被计两个状态，合计就不等于 input_total，构建必须停下。

        这是最后一道防线：转换器写错计数时宁可让构建失败，也不能产出一份
        数量对不上的报告。冲突只记一个状态的口径另见 ``test_review_fixes``。
        """

        outcome = DatasetOutcome(
            dataset=DatasetId.P14_TARIFF, status=DatasetStatus.PROVIDED, input_total=1
        )
        outcome.count(RecordStatus.IMPORTED)
        outcome.count(RecordStatus.CONFLICT)
        with self.assertRaises(SnapshotFailed) as caught:
            _check_count_reconciliation([outcome], [])
        self.assertIn("p14_tariff", str(caught.exception))
        self.assertIn("计数对不上", str(caught.exception))


class RejectedRowTest(P23TestCase):
    """坏行只影响自己：编码失真/非法日期/格式不符 → 该行 rejected，其余照常导入。"""

    def test_distorted_code_rejects_only_that_row(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[
                IndexRow("101210010", "合成样本失真", numeric=True),
                IndexRow("9999999941", "合成样本正常"),
            ],
            element_rows=[ElementRow("9999999941", "合成样本正常", "合成要素甲", "1")],
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        tariff = by_name["p14_tariff"]["counts"]
        self.assertEqual(tariff["rejected"], 2, "两个来源里同一行失真，各拒绝一次")
        self.assertEqual(tariff["imported"], 1)
        self.assertEqual(tariff["output_records"], 1)
        self.assertEqual(build.report["status"], "partial")
        self.assertEqual(int(build.exit_code), 1)
        rejected = [
            item for item in build.report["records"]
            if item["dataset"] == "p14_tariff" and item["status"] == "rejected"
        ]
        self.assertEqual(len(rejected), 2)
        for item in rejected:
            self.assertEqual([issue["code"] for issue in item["issues"]],
                             ["HS_CELL_NUMERIC_PRECISION_LOST"])
            self.assertIn("101210010", item["reason"], "报告必须回显原始数值")
        codes = json.loads((build.snapshot_dir / "tariff.json").read_text(encoding="utf-8"))["codes"]
        self.assertEqual([item["code"] for item in codes], ["9999999941"],
                         "失真编码不得进入输出")

    def test_invalid_flag_rejects_whole_element_schema(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[
                IndexRow("9999999921", "合成样本坏标记", element_count=2, required_count=1),
                IndexRow("9999999922", "合成样本好清单", element_count=1, required_count=1),
            ],
            element_rows=[
                ElementRow("9999999921", "合成样本坏标记", "合成要素甲", "1"),
                ElementRow("9999999921", "合成样本坏标记", "合成要素乙", "2"),
                ElementRow("9999999922", "合成样本好清单", "合成要素丙", "1"),
            ],
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        elements = by_name["p14_elements"]["counts"]
        self.assertEqual(elements["rejected"], 4, "整份清单拒绝：2 行 × 2 个来源")
        self.assertEqual(elements["imported"], 1)
        self.assertEqual(elements["output_records"], 1)
        schemas = json.loads((build.snapshot_dir / "elements.json").read_text(encoding="utf-8"))["schemas"]
        self.assertEqual([item["hs_code"] for item in schemas], ["9999999922"],
                         "被拒绝的整份清单不得出现在输出里")
        rejected = [
            item for item in build.report["records"]
            if item["dataset"] == "p14_elements" and item["status"] == "rejected"
        ]
        self.assertTrue(
            any("ELEMENT_SCHEMA_REJECTED" in {issue["code"] for issue in item["issues"]}
                for item in rejected),
            "必须给出整份清单被拒绝的原因码",
        )
        self.assertTrue(
            any("ELEMENT_FLAG_INVALID" in {issue["code"] for issue in item["issues"]}
                for item in rejected),
            "必须给出非法必填标记的行级原因",
        )
        # 整份清单被拒绝后，目录侧“应有清单但明细缺失”记 pending，而不是偷偷补空清单。
        pending = [
            item for item in build.report["records"]
            if item["dataset"] == "p14_elements" and item["status"] == "pending"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual([issue["code"] for issue in pending[0]["issues"]],
                         ["ELEMENT_SCHEMA_REJECTED"],
                         "被整份拒绝的清单要报“被拒绝”，不能说“明细里没有这一行”")
        self.assertEqual(elements["pending"], 1)
        self.assertEqual(build.report["status"], "partial")
        self.assertEqual(int(build.exit_code), 1)

    def test_tax_rows_with_invalid_date_or_format_are_rejected(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[
                IndexRow("9999999931", "合成样本正常税率"),
                IndexRow("9999999932", "合成样本坏日期"),
                IndexRow("9999999933", "合成样本坏格式"),
            ],
            element_rows=[
                ElementRow("9999999931", "合成样本正常税率", "合成要素甲", "1"),
                ElementRow("9999999932", "合成样本坏日期", "合成要素乙", "1"),
                ElementRow("9999999933", "合成样本坏格式", "合成要素丙", "1"),
            ],
            tax_rows=[
                TaxRow("9999999931", "合成样本正常税率", "0.13", percent=True),
                TaxRow("9999999932", "合成样本坏日期", "0.13", effective_from="2026-13-01"),
                TaxRow("9999999933", "合成样本坏格式", "0.13", percent=None),
            ],
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        tax = by_name["p17_tax_rates"]["counts"]
        self.assertEqual(tax["rejected"], 2)
        self.assertEqual(tax["imported"], 1)
        self.assertEqual(tax["output_records"], 1)
        records = {
            item["business_key"]: item for item in build.report["records"]
            if item["dataset"] == "p17_tax_rates"
        }
        self.assertIn("TAX_EFFECTIVE_FROM_INVALID",
                      {issue["code"] for issue in records["9999999932"]["issues"]})
        self.assertIn("2026-13-01", records["9999999932"]["reason"])
        self.assertIn("TAX_RATE_FORMAT_UNVERIFIED",
                      {issue["code"] for issue in records["9999999933"]["issues"]})
        rates = json.loads((build.snapshot_dir / "tax_rates.json").read_text(encoding="utf-8"))["rates"]
        self.assertEqual([item["hs_code"] for item in rates], ["9999999931"])
        self.assertAlmostEqual(rates[0]["rate_percent"], 13.0)
        self.assertEqual(build.report["status"], "partial")
        self.assertEqual(int(build.exit_code), 1)

    def test_subheading_code_is_recorded_but_pending(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[IndexRow("99999999", "合成样本八位", element_count=1, required_count=1)],
            element_rows=[ElementRow("99999999", "合成样本八位", "合成要素八位", "1")],
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        tariff = by_name["p14_tariff"]["counts"]
        self.assertEqual(tariff["pending"], 1, "8 位子目写入但必须记待核")
        self.assertEqual(tariff["output_records"], 1, "记录仍写入快照")
        codes = json.loads((build.snapshot_dir / "tariff.json").read_text(encoding="utf-8"))["codes"]
        self.assertEqual([item["code"] for item in codes], ["99999999"])
        self.assertIn("8 位", codes[0]["note"], "记录本身必须写明只有 8 位")
        pending = [
            item for item in build.report["records"]
            if item["dataset"] == "p14_tariff" and item["status"] == "pending"
        ]
        self.assertEqual(len(pending), 1)
        self.assertEqual([issue["code"] for issue in pending[0]["issues"]], ["HS_SUBHEADING_ONLY"])

    def test_missing_declared_input_is_input_missing(self):
        batch = build_core_batch(self.root / "src")

        def mutate(payload):
            for item in payload["datasets"]:
                if item["dataset"] == "p17_tax_rates":
                    item["inputs"] = ["MX"]
            payload["inputs"].append(
                {
                    "source_id": "MX",
                    "kind": "hs_element_tax_rate_workbook",
                    "repository_path": "mappings/合成_不存在的原件.xlsx",
                    "sha256": "0" * 64,
                    "bytes": 123,
                }
            )

        path = mutate_manifest(batch, mutate, name="missing-input.json")
        build = build_snapshot(SnapshotRequest(manifest_path=path, out_root=batch.out_root))
        by_name = {item["dataset"]: item for item in build.report["datasets"]}
        self.assertEqual(by_name["p17_tax_rates"]["status"], "input_missing")
        self.assertTrue(by_name["p17_tax_rates"]["reason"])
        self.assertTrue(
            any("退税率缺失原件" in note for note in build.report["notes"]),
            "报告 notes 必须说明缺了哪一类原件",
        )
        self.assertEqual(int(build.exit_code), 1, "缺失输入属于 partial，不是 success")
        self.assertFalse((build.snapshot_dir / "tax_rates.json").exists())


class AtomicBuildTest(P23TestCase):
    """失败构建不留半份快照，既有快照仍可读。"""

    def test_failed_build_leaves_no_partial_snapshot(self):
        first = build_publish_batch(self.root / "a", label="A")
        out_root = self.root / "out"
        built = build_snapshot(
            SnapshotRequest(manifest_path=first.manifest, out_root=out_root)
        )
        old_report = built.report_path.read_text(encoding="utf-8")

        second = build_publish_batch(
            self.root / "b", label="B", name_suffix="（内容变更 B）"
        )
        with patch.object(
            snapshot_module,
            "verify_sources_unchanged",
            return_value=["M01：导入后 SHA-256 变化（合成样本）"],
        ):
            with self.assertRaises(SnapshotFailed) as caught:
                build_snapshot(
                    SnapshotRequest(manifest_path=second.manifest, out_root=out_root)
                )
        self.assertIn("导入后原件发生变化", str(caught.exception))

        candidates = sorted(
            entry.name for entry in (out_root / "candidates").iterdir()
        )
        self.assertEqual(candidates, [built.snapshot_dir.name],
                         "失败的构建不得留下半个快照目录或临时目录")
        self.assertFalse(
            any(name.startswith(".tmp-") for name in candidates),
            "临时目录必须被清掉",
        )
        self.assertEqual(built.report_path.read_text(encoding="utf-8"), old_report,
                         "既有快照的报告必须原封不动")
        payload = json.loads(built.report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["snapshot_id"], built.snapshot_id)
        self.assertEqual(len(payload["datasets"]), 6, "报告里六类数据类别都要有成对声明")

    def test_failed_build_does_not_touch_older_release_data(self):
        """失败的构建只动 candidates/，已发布的 release 目录与指针不变。"""

        batch = build_publish_batch(self.root / "c", label="C")
        out_root = self.root / "out"
        built = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=out_root)
        )
        write_verification(out_root, built.snapshot_id)
        published = publish_release(
            PublishRequest(snapshot_dir=built.snapshot_dir, out_root=out_root)
        )
        release_file = published.release_dir / "release.json"
        before = release_file.read_text(encoding="utf-8")

        second = build_publish_batch(
            self.root / "d", label="D", name_suffix="（内容变更 D）"
        )
        with patch.object(
            snapshot_module,
            "verify_sources_unchanged",
            return_value=["M01：导入后 SHA-256 变化（合成样本）"],
        ):
            with self.assertRaises(SnapshotFailed):
                build_snapshot(
                    SnapshotRequest(manifest_path=second.manifest, out_root=out_root)
                )
        self.assertEqual(release_file.read_text(encoding="utf-8"), before)
        self.assertTrue((out_root / "published" / "current.json").is_file())
        self.assertEqual(
            sorted(entry.name for entry in (out_root / "candidates").iterdir()),
            [built.snapshot_dir.name],
        )
