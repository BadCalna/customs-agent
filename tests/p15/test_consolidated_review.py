"""Independent-review reproductions: no real business contents in fixtures."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from adapters.files.ooxml import Workbook
from adapters.sources.history_consolidated import ConsolidatedFormatError, detect_workbook_format
from adapters.sources.history_import import import_history_templates, main
from tests.p04.fixtures import Package, png
from tests.p15.consolidated_fixtures import BASE_ROW, CONSOLIDATED_HEADERS, build_consolidated_workbook
from tests.p15.test_consolidated_import import _request, _records


class ReviewRegressions(SimpleTestCase):
    def test_explicit_header_and_sheet_are_supported(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            out = import_history_templates(_request(tmp, [p], sheet="Sheet1", header_row=1))
            self.assertEqual(out.report["totals"]["source_records"], 2)
            self.assertEqual(out.report["files"][0]["mapping"]["header_row"], 1)

    def test_auto_never_chooses_between_legacy_and_separate_consolidated_sheet(self):
        package = Package()
        s = package.sheet("汇总")
        for col, value in zip("ABCDE", ("中文品名", "HS 编码", "供应商", "报关单号", "申报要素")):
            s.text(f"{col}1", value)
        package.sheet("装箱明细")
        with tempfile.TemporaryDirectory() as raw:
            p = Path(raw) / "ambiguous.xlsx"
            package.build(p)
            with self.assertRaises(ConsolidatedFormatError) as caught:
                detect_workbook_format(Workbook.open(p))
            self.assertEqual(caught.exception.code, "input_format_ambiguous")

    def test_auto_mapping_works_in_cli_default_output_path(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            mapping = tmp / "map.json"
            mapping.write_text(json.dumps({"sheet": "Sheet1", "header_row": 1}), encoding="utf-8")
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                status = main(["import", "--files", str(p), "--input-format", "auto",
                               "--mapping", str(mapping), "--dataset-kind", "synthetic", "--skip-validation"])
                self.assertEqual(status, 0)
                self.assertEqual(len(list(tmp.glob("data/private/p15-import/*/history_records.json"))), 1)
            finally:
                os.chdir(previous)

    def test_new_variant_before_existing_same_anchor_is_not_swallowed(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW)])
            first = import_history_templates(_request(tmp, [p]))
            old_id = _records(first)[0]["record_id"]
            variant = dict(BASE_ROW, elements="材质：碳钢|规格：DN80")
            build_consolidated_workbook(p, rows=[variant, dict(BASE_ROW)])
            out = import_history_templates(_request(tmp, [p]))
            self.assertEqual(out.report["totals"]["records_added"], 1)
            self.assertEqual(out.report["totals"]["records_duplicate"], 1)
            self.assertEqual(out.report["totals"]["records_changed"], 0)
            self.assertEqual(len(_records(out)), 2)
            self.assertIn(old_id, {x["record_id"] for x in _records(out)})

    def test_multiple_changes_with_shared_anchor_are_not_guessed_even_when_replacing(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            a = dict(BASE_ROW, elements="材质：合成甲|规格：A")
            b = dict(BASE_ROW, elements="材质：合成乙|规格：B")
            p = build_consolidated_workbook(tmp / "in.xlsx", rows=[a, b])
            old = import_history_templates(_request(tmp, [p]))
            old_ids = {r["record_id"] for r in _records(old)}
            build_consolidated_workbook(p, rows=[dict(a, remark="改甲"), dict(b, remark="改乙")])
            out = import_history_templates(_request(tmp, [p], replace_changed=True, accept_conflicts=True))
            self.assertTrue(old_ids.issubset({r["record_id"] for r in _records(out)}))
            self.assertEqual(out.report["totals"]["records_changed"], 0)
            self.assertEqual(out.report["totals"]["records_missing_from_source"], 2)
            self.assertTrue(any(x["kind"] == "identity_uncertain" for x in out.report["conflicts"]))

    def test_original_id_remark_changes_are_not_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            headers = CONSOLIDATED_HEADERS + ("原始记录ID",)
            p = build_consolidated_workbook(tmp / "in.xlsx", headers=headers,
                                            rows=[dict(BASE_ROW, original_record_id="SYN-ID")])
            import_history_templates(_request(tmp, [p]))
            build_consolidated_workbook(p, headers=headers,
                                        rows=[dict(BASE_ROW, original_record_id="SYN-ID", remark="changed")])
            out = import_history_templates(_request(tmp, [p]))
            self.assertEqual(out.report["totals"]["records_changed"], 1)
            self.assertEqual(_records(out)[0]["attributes"]["remark"], "")

    def test_semantic_spaces_and_case_are_not_erased_in_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", rows=[
                dict(BASE_ROW, elements="规格：AB 12|材质：甲"),
                dict(BASE_ROW, elements="规格：AB1 2|材质：甲"),
                dict(BASE_ROW, purchase_contract_no="syn-p-001"),
            ])
            out = import_history_templates(_request(tmp, [p]))
            self.assertEqual(len({r["attributes"]["content_hash"] for r in _records(out)}), 3)
            self.assertEqual(len(_records(out)), 3)

    def test_formula_only_row_with_missing_cache_is_rejected_not_blank(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            package = Package()
            s = package.sheet("Sheet1")
            s.text("A1", "中文品名").text("B1", "HS 编码")
            s.formula("A2", "UNKNOWN()").formula("B2", "UNKNOWN()")
            p = tmp / "formula.xlsx"
            package.build(p)
            out = import_history_templates(_request(tmp, [p], skip_validation=True))
            self.assertEqual(out.report["totals"]["source_records"], 1)
            self.assertEqual(out.report["totals"]["records_rejected"], 1)
            self.assertEqual(out.report["totals"]["records_ignored"], 0)

    def test_long_numeric_identifier_is_preserved_but_requires_review(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(
                BASE_ROW, tax_classification_code="1090000000000000000", __kind_tax_classification_code="number")])
            out = import_history_templates(_request(tmp, [p]))
            r = _records(out)[0]
            self.assertEqual(r["attributes"]["tax_classification_code_raw"], "1090000000000000000")
            self.assertEqual(r["attributes"]["needs_review"], "true")
            self.assertIn("numeric_precision_risk_tax_classification_code", r["attributes"]["review_issue_codes"])

    def test_corrupt_and_symlink_escape_local_images_are_not_available(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            root = tmp / "images"
            root.mkdir()
            (root / "bad.png").write_bytes(b"not an image")
            (tmp / "outside.png").write_bytes(png())
            (root / "escape.png").symlink_to(tmp / "outside.png")
            p = build_consolidated_workbook(tmp / "in.xlsx", headers=CONSOLIDATED_HEADERS + ("图片路径",), rows=[
                dict(BASE_ROW, image="bad.png"), dict(BASE_ROW, image="escape.png", declaration_no="SYN-2")])
            out = import_history_templates(_request(tmp, [p], image_root=root))
            statuses = [r["attributes"]["image_status"] for r in _records(out)]
            self.assertEqual(statuses, ["read_failed", "external_path_unsafe"])

    def test_image_text_is_not_misclassified_as_external_path(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", headers=CONSOLIDATED_HEADERS + ("图片",), rows=[
                dict(BASE_ROW, image="无图"), dict(BASE_ROW, image="见附件", declaration_no="SYN-2")])
            out = import_history_templates(_request(tmp, [p]))
            statuses = [r["attributes"]["image_status"] for r in _records(out)]
            self.assertEqual(statuses, ["absent", "text_unrecognized"])

    def test_merged_image_is_never_borrowed_to_next_product(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", with_image=True, merged_ranges=("P2:P3",))
            out = import_history_templates(_request(tmp, [p]))
            statuses = [r["attributes"]["image_status"] for r in _records(out)]
            self.assertEqual(statuses, ["available", "read_failed"])

    def test_excel_error_cache_rejects_required_field(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            package = Package()
            s = package.sheet("Sheet1")
            s.text("A1", "中文品名").text("B1", "HS 编码")
            s.text("A2", "合成错误件").formula("B2", "LOOKUP()", "#N/A")
            p = tmp / "error.xlsx"
            package.build(p)
            out = import_history_templates(_request(tmp, [p]))
            self.assertEqual(out.report["totals"]["records_rejected"], 1)
            self.assertEqual(_records(out), [])

    def test_unstructured_or_embedded_elements_are_reviewed_not_guessed(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", rows=[
                dict(BASE_ROW, elements="0|1|塑料|无品牌|SYN-001"),
                dict(BASE_ROW, elements="材质：非合金钢种类：无缝管", declaration_no="SYN-2")])
            out = import_history_templates(_request(tmp, [p]))
            rows = _records(out)
            self.assertEqual(out.report["totals"]["records_needs_review"], 2)
            self.assertEqual(rows[0]["elements"], {})
            self.assertIn("element_unstructured", rows[0]["attributes"]["review_issue_codes"])
            self.assertIn("element_embedded_label", rows[1]["attributes"]["review_issue_codes"])

    def test_image_disappearance_clears_current_image_not_old_files(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx", with_image=True)
            old = import_history_templates(_request(tmp, [p], copy_images=True))
            image = old.out_dir / _records(old)[0]["attributes"]["image_output_path"]
            self.assertTrue(image.is_file())
            build_consolidated_workbook(p)
            out = import_history_templates(_request(tmp, [p]))
            for row in _records(out):
                self.assertEqual(row["attributes"]["image_status"], "column_missing")
                for field in ("image_sha256", "image_output_path", "image_format", "image_size"):
                    self.assertEqual(row["attributes"][field], "")
            self.assertTrue(image.is_file(), "old image remains usable for old generation")

    def test_failure_cleans_staging_and_keeps_visible_diagnostics(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            with self.assertRaises(ValueError):
                import_history_templates(_request(tmp, [p], input_format="legacy-template"))
            self.assertEqual(list((tmp / "out").glob(".staging-*")), [])
            self.assertTrue(list((tmp / "out").glob("import_failure-*.json")))

    def test_fresh_can_replace_dataset_kind_only_with_explicit_permission(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            import_history_templates(_request(tmp, [p]))
            with self.assertRaises(ValueError):
                import_history_templates(_request(tmp, [p], dataset_kind="real", declared_success=True, fresh=True))
            out = import_history_templates(_request(tmp, [p], dataset_kind="real", declared_success=True,
                                                     fresh=True, allow_dataset_replace=True))
            payload = json.loads(out.records_path.read_text())
            self.assertFalse(payload["dataset"]["synthetic"])
            self.assertEqual(len(payload["records"]), 2)

    def test_removed_source_file_set_is_rejected_in_explicit_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            a = build_consolidated_workbook(tmp / "a.xlsx")
            b = build_consolidated_workbook(tmp / "b.xlsx")
            first = import_history_templates(_request(tmp, [a]))
            data = first.records_path.read_bytes()
            with self.assertRaises(ValueError):
                import_history_templates(_request(tmp, [b]))
            self.assertEqual(first.records_path.read_bytes(), data)

    def test_old_identity_version_refuses_merge_and_requires_explicit_rebuild(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            out = import_history_templates(_request(tmp, [p]))
            # Construct pre-release flat candidate without identity_version; do not
            # mutate an immutable published generation.
            legacy = tmp / "legacy-output"
            legacy.mkdir()
            payload = json.loads(out.records_path.read_text())
            payload["dataset"].pop("identity_version")
            for rec in payload["records"]:
                rec["attributes"].pop("identity_version")
            (legacy / "history_records.json").write_text(json.dumps(payload))
            before = (legacy / "history_records.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "身份版本"):
                import_history_templates(_request(tmp, [p], out_dir=legacy))
            self.assertEqual((legacy / "history_records.json").read_bytes(), before)
            rebuilt = import_history_templates(_request(tmp, [p], out_dir=legacy, fresh=True))
            self.assertEqual(rebuilt.report["totals"]["records_total_in_output"], 2)
            repeat = import_history_templates(_request(tmp, [p], out_dir=legacy))
            self.assertEqual(repeat.report["totals"]["records_duplicate"], 2)
            self.assertEqual(repeat.report["totals"]["records_added"], 0)

    def test_skip_validation_replaces_previous_success_with_explicit_skipped_report(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            old = import_history_templates(_request(tmp, [p]))
            self.assertTrue(json.loads(old.validation_json_path.read_text())["all_passed"])
            out = import_history_templates(_request(tmp, [p], skip_validation=True))
            val = json.loads(out.validation_json_path.read_text())
            self.assertEqual(val["status"], "skipped")
            self.assertFalse(val["all_passed"])
            self.assertEqual(val["cases"], [])
            self.assertEqual(out.report["validation"]["status"], "skipped")

    def test_standalone_validate_does_not_mutate_old_generation(self):
        from adapters.sources.history_import import validate_history_queries
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            p = build_consolidated_workbook(tmp / "in.xlsx")
            out = import_history_templates(_request(tmp, [p]))
            old_generation = (out.out_dir / ".current").resolve()
            before = {f.name: f.read_bytes() for f in old_generation.iterdir() if f.is_file()}
            validate_history_queries(out.records_path, out_dir=out.out_dir)
            self.assertNotEqual((out.out_dir / ".current").resolve(), old_generation)
            self.assertEqual(before, {f.name: f.read_bytes() for f in old_generation.iterdir() if f.is_file()})
