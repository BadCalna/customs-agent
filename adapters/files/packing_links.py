"""装箱明细提取与申报商品成员关联（P04）。

口径继承 P03 试验 ``experiments/excel_fidelity/mapping.py`` 的 ``parse_items`` / ``map_workbook``：

- **一条申报记录＝一个商品**（申报要素页签一行）；装箱行数只决定该商品有多少个成员，
  不按装箱行数计商品，也不按两页签相同行号匹配。
- 关联键只有 ``合同号 + 项号``；合并申报（``1-3``、``1、2、3``）展开为多个成员，
  分箱多行＝多个成员。
- 倒序范围、无法解析、缺失关键字段、重复/竞争、同 key 不同事实、未匹配都进问题列表，
  **不任意选一条**；问题带页签与单元格。
- ``parse_items`` 只做词法展开：不做数值改写（``01`` 与 ``1`` 不当同一项），不猜分组。

本模块不写数据库、不调用网络/模型；输出是可直接喂给 P06 落库的字典字段
（``LinkedDeclaration.item_values`` / ``member_values``，键与 ``Item``、``ItemMember`` 对齐）。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from adapters.files.declarations import (
    HEADER_SCAN_ROWS,
    ALIAS_NAME_ALIASES,
    CONTRACT_ALIASES,
    ITEM_ALIASES,
    MANUFACTURER_MODEL_ALIASES,
    MATERIAL_ALIASES,
    MERGE_HINT_ALIASES,
    NAME_ALIASES,
    PRIMARY_NAME_ALIASES,
    QUANTITY_ALIASES,
    SPECIFICATION_ALIASES,
    UNIT_ALIASES,
    FieldFact,
    Issue,
    Layout,
    SemanticLayoutError,
    SemanticRecord,
    SheetExtraction,
    extract_declarations,
    extract_records,
    locate_layout,
    normalized,
)

__all__ = [
    "DeclarationLink",
    "DeclarationMemberMap",
    "KeyParseError",
    "MemberLink",
    "PACKING_ALIASES",
    "PACKING_OPTIONAL",
    "PACKING_REQUIRED",
    "PACKING_STRONG_FIELDS",
    "contract_key",
    "extract_packing",
    "link_declarations",
    "locate_packing_layout",
    "map_workbook",
    "parse_items",
]

#: 装箱明细的语义别名；与申报要素分开维护，避免两页签共用别名产生误匹配。
PACKING_ALIASES: dict[str, tuple[str, ...]] = {
    "contract": CONTRACT_ALIASES,
    "item": ITEM_ALIASES,
    "primary_name": PRIMARY_NAME_ALIASES,
    "name": NAME_ALIASES,
    "alias": ALIAS_NAME_ALIASES,
    "specification": SPECIFICATION_ALIASES,
    "manufacturer_model": MANUFACTURER_MODEL_ALIASES,
    "material": MATERIAL_ALIASES,
    "quantity": QUANTITY_ALIASES,
    "unit": UNIT_ALIASES,
    "merge_hint": MERGE_HINT_ALIASES,
}

#: 必填表头：主品名可以退回“品名”，所以在定位后再单独校验。
PACKING_REQUIRED = ("contract", "item")
PACKING_OPTIONAL = (
    "alias",
    "specification",
    "manufacturer_model",
    "material",
    "quantity",
    "unit",
    "merge_hint",
)
#: 装箱明细里“像商品”的强事实字段（合同号/项号/主品名之外）。
PACKING_STRONG_FIELDS = ("specification", "manufacturer_model", "material")

#: “同 key 不同事实”比较的字段：都是装箱明细里可核对的事实，不含数量/单位（分箱本来就不同）。
FACT_FIELDS = ("primary_name", "alias", "specification", "manufacturer_model", "material")
#: 单个合并范围展开的上限；超过即视为异常输入，不静默展开。
MAX_ITEM_RANGE = 1000
_CONTRACT_SPLIT = re.compile(r"[\r\n、]")
_ITEM_SEPARATORS = re.compile(r"[、,;；\n\r]+")
_ITEM_RANGE = re.compile(r"([0-9]+)\s*[-~～—–－]\s*([0-9]+)")


class KeyParseError(ValueError):
    """合同号/项号无法按原文展开成成员；调用方把它列成问题，不替代选择。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def contract_key(value: str) -> tuple[str, str]:
    """返回 ``(归一后的关联键, 去首尾空白的原文)``；空合同或多合同直接报错。"""
    text = (value or "").strip()
    if not text:
        raise KeyParseError("missing_contract", "合同号为空，无法建立成员关联")
    if _CONTRACT_SPLIT.search(text):
        raise KeyParseError(
            "multiple_contracts", "同一格写入了多个合同号，不能推断这个成员属于哪一份合同"
        )
    return normalized(text), text


