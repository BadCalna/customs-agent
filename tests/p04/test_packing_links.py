"""装箱明细提取与成员关联：分箱、合并申报、冲突与未匹配（P04）。

样例全部由 :mod:`tests.p04.builders` 与 :mod:`tests.p04.fixtures` 构造，不含真实业务数据。
"""

from __future__ import annotations

import json

from adapters.files.declarations import SemanticLayoutError
from adapters.files.packing_links import (
    KeyParseError,
    contract_key,
    extract_packing,
    map_workbook,
    parse_items,
)
from tests.p04.builders import (
    DECLARATION_SHEET,
    WorkbookTestCase,
    build_package,
)
from tests.p04.fixtures import standard_package


def row(contract, item, name, hs="0000000000", elements="用途：测试", **extra):
    """申报要素行（列序：合同号、项号、品名、海关编码、申报要素、退税率、附图、意见）。"""
    return (
        contract,
        item,
        name,
        hs,
        elements,
        extra.get("refund_rate"),
        extra.get("image"),
        extra.get("opinion"),
    )


def pack(
    contract,
    item,
    primary_name,
    *,
    alias=None,
    specification=None,
    manufacturer_model=None,
    material=None,
    quantity=None,
    unit=None,
    merge_hint=None,
):
    """装箱明细行（列序见 ``DECLARATION_HEADERS``/``PACKING_HEADERS``）。"""
    return (
        contract,
        item,
        primary_name,
        alias,
        specification,
        manufacturer_model,
        material,
        quantity,
        unit,
        merge_hint,
    )


class ParseTest(WorkbookTestCase):
    """项号/合同号的词法展开：只按原文，不做数值改写。"""

    def test_parse_items_range_and_list(self):
        self.assertEqual(parse_items("1-3"), ["1", "2", "3"])
        self.assertEqual(parse_items("1、2、3"), ["1", "2", "3"])
        self.assertEqual(parse_items("7"), ["7"])
        self.assertEqual(parse_items("01-03"), ["01", "02", "03"])
        self.assertEqual(parse_items("1；3"), ["1", "3"])

    def test_parse_items_rejects_ambiguous_input(self):
        cases = {
            "": "missing_item",
            "3-1": "reversed_item_range",
            "甲": "unparsed_item_list",
            "1、1": "duplicate_item_in_list",
            "1-2000": "excessive_item_range",
        }
        for value, code in cases.items():
            with self.assertRaises(KeyParseError) as caught:
                parse_items(value)
            self.assertEqual(caught.exception.code, code, value)

    def test_contract_key(self):
        self.assertEqual(contract_key(" SYN-C "), ("SYN-C", "SYN-C"))
        with self.assertRaises(KeyParseError) as caught:
            contract_key("")
        self.assertEqual(caught.exception.code, "missing_contract")
        with self.assertRaises(KeyParseError) as caught:
            contract_key("SYN-A、SYN-B")
        self.assertEqual(caught.exception.code, "multiple_contracts")


