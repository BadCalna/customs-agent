"""P20 固定结果集、模板保真与独立清单的行为验证；全部使用合成资料。"""
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from apps.api.schemas import IssueEntry
from adapters.files.export_workbook import (
    ExportError, ExportLine, ExportSelection, _zip_replace, audit_fill, export_precheck, normalize_export_views,
)
from adapters.files.export_issues import HEADERS, STYLES, export_issues
from adapters.files.ooxml import NS, S, Workbook
from experiments.excel_fidelity.workbook import FidelityError
from tests.p03.fixtures import make_fixture
from tests.p04.builders import build_package


def sample(headers=None, item="55-27"):
    headers = headers or ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "AI推荐HScode", "AI预审结论", "业务员确认HScode", "置信度")
    package = build_package(declaration_headers=headers, declaration_rows=[
        ("SYN-C", item, "合成商品", "0000000001", "原要素", "", "", "0000000002", "原专家意见"),
    ])
    package.sheets[0].dimension = "A1:I2"
    package.parts["xl/styles.xml"] = STYLES
    with tempfile.TemporaryDirectory() as directory:
        return Path(package.build(Path(directory) / "source.xlsx")).read_bytes()


def selection(source, **kwargs):
    line = ExportLine("item-1", "version-1", "run-1", "节点①申报要素", 2,
                      suggested_hs="0000000003", comparison_status="inconsistent", reason="合成依据", **kwargs)
    return ExportSelection("task-1", "version-1", "run-1", hashlib.sha256(source).hexdigest(), (line,))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.source = sample()
        self.selection = selection(self.source)

    def test_semantic_write_preserves_original_and_manual_columns(self):
        artifact = export_precheck(self.source, self.selection)
        sheet = Workbook(artifact.content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("D2").value, "0000000001")
        self.assertEqual(sheet.cell("F2").value, "0000000003")
        self.assertIn("推荐码与原码不一致", sheet.cell("G2").value)
        self.assertIn("测试结果", sheet.cell("G2").value)
        self.assertEqual(sheet.cell("H2").value, "0000000002")
        self.assertEqual(sheet.cell("I2").value, "原专家意见")
        self.assertEqual(sheet.cell("B2").value, "55-27")
        self.assertEqual(hashlib.sha256(self.source).hexdigest(), self.selection.source_sha256)

    def test_preview_watermarks_removed_from_all_sheets_without_changing_print_or_manual_data(self):
        book = Workbook(self.source)
        changes = {}
        for sheet in book.sheets:
            raw = book.media_bytes(sheet.part).decode()
            view = ('<sheetViews><sheetView workbookViewId="0" view="pageBreakPreview" zoomScale="75">'
                    '<pane xSplit="2" ySplit="1" topLeftCell="C2" state="frozen"/>'
                    '<selection pane="bottomRight" activeCell="H2" sqref="H2"/></sheetView></sheetViews>')
            setup = '<pageSetup paperSize="9" scale="40" fitToWidth="1" fitToHeight="0"/>'
            changes[sheet.part] = raw.replace('<sheetData>', view + '<sheetData>').replace('</worksheet>', setup + '</worksheet>').encode()
        source = _zip_replace(self.source, changes)
        artifact = export_precheck(source, selection(source))
        after = Workbook(artifact.content)
        for sheet in after.sheets:
            root = ET.fromstring(after.media_bytes(sheet.part))
            view = root.find('s:sheetViews/s:sheetView', NS)
            self.assertEqual(view.get('view'), 'normal')
            self.assertEqual(view.get('zoomScale'), '75')
            self.assertEqual(view.find('s:pane', NS).get('state'), 'frozen')
            self.assertEqual(view.find('s:selection', NS).get('activeCell'), 'H2')
            self.assertEqual(root.find('s:pageSetup', NS).get('scale'), '40')
        self.assertEqual(after.find_sheet('申报要素').cell('H2').value, '0000000002')
        self.assertEqual(len(artifact.manifest['view_adjustment']['views']), len(book.sheets))
        unchanged, report = normalize_export_views(artifact.content)
        self.assertEqual(unchanged, artifact.content)
        self.assertEqual(report['changed_parts'], [])

    def test_positions_can_change(self):
        source = sample(headers=("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "业务员确认HScode", "置信度", "AI预审结论（业务员参考）", "AI推荐hscode"))
        artifact = export_precheck(source, selection(source))
        sheet = Workbook(artifact.content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("I2").value, "0000000003")
        self.assertIn("合成依据", sheet.cell("H2").value)
        self.assertEqual(sheet.cell("F2").value, "")

    def test_insert_missing_columns_and_append_only_missing_conclusion(self):
        for headers in (("采购合同号", "合同项号", "品名", "海关编码", "申报要素"),
                        ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "AI推荐HScode")):
            with self.subTest(headers=headers):
                source = sample(headers=headers)
                artifact = export_precheck(source, selection(source))
                book = Workbook(artifact.content)
                sheet = book.find_sheet("申报要素")
                columns = artifact.manifest["result_columns"]
                self.assertEqual(sheet.cell(columns["ai_hs"] + "2").value, "0000000003")
                self.assertIn("合成依据", sheet.cell(columns["precheck_conclusion"] + "2").value)
                self.assertEqual(len(artifact.manifest["preprocessing"]), 1)
                self.assertEqual(sheet.cell("D2").value, "0000000001")

    def test_ambiguous_result_columns_and_namespace_prefix(self):
        source = sample(headers=("采购合同号", "合同项号", "品名", "海关编码", "申报要素",
                                 "AI推荐HScode", "预审结论", "AI推荐HS编码"))
        with self.assertRaises(ExportError) as caught:
            export_precheck(source, selection(source))
        self.assertEqual(caught.exception.code, "ambiguous_result_columns")
        part = Workbook(self.source).find_sheet("申报要素").part
        raw = Workbook(self.source).media_bytes(part).decode()
        raw = re.sub(r'<(/?)([A-Za-z][A-Za-z0-9]*)', r'<\1s:\2', raw)
        raw = raw.replace(f'xmlns="{S}"', f'xmlns:s="{S}"')
        source = _zip_replace(self.source, {part: raw.encode()})
        sheet = Workbook(export_precheck(source, selection(source)).content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("F2").value, "0000000003")

    def test_shared_images_convert_and_missing_resource_refuses_export(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.xlsx"
            make_fixture(path, {"xl/styles.xml": STYLES})
            source = path.read_bytes()
        from adapters.files.declarations import extract_declarations
        from experiments.excel_fidelity.workbook import Workbook as FidelityWorkbook
        extracted = extract_declarations(Workbook(source))
        lines = tuple(ExportLine(f"item-{r.row}", "version-1", "run-1", r.sheet, r.row,
                                 suggested_hs="0000000003") for r in extracted.records)
        chosen = replace(selection(source), lines=lines)
        artifact = export_precheck(source, chosen)
        image_report = next(report for report in artifact.manifest["preprocessing"] if "converted_images" in report)
        self.assertEqual(image_report["converted_images"], 3)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.xlsx"
            output.write_bytes(artifact.content)
            self.assertEqual(FidelityWorkbook(output).image_records(), [])
        self.assertEqual(Workbook(source).media_bytes("xl/media/test.png"),
                         Workbook(artifact.content).media_bytes("xl/media/test.png"))
        broken = _zip_replace(source, {"xl/media/test.png": b"not an image"})
        with self.assertRaises(FidelityError):
            export_precheck(broken, replace(chosen, source_sha256=hashlib.sha256(broken).hexdigest()))

    def test_missing_recommendation_stays_blank(self):
        chosen = replace(self.selection, lines=(replace(self.selection.lines[0], suggested_hs=None, comparison_status="needs_confirmation"),))
        sheet = Workbook(export_precheck(self.source, chosen).content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("F2").value, "")
        self.assertIn("待确认", sheet.cell("G2").value)

    def test_empty_result_cell_does_not_consume_following_manual_cells(self):
        book = Workbook(self.source)
        part = book.find_sheet("申报要素").part
        raw = book.media_bytes(part).decode()
        raw = re.sub(r'<c r="F2".*?</c>', '<c r="F2" s="0"/>', raw)
        raw = re.sub(r'<c r="G2".*?</c>', '<c r="G2" s="0"/>', raw)
        source = _zip_replace(self.source, {part: raw.encode()})
        sheet = Workbook(export_precheck(source, selection(source)).content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("H2").value, "0000000002")
        self.assertEqual(sheet.cell("I2").value, "原专家意见")

    def test_failed_row_cannot_export_stale_recommendation(self):
        chosen = replace(self.selection, lines=(replace(self.selection.lines[0], status="failed", error_message="合成超时"),))
        sheet = Workbook(export_precheck(self.source, chosen).content).find_sheet("申报要素")
        self.assertEqual(sheet.cell("F2").value, "")
        self.assertIn("技术执行失败", sheet.cell("G2").value)

    def test_no_invented_confidence_threshold(self):
        chosen = selection(self.source, confidence=.99)
        value = Workbook(export_precheck(self.source, chosen).content).find_sheet("申报要素").cell("G2").value
        self.assertIn("等级未提供", value)
        self.assertNotIn("置信度：高", value)

    def test_red_low_yellow_medium_and_red_missing_text(self):
        for label, fill in (("低", "FFFFC7CE"), ("中", "FFFFF2CC")):
            with self.subTest(level=label):
                chosen = selection(self.source, confidence_label=label, issues=(IssueEntry(code="MISSING", message="材质待补", missing="材质牌号", suggested_role="supplier"),))
                artifact = export_precheck(self.source, chosen)
                book = Workbook(artifact.content)
                sheet = book.find_sheet("申报要素")
                styles = ET.fromstring(book.media_bytes("xl/styles.xml"))
                xf = styles.find("s:cellXfs", NS)[int(sheet.cell("G2").style)]
                self.assertEqual(styles.find("s:fills", NS)[int(xf.get("fillId"))].find("s:patternFill/s:fgColor", NS).get("rgb"), fill)
                root = ET.fromstring(book.media_bytes(sheet.part))
                self.assertTrue(root.findall('.//s:c[@r="G2"]/s:is/s:r/s:rPr/s:color', NS))

    def test_mixed_version_run_duplicate_row_and_source_rejected(self):
        for change in (dict(version_id="wrong"), dict(run_id="wrong"), dict(source_sha256="0" * 64),
                       dict(lines=self.selection.lines * 2), dict(lines=(replace(self.selection.lines[0], row=77),))):
            with self.subTest(change=change), self.assertRaises(ExportError):
                export_precheck(self.source, replace(self.selection, **change))

    def test_formula_in_result_cell_and_merged_result_rejected(self):
        with ZipFile(io.BytesIO(self.source)) as archive:
            raw = archive.read("xl/worksheets/sheet1.xml")
        for modified in (raw.replace(b'<c r="F2" t="inlineStr"><is><t></t></is></c>', b'<c r="F2"><f>1+1</f><v>2</v></c>'),
                         raw.replace(b'</worksheet>', b'<mergeCells><mergeCell ref="F2:F3"/></mergeCells></worksheet>')):
            source = _zip_replace(self.source, {"xl/worksheets/sheet1.xml": modified})
            with self.assertRaises(ExportError):
                export_precheck(source, selection(source))

    def test_unchanged_other_sheet_detects_corruption(self):
        with patch("adapters.files.export_workbook.audit_fill", wraps=audit_fill) as check:
            export_precheck(self.source, self.selection)
            before, after, name, writes = check.call_args.args
        damaged = _zip_replace(after, {"xl/worksheets/sheet2.xml": b"<broken/>"})
        with self.assertRaises(Exception):
            audit_fill(before, damaged, name, writes)

    def test_invalid_text_not_truncated_or_executed(self):
        for reason in ("x" * 32768, "bad\x00value"):
            chosen = replace(self.selection, lines=(replace(self.selection.lines[0], reason=reason),))
            with self.assertRaises(ExportError):
                export_precheck(self.source, chosen)
        chosen = replace(self.selection, lines=(replace(self.selection.lines[0], reason='=HYPERLINK("https://example.com")'),))
        cell = Workbook(export_precheck(self.source, chosen).content).find_sheet("申报要素").cell("G2")
        self.assertIsNone(cell.formula)
        self.assertIn("=HYPERLINK", cell.value)

    def test_expert_layout_requires_separate_confirmation(self):
        chosen = replace(self.selection, run_kind="expert_check", submission_id="submission-1")
        with self.assertRaises(ExportError) as caught:
            export_precheck(self.source, chosen)
        self.assertEqual(caught.exception.code, "expert_layout_pending")

    def test_issue_list_eight_columns_leading_zero_and_literal_text(self):
        chosen = selection(self.source, issues=(IssueEntry(code="MISSING", message="=literal", missing="材质", suggested_role="supplier"),))
        artifact = export_issues(self.source, chosen)
        sheet = Workbook(artifact.content).sheet("补料清单")
        self.assertEqual(tuple(sheet.cell(f"{chr(65+i)}4").value for i in range(8)), HEADERS)
        self.assertEqual(sheet.cell("B5").value, "55-27")
        self.assertEqual(sheet.cell("E5").value, "=literal")
        self.assertIsNone(sheet.cell("E5").formula)
        self.assertEqual(sheet.cell("G5").value, "供应商")
        self.assertEqual(sheet.cell("H5").value, "")

    def test_source_failure_is_not_invented_supplier_material(self):
        chosen = selection(self.source, issues=(IssueEntry(code="SOURCE_QUERY_FAILED", message="来源查询失败", suggested_role="expert"),))
        sheet = Workbook(export_issues(self.source, chosen).content).sheet("补料清单")
        self.assertEqual(sheet.cell("F5").value, "")
        self.assertEqual(sheet.cell("G5").value, "物流/专家")

    def test_failed_execution_does_not_export_stale_supplement_requests(self):
        chosen = selection(self.source, issues=(IssueEntry(code="OLD", message="旧问题", missing="旧待补字段"),))
        chosen = replace(chosen, lines=(replace(chosen.lines[0], status="failed", error_message="本轮超时"),))
        artifact = export_issues(self.source, chosen)
        sheet = Workbook(artifact.content).sheet("补料清单")
        self.assertEqual(artifact.manifest["issue_count"], 1)
        self.assertIn("技术执行失败", sheet.cell("E5").value)
        self.assertNotIn("旧待补字段", sheet.cell("F5").value)

    def test_empty_issue_list_not_labelled_approved(self):
        artifact = export_issues(self.source, self.selection)
        self.assertEqual(artifact.manifest["issue_count"], 0)
        self.assertIn("不表示", Workbook(artifact.content).sheet("补料清单").cell("A5").value)
