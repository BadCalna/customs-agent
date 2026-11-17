"""申报要素记录提取（P04）：语义表头定位 + “一条申报记录＝一个商品行”。

口径来源与边界：

- 语义继承 P03 试验 ``experiments/excel_fidelity/mapping.py`` 的 ``detect_layout`` / 行识别口径。
  生产代码不 import ``experiments``，所需逻辑在本模块重写；与 P03 的差异见
  ``docs/开发文档/交接/P04.md``“来源与差异”。
- 字段、商品行与位置口径见 D2（模板映射、商品行＝申报要素一行、ItemMember、附图与 DISPIMG），
  字段与错误口径见 D3（原文保留、空白即空白、来源可追溯）。
- 本模块只做三件事：**按语义定位表头与列**（不写死列字母）、**按行提取原文与来源单元格**、
  **把缺失/歧义列成带页签与单元格的问题**。不做归类、不补值、不猜测；原 HS 空白就空白，
  异常原值与已有意见原样保留。

结构读不出（找不到页签、表头行缺失/重复、必填列同义重复）抛 :class:`SemanticLayoutError`，
与“确实没有商品行”分开表达（R11）。行级问题用 :class:`Issue` 表达，每条问题都带页签与单元格。

设计取舍：

- 一条申报记录＝申报要素页签的一行；行数不按装箱行数计算，也不和装箱页签按相同行号对齐。
- 纵向合并只在**本列**取锚点（``Sheet.value_at`` 的口径）；横向合并不借用相邻列内容，
  避免把别的字段当成这一列的值。
- 表头同义重复：**必填字段**直接报错（不选一列）；**可选/结果字段**标记歧义并整体丢弃该字段，
  两列原文仍以“未识别列”保留，等待主控确认口径后补别名。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Mapping, Sequence

from adapters.files.ooxml import (
    OoxmlError,
    Sheet,
    Workbook,
    cell_position,
    col_label,
    col_number,
)

__all__ = [
    "DECLARATION_ALIASES",
    "DECLARATION_OPTIONAL",
    "DECLARATION_REQUIRED",
    "DECLARATION_STRONG_FIELDS",
    "HEADER_SCAN_ROWS",
    "LEGACY_APPROVAL_MARKERS",
    "FIELD_LABELS",
    "FieldFact",
    "Issue",
    "Layout",
    "SemanticLayoutError",
    "SemanticRecord",
    "SheetExtraction",
    "evidence_fact",
    "extract_declarations",
    "extract_records",
    "field_matches",
    "find_sheet",
    "floating_image_rows",
    "locate_declaration_layout",
    "locate_layout",
    "normalized",
]

#: 表头扫描上限：真实模板表头在 A1:AA56 之类区域的前几行，给足余量又不扫进数据区。
HEADER_SCAN_ROWS = 30
#: 未识别列的报告上限；超出时只保留前若干列并给出警告，避免畸形输入撑爆报告。
MAX_UNMAPPED_COLUMNS = 64

#: 旧模板常见把审批痕迹写在无表头列（实测列 Z 整列为“OK”）。按 Jay 2026-11-16 口径：
#: 这类标记**不保留为业务字段**，也不作为当前或历史通过状态；原文件本身不被改写。
LEGACY_APPROVAL_MARKERS = frozenset({
    "OK", "OK.", "OK!", "OKAY", "PASS", "PASSED", "√", "✓", "通过", "已通过", "审核通过",
})

# --------------------------------------------------------------------------- 语义别名
# 别名只写“表头原文”，匹配时做全半角、大小写、空白与括号说明归一（见 field_matches）。
CONTRACT_ALIASES = ("采购合同号", "合同号", "合同编号")
ITEM_ALIASES = ("合同项号", "合同项次")
NAME_ALIASES = ("品名", "商品品名")
PRIMARY_NAME_ALIASES = ("合同第一条品名",)
ALIAS_NAME_ALIASES = ("附件清单品名",)
SPECIFICATION_ALIASES = ("规格型号", "规格")
MANUFACTURER_MODEL_ALIASES = ("厂家提供型号", "供应商提供型号")
MATERIAL_ALIASES = ("物料代码", "物料编码")
HS_ALIASES = ("海关编码", "HSCODE", "HS编码")
ELEMENTS_ALIASES = ("申报要素",)
IMAGE_ALIASES = ("附图", "商品图片")
REFUND_RATE_ALIASES = ("退税率",)
MERGE_HINT_ALIASES = ("合并申报",)
QUANTITY_ALIASES = ("数量",)
UNIT_ALIASES = ("单位",)
#: 历史意见列：真实模板多数没有表头，没表头但有内容的列按“未识别列”原样保留，不当作结论。
EXISTING_OPINION_ALIASES = ("已有意见", "已有审核意见", "审核意见", "历史意见", "历史审核意见")

#: AI/人工结果列（P03 已确认的表头与别名）。P04 只定位与保留原文，不生成结果。
RESULT_ALIASES: dict[str, tuple[str, ...]] = {
    "ai_hs": ("AI推荐HScode", "AI推荐HS编码", "AI推荐海关编码"),
    "confirmed_hs": ("业务员确认HScode", "业务员确认HS编码"),
    "confidence": ("置信度",),
    "precheck_conclusion": ("预审结论", "AI预审结论"),
}

DECLARATION_REQUIRED = ("contract", "item", "name", "hs", "elements")
#: 判定“这一行像商品”的强事实字段；只有汇总公式、没有这些事实的行不当商品（P03 口径）。
DECLARATION_STRONG_FIELDS = ("hs", "elements", "image", "refund_rate")
DECLARATION_OPTIONAL = (
    "quantity",
    "unit",
    "refund_rate",
    "image",
    "existing_opinion",
    *RESULT_ALIASES,
)

DECLARATION_ALIASES: dict[str, tuple[str, ...]] = {
    "contract": CONTRACT_ALIASES,
    "item": ITEM_ALIASES,
    "name": NAME_ALIASES,
    "hs": HS_ALIASES,
    "elements": ELEMENTS_ALIASES,
    "quantity": QUANTITY_ALIASES,
    "unit": UNIT_ALIASES,
    "refund_rate": REFUND_RATE_ALIASES,
    "image": IMAGE_ALIASES,
    "existing_opinion": EXISTING_OPINION_ALIASES,
    **RESULT_ALIASES,
}

#: 问题与提示里使用的字段中文名，避免下游再造一套措辞。
FIELD_LABELS: dict[str, str] = {
    "contract": "合同号",
    "item": "项号",
    "name": "品名",
    "primary_name": "主品名",
    "alias": "别名",
    "specification": "规格",
    "manufacturer_model": "厂家型号",
    "material": "物料代码",
    "hs": "海关编码",
    "elements": "申报要素",
    "refund_rate": "退税率",
    "image": "附图",
    "quantity": "数量",
    "unit": "单位",
    "merge_hint": "合并申报",
    "existing_opinion": "已有意见",
    "ai_hs": "AI推荐HScode",
    "confirmed_hs": "业务员确认Hscode",
    "confidence": "置信度",
    "precheck_conclusion": "预审结论",
}


# --------------------------------------------------------------------------- 归一与匹配
def normalized(value: str) -> str:
    """表头与关联键的归一：全半角、大小写、全部空白（含换行）都抹平。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "")).upper()