class PackingExtractionTest(WorkbookTestCase):
    def test_packing_facts_have_sources(self):
        extraction = extract_packing(
            self.build(
                build_package(
                    declaration_rows=[row("SYN-C", "1", "合成甲")],
                    packing_rows=[
                        pack(
                            "SYN-C",
                            "1",
                            "合成甲",
                            alias="别名甲",
                            specification="规格A",
                            manufacturer_model="MODEL-A",
                            material="M-1",
                            quantity=10,
                            unit="个",
                            merge_hint="否",
                        )
                    ],
                )
            )
        )
        record = extraction.records[0]
        self.assertEqual(record.row, 5)
        self.assertEqual(record.cell("contract"), "A5")
        self.assertEqual(record.cell("item"), "B5")
        self.assertEqual(record.cell("primary_name"), "C5")
        self.assertEqual(record.raw["alias"], "别名甲")
        self.assertEqual(record.raw["specification"], "规格A")
        self.assertEqual(record.raw["manufacturer_model"], "MODEL-A")
        self.assertEqual(record.raw["material"], "M-1")
        self.assertEqual(record.raw["quantity"], "10")
        self.assertEqual(record.raw["unit"], "个")
        self.assertEqual(record.raw["merge_hint"], "否")
        self.assertEqual(record.status, "identified")
        self.assertEqual(record.source_cells, "A5:J5")

    def test_primary_name_falls_back_to_legacy_name_header(self):
        headers = ("合同号", "合同项号", "品名", "规格", "数量", "单位")
        extraction = extract_packing(
            self.build(
                build_package(
                    declaration_rows=[row("SYN-C", "1", "合成甲")],
                    packing_headers=headers,
                    packing_rows=[("SYN-C", "1", "合成甲", "规格A", 10, "个")],
                )
            )
        )
        self.assertIn("legacy_primary_name_from:品名", extraction.layout.warnings)
        self.assertNotIn("missing_column:primary_name", extraction.layout.warnings)
        self.assertEqual(extraction.records[0].raw["primary_name"], "合成甲")

    def test_missing_primary_name_flags_row(self):
        extraction = extract_packing(
            self.build(
                build_package(
                    declaration_rows=[row("SYN-C", "1", "合成甲")],
                    packing_rows=[
                        pack("SYN-C", "1", None, specification="规格A", quantity=10, unit="个")
                    ],
                )
            )
        )
        record = extraction.records[0]
        self.assertEqual(record.status, "requires_review")
        self.assertEqual([issue.code for issue in record.issues], ["missing_primary_name"])
        self.assertEqual(record.issues[0].cell, "C5")

    def test_packing_summary_row_is_not_a_member(self):
        package = build_package(
            declaration_rows=[row("SYN-C", "1", "合成甲")],
            packing_rows=[pack("SYN-C", "1", "合成甲", quantity=10, unit="个")],
        )
        sheet = next(s for s in package.sheets if s.name.endswith("装箱明细"))
        sheet.formula("H6", "SUM(H5:H5)")
        sheet.formula("I6", "SUM(I5:I5)")
        extraction = extract_packing(self.build(package))
        self.assertEqual([record.row for record in extraction.records], [5])
        self.assertEqual(
            [(item["row"], item["reason"]) for item in extraction.non_item_rows],
            [(6, "summary_formula_only")],
        )

    def test_missing_packing_sheet(self):
        with self.assertRaises(SemanticLayoutError) as caught:
            extract_packing(self.build(build_package(include_packing=False)))
        self.assertEqual(caught.exception.code, "sheet_not_found")
        self.assertIn("装箱明细", str(caught.exception))