def parse_items(value: str) -> list[str]:
    """把项号原文展开成成员列表。

    支持单个项号、``1、2、3`` 这类列举以及 ``1-3`` / ``1~3`` 这类范围；范围保留原文的
    前导零宽度（``01-03`` → ``01``、``02``、``03``），不做数值改写。倒序、跨度过大、
    无法解析、同一行重复项号一律抛 :class:`KeyParseError`，交调用方列成问题。
    """
    text = unicodedata.normalize("NFKC", value or "").strip()
    if not text:
        raise KeyParseError("missing_item", "项号为空，无法建立成员关联")
    result: list[str] = []
    for segment in _ITEM_SEPARATORS.split(text):
        segment = segment.strip()
        if not segment:
            continue
        if re.fullmatch(r"[0-9]+", segment):
            members = [segment]
        else:
            match = _ITEM_RANGE.fullmatch(segment)
            if match is None:
                raise KeyParseError(
                    "unparsed_item_list", f"项号无法按原文解析：{segment!r}"
                )
            first, last = int(match.group(1)), int(match.group(2))
            if last < first:
                raise KeyParseError(
                    "reversed_item_range", f"倒序项号范围不展开：{segment!r}"
                )
            if last - first > MAX_ITEM_RANGE:
                raise KeyParseError(
                    "excessive_item_range",
                    f"项号范围过大（{segment!r}），不静默展开",
                )
            width = len(match.group(1)) if match.group(1).startswith("0") else 0
            members = [str(number).zfill(width) for number in range(first, last + 1)]
        if any(member in result for member in members):
            raise KeyParseError(
                "duplicate_item_in_list", f"项号列表里有重复项号：{text!r}"
            )
        result.extend(members)
    if not result:
        raise KeyParseError("missing_item", "项号展开后为空")
    return result


# --------------------------------------------------------------------------- 装箱明细提取
def locate_packing_layout(
    workbook,
    *,
    sheet_label: str = "装箱明细",
    header_scan_rows: int = HEADER_SCAN_ROWS,
) -> Layout:
    layout = locate_layout(
        workbook,
        role="packing",
        sheet_label=sheet_label,
        aliases=PACKING_ALIASES,
        required=PACKING_REQUIRED,
        optional=PACKING_OPTIONAL,
        header_scan_rows=header_scan_rows,
    )
    columns = dict(layout.columns)
    headers = dict(layout.headers)
    warnings = list(layout.warnings)
    if "primary_name" not in columns:
        if "name" not in columns:
            raise SemanticLayoutError(
                "missing_required_columns",
                "装箱明细缺少“合同第一条品名/品名”列，无法建立主品名",
                sheet=layout.sheet,
                cell=f"A{layout.header_row}",
                field="primary_name",
                details={"row": layout.header_row},
            )
        # 旧模板只有“品名”列：借用它当主品名，但明确记为表头差异，不声称存在主品名列。
        columns["primary_name"] = columns["name"]
        headers["primary_name"] = headers["name"]
        warnings = [
            warning for warning in warnings if warning != "missing_column:primary_name"
        ]
        warnings.append("legacy_primary_name_from:品名")
    return replace(
        layout,
        columns=columns,
        headers=headers,
        warnings=tuple(warnings),
    )