def field_matches(value: str, alias: str) -> bool:
    """表头是否命中某个别名。

    除完全相等外，只接受“别名＋括号说明”的形式（真实模板常在表头后加
    “（查询参考网站如下…）”这类说明）；不做包含匹配，避免把“英文品名”当成“品名”。
    """
    value, alias = normalized(value), normalized(alias)
    if not value or not alias:
        return False
    return value == alias or value.startswith(alias + "(") or (
        alias == "退税率" and value == "退税率%"
    )


# --------------------------------------------------------------------------- 问题与错误
@dataclass(frozen=True)
class Issue:
    """行级或任务级问题；``sheet`` + ``cell`` 必须能定位回原表。

    ``severity``：``info`` 只作提示（不改变识别状态）、``warning`` 需人工核对、
    ``error`` 是资料错误。``blocking=True`` 表示上传阶段必须拒绝该文件（P06 据此
    在创建任务/版本前报错，见 Jay 2026-11-16 口径：项号倒序/无法解析之类按笔误处理）。
    """

    code: str
    message: str
    sheet: str
    cell: str = ""
    row: int = 0
    field: str = ""
    severity: str = "warning"
    blocking: bool = False
    details: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "sheet": self.sheet,
            "cell": self.cell,
            "row": self.row,
            "field": self.field,
            "severity": self.severity,
            "blocking": self.blocking,
            "details": dict(self.details),
        }