class LinkTest(WorkbookTestCase):
    """一条申报记录一个商品；装箱行只是成员。"""

    def test_one_declaration_row_per_product_with_multiple_members(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("SYN-C", "1", "合成甲"),
                    row("SYN-C", "2", "合成乙"),
                    row("SYN-C", "3", "合成丙"),
                    row("SYN-C", "4", "合成丁"),
                ],
                packing_rows=[
                    pack("SYN-C", "1", "合成甲", material="M-1", quantity=10, unit="个"),
                    pack("SYN-C", "2", "合成乙", material="M-2", quantity=20, unit="个"),
                    pack("SYN-C", "2", "合成乙", material="M-2", quantity=5, unit="个"),
                    pack("SYN-C", "3", "合成丙", material="M-3", quantity=7, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual(len(mapping.links), 4)
        self.assertEqual(len(mapping.packing.records), 4)
        self.assertEqual([link.row for link in mapping.links], [2, 3, 4, 5])
        self.assertEqual([member.packing_row for member in mapping.links[0].members], [5])
        self.assertEqual([member.packing_row for member in mapping.links[1].members], [6, 7])
        self.assertEqual([member.packing_row for member in mapping.links[2].members], [8])
        self.assertEqual(mapping.links[3].members, ())
        self.assertEqual(
            [issue.code for issue in mapping.links[3].issues],
            ["missing_packing_member:4"],
        )
        self.assertEqual(mapping.links[3].issues[0].cell, "B5")
        self.assertEqual(
            [link.status for link in mapping.links],
            ["identified", "identified", "identified", "requires_review"],
        )
        self.assertEqual(mapping.issues, ())

    def test_same_row_number_is_not_a_link(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "9", "合成甲")],
                packing_header_row=1,
                packing_rows=[pack("SYN-C", "8", "合成甲", quantity=10, unit="个")],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual(mapping.links[0].row, 2)
        self.assertEqual(mapping.packing.records[0].row, 2)
        self.assertEqual(mapping.links[0].members, ())
        self.assertEqual(
            [issue.code for issue in mapping.links[0].issues],
            ["missing_packing_member:9"],
        )

    def test_merged_declaration_range_expands_to_members(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("SYN-C", "1-3", "合成甲乙丙"),
                    row("SYN-C", "4、5", "合成丁戊"),
                ],
                packing_rows=[
                    pack("SYN-C", "1", "合成甲", quantity=1, unit="个"),
                    pack("SYN-C", "2", "合成乙", quantity=2, unit="个"),
                    pack("SYN-C", "3", "合成丙", quantity=3, unit="个"),
                    pack("SYN-C", "4", "合成丁", quantity=4, unit="个"),
                    pack("SYN-C", "5", "合成戊", quantity=5, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        first, second = mapping.links
        self.assertEqual([member.item_no for member in first.members], ["1", "2", "3"])
        self.assertEqual([member.packing_row for member in first.members], [5, 6, 7])
        self.assertEqual([member.item_no for member in second.members], ["4", "5"])
        self.assertEqual([member.packing_row for member in second.members], [8, 9])
        self.assertEqual([link.status for link in mapping.links], ["identified", "identified"])
        self.assertEqual(mapping.issues, ())

    def test_zero_padded_range_keeps_original_width(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "01-03", "合成甲乙丙")],
                packing_rows=[
                    pack("SYN-C", "01", "合成甲", quantity=1, unit="个"),
                    pack("SYN-C", "02", "合成乙", quantity=2, unit="个"),
                    pack("SYN-C", "03", "合成丙", quantity=3, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual(
            [member.item_no for member in mapping.links[0].members], ["01", "02", "03"]
        )
        self.assertEqual(mapping.links[0].status, "identified")

    def test_reversed_range_is_reported_not_expanded(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "3-1", "合成甲乙丙")],
                packing_rows=[
                    pack("SYN-C", "1", "合成甲", quantity=1, unit="个"),
                    pack("SYN-C", "2", "合成乙", quantity=2, unit="个"),
                    pack("SYN-C", "3", "合成丙", quantity=3, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        link = mapping.links[0]
        self.assertEqual(link.members, ())
        self.assertEqual(link.status, "requires_review")
        self.assertEqual(
            [(issue.code, issue.cell) for issue in link.issues],
            [("reversed_item_range", "B2")],
        )
        # Jay 2026-11-16：倒序项号按笔误处理，上传时就该拒绝（P06 不建任务）。
        self.assertTrue(link.issues[0].blocking)
        self.assertEqual(link.issues[0].severity, "error")
        self.assertFalse(mapping.ready_for_import)
        self.assertEqual(
            [issue.code for issue in mapping.blocking_issues], ["reversed_item_range"]
        )
        self.assertEqual(
            [issue.code for issue in mapping.issues], ["unlinked_packing_row"] * 3
        )

    def test_unparsable_and_duplicate_item_lists(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("SYN-C", "甲", "合成甲"),
                    row("SYN-C", "1、1", "合成乙"),
                ],
                packing_rows=[pack("SYN-C", "1", "合成甲", quantity=1, unit="个")],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual(
            [issue.code for issue in mapping.links[0].issues], ["unparsed_item_list"]
        )
        self.assertEqual(
            [issue.code for issue in mapping.links[1].issues],
            ["duplicate_item_in_list"],
        )
        self.assertEqual(
            [issue.cell for issue in mapping.links[1].issues], ["B3"]
        )
        self.assertFalse(mapping.ready_for_import)
        self.assertEqual(
            [issue.code for issue in mapping.blocking_issues],
            ["unparsed_item_list", "duplicate_item_in_list"],
        )

    def test_same_item_with_multiple_packing_rows_is_one_commodity_in_boxes(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "2", "合成乙")],
                packing_rows=[
                    pack("SYN-C", "2", "合成乙", specification="规格A", quantity=10, unit="个"),
                    pack("SYN-C", "2", "合成乙", specification="规格B", quantity=5, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        link = mapping.links[0]
        # Jay 2026-11-16 口径：同合同项多条装箱行 = 同一商品分多箱，
        # 申报行只保留一条，装箱行全部作成员；事实差异只作 info 提示，不改识别状态。
        self.assertEqual([member.packing_row for member in link.members], [5, 6])
        self.assertEqual(link.status, "identified")
        issue = link.issues[0]
        self.assertEqual(issue.code, "packing_members_same_item:2")
        self.assertEqual(issue.severity, "info")
        self.assertFalse(issue.blocking)
        self.assertEqual(issue.details["packing_rows"], [5, 6])
        self.assertEqual(issue.details["differing_fields"], ["specification"])
        self.assertTrue(mapping.ready_for_import)

    def test_missing_item_number_blocks_upload(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", None, "合成甲")],
                packing_rows=[pack("SYN-C", "1", "合成甲", quantity=1, unit="个")],
            )
        )
        mapping = map_workbook(workbook)
        self.assertFalse(mapping.ready_for_import)
        self.assertEqual([issue.code for issue in mapping.blocking_issues], ["missing_item"])

    def test_same_key_claimed_by_two_declarations(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("SYN-C", "2", "合成乙"),
                    row("SYN-C", "2", "合成乙（另一行）"),
                ],
                packing_rows=[
                    pack("SYN-C", "2", "合成乙", specification="规格A", quantity=10, unit="个")
                ],
            )
        )
        mapping = map_workbook(workbook)
        first, second = mapping.links
        self.assertEqual([member.packing_row for member in first.members], [5])
        self.assertEqual([member.packing_row for member in second.members], [5])
        self.assertEqual([link.status for link in mapping.links], ["requires_review"] * 2)
        self.assertEqual(
            first.issues[0].code, "member_claimed_by_multiple_declarations:2"
        )
        self.assertEqual(first.issues[0].details["declaration_rows"], [2, 3])
        self.assertEqual(first.conflicting_declaration_rows, (3,))
        self.assertEqual(second.conflicting_declaration_rows, (2,))

    def test_conflicting_primary_name_columns_do_not_fall_back_to_name(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "2", "合成乙")],
                packing_headers=("合同号", "合同项号", "合同第一条品名", "合同第一条品名", "品名", "规格"),
                packing_rows=[("SYN-C", "2", "主名甲", "主名乙", "合成乙", "规格B")],
            )
        )
        mapping = map_workbook(workbook)
        layout = mapping.packing.layout
        self.assertIn("ambiguous_primary_name:no_fallback_to_name", layout.warnings)
        self.assertNotIn("legacy_primary_name_from:品名", layout.warnings)
        self.assertEqual(
            [column.header_cell for column in layout.ambiguous_columns], ["C4", "D4"]
        )
        record = mapping.packing.records[0]
        self.assertEqual(record.status, "requires_review")
        self.assertEqual([issue.code for issue in record.issues], ["missing_primary_name"])
        # 两个冲突列的原文都保留下来供人工核对，没有借用“品名”列冒充主品名。
        self.assertEqual([item["value"] for item in record.unmapped], ["主名甲", "主名乙"])

    def test_contested_packing_row_stays_candidate_not_written(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("SYN-C", "2", "合成乙"),
                    row("SYN-C", "2", "合成乙（另一行）"),
                ],
                packing_rows=[
                    pack("SYN-C", "2", "合成乙", specification="规格A", quantity=10, unit="个")
                ],
            )
        )
        mapping = map_workbook(workbook)
        for link in mapping.links:
            # 归属有歧义的装箱行只保留候选与来源，不进入正式落库输出。
            self.assertEqual(link.member_values(), ())
            self.assertEqual([member.packing_row for member in link.members], [5])
            self.assertTrue(all(member.ownership_ambiguous for member in link.members))

    def test_item_values_keep_retained_comments_with_source(self):
        headers = ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", None, "历史意见")
        package = build_package(
            declaration_headers=headers,
            declaration_rows=[("SYN-C", "1", "合成甲", "0000000000", "用途", "历史备注", "旧意见")],
            packing_rows=[],
        )
        link = map_workbook(self.build(package)).links[0]
        attributes = link.item_values()["attributes"]
        self.assertEqual(attributes["existing_opinion"]["value"], "旧意见")
        self.assertEqual(attributes["existing_opinion"]["cell"], "G2")
        self.assertEqual(attributes["existing_opinion"]["sheet"], DECLARATION_SHEET)
        self.assertEqual(attributes["unmapped_columns"][0]["value"], "历史备注")
        self.assertEqual(attributes["unmapped_columns"][0]["cell"], "F2")
        self.assertEqual(attributes["unmapped_columns"][0]["column"], "F")
        self.assertEqual(attributes["unmapped_columns"][0]["sheet"], DECLARATION_SHEET)

    def test_unlinked_packing_row_is_a_task_issue(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "1", "合成甲")],
                packing_rows=[
                    pack("SYN-C", "1", "合成甲", quantity=10, unit="个"),
                    pack("SYN-C", "5", "合成戊", quantity=5, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual([issue.code for issue in mapping.issues], ["unlinked_packing_row"])
        issue = mapping.issues[0]
        self.assertEqual((issue.sheet, issue.cell, issue.row), ("节点①装箱明细", "B6", 6))
        self.assertEqual(issue.details["item_no"], "5")

    def test_non_atomic_packing_item_is_a_task_issue(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "1", "合成甲"), row("SYN-C", "2", "合成乙")],
                packing_rows=[pack("SYN-C", "1-2", "合成甲乙", quantity=1, unit="个")],
            )
        )
        mapping = map_workbook(workbook)
        self.assertEqual(
            [issue.code for issue in mapping.issues], ["packing_item_not_atomic"]
        )
        self.assertEqual(mapping.issues[0].cell, "B5")
        self.assertEqual(
            [issue.code for link in mapping.links for issue in link.issues],
            ["missing_packing_member:1", "missing_packing_member:2"],
        )

    def test_missing_and_multiple_contracts(self):
        workbook = self.build(
            build_package(
                declaration_rows=[
                    row("", "1", "合成甲"),
                    row("SYN-A、SYN-B", "2", "合成乙"),
                ],
                packing_rows=[
                    pack("SYN-A", "1", "合成甲", quantity=1, unit="个"),
                    pack("SYN-A", "2", "合成乙", quantity=1, unit="个"),
                ],
            )
        )
        mapping = map_workbook(workbook)
        first, second = mapping.links
        self.assertEqual([issue.code for issue in first.issues], ["missing_contract"])
        self.assertEqual([issue.code for issue in first.record.issues][0], "missing_contract")
        self.assertEqual(first.issues[0].sheet, DECLARATION_SHEET)
        self.assertEqual(first.issues[0].cell, "A2")
        self.assertEqual([issue.code for issue in second.issues], ["multiple_contracts"])
        self.assertEqual(second.issues[0].cell, "A3")
        self.assertEqual([link.status for link in mapping.links], ["requires_review"] * 2)

    def test_packing_row_requires_review_propagates_to_member(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "2", "合成乙")],
                packing_rows=[pack("SYN-C", "2", None, specification="规格B", quantity=1, unit="个")],
            )
        )
        mapping = map_workbook(workbook)
        link = mapping.links[0]
        codes = [issue.code for issue in link.issues]
        self.assertEqual(codes, ["packing_row_requires_review"])
        issue = link.issues[0]
        self.assertEqual((issue.sheet, issue.cell), ("节点①装箱明细", "B5"))
        self.assertEqual(issue.details["packing_row"], 5)
        self.assertEqual(link.status, "requires_review")
        self.assertEqual(
            [member.issues[0].code for member in link.members], ["missing_primary_name"]
        )


