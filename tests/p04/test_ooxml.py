"""OOXML 读取地基：只读、异常显式、图片资源可追溯（P04/P05 共用）。"""

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from adapters.files.ooxml import (
    OoxmlError,
    Workbook,
    cell_position,
    col_label,
    col_number,
    shift_formula_to,
)
from tests.p04.fixtures import (
    Package,
    drawing_with_anchor,
    formula,
    png,
    standard_package,
)


class PackageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def build(self, package: Package, name="sample.xlsx") -> Path:
        return Path(package.build(self.root / name))

    def test_reads_sheets_cells_and_merges(self):
        path = self.build(standard_package())
        workbook = Workbook.open(path)
        self.assertEqual([sheet.name for sheet in workbook.sheets],
                         ["节点①装箱明细", "节点①申报要素", "节点①箱件汇总"])
        packing = workbook.find_sheet("装箱明细")
        self.assertEqual(packing.cells["A4"].value, "合同号")
        self.assertEqual(packing.cells["F5"].value, "10")
        declaration = workbook.find_sheet("申报要素")
        self.assertEqual(declaration.cells["B3"].value, "2")
        self.assertEqual(declaration.merges[0].ref, "A6:C6")
        self.assertEqual(workbook.source_sha256, path.read_bytes() and workbook.source_sha256)
        self.assertEqual(len(workbook.source_sha256), 64)

    def test_vertical_merge_borrows_anchor_but_horizontal_does_not(self):
        package = Package()
        sheet = package.sheet("申报要素")
        sheet.text("A1", "纵向合并值").merge("A1:A3")
        sheet.text("B1", "横向合并值").merge("B1:C1")
        workbook = Workbook.open(self.build(package))
        sheet = workbook.sheet("申报要素")
        self.assertEqual(sheet.value_at(2, 1), ("纵向合并值", "A1", True))
        self.assertEqual(sheet.value_at(1, 3), ("", "C1", False))

    def test_shared_formula_is_shifted_per_row(self):
        package = Package()
        sheet = package.sheet("申报要素")
        sheet.shared("D2", "B2+C2", si=1, span="D2:D4")
        sheet.shared("D3", None, si=1)
        sheet.shared("D4", None, si=1)
        workbook = Workbook.open(self.build(package))
        formulas = workbook.sheet("申报要素").formulas
        self.assertEqual(formulas["D2"], "B2+C2")
        self.assertEqual(formulas["D3"], "B3+C3")
        self.assertEqual(formulas["D4"], "B4+C4")

    def test_shared_formula_without_master_is_an_error(self):
        package = Package()
        package.sheet("申报要素").shared("D3", None, si=7)
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(self.build(package))
        self.assertEqual(caught.exception.code, "shared_formula_master_missing")

    def test_ambiguous_shared_formula_master_is_rejected(self):
        package = Package()
        sheet = package.sheet("申报要素")
        sheet.shared("D2", "B2", si=3, span="D2:D3")
        sheet.shared("D3", "B3", si=3, span="D3:D4")
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(self.build(package))
        self.assertEqual(caught.exception.code, "ambiguous_shared_formula_master")

    def test_shared_formula_shifting_rejects_whole_column_reference(self):
        with self.assertRaises(OoxmlError) as caught:
            shift_formula_to("SUM(A:A)", "A2", "A3")
        self.assertEqual(caught.exception.code, "shared_formula_needs_native_editor")

    def test_dispimg_reference_resolves_media_and_decodes_pixels(self):
        workbook = Workbook.open(self.build(standard_package()))
        references = workbook.image_references()
        self.assertEqual([(ref.sheet, ref.cell) for ref in references],
                         [("节点①申报要素", "E2"), ("节点①申报要素", "E3")])
        first = references[0]
        self.assertEqual(first.status, "resolved")
        self.assertEqual(first.image_id, "ID_SYNTHETIC_A")
        self.assertTrue(first.resources[0].media.usable)
        self.assertEqual(first.resources[0].media.sha256, workbook.media(
            first.resources[0].media.part).sha256)

    def test_missing_media_is_not_treated_as_absent_image(self):
        package = standard_package()
        del package.media["xl/media/image1.png"]
        workbook = Workbook.open(self.build(package))
        reference = workbook.image_references()[0]
        self.assertEqual(reference.status, "resolved")
        self.assertEqual(reference.resources[0].media.status, "missing_resource")
        self.assertFalse(reference.resources[0].media.usable)

    def test_corrupt_media_is_corrupt_resource(self):
        package = standard_package()
        package.media["xl/media/image1.png"] = b"not a png"
        workbook = Workbook.open(self.build(package))
        media = workbook.image_references()[0].resources[0].media
        self.assertEqual(media.status, "corrupt_resource")
        self.assertTrue(media.decode_status.startswith("failed:"))

    def test_expression_dispimg_requires_review_with_candidates(self):
        package = standard_package()
        sheet = package.sheet("申报要素")
        sheet.formula("E7", 'IF(A2="",_xlfn.DISPIMG("ID_SYNTHETIC_A",1),"")')
        workbook = Workbook.open(self.build(package))
        reference = next(ref for ref in workbook.image_references() if ref.cell == "E7")
        self.assertEqual(reference.status, "requires_review")
        self.assertEqual(reference.formula_status, "expression_display_not_evaluated")
        self.assertEqual([resource.image_id for resource in reference.resources], ["ID_SYNTHETIC_A"])

    def test_duplicate_image_definition_is_ambiguous(self):
        package = standard_package()
        package.cell_image("ID_SYNTHETIC_A", "xl/media/image2.png")
        package.image("xl/media/image2.png", png(b"\x10\x20\x30"))
        workbook = Workbook.open(self.build(package))
        reference = workbook.image_references()[0]
        self.assertEqual(reference.status, "ambiguous_definition")
        self.assertEqual(len(reference.resources), 2)

    def test_picture_anchor_exact_cell(self):
        package = standard_package()
        package.image("xl/media/image9.png", png(b"\xff\x00\x00"))
        package.attach_drawing(
            "节点①申报要素", drawing_with_anchor("xl/media/image9.png", row=4, column=6),
            "xl/media/image9.png",
        )
        workbook = Workbook.open(self.build(package))
        anchors = [anchor for anchor in workbook.picture_anchors() if anchor.images]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].exact_cell(7), 5)
        self.assertIsNone(anchors[0].exact_cell(6))

    def test_unrecognized_sheet_label_is_an_error(self):
        workbook = Workbook.open(self.build(standard_package()))
        with self.assertRaises(OoxmlError) as caught:
            workbook.find_sheet("节点①")
        self.assertEqual(caught.exception.code, "sheet_ambiguous")
        with self.assertRaises(OoxmlError) as caught:
            workbook.find_sheet("不存在的页签")
        self.assertEqual(caught.exception.code, "sheet_not_found")

    def test_non_xlsx_zip_is_rejected(self):
        path = self.root / "wrong.xlsx"
        with ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "not an excel")
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(path)
        self.assertEqual(caught.exception.code, "not_spreadsheet")

    def test_duplicate_zip_entry_is_rejected(self):
        path = self.root / "duplicate.xlsx"
        Package().build(path)
        with ZipFile(path, "a") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(path)
        self.assertEqual(caught.exception.code, "duplicate_zip_entry")

    def test_dtd_and_entity_xml_is_rejected(self):
        package = Package()
        package.sheet("申报要素")
        package.parts["xl/sharedStrings.xml"] = (
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<sst xmlns="%s"><si><t>&xxe;</t></si></sst>' % "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        )
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(self.build(package))
        self.assertEqual(caught.exception.code, "unsupported_xml_declaration")

    def test_relationship_escaping_package_is_rejected(self):
        package = Package()
        package.sheet("申报要素")
        package.parts["xl/_rels/workbook.xml.rels"] = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Type="worksheet" Target="../../etc/passwd"/></Relationships>'
        )
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(self.build(package))
        self.assertEqual(caught.exception.code, "unsafe_relationship_target")

    def test_expanded_size_limit_is_enforced(self):
        path = self.build(standard_package())
        with self.assertRaises(OoxmlError) as caught:
            Workbook.open(path, max_expanded_bytes=1024)
        self.assertEqual(caught.exception.code, "expanded_size_exceeded")

    def test_cell_reference_helpers(self):
        self.assertEqual(cell_position("$B$7"), (2, 7))
        self.assertEqual(cell_position("aa10"), (27, 10))
        self.assertEqual(col_label(27), "AA")
        self.assertEqual(col_number("AA"), 27)
        for bad in ("", "A0", "XFE1", "1A"):
            with self.assertRaises(OoxmlError):
                cell_position(bad)

    def test_boolean_and_inline_values_keep_original_text(self):
        package = Package()
        sheet = package.sheet("申报要素")
        sheet.cells["A1"] = '<c r="A1" t="b"><v>1</v></c>'
        sheet.cells["B1"] = '<c r="B1" t="inlineStr"><is><r><t>富</t></r><r><t>文本</t></r></is></c>'
        package.shared_strings.append("共享文本")
        sheet.cells["C1"] = '<c r="C1" t="s"><v>0</v></c>'
        workbook = Workbook.open(self.build(package))
        sheet = workbook.sheet("申报要素")
        self.assertEqual(sheet.cells["A1"].value, "TRUE")
        self.assertEqual(sheet.cells["B1"].value, "富文本")
        self.assertEqual(sheet.cells["C1"].value, "共享文本")

    def test_invalid_drawing_coordinate_raises_ooxml_error_instead_of_value_error(self):
        for bad_col in ("--5", "²"):
            with self.subTest(bad_col=bad_col):
                package = standard_package()
                package.image("xl/media/image9.png", png(b"\xff\x00\x00"))
                bad_drawing = drawing_with_anchor("xl/media/image9.png", row=4, column=6).replace(
                    "<xdr:col>6</xdr:col>", f"<xdr:col>{bad_col}</xdr:col>", 1
                )
                package.attach_drawing("节点①申报要素", bad_drawing, "xl/media/image9.png")
                workbook = Workbook.open(self.build(package))
                with self.assertRaises(OoxmlError) as caught:
                    workbook.picture_anchors()
                self.assertEqual(caught.exception.code, "invalid_drawing_coordinate")


if __name__ == "__main__":
    unittest.main()