class SemanticLayoutError(ValueError):
    """页签/表头/字段列层面的结构问题；调用方不能把它当成“没有商品”。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        sheet: str | None = None,
        cell: str | None = None,
        field: str = "",
        details: Mapping[str, Any] | None = None,
    ):
        self.code = code
        self.sheet = sheet
        self.cell = cell
        self.field = field
        self.details = dict(details or {})
        super().__init__(message or code)

    def as_issue(self) -> dict:
        issue = {
            "code": self.code,
            "message": str(self),
            "sheet": self.sheet or "",
            "cell": self.cell or "",
            "field": self.field,
            "details": dict(self.details),
        }
        return issue


# --------------------------------------------------------------------------- 结构与事实
@dataclass(frozen=True)
class ColumnMatch:
    """一个字段在某一行匹配到的列；``field`` 为空表示“未识别语义的列”。"""

    field: str
    column: str
    header: str
    header_cell: str
    has_data: bool = False

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "column": self.column,
            "header": self.header,
            "header_cell": self.header_cell,
            "has_data": self.has_data,
        }


@dataclass(frozen=True)
class Layout:
    """页签的语义位置：表头行、字段列、警告与未识别列。"""

    role: str
    sheet: str
    part: str
    header_row: int
    columns: Mapping[str, str]
    headers: Mapping[str, str]
    required: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    ambiguous_columns: tuple[ColumnMatch, ...] = ()
    unmapped_columns: tuple[ColumnMatch, ...] = ()

    def column(self, field: str) -> str | None:
        return self.columns.get(field)

    def header_cell(self, field: str) -> str:
        column = self.columns.get(field)
        return f"{column}{self.header_row}" if column else ""

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "sheet": self.sheet,
            "part": self.part,
            "header_row": self.header_row,
            "columns": dict(self.columns),
            "headers": dict(self.headers),
            "required": list(self.required),
            "warnings": list(self.warnings),
            "ambiguous_columns": [column.as_dict() for column in self.ambiguous_columns],
            "unmapped_columns": [column.as_dict() for column in self.unmapped_columns],
        }


@dataclass(frozen=True)
class FieldFact:
    """某字段在某行的原文、来源单元格与是否取自纵向合并锚点。"""

    field: str
    value: str
    cell: str
    column: str
    header: str
    formula: str | None = None
    from_merge_anchor: bool = False

    @property
    def filled(self) -> bool:
        return bool(self.value.strip()) or self.formula is not None

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "cell": self.cell,
            "column": self.column,
            "header": self.header,
            "formula": self.formula,
            "from_merge_anchor": self.from_merge_anchor,
        }


@dataclass(frozen=True)
class SemanticRecord:
    """一行记录：原文、来源、结构化属性、状态与问题。

    ``raw`` 保留未 strip 的原文（原 HS 空白就是空字符串）；``attributes`` 只按分隔符等
    可核对规则做结构化，不代表已归类。
    """

    role: str
    sheet: str
    row: int
    status: str
    issues: tuple[Issue, ...]
    facts: Mapping[str, FieldFact]
    raw: Mapping[str, str]
    attributes: Mapping[str, Any]
    unmapped: tuple[Mapping[str, Any], ...] = ()
    source_cells: str = ""
    floating_image_cells: tuple[str, ...] = ()

    @property
    def identified(self) -> bool:
        return self.status == "identified"

    def value(self, field: str) -> str:
        fact = self.facts.get(field)
        return fact.value if fact is not None else ""

    def cell(self, field: str) -> str:
        fact = self.facts.get(field)
        return fact.cell if fact is not None else ""

    @property
    def image_cell(self) -> str:
        fact = self.facts.get("image")
        return fact.cell if fact is not None else ""

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "sheet": self.sheet,
            "row": self.row,
            "status": self.status,
            "issues": [issue.as_dict() for issue in self.issues],
            "facts": {name: fact.as_dict() for name, fact in self.facts.items()},
            "raw": dict(self.raw),
            "attributes": dict(self.attributes),
            "unmapped": [dict(item) for item in self.unmapped],
            "source_cells": self.source_cells,
            "floating_image_cells": list(self.floating_image_cells),
        }


@dataclass(frozen=True)
class SheetExtraction:
    """一个页签的提取结果：位置、记录、非商品行。"""

    role: str
    layout: Layout
    records: tuple[SemanticRecord, ...]
    non_item_rows: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[Issue, ...] = ()

    @property
    def data_rows(self) -> tuple[int, ...]:
        return tuple(record.row for record in self.records)

    def record(self, row: int) -> SemanticRecord | None:
        return next((record for record in self.records if record.row == row), None)

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "layout": self.layout.as_dict(),
            "records": [record.as_dict() for record in self.records],
            "non_item_rows": [dict(item) for item in self.non_item_rows],
            "data_rows": list(self.data_rows),
            "issues": [issue.as_dict() for issue in self.issues],
        }


# --------------------------------------------------------------------------- 表头定位
def find_sheet(workbook: Workbook, label: str) -> Sheet:
    """按页签名的包含关系取唯一页签；找不到或匹配多个抛 :class:`SemanticLayoutError`。

    ``Workbook.find_sheet`` 的 ``OoxmlError`` 在这里转成布局问题，方便调用方
    统一按“结构读不出，不等于没有商品”处理。
    """
    try:
        return workbook.find_sheet(label)
    except OoxmlError as exc:
        # 找不到页签时 ``OoxmlError`` 不带页签名，这里补上被查找的标签，保证问题仍能定位。
        raise SemanticLayoutError(
            exc.code, str(exc), sheet=exc.sheet or label
        ) from exc


def _scan_header_rows(
    sheet: Sheet,
    aliases: Mapping[str, Sequence[str]],
    limit: int,
) -> dict[int, dict[str, list[ColumnMatch]]]:
    """逐行扫描前 ``limit`` 行，返回“行 → 字段 → 匹配列”。"""
    matches: dict[int, dict[str, list[ColumnMatch]]] = defaultdict(lambda: defaultdict(list))
    for cell in sheet.cells.values():
        if cell.row > limit or not cell.value:
            continue
        text = cell.value
        for name, alias_list in aliases.items():
            if not any(field_matches(text, alias) for alias in alias_list):
                continue
            matches[cell.row][name].append(
                ColumnMatch(
                    field=name,
                    column=col_label(cell.column),
                    header=text,
                    header_cell=cell.ref,
                )
            )
    return matches


def _choose_header_row(
    sheet: Sheet,
    matches: Mapping[int, Mapping[str, list[ColumnMatch]]],
    required: Sequence[str],
    role: str,
    limit: int,
) -> int:
    complete = sorted(
        row for row in matches if all(name in matches[row] for name in required)
    )
    if len(complete) > 1:
        raise SemanticLayoutError(
            "header_row_ambiguous",
            f"{role} 页签有多个行同时含必填表头 {list(required)}：{complete}，不能任选一行",
            sheet=sheet.name,
            details={"rows": complete},
        )
    if complete:
        return complete[0]
    ranked = sorted(
        matches,
        key=lambda row: (-sum(1 for name in required if name in matches[row]), row),
    )
    if ranked and sum(1 for name in required if name in matches[ranked[0]]) >= 2:
        row = ranked[0]
        missing = [name for name in required if name not in matches[row]]
        raise SemanticLayoutError(
            "missing_required_columns",
            f"{role} 页签第 {row} 行像表头，但缺少必填列："
            + "、".join(FIELD_LABELS.get(name, name) for name in missing),
            sheet=sheet.name,
            cell=f"A{row}",
            field=",".join(missing),
            details={
                "row": row,
                "missing": missing,
                "matched": sorted(matches[row]),
            },
        )
    raise SemanticLayoutError(
        "header_row_missing",
        f"{role} 页签前 {limit} 行找不到同时包含必填表头 "
        f"{[FIELD_LABELS.get(name, name) for name in required]} 的表头行",
        sheet=sheet.name,
        details={"scanned_rows": limit},
    )


def _is_legacy_approval(value: str) -> bool:
    """旧审批标记（OK/通过等）；只按原文归一后整体判断，不做包含匹配。"""
    return normalized(value).strip("。.!！") in LEGACY_APPROVAL_MARKERS


def _unmapped_columns(
    sheet: Sheet,
    columns: Mapping[str, str],
    header_row: int,
) -> tuple[tuple[ColumnMatch, ...], tuple[str, ...], tuple[str, ...]]:
    """有表头或数据、但没有命中任何语义的列；原文按行保留，不当作已知字段。

    无表头且整列都是旧“OK/通过”标记的列按口径丢弃，返回 ``(保留列, 丢弃列, 混合列)``；
    有表头的列（如明确的“审核意见”）即使内容含 OK 也原样保留，属于历史上下文。
    """
    mapped = set(columns.values())
    grouped: dict[str, list] = defaultdict(list)
    for cell in sheet.cells.values():
        column = col_label(cell.column)
        if column in mapped:
            continue
        grouped[column].append(cell)
    result: list[ColumnMatch] = []
    ignored: list[str] = []
    mixed: list[str] = []
    for column in sorted(grouped, key=col_number):
        cells = grouped[column]
        header_cell = next(
            (cell for cell in cells if cell.row == header_row and cell.value), None
        )
        has_data = any(cell.row > header_row and cell.has_content for cell in cells)
        if header_cell is None and not has_data:
            continue
        header_text = header_cell.value if header_cell is not None else ""
        data_values = [
            cell.value for cell in cells
            if cell.row > header_row and (cell.value or "").strip()
        ]
        if not header_text.strip() and data_values:
            marks = [_is_legacy_approval(value) for value in data_values]
            if all(marks):
                ignored.append(column)
                continue
            if any(marks):
                mixed.append(column)
        result.append(
            ColumnMatch(
                field="",
                column=column,
                header=header_text,
                header_cell=header_cell.ref if header_cell is not None else "",
                has_data=has_data,
            )
        )
    return tuple(result), tuple(ignored), tuple(mixed)


def locate_layout(
    workbook: Workbook,
    *,
    role: str,
    sheet_label: str,
    aliases: Mapping[str, Sequence[str]],
    required: Sequence[str],
    optional: Sequence[str],
    header_scan_rows: int = HEADER_SCAN_ROWS,
) -> Layout:
    """按语义定位一个页签的表头行与字段列（不写死列字母）。"""
    sheet = find_sheet(workbook, sheet_label)
    matches = _scan_header_rows(sheet, aliases, header_scan_rows)
    header_row = _choose_header_row(sheet, matches, required, role, header_scan_rows)
    row_matches = matches[header_row]

    warnings: list[str] = []
    ambiguous: list[ColumnMatch] = []
    columns: dict[str, str] = {}
    headers: dict[str, str] = {}
    for name in sorted(row_matches):
        found = row_matches[name]
        if len(found) > 1:
            cells = "、".join(column.header_cell for column in found)
            if name in required:
                raise SemanticLayoutError(
                    "duplicate_semantic_column",
                    f"{role} 页签第 {header_row} 行“{FIELD_LABELS.get(name, name)}”"
                    f"同时匹配多列（{cells}），不随便取一列",
                    sheet=sheet.name,
                    cell=found[0].header_cell,
                    field=name,
                    details={"cells": [column.header_cell for column in found]},
                )
            ambiguous.extend(found)
            warnings.append(f"ambiguous_semantic_column:{name}")
            continue
        columns[name] = found[0].column
        headers[name] = found[0].header

    # 同一列被两个语义命中：同样不猜，直接报歧义。
    collision: dict[str, list[str]] = defaultdict(list)
    for name, column in columns.items():
        collision[column].append(name)
    for column, names in sorted(collision.items(), key=lambda item: col_number(item[0])):
        if len(names) > 1:
            raise SemanticLayoutError(
                "ambiguous_semantic_column",
                f"列 {column} 同时命中多个语义：{names}，不猜字段归属",
                sheet=sheet.name,
                cell=f"{column}{header_row}",
                field=",".join(sorted(names)),
            )

    for name in optional:
        if name not in columns:
            warnings.append(f"missing_column:{name}")

    unmapped, ignored_legacy, mixed_legacy = _unmapped_columns(sheet, columns, header_row)
    if ignored_legacy:
        warnings.append(f"legacy_approval_column_ignored:{','.join(ignored_legacy)}")
    if mixed_legacy:
        warnings.append(f"legacy_approval_values_ignored:{','.join(mixed_legacy)}")
    if len(unmapped) > MAX_UNMAPPED_COLUMNS:
        warnings.append(f"unmapped_columns_truncated:{len(unmapped)}")
        unmapped = unmapped[:MAX_UNMAPPED_COLUMNS]

    return Layout(
        role=role,
        sheet=sheet.name,
        part=sheet.part,
        header_row=header_row,
        columns=columns,
        headers=headers,
        required=tuple(required),
        warnings=tuple(warnings),
        ambiguous_columns=tuple(ambiguous),
        unmapped_columns=unmapped,
    )


# --------------------------------------------------------------------------- 行提取
def _field_fact(sheet: Sheet, layout: Layout, field: str, row: int) -> FieldFact:
    column = layout.columns[field]
    column_number = col_number(column)
    value, cell_ref, from_merge = sheet.value_at(row, column_number)
    # 共享公式从属单元格的 Cell.formula 为空，有效公式只在 Sheet.formulas 中。
    formula = sheet.formulas.get(cell_ref)
    return FieldFact(
        field=field,
        value=value,
        cell=cell_ref,
        column=column,
        header=layout.headers.get(field, ""),
        formula=formula,
        from_merge_anchor=from_merge,
    )


def parse_elements(value: str) -> dict:
    """按分隔符把申报要素原文拆成有序条目；拆不动就原样保留，不补任何要素。"""
    items: list[dict] = []
    for segment in re.split(r"[｜|]", value or ""):
        segment = segment.strip()
        if not segment:
            continue
        label, separator, rest = segment.partition("：")
        if not separator:
            label, separator, rest = segment.partition(":")
        if separator:
            items.append({"label": label.strip(), "value": rest.strip()})
        else:
            items.append({"label": None, "value": segment})
    return {"raw": value, "items": items}


def _attributes(role: str, facts: Mapping[str, FieldFact]) -> dict:
    """结构化属性：只做可核对的字段归集与原文拆条，不判断归类是否正确。"""
    attributes: dict[str, Any] = {}
    if role == "declaration":
        for name in ("name", "quantity", "unit", "refund_rate"):
            fact = facts.get(name)
            if fact is not None and fact.filled:
                attributes[name] = fact.value
        elements = facts.get("elements")
        if elements is not None and elements.filled:
            attributes["elements"] = parse_elements(elements.value)
    else:
        for name in (
            "primary_name",
            "alias",
            "specification",
            "manufacturer_model",
            "material",
            "quantity",
            "unit",
            "merge_hint",
        ):
            fact = facts.get(name)
            if fact is not None and fact.filled:
                attributes[name] = fact.value
    return attributes


def evidence_fact(field: str, fact: FieldFact) -> bool:
    """这一格是否构成“商品事实”。

    退税率列里的说明文字（真实模板常在退税列下方写要素名/备注）不算商品事实；
    数值或公式才算，避免把要素说明区当成商品行（P03 同口径）。
    """
    if field == "refund_rate" and fact.formula is None:
        return bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%?", fact.value.strip()))
    return fact.filled


def _preceding_total(cell, header_row: int, row: int) -> bool:
    """整行只有“汇总本表前序区域”的公式时，才允许判成非商品行。"""
    match = re.fullmatch(
        r"(?:SUM|COUNT|COUNTA|AVERAGE|MIN|MAX)\s*\(\s*\$?([A-Z]{1,3})\$?([0-9]+)"
        r"\s*:\s*\$?\1\$?([0-9]+)\s*\)",
        (cell.formula or "").strip(),
        re.I,
    )
    return bool(match and header_row < int(match[2]) <= int(match[3]) < row)


def _summary_reason(
    sheet: Sheet,
    layout: Layout,
    row: int,
    facts: Mapping[str, FieldFact],
    strong_fields: Sequence[str],
) -> str | None:
    """整行只有汇总本表前序区域的公式、或写成“合计”行时，判为汇总而不是商品。"""
    cells = [cell for cell in sheet.cells.values() if cell.row == row and cell.has_content]
    # 整行所有非空单元格都是“汇总本表前序区域”的公式（哪怕只有一格 SUM）就是合计行。
    # 必须先于强事实判断：合计公式本身（例如退税列的 SUM）会让字段“有内容”，
    # 但不能据此把合计行当成商品；有合同号/项号/品名的行不会走到这里。
    if cells and all(
        _preceding_total(cell, layout.header_row, row) for cell in cells
    ):
        return "summary_formula_only"
    # 写成“合计/总计/小计”的行，只要除品名外的非空单元格都是汇总前序数据的公式，
    # 就判为合计行——即使退税率等列里的汇总公式会让强事实字段“有内容”也不例外。
    # 本函数只在没有合同号/项号（且无浮图）时调用，不会误伤真实商品。
    name_field = "primary_name" if layout.role == "packing" else "name"
    name_fact = facts.get(name_field)
    if name_fact is not None and re.fullmatch(
        r"(?:合计|总计|小计)[:：]?", name_fact.value.strip()
    ):
        name_column = col_number(name_fact.column) if name_fact.column else -1
        others = [cell for cell in cells if cell.column != name_column]
        if all(_preceding_total(cell, layout.header_row, row) for cell in others):
            return "summary_row"
    strong = [facts[name] for name in strong_fields if name in facts]
    if any(fact.filled for fact in strong):
        return None
    return None


def _non_item_reason(row: int, facts: Mapping[str, FieldFact]) -> str:
    """没有身份（项号/品名）也没有事实的行：说明它是什么，但仍保留位置。"""
    if facts.get("contract") is not None and facts["contract"].value.strip():
        return "contract_only_or_footer"
    return "note_only"


def _unmapped_values(
    sheet: Sheet, layout: Layout, row: int
) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    for column in layout.unmapped_columns:
        cell = sheet.cell(f"{column.column}{row}")
        if cell is None or not cell.has_content:
            continue
        if not column.header.strip() and _is_legacy_approval(cell.value):
            continue
        values.append(
            {
                "column": column.column,
                "header": column.header,
                "cell": cell.ref,
                "value": cell.value,
                "formula": sheet.formulas.get(cell.ref),
            }
        )
    return tuple(values)


def _source_cells(
    facts: Mapping[str, FieldFact],
    row: int,
    floating_cells: Sequence[str],
) -> str:
    """记录占用到的单元格范围（同行的最小列到最大列），供 P06 存 ``Item.source_cells``。"""
    numbers = [
        cell_position(fact.cell)[0]
        for fact in facts.values()
        if fact.filled and fact.cell
    ]
    numbers.extend(cell_position(ref)[0] for ref in floating_cells if ref)
    if not numbers:
        return ""
    return f"{col_label(min(numbers))}{row}:{col_label(max(numbers))}{row}"


def extract_records(
    workbook: Workbook,
    layout: Layout,
    *,
    role: str,
    name_field: str,
    strong_fields: Sequence[str],
    floating_image_rows: Mapping[int, Sequence[str]] | None = None,
) -> tuple[tuple[SemanticRecord, ...], tuple[Mapping[str, Any], ...]]:
    """按行提取原文、来源、属性与状态；只按缺失/歧义给状态。

    ``name_field`` 是建立记录所需的“名字”字段：申报要素用 ``name``，装箱明细用
    ``primary_name``（缺“合同第一条品名”列时由“品名”兜底，见 ``locate_packing_layout``）。
    ``strong_fields`` 是“像商品”的强事实字段，用来把只有汇总公式的行排除掉（P03 口径）。
    """
    sheet = find_sheet(workbook, layout.sheet)
    floating = floating_image_rows or {}
    rows = sorted(set(sheet.occupied_rows) | set(floating))
    records: list[SemanticRecord] = []
    non_items: list[Mapping[str, Any]] = []
    issue_fields = ("contract", "item", name_field)

    for row in rows:
        if row <= layout.header_row:
            continue
        facts = {
            name: _field_fact(sheet, layout, name, row) for name in layout.columns
        }
        floating_cells = tuple(floating.get(row, ()))
        item = facts.get("item")
        name = facts.get(name_field)
        has_identity = bool(
            (item is not None and item.value.strip())
            or (name is not None and name.value.strip())
        )
        has_evidence = any(
            evidence_fact(field, fact)
            for field, fact in facts.items()
            if field not in issue_fields
        )
        contract = facts.get("contract")
        item_fact = facts.get("item")
        has_key = bool(
            (contract is not None and contract.value.strip())
            or (item_fact is not None and item_fact.value.strip())
        )
        if not has_key and not floating_cells:
            reason = _summary_reason(sheet, layout, row, facts, strong_fields)
            if reason is not None:
                cells = [
                    {"cell": cell.ref, "value": cell.value}
                    for cell in sheet.cells.values()
                    if cell.row == row and cell.has_content
                ]
                non_items.append({"row": row, "reason": reason, "cells": cells})
                continue
        if not has_identity and not has_evidence and not floating_cells:
            reason = _non_item_reason(row, facts)
            cells = [
                {"cell": cell.ref, "value": cell.value}
                for cell in sheet.cells.values()
                if cell.row == row and cell.has_content
            ]
            non_items.append({"row": row, "reason": reason, "cells": cells})
            continue

        issues: list[Issue] = []
        for field in issue_fields:
            fact = facts.get(field)
            if fact is not None and fact.value.strip():
                continue
            key_field = field in ("contract", "item")
            issues.append(
                Issue(
                    code=f"missing_{field}",
                    message=f"第 {row} 行缺少{FIELD_LABELS.get(field, field)}",
                    sheet=sheet.name,
                    cell=f"{layout.columns.get(field, '')}{row}",
                    row=row,
                    field=field,
                    severity="error" if key_field else "warning",
                    blocking=key_field,
                )
            )
        unmapped = _unmapped_values(sheet, layout, row)
        if not issues:
            status = "identified"
        else:
            status = "requires_review"
        records.append(
            SemanticRecord(
                role=role,
                sheet=sheet.name,
                row=row,
                status=status,
                issues=tuple(issues),
                facts=facts,
                raw={field: fact.value for field, fact in facts.items()},
                attributes=_attributes(role, facts),
                unmapped=unmapped,
                source_cells=_source_cells(facts, row, floating_cells),
                floating_image_cells=floating_cells,
            )
        )
    return tuple(records), tuple(non_items)


def floating_image_rows(workbook: Workbook, layout: Layout, *, issues=None) -> dict[int, tuple[str, ...]]:
    """普通浮图恰好锚定在附图列单个单元格上的行。

    只认“起止锚点都在同一单元格”的图片，用来发现**只有图片、没有文字**的申报行；
    跨行、表外、表头附近的图片不参与行发现（D2/R11：不就近猜归属）。
    """
    image_column = layout.columns.get("image")
    if not image_column:
        return {}
    column_number = col_number(image_column)
    result: dict[int, list[str]] = defaultdict(list)
    def image_error(exc):
        if issues is not None:
            issues.append(Issue(
                code="image_discovery_failed", message=f"浮图扫描失败（{exc.code}）；仅能保留文字/公式可识别商品，可能存在仅有图片而无法识别的行",
                sheet=exc.sheet or layout.sheet, cell=layout.header_cell("image"), row=layout.header_row,
                field="image", details={"cause": exc.code, "may_hide_image_only_rows": True},
            ))

    for anchor in workbook.picture_anchors(sheet_name=layout.sheet, on_error=image_error):
        if anchor.sheet != layout.sheet or not anchor.images:
            continue
        row = anchor.exact_cell(column_number)
        if row is None or row <= layout.header_row:
            continue
        result[row].append(f"{image_column}{row}")
    return {row: tuple(cells) for row, cells in result.items()}


# --------------------------------------------------------------------------- 申报要素入口
def locate_declaration_layout(
    workbook: Workbook,
    *,
    sheet_label: str = "申报要素",
    header_scan_rows: int = HEADER_SCAN_ROWS,
) -> Layout:
    return locate_layout(
        workbook,
        role="declaration",
        sheet_label=sheet_label,
        aliases=DECLARATION_ALIASES,
        required=DECLARATION_REQUIRED,
        optional=DECLARATION_OPTIONAL,
        header_scan_rows=header_scan_rows,
    )


def extract_declarations(
    workbook: Workbook,
    *,
    sheet_label: str = "申报要素",
    layout: Layout | None = None,
) -> SheetExtraction:
    """提取申报要素页签：一条申报记录＝一个商品行。

    不按装箱行数计商品，也不和装箱页签按相同行号对齐；关联见
    :func:`adapters.files.packing_links.link_declarations`。
    """
    layout = layout or locate_declaration_layout(workbook, sheet_label=sheet_label)
    image_issues = []
    floating = floating_image_rows(workbook, layout, issues=image_issues)
    records, non_items = extract_records(
        workbook,
        layout,
        role="declaration",
        name_field="name",
        strong_fields=DECLARATION_STRONG_FIELDS,
        floating_image_rows=floating,
    )
    return SheetExtraction(
        role="declaration", layout=layout, records=records, non_item_rows=non_items, issues=tuple(image_issues)
    )