class P06ShapeTest(WorkbookTestCase):
    """输出要能直接喂给 P06：字段名与 ``Item``/``ItemMember`` 对齐。"""

    def test_item_values_and_member_values(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "2", "合成乙", hs="ABC-9x", elements="0｜用途：测试")],
                packing_rows=[
                    pack("SYN-C", "2", "合成乙", specification="规格B", quantity=10, unit="个")
                ],
            )
        )
        link = map_workbook(workbook).links[0]
        values = link.item_values()
        self.assertEqual(
            set(values),
            {
                "source_sheet",
                "source_row",
                "source_cells",
                "name",
                "attributes",
                "supplier_hs_raw",
                "supplier_elements_raw",
                "supplier_tax_rate_raw",
            },
        )
        self.assertEqual(values["source_sheet"], DECLARATION_SHEET)
        self.assertEqual(values["source_row"], 2)
        self.assertEqual(values["name"], "合成乙")
        self.assertEqual(values["supplier_hs_raw"], "ABC-9x")
        self.assertEqual(values["supplier_elements_raw"], "0｜用途：测试")
        self.assertEqual(values["supplier_tax_rate_raw"], "")
        self.assertEqual(values["attributes"]["elements"]["raw"], "0｜用途：测试")

        members = link.member_values()
        self.assertEqual(len(members), 1)
        self.assertEqual(
            set(members[0]),
            {"relation_type", "source_sheet", "source_row", "contract_no", "item_no"},
        )
        self.assertEqual(members[0]["relation_type"], "packing")
        self.assertEqual(members[0]["source_sheet"], "节点①装箱明细")
        self.assertEqual(members[0]["source_row"], 5)
        self.assertEqual(members[0]["contract_no"], "SYN-C")
        self.assertEqual(members[0]["item_no"], "2")

    def test_unmatched_members_are_not_written_as_member_rows(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "9", "合成甲")],
                packing_rows=[],
            )
        )
        link = map_workbook(workbook).links[0]
        self.assertEqual(link.member_values(), ())
        self.assertEqual(link.status, "requires_review")

    def test_map_as_dict_is_json_serializable(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "1", "合成甲")],
                packing_rows=[
                    pack("SYN-C", "1", "合成甲", quantity=10, unit="个"),
                    pack("SYN-C", "5", "合成戊", quantity=5, unit="个"),
                ],
            )
        )
        payload = map_workbook(workbook).as_dict()
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("不代表已完成 HS 归类", payload["meaning"])
        self.assertEqual(payload["summary"]["declaration_rows"], 1)
        self.assertEqual(payload["summary"]["packing_rows"], 2)
        self.assertIn("unlinked_packing_row", serialized)

    def test_packing_quantities_are_not_summed(self):
        workbook = self.build(
            build_package(
                declaration_rows=[row("SYN-C", "2", "合成乙")],
                packing_rows=[
                    pack("SYN-C", "2", "合成乙", specification="规格B", quantity=20, unit="个"),
                    pack("SYN-C", "2", "合成乙", specification="规格B", quantity=5, unit="个"),
                ],
            )
        )
        link = map_workbook(workbook).links[0]
        self.assertEqual(
            [member.facts["quantity"].value for member in link.members], ["20", "5"]
        )
        # 分箱数量只作为成员事实，不在商品属性里汇总。
        self.assertNotIn("quantity", link.record.attributes)


class FixtureReuseTest(WorkbookTestCase):
    """地基合成样例（``tests/p04/fixtures.py``）可直接复用。"""

    def test_standard_package(self):
        mapping = map_workbook(self.build(standard_package()))
        self.assertEqual(len(mapping.declaration.records), 4)
        self.assertEqual(len(mapping.packing.records), 3)
        self.assertEqual(
            [(member.item_no, member.packing_row) for member in mapping.links[1].members],
            [("2", 6), ("2", 7)],
        )
        self.assertEqual(
            mapping.links[0].record.attributes["elements"]["raw"], "合成要素：合成值"
        )
        self.assertEqual(
            mapping.links[0].record.raw["existing_opinion"], "历史意见，不是本轮结论"
        )
