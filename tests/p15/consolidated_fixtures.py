"""P15 汇总表输入测试夹具。

全部内容为合成占位值，不含真实商品、合同号、编码、图片或客户信息。
构造器直接复用 P04 的 OOXML ``Package``，覆盖汇总表真实结构：
单页签宽表、合并箱号、隐藏列/行、公式有/无缓存、重复列、重复表头、
合计/说明/空行、图片列、原始记录 ID、数值/文本编码等。
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tests.p04.fixtures import Package, png

CONSOLIDATED_HEADERS: tuple[str, ...] = (
    "供应商",
    "采购合同号\n（请注意与合同一致）",
    "税收分类代码",
    "英文品名",
    "中文品名",
    "HS 编码",
    "进项税率",
    "申报要素",
    "合同号",
    "采购合同出货情况",
    "箱号",
    "提单号",
    "报关单号",
    "备注",
    "放行情况",
)

BASE_ROW: dict[str, Any] = {
    "supplier": "合成供应商甲",
    "purchase_contract_no": "SYN-P-001",
    "tax_classification_code": "1090000000000000000",
    "english_name": "Synthetic flange",
    "name": "合成法兰盘",
    "hs": "7307210000",
    "input_tax_rate": "0.13",
    "elements": "0|1|材质：不锈钢|用途：管道连接|规格：DN50",
    "contract_no": "SYN-S-001",
    "shipment_status": "出完",
    "container_no": "SYN-BOX-1",
    "bill_of_lading_no": "SYN-BL-001",
    "declaration_no": "SYN-DECL-001",
    "remark": "",
    "customs_release_status": "",
}

FIELD_ORDER = (
    "supplier",
    "purchase_contract_no",
    "tax_classification_code",
    "english_name",
    "name",
    "hs",
    "input_tax_rate",
    "elements",
    "contract_no",
    "shipment_status",
    "container_no",
    "bill_of_lading_no",
    "declaration_no",
    "remark",
    "customs_release_status",
)


def _column(index: int) -> str:
    result = ""
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _write_headers(
    sheet,
    headers: Sequence[str],
    *,
    header_row: int = 1,
    hidden_columns: Iterable[int] = (),
) -> None:
    for index, header in enumerate(headers, start=1):
        sheet.text(f"{_column(index)}{header_row}", header)
    hidden = sorted(set(hidden_columns))
    if hidden:
        cols = "".join(
            f'<col min="{index}" max="{index}" hidden="1"/>' for index in hidden
        )
        sheet.extra = f"<cols>{cols}</cols>"


def _write_row(sheet, row: int, values: Mapping[str, Any], headers: Sequence[str]) -> None:
    for index, header in enumerate(headers, start=1):
        field = _field_for_header(header, values)
        if field is None:
            continue
        value = values.get(field)
        if value is None or value == "":
            continue
        ref = f"{_column(index)}{row}"
        kind = values.get(f"__kind_{field}", "text")
        if kind == "number":
            sheet.number(ref, str(value))
        elif kind == "formula":
            sheet.formula(ref, str(value), str(values.get(f"__cached_{field}", "")))
        else:
            sheet.text(ref, str(value))


def _field_for_header(header: str, values: Mapping[str, Any]) -> str | None:
    normalized = header.replace("\n", "").split("（")[0].strip()
    explicit = values.get("__header_fields")
    if isinstance(explicit, Mapping) and header in explicit:
        return str(explicit[header])
    aliases = {
        "供应商": "supplier",
        "采购合同号": "purchase_contract_no",
        "税收分类代码": "tax_classification_code",
        "英文品名": "english_name",
        "中文品名": "name",
        "品名": "name",
        "HS 编码": "hs",
        "HS编码": "hs",
        "进项税率": "input_tax_rate",
        "申报要素": "elements",
        "合同号": "contract_no",
        "采购合同出货情况": "shipment_status",
        "箱号": "container_no",
        "提单号": "bill_of_lading_no",
        "报关单号": "declaration_no",
        "备注": "remark",
        "放行情况": "customs_release_status",
        "原始记录ID": "original_record_id",
        "图片": "image",
        "图片路径": "image",
        "规格": "spec",
        "材质": "material",
        "申报日期": "recorded_at",
    }
    return aliases.get(normalized)


def build_consolidated_workbook(
    path: Path,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    headers: Sequence[str] = CONSOLIDATED_HEADERS,
    sheet_name: str = "Sheet1",
    hidden_columns: Iterable[int] = (),
    merged_ranges: Sequence[str] = (),
    extra_rows: Sequence[Mapping[str, str]] = (),
    hidden_rows: Sequence[int] = (),
    with_image: bool = False,
    image_values: Mapping[int, str] | None = None,
) -> Path:
    """构造一个汇总表工作簿；``rows`` 默认两条可区分的商品行。"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    package = Package()
    sheet = package.sheet(sheet_name)
    effective_headers = tuple(headers) + (("图片",) if with_image and "图片" not in headers else ())
    _write_headers(sheet, effective_headers, hidden_columns=hidden_columns)
    records = list(rows) if rows is not None else [
        dict(BASE_ROW),
        dict(
            BASE_ROW,
            name="合成法兰盘",
            hs="7307210000",
            elements="0|1|材质：碳钢|用途：管道连接|规格：DN80",
            english_name="Synthetic flange DN80",
            declaration_no="SYN-DECL-002",
        ),
    ]
    for offset, record in enumerate(records):
        row = 2 + offset
        merged = dict(record)
        if with_image:
            merged.setdefault("image", '_xlfn.DISPIMG("ID_SYN",1)')
            merged.setdefault("__kind_image", "formula")
        _write_row(sheet, row, merged, effective_headers)
    for index, extra in enumerate(extra_rows, start=2 + len(records)):
        for column_index, value in enumerate(extra.get("values", ()), start=1):
            sheet.text(f"{_column(column_index)}{index}", value)
    for ref in merged_ranges:
        sheet.merge(ref)
    if with_image:
        package.cell_image("ID_SYN", "xl/media/syn.png")
        package.image("xl/media/syn.png", png(b"\x10\x20\x30"))
    if hidden_rows:
        sheet.hidden_rows.update(hidden_rows)
    package.build(path)
    return path


def build_minimal_workbook(
    path: Path,
    *,
    headers: Sequence[str] = ("中文品名", "HS 编码", "申报要素"),
    rows: Sequence[Mapping[str, Any]] | None = None,
    sheet_name: str = "Sheet1",
) -> Path:
    package = Package()
    sheet = package.sheet(sheet_name)
    _write_headers(sheet, headers)
    for offset, record in enumerate(rows or [{"name": "合成品", "hs": "7307210000"}]):
        _write_row(sheet, 2 + offset, record, headers)
    package.build(path)
    return path


def default_rows() -> list[dict[str, Any]]:
    return [deepcopy(BASE_ROW)]