def extract_packing(
    workbook,
    *,
    sheet_label: str = "装箱明细",
    layout: Layout | None = None,
) -> SheetExtraction:
    """提取装箱明细：合同号、项号原文与商品事实，全部带来源单元格。"""
    layout = layout or locate_packing_layout(workbook, sheet_label=sheet_label)
    records, non_items = extract_records(
        workbook,
        layout,
        role="packing",
        name_field="primary_name",
        strong_fields=PACKING_STRONG_FIELDS,
    )
    return SheetExtraction(
        role="packing", layout=layout, records=records, non_item_rows=non_items
    )


# --------------------------------------------------------------------------- 关联结果
@dataclass(frozen=True)
class MemberLink:
    """一条申报成员与它命中的装箱行（未命中时 ``packing_row`` 为 0）。"""

    contract_no: str
    item_no: str
    item_raw: str
    packing_sheet: str
    packing_row: int
    facts: Mapping[str, FieldFact] = field(default_factory=dict)
    issues: tuple[Issue, ...] = ()

    @property
    def matched(self) -> bool:
        return self.packing_row > 0

    def as_dict(self) -> dict:
        return {
            "contract_no": self.contract_no,
            "item_no": self.item_no,
            "item_raw": self.item_raw,
            "packing_sheet": self.packing_sheet,
            "packing_row": self.packing_row,
            "facts": {name: fact.as_dict() for name, fact in self.facts.items()},
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class DeclarationLink:
    """一条申报商品记录：申报行原文 + 装箱成员 + 关联问题 + 状态。"""

    record: SemanticRecord
    members: tuple[MemberLink, ...]
    issues: tuple[Issue, ...]
    status: str
    conflicting_declaration_rows: tuple[int, ...] = ()

    @property
    def sheet(self) -> str:
        return self.record.sheet

    @property
    def row(self) -> int:
        return self.record.row

    @property
    def identified(self) -> bool:
        return self.status == "identified"

    @property
    def matched_members(self) -> tuple[MemberLink, ...]:
        return tuple(member for member in self.members if member.matched)

    def item_values(self) -> dict:
        """映射到 ``reviews.Item`` 的已有字段；``version`` 由 P06 补。"""
        facts = self.record.facts
        return {
            "source_sheet": self.record.sheet,
            "source_row": self.record.row,
            "source_cells": self.record.source_cells,
            "name": facts["name"].value if "name" in facts else "",
            "attributes": dict(self.record.attributes),
            "supplier_hs_raw": facts["hs"].value if "hs" in facts else "",
            "supplier_elements_raw": (
                facts["elements"].value if "elements" in facts else ""
            ),
            "supplier_tax_rate_raw": (
                facts["refund_rate"].value if "refund_rate" in facts else ""
            ),
        }

    def member_values(self) -> tuple[dict, ...]:
        """映射到 ``reviews.ItemMember``（只含已命中的装箱行；未命中见问题列表）。"""
        return tuple(
            {
                "relation_type": "packing",
                "source_sheet": member.packing_sheet,
                "source_row": member.packing_row,
                "contract_no": member.contract_no,
                "item_no": member.item_no,
            }
            for member in self.matched_members
        )

    def as_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "row": self.row,
            "status": self.status,
            "issues": [issue.as_dict() for issue in self.issues],
            "conflicting_declaration_rows": list(self.conflicting_declaration_rows),
            "members": [member.as_dict() for member in self.members],
            "record": self.record.as_dict(),
            "item_values": self.item_values(),
            "member_values": [dict(values) for values in self.member_values()],
        }


