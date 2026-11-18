"""P15 汇总表导入：结构识别、映射、身份、冲突、图片、原子发布与 P15 查询。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from adapters.files.ooxml import Workbook
from adapters.sources.history_consolidated import (
    FORMAT_CONSOLIDATED,
    FORMAT_LEGACY,
    ConsolidatedFormatError,
    detect_workbook_format,
    parse_consolidated_workbook,
)
from adapters.sources.history_import import (
    MISSING_DATE,
    ImportIncompleteError,
    ImportRequest,
    _merge_consolidated_records,
    _publish_staging,
    import_history_templates,
)
from adapters.sources.history_search import HistoryQuery, HistorySearchSource
from tests.p04.fixtures import Package, png
from tests.p15.consolidated_fixtures import (
    BASE_ROW,
    CONSOLIDATED_HEADERS,
    build_consolidated_workbook,
    build_minimal_workbook,
)

BASE_DIR = Path(settings.BASE_DIR)
REAL_FILE = Path("/Users/jaychen/Downloads/正确底表/历史成功数据.xlsx")


def _request(tmp: Path, files, **overrides) -> ImportRequest:
    request = ImportRequest(
        files=[Path(item) for item in files],
        out_dir=tmp / "out",
        dataset_kind="synthetic",
        declared_success=False,
        declared_by="测试",
        declaration_note="合成汇总表声明",
        input_format=FORMAT_CONSOLIDATED,
        skip_validation=False,
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


def _records(outcome) -> list[dict]:
    return json.loads(outcome.records_path.read_text(encoding="utf-8"))["records"]


def _by_row(outcome) -> dict[str, dict]:
    return {record["attributes"]["source_row"]: record for record in _records(outcome)}


class ConsolidatedStructureTest(SimpleTestCase):
    def test_header_mapping_and_text_semantics(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "consolidated.xlsx",
                rows=[
                    dict(
                        BASE_ROW,
                        purchase_contract_no="SYN-P-001",
                        contract_no="'SYN-S-001",
                        hs="7307210000",
                    )
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            record = _records(outcome)[0]
            self.assertEqual(record["historical_name"], "合成法兰盘")
            self.assertEqual(record["historical_hs"], "7307210000")
            self.assertEqual(record["attributes"]["purchase_contract_no"], "SYN-P-001")
            self.assertEqual(record["attributes"]["sales_contract_no"], "SYN-S-001")
            # 文本前导单引号是 Excel 文本标记：业务值去掉，raw 保留。
            self.assertEqual(record["attributes"]["contract_no"], "SYN-P-001")
            self.assertEqual(record["attributes"]["tax_classification_code"], "1090000000000000000")
            # 规格/材质从真实申报要素键值提取，不从品名编造。
            self.assertEqual(record["material"], "不锈钢")
            self.assertEqual(record["spec"], "DN50")
            # 汇总表没有申报日期列：沿用既有“未提供”语义，不推断。
            self.assertEqual(record["recorded_at"], MISSING_DATE)
            self.assertEqual(record["attributes"]["recorded_at_missing"], "true")
            self.assertTrue(record["attributes"]["source_file_sha256"])
            self.assertIn("Sheet1!第2行", record["source"]["locator"])
            mapping = outcome.report["files"][0]["mapping"]["columns"]
            self.assertEqual(mapping["name"]["header"], "中文品名")
            self.assertEqual(mapping["hs"]["header"], "HS 编码")
            self.assertEqual(mapping["container_no"]["column"], "K")

    def test_blank_total_note_and_repeated_header_rows_are_reported(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "rows.xlsx",
                rows=[dict(BASE_ROW)],
                extra_rows=[
                    {"values": ()},
                    {"values": ("合计",)},
                    {"values": ("说明：仅用于测试",)},
                    {"values": ("中文品名", "HS 编码", "申报要素")},
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(outcome.report["totals"]["source_records"], 1)
            self.assertEqual(outcome.report["totals"]["records_total_in_output"], 1)
            kinds = outcome.report["files"][0]["row_classification"]
            self.assertEqual(kinds.get("blank_row", 0), 1)
            self.assertEqual(kinds.get("total_row", 0), 1)
            self.assertEqual(kinds.get("note_row", 0), 1)
            self.assertEqual(kinds.get("repeated_header", 0), 1)
            self.assertEqual(outcome.report["totals"]["records_ignored"], 4)

    def test_missing_required_field_is_rejected_but_reported(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "missing-hs.xlsx",
                rows=[dict(BASE_ROW, hs="")],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(outcome.report["totals"]["source_records"], 1)
            self.assertEqual(outcome.report["totals"]["records_rejected"], 1)
            self.assertEqual(outcome.report["totals"]["records_total_in_output"], 0)
            row = outcome.report["files"][0]["row_reports"][0]
            self.assertEqual(row["status"], "skipped")
            self.assertIn("missing_hs", [issue["code"] for issue in row["issues"]])

    def test_duplicate_required_column_is_refused(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            path = build_minimal_workbook(
                tmp / "dup.xlsx",
                headers=("中文品名", "HS 编码", "中文品名"),
                rows=[{"name": "合成品", "hs": "7307210000"}],
            )
            workbook = Workbook.open(path)
            with self.assertRaises(ConsolidatedFormatError) as caught:
                parse_consolidated_workbook(
                    workbook,
                    path=path,
                    source_id="dup",
                    original_filename=path.name,
                )
            self.assertEqual(caught.exception.code, "required_column_ambiguous")

    def test_multiple_header_rows_are_refused_without_explicit_choice(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            package = Package()
            sheet = package.sheet("Sheet1")
            for row in (1, 2):
                for index, header in enumerate(("中文品名", "HS 编码", "申报要素"), start=1):
                    sheet.text(f"{chr(64 + index)}{row}", header)
            sheet.text("A3", "合成品")
            sheet.text("B3", "7307210000")
            path = tmp / "double-header.xlsx"
            package.build(path)
            workbook = Workbook.open(path)
            with self.assertRaises(ConsolidatedFormatError) as caught:
                parse_consolidated_workbook(
                    workbook, path=path, source_id="double", original_filename=path.name
                )
            self.assertEqual(caught.exception.code, "header_row_ambiguous")

    def test_auto_detection_refuses_single_sheet_ambiguity(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            path = build_consolidated_workbook(tmp / "ambiguous.xlsx", sheet_name="申报要素")
            workbook = Workbook.open(path)
            with self.assertRaises(ConsolidatedFormatError) as caught:
                detect_workbook_format(workbook)
            self.assertIn(caught.exception.code, {"input_format_ambiguous", "input_format_unknown"})
            # 自动识别拒绝“猜”；显式选择后仍可导入，说明问题在识别入口而非文件。
            outcome = import_history_templates(
                _request(tmp, [path], input_format=FORMAT_CONSOLIDATED)
            )
            self.assertEqual(outcome.report["input_format"], FORMAT_CONSOLIDATED)

    def test_hidden_column_and_row_are_reported(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "hidden.xlsx",
                rows=[dict(BASE_ROW)],
                hidden_columns=(5,),
                hidden_rows=(2,),
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            issues = [issue["code"] for issue in outcome.report["files"][0]["task_issues"]]
            self.assertIn("hidden_column", issues)
            self.assertIn("hidden_rows_present", issues)
            record = _records(outcome)[0]
            self.assertEqual(record["attributes"]["needs_review"], "true")
            self.assertIn("hidden_row", record["attributes"]["review_issue_codes"])


class ConsolidatedIdentityTest(SimpleTestCase):
    def test_reimport_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "one.xlsx")
            first = import_history_templates(_request(tmp, [workbook]))
            first_ids = [record["record_id"] for record in _records(first)]
            second = import_history_templates(_request(tmp, [workbook]))
            second_ids = [record["record_id"] for record in _records(second)]
            self.assertEqual(first_ids, second_ids)
            self.assertEqual(second.report["totals"]["records_added"], 0)
            self.assertEqual(second.report["totals"]["records_duplicate"], len(first_ids))
            self.assertEqual(second.report["totals"]["records_total_in_output"], len(first_ids))
            self.assertTrue(second.report["totals"]["reconciliation_ok"])

    def test_sort_and_insert_do_not_change_identity(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            row_a = dict(BASE_ROW, name="合成甲", hs="7307210000", declaration_no="D-A")
            row_b = dict(BASE_ROW, name="合成乙", hs="7307990000", declaration_no="D-B")
            row_c = dict(
                BASE_ROW,
                name="合成丙",
                hs="7307930000",
                declaration_no="D-C",
                elements="0|1|材质：碳钢|规格：DN100",
            )
            first = build_consolidated_workbook(tmp / "one.xlsx", rows=[row_a, row_b, row_c])
            first_out = import_history_templates(_request(tmp, [first]))
            first_ids = {record["record_id"] for record in _records(first_out)}

            sorted_workbook = build_consolidated_workbook(
                tmp / "one.xlsx", rows=[row_c, row_a, row_b]
            )
            second_out = import_history_templates(_request(tmp, [sorted_workbook]))
            second_records = _records(second_out)
            self.assertTrue(first_ids.issubset({record["record_id"] for record in second_records}))
            self.assertEqual(second_out.report["totals"]["records_added"], 0)
            self.assertEqual(second_out.report["totals"]["records_total_in_output"], 3)

            inserted = dict(BASE_ROW, name="合成新增", hs="7307990000", declaration_no="D-NEW")
            inserted_workbook = build_consolidated_workbook(
                tmp / "one.xlsx", rows=[row_c, row_a, inserted, row_b]
            )
            third_out = import_history_templates(_request(tmp, [inserted_workbook]))
            self.assertEqual(third_out.report["totals"]["records_added"], 1)
            self.assertEqual(third_out.report["totals"]["records_duplicate"], 3)
            self.assertEqual(third_out.report["totals"]["records_total_in_output"], 4)

    def test_single_row_modification_reports_changed_and_keeps_old(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            original = build_consolidated_workbook(
                tmp / "one.xlsx", rows=[dict(BASE_ROW, name="合成甲", declaration_no="D-A")]
            )
            first = import_history_templates(_request(tmp, [original]))
            old_record = _records(first)[0]

            modified = build_consolidated_workbook(
                tmp / "one.xlsx",
                rows=[dict(BASE_ROW, name="合成甲改", declaration_no="D-A")],
            )
            changed = import_history_templates(_request(tmp, [modified]))
            self.assertEqual(changed.report["totals"]["records_changed"], 1)
            self.assertEqual(changed.report["totals"]["records_added"], 0)
            self.assertEqual(changed.report["totals"]["records_total_in_output"], 1)
            self.assertEqual(_records(changed)[0]["historical_name"], old_record["historical_name"])
            action = changed.report["duplicates"][0]["action"]
            self.assertEqual(action, "kept_existing")

            request = _request(tmp, [modified])
            request.replace_changed = True
            replaced = import_history_templates(request)
            self.assertEqual(replaced.report["totals"]["records_replaced"], 1)
            self.assertEqual(replaced.report["totals"]["records_total_in_output"], 1)
            self.assertEqual(_records(replaced)[0]["historical_name"], "合成甲改")

    def test_delete_row_reports_missing_and_keeps_old(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            row_a = dict(BASE_ROW, name="合成甲", declaration_no="D-A")
            row_b = dict(BASE_ROW, name="合成乙", declaration_no="D-B")
            first = build_consolidated_workbook(tmp / "two.xlsx", rows=[row_a, row_b])
            import_history_templates(_request(tmp, [first]))

            second = build_consolidated_workbook(tmp / "two.xlsx", rows=[row_a])
            outcome = import_history_templates(_request(tmp, [second]))
            self.assertEqual(outcome.report["totals"]["records_missing_from_source"], 1)
            self.assertEqual(outcome.report["totals"]["records_total_in_output"], 2)
            self.assertEqual(outcome.report["totals"]["records_added"], 0)
            missing = outcome.report["missing_from_source"][0]
            self.assertEqual(missing["kind"], "missing_from_source")
            self.assertEqual(missing["action"], "kept")

    def test_same_hs_and_name_different_spec_or_material_are_not_merged(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "variants.xlsx",
                rows=[
                    dict(
                        BASE_ROW,
                        name="合成法兰盘",
                        hs="7307210000",
                        elements="0|1|材质：不锈钢|规格：DN50",
                        declaration_no="D-1",
                    ),
                    dict(
                        BASE_ROW,
                        name="合成法兰盘",
                        hs="7307210000",
                        elements="0|1|材质：不锈钢|规格：DN80",
                        declaration_no="D-2",
                    ),
                    dict(
                        BASE_ROW,
                        name="合成法兰盘",
                        hs="7307210000",
                        elements="0|1|材质：碳钢|规格：DN50",
                        declaration_no="D-3",
                    ),
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            records = _records(outcome)
            self.assertEqual(len(records), 3)
            self.assertEqual(len({record["record_id"] for record in records}), 3)
            self.assertEqual(outcome.report["totals"]["records_duplicate_business_content"], 0)

    def test_exact_duplicate_rows_are_kept_with_occurrence_and_reported(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "dupe.xlsx", rows=[dict(BASE_ROW), dict(BASE_ROW)]
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            records = _records(outcome)
            self.assertEqual(len(records), 2)
            self.assertNotEqual(records[0]["record_id"], records[1]["record_id"])
            self.assertEqual(
                sorted(record["attributes"]["content_occurrence"] for record in records),
                ["0", "1"],
            )
            self.assertEqual(outcome.report["files"][0]["data_quality"]["duplicate_content_groups"], 1)
            self.assertEqual(
                outcome.report["files"][0]["data_quality"]["duplicate_content_extra_rows"], 1
            )
            second = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(second.report["totals"]["records_added"], 0)
            self.assertEqual(second.report["totals"]["records_duplicate"], 2)

    def test_unique_original_record_id_is_preferred(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("原始记录ID",)
            first = build_consolidated_workbook(
                tmp / "id.xlsx",
                headers=headers,
                rows=[dict(BASE_ROW, original_record_id="ORIG-1")],
            )
            first_out = import_history_templates(_request(tmp, [first]))
            record = _records(first_out)[0]
            self.assertEqual(record["attributes"]["identity_basis"], "original_record_id")
            first_id = record["record_id"]

            changed = build_consolidated_workbook(
                tmp / "id.xlsx",
                headers=headers,
                rows=[dict(BASE_ROW, original_record_id="ORIG-1", name="合成改名")],
            )
            second_out = import_history_templates(_request(tmp, [changed]))
            self.assertEqual(_records(second_out)[0]["record_id"], first_id)
            self.assertEqual(second_out.report["totals"]["records_changed"], 1)

    def test_duplicate_original_record_id_falls_back_with_warning(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("原始记录ID",)
            workbook = build_consolidated_workbook(
                tmp / "dup-id.xlsx",
                headers=headers,
                rows=[
                    dict(BASE_ROW, original_record_id="SAME", declaration_no="D-1"),
                    dict(BASE_ROW, original_record_id="SAME", declaration_no="D-2"),
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(len(_records(outcome)), 2)
            self.assertEqual(_records(outcome)[0]["attributes"]["identity_basis"], "content")
            codes = [issue["code"] for issue in outcome.report["files"][0]["task_issues"]]
            self.assertIn("original_record_id_not_unique", codes)

    def test_conflict_keeps_existing_by_default_and_can_keep_both(self):
        existing = {
            "records": [
                {
                    "record_id": "HISTC-OLD",
                    "historical_name": "合成品",
                    "attributes": {
                        "source_file_id": "src",
                        "source_format": FORMAT_CONSOLIDATED,
                        "change_key": "old-change",
                        "business_key": "same-business",
                        "source_row": "2",
                    },
                }
            ]
        }
        incoming = {
            "record_id": "HISTC-NEW",
            "historical_name": "合成品",
            "attributes": {
                "source_file_id": "src",
                "source_format": FORMAT_CONSOLIDATED,
                "change_key": "different-change",
                "business_key": "same-business",
                "source_row": "9",
            },
        }
        final, _, writes, conflicts, missing, actions, added = _merge_consolidated_records(
            existing,
            [incoming],
            replace_changed=False,
            keep_business_duplicates=False,
            accept_conflicts=False,
            current_source_ids=set(),
        )
        self.assertEqual(len(final), 1)
        self.assertEqual(writes, [])
        self.assertEqual(added, set())
        self.assertEqual(conflicts[0]["action"], "kept_existing")
        self.assertEqual(actions["HISTC-NEW"], "conflict_kept_existing")
        self.assertEqual(missing, [])

        final, _, writes, conflicts, _, _, added = _merge_consolidated_records(
            existing,
            [incoming],
            replace_changed=False,
            keep_business_duplicates=False,
            accept_conflicts=True,
            current_source_ids=set(),
        )
        self.assertEqual(len(final), 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(conflicts[0]["action"], "kept_both")
        self.assertIn("HISTC-NEW", added)


class ConsolidatedQualityTest(SimpleTestCase):
    def test_formula_without_cache_is_reported_and_required_field_rejected(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            package = Package()
            sheet = package.sheet("Sheet1")
            for index, header in enumerate(("中文品名", "HS 编码", "申报要素"), start=1):
                sheet.text(f"{chr(64 + index)}1", header)
            sheet.text("A2", "合成品")
            sheet.formula("B2", "=VLOOKUP(A2,Data!A:B,2,0)")  # 无缓存，不执行
            sheet.text("C2", "材质：塑料")
            path = tmp / "formula.xlsx"
            package.build(path)
            outcome = import_history_templates(_request(tmp, [path]))
            self.assertEqual(outcome.report["totals"]["records_rejected"], 1)
            row = outcome.report["files"][0]["row_reports"][0]
            codes = [issue["code"] for issue in row["issues"]]
            self.assertIn("formula_cache_missing_hs", codes)
            self.assertIn("missing_hs", codes)

    def test_formula_with_cache_is_used_but_formula_is_reported(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            package = Package()
            sheet = package.sheet("Sheet1")
            for index, header in enumerate(("中文品名", "HS 编码", "申报要素"), start=1):
                sheet.text(f"{chr(64 + index)}1", header)
            sheet.text("A2", "合成品")
            sheet.formula("B2", "=1+2", "7307210000")
            sheet.text("C2", "材质：塑料")
            path = tmp / "formula-cache.xlsx"
            package.build(path)
            outcome = import_history_templates(_request(tmp, [path]))
            record = _records(outcome)[0]
            self.assertEqual(record["historical_hs"], "7307210000")
            self.assertIn("formula_cache_used_hs", record["attributes"]["import_issue_codes"])

    def test_merged_required_field_is_not_filled_down(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "merged-name.xlsx",
                rows=[
                    dict(BASE_ROW, name="合成合并品", declaration_no="D-1"),
                    dict(BASE_ROW, name="", declaration_no="D-2"),
                ],
                merged_ranges=("E2:E3",),
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(outcome.report["totals"]["source_records"], 2)
            self.assertEqual(outcome.report["totals"]["records_rejected"], 1)
            row_reports = outcome.report["files"][0]["row_reports"]
            second = next(row for row in row_reports if row["source"]["row"] == 3)
            codes = [issue["code"] for issue in second["issues"]]
            self.assertIn("merged_continuation_name", codes)
            self.assertIn("missing_name", codes)
            # 第一条记录的名称不应被复制到第二条。
            self.assertEqual(len(_records(outcome)), 1)

    def test_merged_container_anchor_only_and_continuation_stays_empty(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "merged-container.xlsx",
                rows=[
                    dict(BASE_ROW, declaration_no="D-1", container_no="SYN-BOX-MERGED"),
                    dict(BASE_ROW, declaration_no="D-2", container_no=""),
                ],
                merged_ranges=("K2:K3",),
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            records = _by_row(outcome)
            self.assertEqual(records["2"]["attributes"]["container_no"], "SYN-BOX-MERGED")
            self.assertEqual(records["2"]["attributes"]["container_raw"], "SYN-BOX-MERGED")
            self.assertEqual(records["3"]["attributes"]["container_no"], "")
            self.assertEqual(records["3"]["attributes"]["container_raw"], "")
            self.assertIn(
                "merged_continuation_container_no",
                records["3"]["attributes"]["import_issue_codes"],
            )

    def test_hs_anomalies_and_numeric_precision_are_reported_not_fixed(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(
                tmp / "hs.xlsx",
                rows=[
                    dict(BASE_ROW, hs="853630000", declaration_no="D-1"),
                    dict(
                        BASE_ROW,
                        name="合成精度件",
                        hs="7307210000",
                        tax_classification_code="1.080207130000000E+18",
                        __kind_tax_classification_code="number",
                        declaration_no="D-2",
                    ),
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            records = _by_row(outcome)
            self.assertEqual(records["2"]["historical_hs"], "853630000")
            self.assertEqual(records["2"]["attributes"]["needs_review"], "true")
            self.assertIn("hs_length_not_10", records["2"]["attributes"]["review_issue_codes"])
            self.assertIn("hs_length_not_10", records["2"]["attributes"]["review_issue_codes"])
            self.assertIn(
                "numeric_precision_risk_tax_classification_code",
                records["3"]["attributes"]["import_issue_codes"],
            )
            self.assertEqual(records["3"]["attributes"]["tax_classification_code"], "1.080207130000000E+18")
            quality = outcome.report["files"][0]["data_quality"]
            self.assertEqual(quality["hs"]["lengths"]["9"], 1)
            self.assertEqual(quality["hs"]["lengths"]["10"], 1)

    def test_image_column_missing_is_not_absent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "no-image.xlsx")
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(
                outcome.report["totals"]["image_status"],
                {"column_missing": 2},
            )
            self.assertTrue(
                all(
                    record["attributes"]["image_status"] == "column_missing"
                    for record in _records(outcome)
                )
            )

    def test_dispimg_image_is_available_and_copied(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "image.xlsx", with_image=True)
            outcome = import_history_templates(_request(tmp, [workbook], copy_images=True))
            records = _records(outcome)
            available = [
                record for record in records if record["attributes"]["image_status"] == "available"
            ]
            self.assertEqual(len(available), 2)
            for record in available:
                path = outcome.out_dir / record["attributes"]["image_output_path"]
                self.assertTrue(path.is_file())
                self.assertTrue(record["attributes"]["image_sha256"])
            self.assertEqual(outcome.report["totals"]["image_status"]["available"], 2)

    def test_external_url_is_not_fetched_and_external_path_needs_root(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("图片路径",)
            workbook = build_consolidated_workbook(
                tmp / "external.xlsx",
                headers=headers,
                rows=[
                    dict(BASE_ROW, image="https://example.invalid/a.png", declaration_no="D-1"),
                    dict(BASE_ROW, image="sub/a.png", declaration_no="D-2"),
                ],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            records = _by_row(outcome)
            self.assertEqual(
                records["2"]["attributes"]["image_status"], "external_url_not_fetched"
            )
            self.assertEqual(
                records["3"]["attributes"]["image_status"], "external_path_not_enabled"
            )
            self.assertEqual(outcome.report["totals"]["image_status"]["external_url_not_fetched"], 1)
            self.assertEqual(
                outcome.report["totals"]["image_status"]["external_path_not_enabled"], 1
            )

            root = tmp / "images-root"
            (root / "sub").mkdir(parents=True)
            (root / "sub" / "a.png").write_bytes(png(b"\x01\x02\x03"))
            allowed = import_history_templates(
                _request(tmp, [workbook], image_root=root, copy_images=True)
            )
            allowed_records = _by_row(allowed)
            self.assertEqual(allowed_records["3"]["attributes"]["image_status"], "available")
            self.assertTrue(
                (allowed.out_dir / allowed_records["3"]["attributes"]["image_output_path"]).is_file()
            )

    def test_external_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("图片路径",)
            workbook = build_consolidated_workbook(
                tmp / "traversal.xlsx",
                headers=headers,
                rows=[dict(BASE_ROW, image="../secret.png")],
            )
            root = tmp / "images-root"
            root.mkdir()
            (tmp / "secret.png").write_bytes(png())
            outcome = import_history_templates(
                _request(tmp, [workbook], image_root=root)
            )
            record = _records(outcome)[0]
            self.assertEqual(record["attributes"]["image_status"], "external_path_unsafe")
            self.assertIn(
                "image_external_path_unsafe", record["attributes"]["import_issue_codes"]
            )


    def test_numeric_date_serial_is_not_converted(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("申报日期",)
            workbook = build_consolidated_workbook(
                tmp / "date.xlsx",
                headers=headers,
                rows=[dict(BASE_ROW, recorded_at="45000", __kind_recorded_at="number")],
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            record = _records(outcome)[0]
            self.assertEqual(record["recorded_at"], MISSING_DATE)
            self.assertIn(
                "recorded_at_numeric_serial", record["attributes"]["import_issue_codes"]
            )

    def test_duplicate_source_id_in_one_batch_is_refused(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            first = build_consolidated_workbook(tmp / "a" / "same.xlsx")
            second = build_consolidated_workbook(tmp / "b" / "same.xlsx")
            with self.assertRaises(ValueError):
                import_history_templates(_request(tmp, [first, second]))


    def test_auto_detection_honours_explicit_mapping(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = ("供应商", "英文品名", "商品名称", "海关编码", "报关单号")
            workbook = build_consolidated_workbook(
                tmp / "mapped.xlsx",
                headers=headers,
                rows=[dict(BASE_ROW, __header_fields={"商品名称": "name", "海关编码": "hs"})],
                extra_rows=(),
            )
            mapping = tmp / "mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "fields": {
                            "name": ["商品名称"],
                            "hs": ["海关编码"],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            outcome = import_history_templates(
                _request(tmp, [workbook], input_format="auto", mapping=mapping)
            )
            self.assertEqual(outcome.report["input_format"], FORMAT_CONSOLIDATED)
            self.assertEqual(outcome.report["totals"]["source_records"], 1)

    def test_image_duplicate_columns_report_ambiguous_not_missing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            headers = CONSOLIDATED_HEADERS + ("图片", "图片")
            workbook = build_consolidated_workbook(
                tmp / "ambiguous-image.xlsx", headers=headers
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            self.assertEqual(
                outcome.report["totals"]["image_status"],
                {"column_ambiguous": 2},
            )
            self.assertEqual(
                outcome.report["files"][0]["image"]["column_status"], "column_ambiguous"
            )

    def test_manifest_filename_change_updates_provenance_not_business_content(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            old = build_consolidated_workbook(tmp / "old.xlsx", rows=[dict(BASE_ROW)])
            new = build_consolidated_workbook(tmp / "new.xlsx", rows=[dict(BASE_ROW)])
            manifest_old = tmp / "manifest-old.json"
            manifest_old.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "source_id": "FIX",
                                "original_filename": "old.xlsx",
                                "sha256": __import__("hashlib").sha256(old.read_bytes()).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            first = import_history_templates(
                _request(tmp, [old], manifest=manifest_old)
            )
            manifest_new = tmp / "manifest-new.json"
            manifest_new.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "source_id": "FIX",
                                "original_filename": "new.xlsx",
                                "sha256": __import__("hashlib").sha256(new.read_bytes()).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            second = import_history_templates(
                _request(tmp, [new], manifest=manifest_new)
            )
            self.assertEqual(second.report["totals"]["records_changed"], 0)
            self.assertEqual(second.report["totals"]["records_duplicate"], 1)
            record = _records(second)[0]
            self.assertEqual(record["attributes"]["source_filename"], "new.xlsx")
            self.assertEqual(record["record_id"], _records(first)[0]["record_id"])

    def test_source_file_hash_is_unchanged_after_import(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "one.xlsx")
            before = __import__("hashlib").sha256(workbook.read_bytes()).hexdigest()
            outcome = import_history_templates(_request(tmp, [workbook]))
            after = __import__("hashlib").sha256(workbook.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(
                outcome.report["files"][0]["sha256"],
                outcome.report["files"][0]["sha256_after"],
            )
            self.assertTrue(outcome.report["files"][0]["sha256_unchanged"])


class AtomicPublishTest(SimpleTestCase):
    def test_publish_failure_rolls_back_old_files(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            staging = tmp / "staging"
            out = tmp / "out"
            staging.mkdir()
            out.mkdir()
            old_records = out / "history_records.json"
            old_report = out / "import_report.json"
            old_records.write_text("old-records", encoding="utf-8")
            old_report.write_text("old-report", encoding="utf-8")
            (staging / "history_records.json").write_text("new-records", encoding="utf-8")
            (staging / "import_report.json").write_text("new-report", encoding="utf-8")
            (staging / "import_report.md").write_text("new-md", encoding="utf-8")

            real_replace = os.replace
            calls = {"count": 0}

            def flaky_replace(source, target):
                calls["count"] += 1
                if calls["count"] == 3:
                    raise OSError("simulated publish failure")
                return real_replace(source, target)

            with mock.patch("adapters.sources.history_import.os.replace", side_effect=flaky_replace):
                with self.assertRaises(ImportIncompleteError):
                    _publish_staging(
                        staging,
                        out,
                        [
                            staging / "history_records.json",
                            staging / "import_report.json",
                            staging / "import_report.md",
                        ],
                    )
            self.assertEqual(old_records.read_text(encoding="utf-8"), "old-records")
            self.assertEqual(old_report.read_text(encoding="utf-8"), "old-report")
            self.assertFalse((out / "import_report.md").exists())

    def test_import_failure_before_publish_keeps_old_output(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "one.xlsx")
            first = import_history_templates(_request(tmp, [workbook]))
            old_records = first.records_path.read_text(encoding="utf-8")

            broken = tmp / "broken.xlsx"
            broken.write_bytes(b"not a zip")
            with self.assertRaises(ImportIncompleteError):
                import_history_templates(_request(tmp, [broken]))
            self.assertEqual(first.records_path.read_text(encoding="utf-8"), old_records)
            self.assertTrue(first.report_json_path.is_file())


class ConsolidatedP15IntegrationTest(SimpleTestCase):
    def test_output_loads_in_p15_and_query_behavior(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            row_hit = dict(
                BASE_ROW,
                name="合成法兰盘",
                hs="7307210000",
                elements="0|1|材质：不锈钢|用途：管道连接|规格：DN50",
                declaration_no="D-1",
            )
            row_diff = dict(
                BASE_ROW,
                name="合成法兰盘",
                hs="7307210000",
                elements="0|1|材质：碳钢|用途：管道连接|规格：DN80",
                declaration_no="D-2",
            )
            row_review = dict(
                BASE_ROW,
                name="合成待核件",
                hs="853630000",
                elements="0|1|材质：塑料",
                declaration_no="D-3",
            )
            workbook = build_consolidated_workbook(
                tmp / "p15.xlsx", rows=[row_hit, row_diff, row_review]
            )
            outcome = import_history_templates(_request(tmp, [workbook]))
            source = HistorySearchSource.load(outcome.records_path)
            hit = source.search(
                HistoryQuery(
                    name="合成法兰盘",
                    material="不锈钢",
                    spec="DN50",
                    elements={"材质": "不锈钢", "用途": "管道连接", "规格": "DN50"},
                )
            )
            self.assertEqual(hit.outcome.value, "matched")
            self.assertTrue(hit.candidates)
            self.assertTrue(all(candidate.reference_only for candidate in hit.candidates))
            self.assertTrue(all(not candidate.needs_review for candidate in hit.candidates))
            self.assertTrue(
                any(candidate.historical_hs == "7307210000" for candidate in hit.candidates)
            )

            diff = source.search(
                HistoryQuery(
                    name="合成法兰盘",
                    material="碳钢",
                    spec="DN80",
                    elements={"材质": "碳钢", "用途": "管道连接", "规格": "DN80"},
                )
            )
            self.assertEqual(diff.outcome.value, "matched")
            self.assertTrue(any(candidate.key_attribute_conflict for candidate in diff.candidates))

            review = source.search(HistoryQuery(name="合成待核件"))
            self.assertEqual(review.outcome.value, "no_match")
            audit = HistorySearchSource.load(outcome.records_path, include_needs_review=True)
            audited = audit.search(HistoryQuery(name="合成待核件"))
            self.assertEqual(audited.outcome.value, "matched")
            self.assertTrue(any(candidate.needs_review for candidate in audited.candidates))

            no_match = source.search(HistoryQuery(name="完全不存在的量子咖啡杯"))
            self.assertEqual(no_match.outcome.value, "no_match")
            self.assertEqual(no_match.evidence_status.value, "not_found")

    def test_legacy_import_still_works_with_explicit_format(self):
        from tests.p15.history_import_fixtures import build_synthetic_workbook

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_synthetic_workbook(tmp / "legacy.xlsx")
            outcome = import_history_templates(
                _request(tmp, [workbook], input_format=FORMAT_LEGACY)
            )
            self.assertEqual(outcome.report["input_format"], FORMAT_LEGACY)
            self.assertEqual(outcome.report["totals"]["source_records"], 4)
            self.assertEqual(outcome.report["totals"]["records_total_in_output"], 3)
            self.assertEqual(outcome.report["totals"]["records_skipped"], 1)

    def test_format_mixing_is_rejected(self):
        from tests.p15.history_import_fixtures import build_synthetic_workbook

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            consolidated = build_consolidated_workbook(tmp / "consolidated.xlsx")
            import_history_templates(_request(tmp, [consolidated]))
            legacy = build_synthetic_workbook(tmp / "legacy.xlsx")
            with self.assertRaises(ValueError):
                import_history_templates(
                    _request(tmp, [legacy], input_format=FORMAT_LEGACY)
                )

    def test_fresh_requires_explicit_dataset_replace(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook_a = build_consolidated_workbook(
                tmp / "a.xlsx", rows=[dict(BASE_ROW, name="合成甲")]
            )
            workbook_b = build_consolidated_workbook(
                tmp / "b.xlsx", rows=[dict(BASE_ROW, name="合成乙")]
            )
            import_history_templates(_request(tmp, [workbook_a]))
            with self.assertRaises(ValueError):
                import_history_templates(_request(tmp, [workbook_b], fresh=True))
            replaced = import_history_templates(
                _request(tmp, [workbook_b], fresh=True, allow_dataset_replace=True)
            )
            self.assertEqual(_records(replaced)[0]["historical_name"], "合成乙")

    def test_reimport_retaining_changed_record_keeps_original_hash_and_declaration_in_snapshot(self):
        import hashlib

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_consolidated_workbook(tmp / "real-style.xlsx", rows=[dict(BASE_ROW, hs="7307210000")])
            sha1 = hashlib.sha256(workbook.read_bytes()).hexdigest()
            first = import_history_templates(
                ImportRequest(
                    files=[workbook],
                    out_dir=tmp / "out",
                    dataset_kind="real",
                    declared_success=True,
                    declared_by="Jay",
                    declaration_note="初版人工声明",
                    input_format=FORMAT_CONSOLIDATED,
                    skip_validation=True,
                )
            )
            self.assertEqual(json.loads(first.records_path.read_text())["dataset"]["source_files"][0]["sha256"], sha1)

            build_consolidated_workbook(workbook, rows=[dict(BASE_ROW, hs="7307990000")])
            sha2 = hashlib.sha256(workbook.read_bytes()).hexdigest()
            self.assertNotEqual(sha1, sha2)
            second = import_history_templates(
                ImportRequest(
                    files=[workbook],
                    out_dir=tmp / "out",
                    dataset_kind="real",
                    declared_success=True,
                    declared_by="Reviewer2",
                    declaration_note="修正版人工声明",
                    input_format=FORMAT_CONSOLIDATED,
                    replace_changed=False,
                    skip_validation=True,
                )
            )
            snapshot = json.loads(second.records_path.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["records"][0]["historical_hs"], "7307210000")
            self.assertEqual(snapshot["records"][0]["attributes"]["source_file_sha256"], sha1)
            self.assertEqual(snapshot["dataset"]["source_files"][0]["sha256"], sha1)
            self.assertEqual(snapshot["dataset"]["human_declaration"]["declared_by"], "Jay")
            report = json.loads(second.report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["human_declaration"]["declared_by"], "Reviewer2")
            self.assertEqual(report["source_files"][0]["sha256"], sha2)
            self.assertEqual(report["input_dataset"]["human_declaration"]["declared_by"], "Reviewer2")
            self.assertEqual(report["input_dataset"]["source_files"][0]["sha256"], sha2)


@unittest.skipUnless(
    REAL_FILE.is_file(),
    "未找到真实汇总表（本地验收文件不在仓库内）",
)
class RealConsolidatedImportTest(SimpleTestCase):
    def test_real_file_readonly_import_counts_and_p15_query(self):
        import hashlib

        before = hashlib.sha256(REAL_FILE.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            outcome = import_history_templates(
                ImportRequest(
                    files=[REAL_FILE],
                    out_dir=tmp / "out",
                    dataset_kind="real",
                    declared_success=True,
                    declared_by="Jay",
                    input_format=FORMAT_CONSOLIDATED,
                    skip_validation=False,
                )
            )
            after = hashlib.sha256(REAL_FILE.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(outcome.report["input_format"], FORMAT_CONSOLIDATED)
            self.assertEqual(outcome.report["totals"]["source_records"], 1498)
            self.assertEqual(outcome.report["totals"]["records_total_in_output"], 1498)
            self.assertEqual(outcome.report["totals"]["records_rejected"], 0)
            self.assertEqual(outcome.report["totals"]["records_ignored"], 3)
            self.assertEqual(outcome.report["totals"]["records_needs_review"], 300)
            self.assertEqual(
                outcome.report["totals"]["image_status"], {"column_missing": 1498}
            )
            self.assertEqual(
                outcome.report["files"][0]["row_classification"],
                {"goods_row": 1498, "blank_row": 3},
            )
            records = _records(outcome)
            self.assertTrue(all(record["recorded_at"] == MISSING_DATE for record in records))
            self.assertTrue(all(record["hs_version"] is None for record in records))
            self.assertEqual(
                outcome.report["files"][0]["data_quality"]["merged_range_count"], 30
            )
            self.assertEqual(
                outcome.report["files"][0]["data_quality"]["merged_cell_count"], 335
            )
            source = HistorySearchSource.load(outcome.records_path)
            hit = source.search(HistoryQuery(name="焊接钢管"))
            self.assertEqual(hit.outcome.value, "matched")
            self.assertTrue(hit.candidates)
            no_match = source.search(HistoryQuery(name="完全不存在的量子咖啡杯"))
            self.assertEqual(no_match.outcome.value, "no_match")
            validation = json.loads(outcome.validation_json_path.read_text(encoding="utf-8"))
            self.assertTrue(validation["all_passed"], validation["checks"])
