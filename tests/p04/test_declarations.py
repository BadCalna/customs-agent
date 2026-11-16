"""申报要素提取：语义表头、原文保留、来源单元格与问题定位（P04）。

样例全部由 :mod:`tests.p04.builders` 与 :mod:`tests.p04.fixtures` 构造，不含真实业务数据。
"""

from __future__ import annotations

from adapters.files.declarations import (
    FieldFact,
    SemanticLayoutError,
    evidence_fact,
    extract_declarations,
    field_matches,
    locate_declaration_layout,
    normalized,
)
from tests.p04.builders import (
    DECLARATION_HEADERS,
    DECLARATION_SHEET,
    WorkbookTestCase,
    build_package,
    sheet_of,
)
from tests.p04.fixtures import drawing_with_anchor, png


class AliasTest(WorkbookTestCase):
    """表头归一：大小写、空白、括号说明、明确别名。"""

    def test_normalized_and_field_matches(self):
        self.assertEqual(normalized(" 采购 合同号\n"), "采购合同号")
        self.assertTrue(field_matches("海关编码（查询参考网站如下）", "海关编码"))
        self.assertTrue(field_matches("hscode", "HSCODE"))
        self.assertTrue(field_matches("退税率%", "退税率"))
        self.assertFalse(field_matches("英文品名（可百度翻译）", "品名"))
        self.assertFalse(field_matches("中文单位（与合同保持一致）", "单位"))
        self.assertFalse(field_matches("附件清单品名（如合同不涉及不填写）", "品名"))

    def test_evidence_ignores_text_in_refund_rate_column(self):
        def fact(field, value, formula=None):
            return FieldFact(
                field=field, value=value, cell="F3", column="F", header="退税率", formula=formula
            )

        self.assertFalse(evidence_fact("refund_rate", fact("refund_rate", "整机类型")))
        self.assertTrue(evidence_fact("refund_rate", fact("refund_rate", "0.13")))
        self.assertTrue(evidence_fact("hs", fact("hs", "0000000000")))