@dataclass(frozen=True)
class DeclarationMemberMap:
    """一次映射的完整结果：两页签提取 + 每条申报商品的成员关联 + 任务级问题。"""

    packing: SheetExtraction
    declaration: SheetExtraction
    links: tuple[DeclarationLink, ...]
    issues: tuple[Issue, ...] = ()

    @property
    def identified_count(self) -> int:
        return sum(1 for link in self.links if link.identified)

    @property
    def requires_review_count(self) -> int:
        return sum(1 for link in self.links if not link.identified)

    def link(self, row: int) -> DeclarationLink | None:
        return next((link for link in self.links if link.row == row), None)

    def as_dict(self) -> dict:
        return {
            "packing": self.packing.as_dict(),
            "declaration": self.declaration.as_dict(),
            "links": [link.as_dict() for link in self.links],
            "issues": [issue.as_dict() for issue in self.issues],
            "summary": {
                "declaration_rows": len(self.declaration.records),
                "packing_rows": len(self.packing.records),
                "identified": self.identified_count,
                "requires_review": self.requires_review_count,
                "task_issues": len(self.issues),
            },
            "meaning": "位置与成员映射，不代表已完成 HS 归类或预审",
        }


# --------------------------------------------------------------------------- 关联实现
def _value(record: SemanticRecord, field: str) -> str:
    return record.value(field)


def _field_for(code: str) -> str:
    return "contract" if code in ("missing_contract", "multiple_contracts") else "item"


