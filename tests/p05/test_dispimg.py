"""DISPIMG 逐行提取：状态三分、共享公式逐行、错误码可区分（P05、R11）。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from adapters.files.dispimg import (
    CODE_BLANK_CELL,
    CODE_DEFINITION_DUPLICATE,
    CODE_DEFINITION_MISSING,
    CODE_EXPRESSION,
    CODE_FLOATING_MULTIPLE,
    CODE_FLOATING_UNAVAILABLE,
    CODE_FORMULA_TEXT_ONLY,
    CODE_MEDIA_CORRUPT,
    CODE_MEDIA_EXTERNAL,
    CODE_MEDIA_MISSING,
    CODE_MEDIA_TOO_LARGE,
    CODE_MULTIPLE_REFERENCES,
    CODE_OTHER_FORMULA,
    CODE_SHEET_MISSING,
    CODE_TEXT_CONTENT,
    CODE_UNSUPPORTED_FORMULA,
    ID_SOURCE_SINGLE_CELL_FLOATING,
    STATUS_ABSENT,
    STATUS_AVAILABLE,
    STATUS_READ_FAILED,
    DispimgError,
    extract_images,
    find_image_column,
)
from adapters.files.ooxml import Workbook
from tests.p04.fixtures import Package, formula
from tests.p05.workbooks import (
    DECLARATION_SHEET,
    ambiguous_image_column_package,
    external_package,
    floating_package,
    mixed_package,
    no_image_column_package,
)


class DispimgTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def workbook(self, package, name="sample.xlsx") -> Workbook:
        return Workbook.open(Path(package.build(self.root / name)))

    def extract_mixed(self, **kwargs):
        return extract_images(
            self.workbook(mixed_package()), sheet_name=DECLARATION_SHEET,
            rows=range(2, 17), **kwargs,
        )


class ImageColumnTests(DispimgTestCase):
    def test_detects_image_column_from_header_keyword(self):
        match = find_image_column(self.workbook(mixed_package()), DECLARATION_SHEET)
        self.assertEqual(match.status, "found")
        self.assertEqual(match.column, "E")
        self.assertEqual(match.header, "附图")

    def test_missing_header_is_reported_not_treated_as_no_image(self):
        match = find_image_column(self.workbook(no_image_column_package()), DECLARATION_SHEET)
        self.assertEqual(match.status, "missing")
        self.assertEqual(match.candidates, ())
        extraction = extract_images(
            self.workbook(no_image_column_package()), sheet_name=DECLARATION_SHEET,
            rows=range(2, 17),
        )
        self.assertEqual(extraction.column_status, "missing")
        self.assertEqual(extraction.rows, ())  # 不产生“全部无图”的假结论
        self.assertEqual(extraction.counts, {STATUS_AVAILABLE: 0, STATUS_ABSENT: 0, STATUS_READ_FAILED: 0})

    def test_ambiguous_header_lists_candidates(self):
        match = find_image_column(self.workbook(ambiguous_image_column_package()), DECLARATION_SHEET)
        self.assertEqual(match.status, "ambiguous")
        self.assertEqual(match.candidates, ("E", "F"))
        extraction = extract_images(
            self.workbook(ambiguous_image_column_package()), sheet_name=DECLARATION_SHEET,
            rows=range(2, 17),
        )
        self.assertEqual(extraction.column_status, "ambiguous")
        self.assertEqual(extraction.rows, ())

    def test_explicit_column_keeps_header_but_extracts_rows(self):
        extraction = extract_images(
            self.workbook(no_image_column_package()), sheet_name=DECLARATION_SHEET,
            rows=range(2, 17), column="E",
        )
        self.assertEqual(extraction.column_status, "provided")
        self.assertEqual(extraction.column_header, "备注")
        self.assertEqual(extraction.counts[STATUS_ABSENT], 15)


class RowStatusTests(DispimgTestCase):
    def statuses(self, extraction):
        return {row.row: (row.status, row.code) for row in extraction.rows}

    def test_mixed_workbook_separates_available_absent_and_read_failed(self):
        extraction = self.extract_mixed()
        self.assertEqual(extraction.column_status, "found")
        self.assertEqual(extraction.counts, {
            STATUS_AVAILABLE: 3, STATUS_ABSENT: 4, STATUS_READ_FAILED: 8,
        })
        statuses = self.statuses(extraction)
        self.assertEqual(statuses[2], (STATUS_AVAILABLE, "ok"))
        self.assertEqual(statuses[3][0], STATUS_AVAILABLE)
        self.assertEqual(statuses[4][0], STATUS_AVAILABLE)
        self.assertEqual(statuses[5], (STATUS_ABSENT, CODE_BLANK_CELL))
        self.assertEqual(statuses[6], (STATUS_ABSENT, CODE_TEXT_CONTENT))
        self.assertEqual(statuses[7], (STATUS_READ_FAILED, CODE_DEFINITION_MISSING))
        self.assertEqual(statuses[8], (STATUS_READ_FAILED, CODE_DEFINITION_DUPLICATE))
        self.assertEqual(statuses[9], (STATUS_READ_FAILED, CODE_MEDIA_MISSING))
        self.assertEqual(statuses[10], (STATUS_READ_FAILED, CODE_MEDIA_CORRUPT))
        self.assertEqual(statuses[11], (STATUS_READ_FAILED, CODE_EXPRESSION))
        self.assertEqual(statuses[12], (STATUS_READ_FAILED, CODE_MULTIPLE_REFERENCES))
        self.assertEqual(statuses[13], (STATUS_READ_FAILED, CODE_FORMULA_TEXT_ONLY))
        self.assertEqual(statuses[14], (STATUS_ABSENT, CODE_OTHER_FORMULA))
        self.assertEqual(statuses[15], (STATUS_READ_FAILED, CODE_UNSUPPORTED_FORMULA))
        self.assertEqual(statuses[16], (STATUS_ABSENT, CODE_BLANK_CELL))

    def test_every_read_failure_has_a_reason(self):
        for row in self.extract_mixed().rows:
            if row.status == STATUS_READ_FAILED:
                self.assertTrue(row.message, row)
                self.assertIn(row.code, row.message)

    def test_shared_formula_rows_resolve_per_row(self):
        extraction = self.extract_mixed()
        rows = {row.row: row for row in extraction.rows}
        master, dependent, cached = rows[2], rows[3], rows[4]
        self.assertEqual((master.dispimg_id, master.id_source), ("ID_A", "formula"))
        self.assertEqual((dependent.dispimg_id, dependent.id_source), ("ID_A", "shared_formula"))
        # 从属行缓存值带本行 ID 时以缓存值为准，并保留追溯说明。
        self.assertEqual((cached.dispimg_id, cached.id_source), ("ID_B", "shared_formula_cached"))
        self.assertTrue(cached.notes)
        self.assertTrue(dependent.notes)

    def test_direct_formula_cached_text_keeps_formula_source(self):
        package = Package()
        sheet = package.sheet(DECLARATION_SHEET)
        sheet.text("E1", "附图")
        sheet.text("A2", "TEST-CONTRACT")
        sheet.cells["E2"] = formula(
            "E2", '_xlfn.DISPIMG("ID_A",1)', cached='=DISPIMG("ID_A",1)',
        )
        package.cell_image("ID_A", "xl/media/a.png")
        package.image("xl/media/a.png", b"\x89PNG\r\n\x1a\nfake")
        extraction = extract_images(self.workbook(package), sheet_name=DECLARATION_SHEET, rows=[2])
        # 缓存值不是公式来源：直接公式的 ID 以 <f> 为准；此处媒体是伪造的坏资源。
        self.assertEqual(extraction.rows[0].dispimg_id, "ID_A")
        self.assertEqual(extraction.rows[0].id_source, "formula")
        self.assertEqual(extraction.rows[0].code, CODE_MEDIA_CORRUPT)

    def test_available_rows_expose_media_bytes_and_sha256(self):
        workbook = self.workbook(mixed_package())
        extraction = extract_images(
            workbook, sheet_name=DECLARATION_SHEET, rows=[2, 4],
        )
        first, second = extraction.rows
        self.assertTrue(first.data)
        self.assertEqual(first.size, len(first.data))
        self.assertEqual(first.image_format, "PNG")
        self.assertEqual(first.sha256, workbook.media(first.media_part).sha256)
        self.assertEqual(extraction.rows[1].dispimg_id, "ID_B")
        self.assertNotEqual(first.sha256, second.sha256)

    def test_external_image_link_is_read_failed(self):
        extraction = extract_images(
            self.workbook(external_package()), sheet_name=DECLARATION_SHEET, rows=[2],
        )
        row = extraction.rows[0]
        self.assertEqual(row.status, STATUS_READ_FAILED)
        self.assertEqual(row.code, CODE_MEDIA_EXTERNAL)
        self.assertEqual(row.dispimg_id, "ID_EXT")

    def test_image_size_limit_is_read_failed_not_available(self):
        extraction = self.extract_mixed(max_image_bytes=1)
        row = next(row for row in extraction.rows if row.row == 2)
        self.assertEqual(row.status, STATUS_READ_FAILED)
        self.assertEqual(row.code, CODE_MEDIA_TOO_LARGE)
        self.assertEqual(row.dispimg_id, "ID_A")
        self.assertEqual(len(row.sha256), 64)  # 仍记录资源指纹，方便人工定位
        self.assertEqual(row.data, b"")

    def test_report_is_json_serializable_and_has_no_bytes(self):
        payload = self.extract_mixed().as_dict()
        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["column_status"], "found")
        self.assertEqual(payload["counts"][STATUS_AVAILABLE], 3)
        self.assertNotIn("data", payload["rows"][0])


class FloatingImageTests(DispimgTestCase):
    """附图格内普通浮图：只认精确单格锚点，多张/不可用不猜。"""

    def extract_floating(self, **kwargs):
        return extract_images(
            self.workbook(floating_package()), sheet_name=DECLARATION_SHEET,
            rows=range(2, 9), **kwargs,
        )

    def test_single_cell_floating_image_is_available(self):
        row = self.extract_floating().rows[0]
        self.assertEqual(row.row, 2)
        self.assertEqual(row.status, STATUS_AVAILABLE)
        self.assertEqual(row.code, "ok")
        self.assertEqual(row.id_source, ID_SOURCE_SINGLE_CELL_FLOATING)
        self.assertEqual(row.formula_status, ID_SOURCE_SINGLE_CELL_FLOATING)
        self.assertEqual(row.dispimg_id, "")
        self.assertTrue(row.data)
        self.assertEqual(row.sha256, hashlib.sha256(row.data).hexdigest())
        self.assertEqual(row.image_format, "PNG")
        self.assertTrue(row.notes)

    def test_multiple_floating_images_are_read_failed(self):
        row = next(item for item in self.extract_floating().rows if item.row == 3)
        self.assertEqual(row.status, STATUS_READ_FAILED)
        self.assertEqual(row.code, CODE_FLOATING_MULTIPLE)
        self.assertIn("需人工核对", row.message)

    def test_corrupt_or_missing_floating_image_is_read_failed(self):
        rows = {item.row: item for item in self.extract_floating().rows}
        for row_number in (4, 5):
            with self.subTest(row=row_number):
                self.assertEqual(rows[row_number].status, STATUS_READ_FAILED)
                self.assertEqual(rows[row_number].code, CODE_FLOATING_UNAVAILABLE)
                self.assertIn("需人工核对", rows[row_number].message)

    def test_cross_row_floating_is_not_attributed(self):
        row = next(item for item in self.extract_floating().rows if item.row == 6)
        self.assertEqual(row.status, STATUS_ABSENT)
        self.assertEqual(row.code, CODE_BLANK_CELL)

    def test_formula_takes_precedence_and_reports_floating_for_review(self):
        row = next(item for item in self.extract_floating().rows if item.row == 7)
        self.assertEqual(row.status, STATUS_AVAILABLE)
        self.assertEqual(row.id_source, "formula")
        self.assertEqual(row.dispimg_id, "ID_A")
        self.assertTrue(any("浮图" in note for note in row.notes))

    def test_floating_size_limit_is_read_failed(self):
        row = self.extract_floating(max_image_bytes=1).rows[0]
        self.assertEqual(row.status, STATUS_READ_FAILED)
        self.assertEqual(row.code, CODE_MEDIA_TOO_LARGE)
        self.assertEqual(row.data, b"")


class StructuralErrorTests(DispimgTestCase):
    def test_unknown_sheet_is_explicit_error(self):
        workbook = self.workbook(mixed_package())
        with self.assertRaises(DispimgError) as caught:
            extract_images(workbook, sheet_name="不存在的页签", rows=[2])
        self.assertEqual(caught.exception.code, CODE_SHEET_MISSING)

    def test_invalid_column_is_explicit_error(self):
        workbook = self.workbook(mixed_package())
        with self.assertRaises(DispimgError) as caught:
            extract_images(workbook, sheet_name=DECLARATION_SHEET, rows=[2], column="1A")
        self.assertEqual(caught.exception.code, "image_column_invalid")

    def test_invalid_rows_are_explicit_errors(self):
        workbook = self.workbook(mixed_package())
        for rows, code in (([0], "invalid_row"), (["2"], "invalid_row"), ([2.5], "invalid_row"),
                           ([], "no_rows"), (2, "invalid_row")):
            with self.subTest(rows=rows):
                with self.assertRaises(DispimgError) as caught:
                    extract_images(workbook, sheet_name=DECLARATION_SHEET, rows=rows)
                self.assertEqual(caught.exception.code, code)

    def test_invalid_header_row_is_explicit_error(self):
        workbook = self.workbook(mixed_package())
        with self.assertRaises(DispimgError) as caught:
            extract_images(
                workbook, sheet_name=DECLARATION_SHEET, rows=[2], header_row=0,
            )
        self.assertEqual(caught.exception.code, "header_row_invalid")

    def test_duplicate_rows_are_extracted_once(self):
        extraction = self.extract_mixed()
        again = extract_images(
            self.workbook(mixed_package()), sheet_name=DECLARATION_SHEET,
            rows=[2, 2, 2, 3],
        )
        self.assertEqual([row.row for row in again.rows], [2, 3])
        self.assertEqual(len(extraction.rows), 15)


if __name__ == "__main__":
    unittest.main()