class LayoutTest(WorkbookTestCase):
    """表头行与字段列的语义定位。"""

    def test_semantic_headers_with_notes_and_header_row_offset(self):
        headers = (
            "采购合同号\n",
            "合同项号（按合同顺序填写）",
            "品名（与合同正文一致）",
            "HSCODE（10 位）",
            "申报要素\n（按商编查询）",
            "退税率%",
            "附图",
            "历史意见",
        )
        package = build_package(
            declaration_headers=headers,
            declaration_header_row=3,
            declaration_rows=[
                ("SYN-C", "1", "合成部件甲", "0000000000", "0｜用途：测试", "0.13", None, "原意见保留"),
            ],
            include_packing=False,
        )
        sheet_of(package, DECLARATION_SHEET).text("A1", "申报要素")
        workbook = self.build(package)
        layout = locate_declaration_layout(workbook)
        self.assertEqual(layout.header_row, 3)
        self.assertEqual(
            layout.columns,
            {
                "contract": "A",
                "item": "B",
                "name": "C",
                "hs": "D",
                "elements": "E",
                "refund_rate": "F",
                "image": "G",
                "existing_opinion": "H",
            },
        )
        records = extract_declarations(workbook, layout=layout).records
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.row, 4)
        self.assertEqual(record.status, "identified")
        self.assertEqual(record.cell("contract"), "A4")
        self.assertEqual(record.raw["elements"], "0｜用途：测试")
        self.assertEqual(record.raw["hs"], "0000000000")
        self.assertEqual(
            record.attributes["elements"]["items"],
            [{"label": None, "value": "0"}, {"label": "用途", "value": "测试"}],
        )

    def test_missing_header_row(self):
        empty = build_package(declaration_headers=(None,) * 8, include_packing=False)
        sheet_of(empty, DECLARATION_SHEET).text("A1", "申报要素")
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_declarations(self.build(empty))
        self.assertEqual(caught.exception.code, "header_row_missing")
        self.assertEqual(caught.exception.sheet, DECLARATION_SHEET)

    def test_missing_required_column(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "退税率", "附图", "历史意见")
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "0.13", None, None)],
            include_packing=False,
        )
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_declarations(self.build(package))
        error = caught.exception
        self.assertEqual(error.code, "missing_required_columns")
        self.assertEqual(error.details["missing"], ["elements"])
        self.assertEqual(error.sheet, DECLARATION_SHEET)

    def test_duplicate_required_column_is_rejected(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "海关编码")
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "1111111111")],
            include_packing=False,
        )
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_declarations(self.build(package))
        error = caught.exception
        self.assertEqual(error.code, "duplicate_semantic_column")
        self.assertEqual(error.field, "hs")
        self.assertEqual(error.details["cells"], ["D1", "F1"])

    def test_duplicate_optional_column_marked_ambiguous(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "置信度", "置信度")
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "高", "中")],
            include_packing=False,
        )
        extraction = extract_declarations(self.build(package))
        layout = extraction.layout
        self.assertNotIn("confidence", layout.columns)
        self.assertIn("ambiguous_semantic_column:confidence", layout.warnings)
        self.assertEqual(
            [column.header_cell for column in layout.ambiguous_columns], ["F1", "G1"]
        )
        # 两列原文都保留在“未识别列”，没有被当成某一列的值。
        record = extraction.records[0]
        self.assertEqual(
            [item["value"] for item in record.unmapped], ["高", "中"]
        )

    def test_headerless_legacy_ok_column_is_dropped_but_comments_kept(self):
        # Jay 2026-11-16 口径：无表头的旧“OK/通过”标记列不保留为业务字段。
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", None, None)
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "OK", "历史说明")],
            include_packing=False,
        )
        extraction = extract_declarations(self.build(package))
        self.assertIn("legacy_approval_column_ignored:F", extraction.layout.warnings)
        self.assertNotIn("F", [column.column for column in extraction.layout.unmapped_columns])
        record = extraction.records[0]
        self.assertEqual([(item["column"], item["value"]) for item in record.unmapped],
                         [("G", "历史说明")])
        self.assertNotIn("OK", str(record.as_dict()))

    def test_mixed_headerless_column_skips_ok_values_only(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", None)
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途", "OK"),
                ("SYN-C", "2", "合成乙", "0000000000", "用途", "历史说明"),
            ],
            include_packing=False,
        )
        extraction = extract_declarations(self.build(package))
        self.assertIn("legacy_approval_values_ignored:F", extraction.layout.warnings)
        self.assertEqual([item["value"] for item in extraction.records[0].unmapped], [])
        self.assertEqual([item["value"] for item in extraction.records[1].unmapped], ["历史说明"])

    def test_single_total_formula_row_is_not_a_product(self):
        # 审阅复现：一条商品 + 一行只有 SUM(F2:F2) 的行，应只有 1 个商品，合计行进报告。
        package = build_package(
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "0.13")],
            include_packing=False,
        )
        sheet = sheet_of(package, DECLARATION_SHEET)
        sheet.formula("F3", "SUM(F2:F2)")
        extraction = extract_declarations(self.build(package))
        self.assertEqual(len(extraction.records), 1)
        self.assertEqual(
            [(item["row"], item["reason"]) for item in extraction.non_item_rows],
            [(3, "summary_formula_only")],
        )

    def test_labelled_opinion_column_keeps_original_text(self):
        # 有明确表头的“历史意见”属于 D2 历史上下文，原文保留（含 OK 字样），不按旧 OK 列丢弃。
        package = build_package(
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "0.13", None, "OK")],
            include_packing=False,
        )
        extraction = extract_declarations(self.build(package))
        self.assertEqual(extraction.records[0].facts["existing_opinion"].value, "OK")

    def test_missing_optional_columns_are_warnings(self):
        extraction = extract_declarations(
            self.build(
                build_package(
                    declaration_rows=[
                        ("SYN-C", "1", "合成甲", "0000000000", "用途", "0.13", None, None),
                    ],
                    include_packing=False,
                )
            )
        )
        for field in (
            "quantity",
            "unit",
            "ai_hs",
            "confirmed_hs",
            "confidence",
            "precheck_conclusion",
        ):
            self.assertIn(f"missing_column:{field}", extraction.layout.warnings)
        self.assertNotIn("missing_column:image", extraction.layout.warnings)

    def test_duplicate_header_rows_are_ambiguous(self):
        package = build_package(
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途")],
            include_packing=False,
        )
        sheet = sheet_of(package, DECLARATION_SHEET)
        for index, header in enumerate(DECLARATION_HEADERS, start=1):
            sheet.text(f"{chr(64 + index)}3", header)
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_declarations(self.build(package))
        self.assertEqual(caught.exception.code, "header_row_ambiguous")
        self.assertEqual(caught.exception.details["rows"], [1, 3])

    def test_missing_declaration_sheet(self):
        package = build_package(include_declaration=False)
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_declarations(self.build(package))
        self.assertEqual(caught.exception.code, "sheet_not_found")
        self.assertEqual(caught.exception.sheet, "申报要素")
        self.assertIn("申报要素", str(caught.exception))


