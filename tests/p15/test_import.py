"""P15 历史模板导入：解析、稳定标识、重复导入、图片状态与查询验证。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from adapters.sources.history_import import (
    DEFAULT_DECLARATION_NOTE,
    IMAGE_COLUMN_MISSING,
    MISSING_DATE,
    ImportRequest,
    _merge_records,
    _record_id,
    import_history_templates,
    parse_element_pairs,
)
from tests.p15.history_import_fixtures import build_synthetic_workbook

BASE_DIR = Path(settings.BASE_DIR)
REAL_DIR = BASE_DIR / "data" / "import-basis" / "2026-11-17"
REAL_MANIFEST = REAL_DIR / "manifest.json"
REAL_FILES = sorted((REAL_DIR / "historical-success").glob("*.xlsx"))


class ElementParsingTest(SimpleTestCase):
    def test_indexed_real_style(self):
        elements, issues = parse_element_pairs(
            "0:品牌类型|境内自主品牌 1:出口享惠情况|不确定 "
            "2:用途（如微处理器）|用于电机散热 3:型号|GX/250A"
        )
        self.assertEqual(elements["品牌类型"], "境内自主品牌")
        self.assertEqual(elements["出口享惠情况"], "不确定")
        self.assertEqual(elements["用途（如微处理器）"], "用于电机散热")
        self.assertEqual(elements["型号"], "GX/250A")
        self.assertEqual([issue["code"] for issue in issues], [])

    def test_pipe_label_value_style(self):
        elements, issues = parse_element_pairs(
            "0｜1｜用途：用于轮胎硫化机｜材质：丁腈橡胶｜型号：12.00R20"
        )
        self.assertEqual(elements, {"用途": "用于轮胎硫化机", "材质": "丁腈橡胶", "型号": "12.00R20"})
        self.assertEqual(len(issues), 0)

    def test_duplicate_label_is_reported_not_silently_overwritten(self):
        elements, issues = parse_element_pairs("材质：不锈钢｜材质：碳钢")
        self.assertEqual(elements["材质"], "不锈钢")
        self.assertIn("element_duplicate_label", [issue["code"] for issue in issues])

    def test_unstructured_is_reported(self):
        elements, issues = parse_element_pairs("没有任何分隔符的申报要素")
        self.assertEqual(elements, {})
        self.assertIn("element_unstructured", [issue["code"] for issue in issues])


class StableIdentityTest(SimpleTestCase):
    def test_record_id_is_stable_and_source_scoped(self):
        first = _record_id("H01", "申报要素", 2, "C-1", "3")
        self.assertEqual(first, _record_id("H01", "申报要素", 2, "C-1", "3"))
        self.assertNotEqual(first, _record_id("H01", "申报要素", 2, "C-1", "4"))
        self.assertNotEqual(first, _record_id("H02", "申报要素", 2, "C-1", "3"))


class DuplicateMergeTest(SimpleTestCase):
    def _payload(self, *, name: str, record_id: str = "HIST-1", source_key: str = "src|sheet|2|C|1"):
        return {
            "record_id": record_id,
            "historical_name": name,
            "attributes": {"source_key": source_key, "source_file_id": "src"},
        }

    def test_duplicate_is_kept_once(self):
        existing = {"records": [self._payload(name="旧名")]}
        final, duplicates, writes = _merge_records(existing, [self._payload(name="旧名")], replace_changed=False)
        self.assertEqual(len(final), 1)
        self.assertEqual(duplicates[0]["kind"], "duplicate")
        self.assertEqual(writes, [])

    def test_changed_keeps_existing_by_default_and_replaces_when_requested(self):
        existing = {"records": [self._payload(name="旧名")]}
        final, duplicates, writes = _merge_records(existing, [self._payload(name="新名")], replace_changed=False)
        self.assertEqual(final[0]["historical_name"], "旧名")
        self.assertEqual(duplicates[0]["kind"], "changed")
        self.assertEqual(duplicates[0]["action"], "kept_existing")
        self.assertEqual(writes, [])

        final, duplicates, writes = _merge_records(existing, [self._payload(name="新名")], replace_changed=True)
        self.assertEqual(final[0]["historical_name"], "新名")
        self.assertEqual(duplicates[0]["action"], "replaced")
        self.assertEqual(len(writes), 1)

    def test_duplicate_within_batch_is_reported(self):
        final, duplicates, writes = _merge_records(None, [self._payload(name="甲"), self._payload(name="甲")], replace_changed=False)
        self.assertEqual(len(final), 1)
        self.assertEqual(duplicates[0]["kind"], "duplicate_within_batch")
        self.assertEqual(writes, [self._payload(name="甲")])


class SyntheticImportTest(SimpleTestCase):
    def _request(self, tmp: Path, files, *, copy_images=False, declared_success=False):
        return ImportRequest(
            files=files,
            out_dir=tmp / "out",
            dataset_kind="synthetic",
            declared_success=declared_success,
            declared_by="测试",
            declaration_note="合成样本声明",
            copy_images=copy_images,
            skip_validation=False,
        )

    def test_import_generates_p15_records_images_reports_and_validation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            with_image = build_synthetic_workbook(tmp / "with-image.xlsx")
            without_image = build_synthetic_workbook(tmp / "without-image.xlsx", with_image_column=False)
            outcome = import_history_templates(self._request(tmp, [with_image, without_image]))

            records = json.loads(outcome.records_path.read_text(encoding="utf-8"))
            self.assertEqual(records["schema_version"], "p15.history.1")
            self.assertTrue(records["dataset"]["synthetic"])
            self.assertEqual(len(records["records"]), 6)
            self.assertEqual(outcome.report["totals"]["source_records"], 8)
            self.assertEqual(outcome.report["totals"]["records_skipped"], 2)
            self.assertEqual(outcome.report["totals"]["parse_failed_files"], 0)

            by_name = {}
            for record in records["records"]:
                by_name.setdefault(record["historical_name"], []).append(record)
            first = by_name["合成法兰盘"][0]
            self.assertEqual(first["historical_hs"], "7307210000")
            self.assertEqual(first["material"], "不锈钢")
            self.assertEqual(first["spec"], "DN50")
            self.assertEqual(first["recorded_at"], "2025-01-15")
            self.assertEqual(first["source"]["system"], "synthetic-history-template")
            self.assertEqual(first["attributes"]["image_status"], "available")
            self.assertIn("source_key", first["attributes"])
            # 历史记录只作参考，不是建议：P15 的结构约束仍然有效。
            self.assertTrue(first["attributes"]["dataset_kind"] == "synthetic")

            image_statuses = {
                record["attributes"]["image_status"] for record in records["records"]
            }
            self.assertIn("available", image_statuses)
            self.assertIn("absent", image_statuses)
            self.assertIn("read_failed", image_statuses)
            self.assertIn(IMAGE_COLUMN_MISSING, image_statuses)
            self.assertNotIn("read_failed", {
                record["attributes"]["image_status"]
                for record in records["records"]
                if record["attributes"]["image_status"] == "available"
            })

            missing_date = [
                record for record in records["records"] if record["recorded_at"] == MISSING_DATE
            ]
            self.assertTrue(missing_date)
            self.assertIn("未提供", missing_date[0]["notes"])
            self.assertEqual(missing_date[0]["attributes"]["recorded_at_missing"], "true")

            report = json.loads(outcome.report_json_path.read_text(encoding="utf-8"))
            self.assertTrue(report["policy"]["reference_only"])
            self.assertEqual(report["human_declaration"], None)
            self.assertEqual(report["totals"]["image_status"]["available"], 1)
            self.assertEqual(report["totals"]["image_status"]["read_failed"], 1)
            self.assertEqual(report["totals"]["image_status"]["column_missing"], 4)

            validation = json.loads(outcome.validation_json_path.read_text(encoding="utf-8"))
            self.assertTrue(validation["all_passed"])
            self.assertEqual(validation["checks"], {
                "hit-basic": True,
                "no-match": True,
                "key-attribute-diff": True,
            })

    def test_reimport_is_idempotent_and_reports_duplicates(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_synthetic_workbook(tmp / "one.xlsx")
            first = import_history_templates(self._request(tmp, [workbook]))
            first_count = len(json.loads(first.records_path.read_text(encoding="utf-8"))["records"])
            second = import_history_templates(self._request(tmp, [workbook]))
            second_count = len(json.loads(second.records_path.read_text(encoding="utf-8"))["records"])
            self.assertEqual(first_count, second_count)
            self.assertEqual(second.report["totals"]["records_imported_this_run"], 0)
            self.assertEqual(second.report["totals"]["records_duplicate"], first_count)
            self.assertEqual(second.report["totals"]["records_changed"], 0)
            self.assertTrue(all(item["action"] == "kept" for item in second.report["duplicates"]))

    def test_parse_failure_is_reported_and_does_not_hide_good_file(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            good = build_synthetic_workbook(tmp / "good.xlsx")
            broken = tmp / "broken.xlsx"
            broken.write_bytes(b"not a zip")
            outcome = import_history_templates(self._request(tmp, [good, broken]))
            self.assertEqual(outcome.report["totals"]["parse_failed_files"], 1)
            self.assertEqual(outcome.report["totals"]["parsed_files"], 1)
            failed = next(item for item in outcome.report["files"] if item["status"] == "parse_failed")
            self.assertTrue(failed["error"]["code"])
            self.assertGreater(outcome.report["totals"]["records_total_in_output"], 0)

    def test_changed_file_is_reported_and_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_synthetic_workbook(tmp / "one.xlsx")
            first = import_history_templates(self._request(tmp, [workbook]))
            self.assertEqual(first.report["totals"]["records_imported_this_run"], 3)

            # 同一个文件路径、同一个来源位置，但品名变化：应报 changed，默认保留旧记录。
            build_synthetic_workbook(tmp / "one.xlsx", name="合成法兰盘改")
            changed = import_history_templates(self._request(tmp, [workbook]))
            self.assertEqual(changed.report["totals"]["records_changed"], 2)
            self.assertEqual(changed.report["totals"]["records_imported_this_run"], 0)
            records = json.loads(changed.records_path.read_text(encoding="utf-8"))["records"]
            self.assertIn("合成法兰盘", {record["historical_name"] for record in records})
            self.assertNotIn("合成法兰盘改", {record["historical_name"] for record in records})

            # 显式替换后才更新。
            request = self._request(tmp, [workbook])
            request.replace_changed = True
            replaced = import_history_templates(request)
            self.assertEqual(replaced.report["totals"]["records_changed"], 2)
            records = json.loads(replaced.records_path.read_text(encoding="utf-8"))["records"]
            self.assertIn("合成法兰盘改", {record["historical_name"] for record in records})

    def test_copy_images_writes_unique_files(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_synthetic_workbook(tmp / "one.xlsx")
            request = self._request(tmp, [workbook], copy_images=True)
            outcome = import_history_templates(request)
            images = list((outcome.out_dir / "images").glob("*"))
            self.assertEqual(len(images), 1)
            record = json.loads(outcome.records_path.read_text(encoding="utf-8"))["records"][0]
            self.assertTrue(record["attributes"]["image_output_path"].startswith("images/"))


class DeclarationRequiredTest(SimpleTestCase):
    def test_real_import_requires_explicit_human_declaration(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            workbook = build_synthetic_workbook(tmp / "one.xlsx")
            request = ImportRequest(
                files=[workbook],
                out_dir=tmp / "out",
                dataset_kind="real",
                declared_success=False,
                declaration_note=DEFAULT_DECLARATION_NOTE,
            )
            with self.assertRaises(ValueError):
                import_history_templates(request)


@unittest.skipUnless(
    REAL_MANIFEST.is_file() and len(REAL_FILES) == 3,
    "未找到已归档的三份真实历史模板（应位于 data/import-basis/2026-11-17/）",
)
class RealTemplateImportTest(SimpleTestCase):
    def test_real_templates_parse_and_validate(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            outcome = import_history_templates(
                ImportRequest(
                    files=REAL_FILES,
                    out_dir=tmp / "out",
                    manifest=REAL_MANIFEST,
                    dataset_kind="real",
                    declared_success=True,
                    declared_by="Jay",
                    declaration_note=DEFAULT_DECLARATION_NOTE,
                    skip_validation=False,
                )
            )
            self.assertEqual(outcome.report["totals"]["source_records"], 86)
            self.assertEqual(outcome.report["totals"]["records_imported_this_run"], 86)
            self.assertEqual(outcome.report["totals"]["parse_failed_files"], 0)
            # H02/H03 的 72 条 DISPIMG 可读；H01 没有图片列，必须记 column_missing 而非无图。
            self.assertEqual(outcome.report["totals"]["image_status"]["available"], 72)
            self.assertEqual(outcome.report["totals"]["image_status"]["column_missing"], 14)
            self.assertTrue(outcome.report["human_declaration"])
            self.assertFalse(outcome.report["human_declaration"]["independently_verified"])
            validation = json.loads(outcome.validation_json_path.read_text(encoding="utf-8"))
            self.assertTrue(validation["all_passed"], validation["checks"])
            # 真实模板缺申报日期：必须使用占位符，不能编造日期。
            records = json.loads(outcome.records_path.read_text(encoding="utf-8"))["records"]
            self.assertTrue(all(record["recorded_at"] == MISSING_DATE for record in records))
            self.assertTrue(all(record["hs_version"] is None for record in records))
            self.assertTrue(all(record["valid_until"] is None for record in records))