def _issue(
    record: SemanticRecord,
    field: str,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> Issue:
    """按字段所在列定位问题单元格；字段缺失时退回字段名，保证仍能定位到页签+位置。"""
    fact = record.facts.get(field)
    column = fact.column if fact is not None else field
    return Issue(
        code=code,
        message=message,
        sheet=record.sheet,
        cell=f"{column}{record.row}" if re.fullmatch(r"[A-Z]{1,3}", column) else column,
        row=record.row,
        field=field,
        details=dict(details or {}),
    )


def _signature(record: SemanticRecord) -> tuple[str, ...]:
    return tuple(record.value(field).strip() for field in FACT_FIELDS if field in record.facts)


def _differing_fields(records: Sequence[SemanticRecord]) -> list[str]:
    differing: list[str] = []
    for field in FACT_FIELDS:
        values = {
            record.value(field).strip()
            for record in records
            if field in record.facts
        }
        if len(values) > 1:
            differing.append(field)
    return differing


def link_declarations(
    packing: SheetExtraction,
    declaration: SheetExtraction,
) -> DeclarationMemberMap:
    """按 ``合同号 + 项号`` 关联；不按行号对齐，冲突全部报出来。"""
    issues: list[Issue] = []
    index: dict[tuple[str, str], list[SemanticRecord]] = defaultdict(list)
    indexed_rows: set[int] = set()

    for record in packing.records:
        try:
            contract, _ = contract_key(_value(record, "contract"))
            members = parse_items(_value(record, "item"))
            if len(members) != 1:
                raise KeyParseError(
                    "packing_item_not_atomic",
                    f"装箱行项号展开为 {len(members)} 个值，不能当作单一成员",
                )
        except KeyParseError as exc:
            issues.append(
                _issue(record, _field_for(exc.code), exc.code, str(exc))
            )
            continue
        index[(contract, normalized(members[0]))].append(record)
        indexed_rows.add(record.row)

    pending: list[dict[str, Any]] = []
    owners: dict[tuple[str, str], list[int]] = defaultdict(list)

    for record in declaration.records:
        link_issues: list[Issue] = []
        members: list[MemberLink] = []
        conflicting: set[int] = set()
        contract: str | None = None
        contract_raw = _value(record, "contract").strip()
        try:
            contract, contract_raw = contract_key(_value(record, "contract"))
        except KeyParseError as exc:
            link_issues.append(
                _issue(record, _field_for(exc.code), exc.code, str(exc))
            )

        item_members: list[str] | None = None
        try:
            item_members = parse_items(_value(record, "item"))
        except KeyParseError as exc:
            link_issues.append(
                _issue(record, _field_for(exc.code), exc.code, str(exc))
            )

        if contract is not None and item_members is not None:
            for item in item_members:
                key = (contract, normalized(item))
                owners[key].append(record.row)
                candidates = index.get(key, [])
                if not candidates:
                    link_issues.append(
                        _issue(
                            record,
                            "item",
                            f"missing_packing_member:{item}",
                            f"合同 {contract_raw} 项号 {item} 在装箱明细中找不到对应行",
                            details={"contract_no": contract_raw, "item": item},
                        )
                    )
                if len({_signature(candidate) for candidate in candidates}) > 1:
                    link_issues.append(
                        _issue(
                            record,
                            "item",
                            f"same_key_different_facts:{item}",
                            f"合同 {contract_raw} 项号 {item} 对应多条装箱行且事实不同，不选一条",
                            details={
                                "contract_no": contract_raw,
                                "item": item,
                                "packing_rows": [candidate.row for candidate in candidates],
                                "differing_fields": _differing_fields(candidates),
                            },
                        )
                    )
                for candidate in candidates:
                    member_issues = tuple(candidate.issues)
                    members.append(
                        MemberLink(
                            contract_no=_value(candidate, "contract").strip(),
                            item_no=item,
                            item_raw=_value(record, "item"),
                            packing_sheet=candidate.sheet,
                            packing_row=candidate.row,
                            facts=candidate.facts,
                            issues=member_issues,
                        )
                    )
                    if member_issues:
                        link_issues.append(
                            _issue(
                                candidate,
                                "item",
                                "packing_row_requires_review",
                                f"装箱第 {candidate.row} 行自身待核："
                                + "、".join(issue.code for issue in member_issues),
                                details={
                                    "packing_row": candidate.row,
                                    "codes": [issue.code for issue in member_issues],
                                },
                            )
                        )
        pending.append(
            {
                "record": record,
                "members": members,
                "issues": link_issues,
                "conflicting": conflicting,
            }
        )

    for key, rows in owners.items():
        claimed = sorted(set(rows))
        if len(claimed) < 2:
            continue
        for entry in pending:
            if entry["record"].row not in claimed:
                continue
            entry["issues"].append(
                _issue(
                    entry["record"],
                    "item",
                    f"member_claimed_by_multiple_declarations:{key[1]}",
                    f"合同项号 {key[1]} 被多条申报行认领（第 "
                    + "、".join(str(row) for row in claimed)
                    + " 行），不任意分配给其中一条",
                    details={
                        "item": key[1],
                        "declaration_rows": claimed,
                    },
                )
            )
            entry["conflicting"].update(row for row in claimed if row != entry["record"].row)

    links = tuple(
        DeclarationLink(
            record=entry["record"],
            members=tuple(entry["members"]),
            issues=tuple(entry["issues"]),
            status=(
                "requires_review"
                if entry["issues"] or entry["record"].issues
                else "identified"
            ),
            conflicting_declaration_rows=tuple(sorted(entry["conflicting"])),
        )
        for entry in pending
    )

    used = {member.packing_row for link in links for member in link.matched_members}
    for record in packing.records:
        if record.row in used or record.row not in indexed_rows:
            continue
        issues.append(
            _issue(
                record,
                "item",
                "unlinked_packing_row",
                f"装箱第 {record.row} 行没有被任何申报行认领",
                details={
                    "contract_no": _value(record, "contract").strip(),
                    "item_no": _value(record, "item").strip(),
                },
            )
        )

    return DeclarationMemberMap(
        packing=packing,
        declaration=declaration,
        links=links,
        issues=tuple(issues),
    )


def map_workbook(
    workbook,
    *,
    declaration_label: str = "申报要素",
    packing_label: str = "装箱明细",
) -> DeclarationMemberMap:
    """一次性完成“申报商品提取 + 装箱成员关联”，给 P06 落库用。

    结构读不出（缺页签/表头/必填列，或必填列同义重复）抛
    :class:`adapters.files.declarations.SemanticLayoutError`；
    行级缺失与关联歧义都在返回值的问题列表里，不抛异常。
    """
    packing = extract_packing(workbook, sheet_label=packing_label)
    declaration = extract_declarations(workbook, sheet_label=declaration_label)
    return link_declarations(packing, declaration)