class RecordTest(WorkbookTestCase):
    """一条申报记录＝一个商品行；原文、来源、状态与问题。"""

    def test_hs_blank_and_abnormal_are_preserved(self):
        extraction = extract_declarations(
            self.build(
                build_package(
                    declaration_rows=[
                        ("SYN-C", "1", "合成甲", None, "用途：测试"),
                        ("SYN-C", "2", "合成乙", "ABC-9x", "用途：测试"),
                    ],
                    include_packing=False,
                )
            )
        )
        first, second = extraction.records
        self.assertEqual(first.raw["hs"], "")
        self.assertEqual(second.raw["hs"], "ABC-9x")
        self.assertEqual(first.status, "identified")
        self.assertEqual(second.status, "identified")
        # 原 HS 空白不补值，也不因空白被判成行级问题；问题只按身份字段缺失给。
        self.assertEqual([issue.code for issue in first.issues], [])
        self.assertEqual(second.cell("hs"), "D3")

    def test_existing_opinion_and_result_columns_are_located_and_kept(self):
        headers = DECLARATION_HEADERS + (
            "AI推荐HScode",
            "业务员确认Hscode",
            "置信度",
            "AI预审结论",
        )
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[
                (
                    "SYN-C",
                    "1",
                    "合成甲",
                    "0000000000",
                    "用途：测试",
                    "0.13",
                    None,
                    "历史意见：保留原文",
                    "1111111111",
                    "2222222222",
                    "高",
                    "待补型号",
                )
            ],
            include_packing=False,
        )
        extraction = extract_declarations(self.build(package))
        record = extraction.records[0]
        self.assertEqual(record.raw["existing_opinion"], "历史意见：保留原文")
        self.assertEqual(record.raw["ai_hs"], "1111111111")
        self.assertEqual(record.raw["confirmed_hs"], "2222222222")
        self.assertEqual(record.raw["confidence"], "高")
        self.assertEqual(record.raw["precheck_conclusion"], "待补型号")
        self.assertEqual(record.cell("existing_opinion"), "H2")
        self.assertEqual(record.status, "identified")
        self.assertEqual(record.facts["existing_opinion"].header, "历史意见")

    def test_blank_rows_are_skipped(self):
        extraction = extract_declarations(
            self.build(
                build_package(
                    declaration_rows=[
                        ("SYN-C", "1", "合成甲", "0000000000", "用途：测试"),
                        (None,) * 8,
                        ("SYN-C", "2", "合成乙", "0000000000", "用途：测试"),
                    ],
                    include_packing=False,
                )
            )
        )
        self.assertEqual([record.row for record in extraction.records], [2, 4])
        self.assertEqual(extraction.non_item_rows, ())

    def test_image_only_row_is_a_record_with_missing_identity(self):
        package = build_package(
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途：测试", None, None, None),
            ],
            include_packing=False,
        )
        package.image("xl/media/image1.png", png())
        package.attach_drawing(
            DECLARATION_SHEET,
            drawing_with_anchor("xl/media/image1.png", row=2, column=6),
            "xl/media/image1.png",
        )
        extraction = extract_declarations(self.build(package))
        self.assertEqual([record.row for record in extraction.records], [2, 3])
        record = extraction.records[1]
        self.assertEqual(record.status, "requires_review")
        self.assertEqual(
            [issue.code for issue in record.issues],
            ["missing_contract", "missing_item", "missing_name"],
        )
        self.assertEqual(
            [issue.cell for issue in record.issues], ["A3", "B3", "C3"]
        )
        self.assertEqual(record.floating_image_cells, ("G3",))
        self.assertEqual(record.source_cells, "G3:G3")

    def test_vertical_merge_borrows_contract_anchor(self):
        package = build_package(
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途：测试"),
                (None, "2", "合成乙", "0000000000", "用途：测试"),
            ],
            include_packing=False,
        )
        sheet_of(package, DECLARATION_SHEET).merge("A2:A3")
        extraction = extract_declarations(self.build(package))
        second = extraction.records[1]
        fact = second.facts["contract"]
        self.assertEqual(fact.value, "SYN-C")
        self.assertEqual(fact.cell, "A2")
        self.assertTrue(fact.from_merge_anchor)
        self.assertEqual(second.status, "identified")

    def test_horizontal_merge_does_not_borrow_neighbour_column(self):
        package = build_package(
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途：测试"),
                ("SYN-C", "2", None, "0000000000", "用途：测试"),
            ],
            include_packing=False,
        )
        sheet = sheet_of(package, DECLARATION_SHEET)
        sheet.merge("C3:D3")
        sheet.text("D3", "错位内容")
        extraction = extract_declarations(self.build(package))
        record = extraction.records[1]
        self.assertEqual(record.raw["name"], "")
        self.assertEqual(record.cell("name"), "C3")
        self.assertFalse(record.facts["name"].from_merge_anchor)
        self.assertEqual([issue.code for issue in record.issues], ["missing_name"])
        # 邻列内容只属于它自己的字段，不被“借”成品名。
        self.assertEqual(record.raw["hs"], "错位内容")

    def test_summary_footer_and_note_rows_are_not_items(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "数量", "单位")
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途：测试", 10, "个"),
            ],
            include_packing=False,
        )
        sheet = sheet_of(package, DECLARATION_SHEET)
        sheet.formula("F3", "SUM(F2:F2)")
        sheet.formula("G3", "SUM(G2:G2)")
        sheet.text("C4", "合计")
        sheet.formula("F4", "SUM(F2:F3)")
        sheet.text("I5", "说明：本表只用于测试")
        sheet.text("A6", "SYN-C 出完")
        extraction = extract_declarations(self.build(package))
        self.assertEqual([record.row for record in extraction.records], [2])
        reasons = {item["row"]: item["reason"] for item in extraction.non_item_rows}
        self.assertEqual(
            reasons,
            {
                3: "summary_formula_only",
                4: "summary_row",
                5: "note_only",
                6: "contract_only_or_footer",
            },
        )

    def test_row_with_only_strong_fact_is_a_record(self):
        extraction = extract_declarations(
            self.build(
                build_package(
                    declaration_rows=[
                        ("SYN-C", "1", "合成甲", "0000000000", "用途：测试"),
                        (None, None, None, "0000000000", None),
                    ],
                    include_packing=False,
                )
            )
        )
        record = extraction.records[1]
        self.assertEqual(record.status, "requires_review")
        self.assertEqual(
            [issue.code for issue in record.issues],
            ["missing_contract", "missing_item", "missing_name"],
        )

    def test_dispimg_formula_is_kept_in_fact(self):
        package = build_package(
            declaration_rows=[
                ("SYN-C", "1", "合成甲", "0000000000", "用途：测试", None, None, None),
            ],
            include_packing=False,
        )
        sheet = sheet_of(package, DECLARATION_SHEET)
        sheet.shared("G2", '_xlfn.DISPIMG("ID_SYNTHETIC_A",1)', si=0, span="G2:G2")
        package.cell_image("ID_SYNTHETIC_A", "xl/media/synthetic.png")
        package.image("xl/media/synthetic.png", png())
        extraction = extract_declarations(self.build(package))
        fact = extraction.records[0].facts["image"]
        self.assertEqual(fact.value, "")
        self.assertIn("DISPIMG", fact.formula)
        self.assertTrue(fact.filled)
        self.assertEqual(fact.cell, "G2")
