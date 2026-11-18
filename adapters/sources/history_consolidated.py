"""P15 历史汇总表输入适配器（``consolidated``）。

定位
----
真实历史数据被汇总成《历史成功数据.xlsx》一类的单页签宽表。本模块只做
**只读结构识别、字段映射、行分类、数据质量诊断和稳定身份计算**，产出可以被
``history_import`` 装配成 P15 ``HistoryRecord`` 的中立行对象；它不改 P04/P05
的官方导入模块，也不写数据库、不调用模型、不访问网络。

诚实边界
--------
- 文件名里的“历史成功数据”不是海关放行凭证。成功口径由导入批次的人工声明
  记录，本模块不根据文件名、归档时间或导入时间补申报日期/放行结论。
- 汇总表没有的列（如申报日期、项号、原始模板来源、图片）一律留空并报告，
  不从品名/规格/合同号推断，也不从旧模板“恢复”已经丢失的图片。
- 合并单元格只按明确规则取值：只有锚点单元格取原值；被合并覆盖的续行保持
  为空并记录 ``merged_continuation``，绝不把上一商品的 HS/要素/图片向下填。
- 公式单元格区分公式与缓存值：有缓存值时使用缓存并记录公式；缓存缺失时报告
  ``formula_cache_missing``，不执行公式、不访问外部链接。
- 外部图片路径/URL 默认不读取、不联网；只有显式给出图片根目录时才读取根目录
  内的本地文件，并拒绝目录穿越与符号链接越界。

身份
----
汇总表通常没有唯一主键。本模块默认用“真实可用业务字段的内容哈希 + 同内容
出现序号”生成 ``HISTC-`` 稳定 ID：

- 不受排序、插行、重新保存影响；
- 不因整份文件哈希变化把全部商品都变成新记录；
- 同 HS+品名但规格/材质/申报要素不同会得到不同 ID，不会错误合并；
- 单条修改会表现为“旧记录从来源消失 + 新内容出现”，报告可配对为 changed；
- 明确提供唯一 ``original_record_id`` 列时优先使用该列。

源文件哈希只作为溯源字段，不进入每行主身份。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from adapters.files.dispimg import (
    DEFAULT_MAX_IMAGE_BYTES,
    STATUS_ABSENT,
    STATUS_AVAILABLE,
    STATUS_READ_FAILED,
    RowImage,
    extract_column_images,
)
from adapters.files.ooxml import MediaInfo, OoxmlError, Sheet, Workbook, col_label, _decode_media

FORMAT_LEGACY = "legacy-template"
FORMAT_CONSOLIDATED = "consolidated"
FORMAT_AUTO = "auto"
FORMAT_CHOICES = (FORMAT_AUTO, FORMAT_LEGACY, FORMAT_CONSOLIDATED)

#: 导入状态：图片列缺失/歧义/行缺失，与 P05 的 available/absent/read_failed 分开。
IMAGE_COLUMN_MISSING = "column_missing"
IMAGE_COLUMN_AMBIGUOUS = "column_ambiguous"
IMAGE_ROW_MISSING = "row_missing"
IMAGE_EXTERNAL_URL_NOT_FETCHED = "external_url_not_fetched"
IMAGE_EXTERNAL_PATH_NOT_ENABLED = "external_path_not_enabled"
IMAGE_EXTERNAL_PATH_UNSAFE = "external_path_unsafe"
IMAGE_EXTERNAL_PATH_NOT_FOUND = "external_path_not_found"
IMAGE_TEXT_UNRECOGNIZED = "text_unrecognized"

#: 规范字段 -> 真实列名候选。真实列名优先，别名只用于同一业务含义的常见变体。
CONSOLIDATED_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "supplier": ("供应商", "供货商", "供应商名称"),
    "purchase_contract_no": (
        "采购合同号",
        "采购合同编号",
        "采购合同",
    ),
    "tax_classification_code": ("税收分类代码", "税收分类编码", "税收编码"),
    "english_name": ("英文品名", "英文名称", "英文商品名称"),
    "name": ("中文品名", "品名", "商品名称", "中文名称"),
    "hs": ("HS 编码", "HS编码", "海关编码", "商品编号", "HS Code", "HS CODE"),
    "input_tax_rate": ("进项税率", "进项税率(%)", "进项税率（%）"),
    "elements": ("申报要素", "申报要素原文", "要素"),
    "contract_no": ("合同号", "销售合同号", "销售合同编号"),
    "shipment_status": ("采购合同出货情况", "出货情况", "出运情况"),
    "container_no": ("箱号", "集装箱号", "箱号/柜号"),
    "bill_of_lading_no": ("提单号", "提单号码", "B/L No."),
    "declaration_no": ("报关单号", "报关单编号", "报关单号码"),
    "remark": ("备注", "说明", "备注说明"),
    "customs_release_status": ("放行情况", "放行状态", "海关放行情况"),
    "item_no": ("项号", "合同项号", "商品项号", "项次"),
    "quantity": ("数量", "成交数量"),
    "unit": ("单位", "计量单位"),
    "refund_rate": ("退税率", "出口退税率"),
    "spec": ("规格", "规格型号", "型号规格"),
    "material": ("材质", "材料"),
    "recorded_at": ("申报日期", "报关日期", "业务日期", "出口日期", "日期"),
    "image": (
        "图片",
        "附图",
        "商品图片",
        "产品图片",
        "图片路径",
        "图片链接",
        "DISPIMG",
    ),
    "original_source_file": ("源文件", "原始文件", "来源文件", "原始模板", "原始模板文件"),
    "original_source_row": ("源行", "源行号", "原始行号", "来源行", "原始模板行"),
    "original_record_id": ("原始记录ID", "原始记录编号", "原始ID", "记录ID", "源记录ID"),
}

REQUIRED_FIELDS: tuple[str, ...] = ("name", "hs")
RECOMMENDED_FIELDS: tuple[str, ...] = (
    "elements",
    "purchase_contract_no",
    "contract_no",
    "declaration_no",
)
#: 身份用的业务字段。规格/材质/要素不同必须保留为不同记录。
IDENTITY_FIELDS: tuple[str, ...] = (
    "supplier",
    "purchase_contract_no",
    "contract_no",
    "declaration_no",
    "hs",
    "name",
    "english_name",
    "elements",
    "spec",
    "material",
    "tax_classification_code",
    "input_tax_rate",
    "shipment_status",
    "container_no",
    "bill_of_lading_no",
    "remark",
    "customs_release_status",
    "item_no",
    "quantity",
    "unit",
    "refund_rate",
    "recorded_at",
    "original_source_file",
    "original_source_row",
)
#: 变更配对字段：只保留“这是同一业务商品行”的核心线索，避免备注/箱号变化被误判为新增。
CHANGE_FIELDS: tuple[str, ...] = (
    "supplier",
    "purchase_contract_no",
    "contract_no",
    "declaration_no",
)
#: 冲突判定字段：核心商品身份 + 规格/材质/要素；其余字段变化属于同一业务行的内容变化。
CONFLICT_FIELDS: tuple[str, ...] = (
    "supplier",
    "purchase_contract_no",
    "contract_no",
    "declaration_no",
    "hs",
    "name",
    "english_name",
    "elements",
    "spec",
    "material",
)

CONSOLIDATED_IDENTITY_VERSION = "p15.consolidated.identity.2"
RECORD_ID_PREFIX = "HISTC-"
SPACES = re.compile(r"\s+")
TOTAL_MARKERS = frozenset({"合计", "总计", "小计", "total", "subtotal"})
FORMULA_ERROR_MARKERS = frozenset(
    {"#N/A", "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!"}
)
SCIENTIFIC = re.compile(r"[eE][+-]?\d+")
INTEGER_TEXT = re.compile(r"^-?\d+$")
DECIMAL_TEXT = re.compile(r"^-?\d+\.0+$")
DOT_LABEL_VALUE = re.compile(
    r"^\s*(\d{1,2})\s*[.、]\s*(.*?)\s*(?:\[\?\]|\[？\]|[:：])\s*(.*?)\s*$",
    re.MULTILINE,
)


class ConsolidatedFormatError(ValueError):
    """结构识别/字段映射失败；调用方应拒绝而不是猜。"""

    def __init__(self, code: str, message: str = "", *, details: Mapping[str, Any] | None = None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message or code)


# --------------------------------------------------------------------------- 基础工具
def _normalize(text: Any) -> str:
    return SPACES.sub(" ", "" if text is None else str(text)).strip()


def _compact(text: Any) -> str:
    return SPACES.sub("", _normalize(text)).casefold()


def _strip_text_marker(text: str) -> str:
    """去掉 Excel/WPS 用来强制文本的前导单引号；原始值另存 raw。"""

    value = text or ""
    stripped = value.lstrip()
    if stripped.startswith("'"):
        return stripped[1:]
    return value


def normalized_value(text: Any) -> str:
    return _normalize(_strip_text_marker("" if text is None else str(text)))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_key(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    blocking: bool = False,
    sheet: str = "",
    row: int | None = None,
    cell: str = "",
    field_name: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "sheet": sheet,
        "row": row,
        "cell": cell,
        "field": field_name,
        "severity": severity,
        "blocking": blocking,
        "details": dict(details or {}),
    }


# --------------------------------------------------------------------------- 数据类
@dataclass
class ColumnMatch:
    field: str
    header: str
    column: int
    letter: str
    hidden: bool = False


@dataclass
class ConsolidatedLayout:
    sheet: str
    sheet_part: str
    header_row: int
    columns: dict[str, ColumnMatch]
    headers: dict[int, str]
    duplicate_columns: list[dict[str, Any]]
    unmatched_headers: list[str]
    hidden_columns: list[str]
    hidden_rows: list[int]
    merges: list[dict[str, Any]]
    header_candidates: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    mapping_source: str = "default"


@dataclass
class CellRead:
    field: str
    text: str = ""
    raw: str = ""
    ref: str = ""
    status: str = "empty"
    cell_kind: str = ""
    formula: str = ""
    formula_has_cache: bool = False
    number_format: str = ""
    merge_ref: str = ""
    merge_anchor: str = ""
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConsolidatedRow:
    """一条汇总表商品行，接口与 P04 ``SemanticRecord`` 的最小子集兼容。"""

    sheet: str
    row: int
    values: dict[str, str]
    raw_values: dict[str, str]
    cells: dict[str, CellRead]
    issues: list[dict[str, Any]]
    requires_review: bool = False
    report_fields: dict[str, Any] = field(default_factory=dict)
    source_extra: dict[str, Any] = field(default_factory=dict)
    notes_prefix: str = ""
    record_id: str = ""
    source_key: str = ""
    change_key: str = ""
    business_key: str = ""
    content_hash: str = ""
    identity_basis: str = "content"
    content_occurrence: int = 0
    business_occurrence: int = 0
    change_occurrence: int = 0
    locator: str = ""
    source_system: str = ""
    expects_item: bool = False
    extra_attributes: dict[str, str] = field(default_factory=dict)
    source_cells: str = ""
    row_image: Any | None = None
    image_status_override: str = ""

    @property
    def status(self) -> str:
        return "identified"

    @property
    def unmapped(self) -> tuple[Any, ...]:
        return ()

    @property
    def recorded_at_value(self) -> str:
        return self.values.get("recorded_at", "")

    @property
    def recorded_at_cell(self) -> str:
        return self.cells.get("recorded_at", CellRead("recorded_at")).ref

    def value(self, field_name: str) -> str:
        if field_name == "contract":
            return self.values.get("purchase_contract_no", "") or self.values.get(
                "contract_no", ""
            )
        return self.values.get(field_name, "")

    def cell(self, field_name: str) -> str:
        item = self.cells.get(field_name)
        return item.ref if item is not None else ""

    def as_skipped_report(self, *, kind: str, reason: str) -> dict[str, Any]:
        return {
            "record_id": "",
            "source_key": "",
            "source": {
                "file_id": self.source_extra.get("source_file_id", ""),
                "filename": self.source_extra.get("source_filename", ""),
                "sheet": self.sheet,
                "row": self.row,
                "source_cells": self.source_cells,
            },
            "status": "ignored",
            "row_kind": kind,
            "reason": reason,
            "fields": {},
            "missing_fields": [],
            "issues": list(self.issues),
        }


@dataclass
class ConsolidatedParseResult:
    layout: ConsolidatedLayout
    records: list[ConsolidatedRow]
    skipped_rows: list[dict[str, Any]]
    row_classification: dict[str, int]
    data_quality: dict[str, Any]
    image_info: dict[str, Any]
    image_issues: list[dict[str, Any]]
    task_issues: list[dict[str, Any]]
    warnings: list[str]
    source_file_sha256: str


# --------------------------------------------------------------------------- 列映射
def load_mapping_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConsolidatedFormatError("mapping_unreadable", f"映射配置无法读取：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConsolidatedFormatError("mapping_invalid_json", f"映射配置不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ConsolidatedFormatError("mapping_invalid", "映射配置顶层必须是 JSON 对象")
    fields = payload.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            raise ConsolidatedFormatError("mapping_invalid", "mapping.fields 必须是对象")
        unknown = sorted(set(fields) - set(CONSOLIDATED_FIELD_ALIASES))
        if unknown:
            raise ConsolidatedFormatError(
                "mapping_unknown_field", f"映射配置含未知字段：{unknown}"
            )
        for key, aliases in fields.items():
            if isinstance(aliases, str):
                payload["fields"][key] = [aliases]
            elif not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
                raise ConsolidatedFormatError(
                    "mapping_invalid", f"字段 {key} 的别名必须是字符串或字符串数组"
                )
    for key in ("sheet", "header_row"):
        if key in payload and payload[key] not in (None, ""):
            if key == "header_row":
                try:
                    payload[key] = int(payload[key])
                except (TypeError, ValueError) as exc:
                    raise ConsolidatedFormatError("mapping_invalid", "header_row 必须是整数") from exc
                if payload[key] < 1:
                    raise ConsolidatedFormatError("mapping_invalid", "header_row 必须 >= 1")
            elif not isinstance(payload[key], str):
                raise ConsolidatedFormatError("mapping_invalid", "sheet 必须是字符串")
    return payload


def _field_aliases(mapping: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    aliases = dict(CONSOLIDATED_FIELD_ALIASES)
    for key, values in (mapping.get("fields") or {}).items():
        aliases[key] = tuple(values)
    return aliases


def _normalize_header(text: Any) -> str:
    return SPACES.sub(" ", "" if text is None else str(text)).strip()


def _header_matches(header: str, alias: str) -> bool:
    normalized_header = _normalize_header(header)
    normalized_alias = _normalize_header(alias)
    if not normalized_header or not normalized_alias:
        return False
    if normalized_header.casefold() == normalized_alias.casefold():
        return True
    # 真实表头常带括号说明，如“采购合同号\n（请注意与合同一致）”。
    if normalized_header.casefold().startswith(normalized_alias.casefold()):
        remainder = normalized_header[len(normalized_alias):].strip()
        return remainder.startswith(("（", "(")) and remainder.endswith(("）", ")"))
    return False


def _sheet_max_row(sheet: Sheet) -> int:
    return max((cell.row for cell in sheet.cells.values()), default=0)


def _sheet_max_col(sheet: Sheet) -> int:
    return max((cell.column for cell in sheet.cells.values()), default=0)


def _read_sheet_metadata(path: Path, sheet_part: str) -> dict[str, Any]:
    """读取隐藏列/行与数字格式；失败只影响诊断，不影响单元格值读取。"""

    result: dict[str, Any] = {
        "hidden_columns": [],
        "hidden_rows": [],
        "number_formats": {},
        "metadata_error": "",
    }
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            sheet_xml = archive.read(sheet_part)
            styles_xml = archive.read("xl/styles.xml") if "xl/styles.xml" in names else b""
    except (OSError, KeyError, BadZipFile) as exc:
        result["metadata_error"] = f"{type(exc).__name__}: {exc}"
        return result

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        root = ET.fromstring(sheet_xml)
        for col in root.findall("s:cols/s:col", namespace):
            if col.get("hidden") in {"1", "true", "TRUE"}:
                try:
                    first = int(col.get("min", "0"))
                    last = int(col.get("max", "0"))
                except ValueError:
                    continue
                result["hidden_columns"].extend(
                    col_label(number) for number in range(first, last + 1) if number > 0
                )
        for row in root.findall("s:sheetData/s:row", namespace):
            if row.get("hidden") in {"1", "true", "TRUE"}:
                try:
                    result["hidden_rows"].append(int(row.get("r", "0")))
                except ValueError:
                    pass
    except ET.ParseError:
        result["metadata_error"] = "sheet_xml_unparseable"

    if styles_xml:
        try:
            styles = ET.fromstring(styles_xml)
            custom = {
                int(item.get("numFmtId", "0")): item.get("formatCode", "")
                for item in styles.findall("s:numFmts/s:numFmt", namespace)
            }
            for index, xf in enumerate(styles.findall("s:cellXfs/s:xf", namespace)):
                num_fmt_id = int(xf.get("numFmtId", "0"))
                result["number_formats"][str(index)] = custom.get(num_fmt_id, "")
        except (ET.ParseError, ValueError):
            result["metadata_error"] = result["metadata_error"] or "styles_xml_unparseable"
    return result


def _header_matches_for_row(
    sheet: Sheet, header_row: int, aliases: Mapping[str, tuple[str, ...]]
) -> tuple[dict[str, ColumnMatch], list[dict[str, Any]], dict[int, str]]:
    max_col = max(_sheet_max_col(sheet), 1)
    headers: dict[int, str] = {}
    for column in range(1, max_col + 1):
        cell = sheet.cell_at(header_row, column)
        text = _normalize_header(cell.value if cell is not None else "")
        if text:
            headers[column] = text
    matched: dict[str, ColumnMatch] = {}
    duplicates: list[dict[str, Any]] = []
    for field_name, candidates in aliases.items():
        hits = [
            (column, header)
            for column, header in headers.items()
            if any(_header_matches(header, alias) for alias in candidates)
        ]
        if len(hits) == 1:
            column, header = hits[0]
            matched[field_name] = ColumnMatch(
                field=field_name,
                header=header,
                column=column,
                letter=col_label(column),
            )
        elif len(hits) > 1:
            duplicates.append(
                {
                    "field": field_name,
                    "columns": [col_label(column) for column, _ in hits],
                    "headers": [header for _, header in hits],
                }
            )
    return matched, duplicates, headers


def _candidate_header_rows(
    sheet: Sheet, aliases: Mapping[str, tuple[str, ...]], *, limit: int = 30
) -> list[dict[str, Any]]:
    max_row = _sheet_max_row(sheet)
    candidates: list[dict[str, Any]] = []
    for row in range(1, min(max_row, limit) + 1):
        matched, duplicates, headers = _header_matches_for_row(sheet, row, aliases)
        required_hits = sum(1 for name in REQUIRED_FIELDS if name in matched)
        optional_hits = len(set(matched) - set(REQUIRED_FIELDS))
        candidates.append(
            {
                "row": row,
                "matched": matched,
                "duplicates": duplicates,
                "headers": headers,
                "required_hits": required_hits,
                "optional_hits": optional_hits,
                "score": required_hits * 100 + optional_hits,
            }
        )
    return candidates


def _select_layout(
    workbook: Workbook,
    *,
    path: Path,
    mapping: Mapping[str, Any],
) -> ConsolidatedLayout:
    aliases = _field_aliases(mapping)
    specified_sheet = str(mapping.get("sheet") or "").strip()
    specified_header = mapping.get("header_row")

    if specified_sheet:
        try:
            sheet = workbook.sheet(specified_sheet)
        except OoxmlError as exc:
            raise ConsolidatedFormatError(
                "sheet_not_found", f"指定的页签不存在：{specified_sheet}（{exc}）"
            ) from exc
        sheets = [sheet]
    else:
        sheets = [sheet for sheet in workbook.sheets if sheet.visible]

    evaluated: list[tuple[Sheet, list[dict[str, Any]]]] = []
    for sheet in sheets:
        candidates = (
            _candidate_header_rows(sheet, aliases, limit=specified_header or 30)
            if specified_header
            else _candidate_header_rows(sheet, aliases)
        )
        evaluated.append((sheet, candidates))

    if specified_header:
        chosen = [
            (sheet, next((item for item in candidates if item["row"] == specified_header), None))
            for sheet, candidates in evaluated
        ]
        chosen = [(sheet, candidate) for sheet, candidate in chosen if candidate is not None]
        if not chosen:
            raise ConsolidatedFormatError(
                "header_row_not_found", f"指定表头行 {specified_header} 没有可识别列"
            )
        if len(chosen) > 1:
            raise ConsolidatedFormatError(
                "sheet_ambiguous",
                "指定表头行在多个页签同时可识别，请用映射配置指定 sheet",
                details={"sheets": [item[0].name for item in chosen]},
            )
        sheet, candidate = chosen[0]
        header_candidates = [(sheet, candidate)]
    else:
        valid = [
            (sheet, candidate)
            for sheet, candidates in evaluated
            for candidate in candidates
            if candidate["required_hits"] == len(REQUIRED_FIELDS)
        ]
        if not valid:
            for evaluated_sheet, candidates in evaluated:
                for candidate_item in candidates:
                    ambiguous_required = [
                        item
                        for item in candidate_item["duplicates"]
                        if item["field"] in REQUIRED_FIELDS
                    ]
                    if ambiguous_required:
                        raise ConsolidatedFormatError(
                            "required_column_ambiguous",
                            f"必填字段匹配到多列，不能擅自选择：{[item['field'] for item in ambiguous_required]}",
                            details={
                                "sheet": evaluated_sheet.name,
                                "row": candidate_item["row"],
                                "duplicates": ambiguous_required,
                            },
                        )
            details = {
                "sheets": [
                    {
                        "sheet": sheet.name,
                        "headers": [
                            {"row": item["row"], "headers": list(item["headers"].values())}
                            for item in candidates[:3]
                        ],
                    }
                    for sheet, candidates in evaluated
                ]
            }
            raise ConsolidatedFormatError(
                "consolidated_headers_not_found",
                "没有找到同时包含中文品名与 HS 编码的汇总表表头；不能猜字段",
                details=details,
            )
        best_score = max(candidate["score"] for _, candidate in valid)
        top = [(sheet, candidate) for sheet, candidate in valid if candidate["score"] == best_score]
        if len(top) > 1:
            # 同分候选只有在同一页签的“重复表头行”时才可降级为告警；
            # 不同页签/不同结构必须让调用方显式选择。
            sheets_involved = {sheet.name for sheet, _ in top}
            if len(sheets_involved) > 1:
                raise ConsolidatedFormatError(
                    "sheet_ambiguous",
                    "多个页签都能识别为汇总表，请用 --sheet 或映射配置指定",
                    details={"sheets": sorted(sheets_involved)},
                )
            raise ConsolidatedFormatError(
                "header_row_ambiguous",
                "同一页签存在多个同分疑似表头行，请用映射配置指定 header_row",
                details={"rows": sorted(candidate["row"] for _, candidate in top)},
            )
        sheet, candidate = top[0]
        header_candidates = [item for item in valid if item[0] is sheet]
        header_candidates.sort(key=lambda item: item[1]["row"])

    header_row = int(candidate["row"])
    matched = dict(candidate["matched"])
    duplicates = list(candidate["duplicates"])
    headers = dict(candidate["headers"])

    duplicate_fields = {item["field"] for item in duplicates}
    for name in REQUIRED_FIELDS:
        if name in duplicate_fields:
            raise ConsolidatedFormatError(
                "required_column_ambiguous",
                f"必填字段 {name} 匹配到多列，不能擅自选择",
                details={"duplicates": duplicates},
            )
    missing_required = [name for name in REQUIRED_FIELDS if name not in matched]
    if missing_required:
        raise ConsolidatedFormatError(
            "required_column_missing",
            f"缺少必填列：{missing_required}；发现的表头：{list(headers.values())}",
            details={"missing": missing_required, "headers": list(headers.values())},
        )

    warnings: list[dict[str, Any]] = []
    for item in duplicates:
        for column in item["columns"]:
            matched.pop(item["field"], None)
            warnings.append(
                _issue(
                    "optional_column_ambiguous",
                    f"可选字段 {item['field']} 匹配到多列 {item['columns']}，已不取该字段",
                    severity="warning",
                    sheet=sheet.name,
                    field_name=item["field"],
                    details=item,
                )
            )
    metadata = _read_sheet_metadata(path, sheet.part)
    hidden_columns = list(metadata["hidden_columns"])
    for name, match in matched.items():
        match.hidden = match.letter in hidden_columns
        if match.hidden:
            warnings.append(
                _issue(
                    "hidden_column",
                    f"字段 {name} 所在列 {match.letter} 是隐藏列；仍读取，但来源需人工确认",
                    severity="warning",
                    sheet=sheet.name,
                    cell=f"{match.letter}{header_row}",
                    field_name=name,
                )
            )
    hidden_rows = sorted(set(metadata["hidden_rows"]))
    if hidden_rows:
        warnings.append(
            _issue(
                "hidden_rows_present",
                f"页签存在 {len(hidden_rows)} 个隐藏行；仍逐行识别并报告，不静默吞行",
                severity="warning",
                sheet=sheet.name,
                details={"rows": hidden_rows[:50], "count": len(hidden_rows)},
            )
        )
    recommended_missing = [name for name in RECOMMENDED_FIELDS if name not in matched]
    for name in recommended_missing:
        warnings.append(
            _issue(
                "recommended_column_missing",
                f"汇总表没有 {name} 列；该字段留空，不推断",
                severity="warning",
                sheet=sheet.name,
                field_name=name,
            )
        )
    if metadata.get("metadata_error"):
        warnings.append(
            _issue(
                "sheet_metadata_unavailable",
                f"隐藏列/数字格式元数据读取不完整：{metadata['metadata_error']}",
                severity="warning",
                sheet=sheet.name,
            )
        )
    merges = [
        {
            "ref": merge.ref,
            "top": merge.top,
            "left": merge.left,
            "bottom": merge.bottom,
            "right": merge.right,
            "vertical": merge.vertical,
        }
        for merge in sheet.merges
    ]
    unmatched = [
        header
        for column, header in headers.items()
        if column not in {match.column for match in matched.values()}
    ]
    return ConsolidatedLayout(
        sheet=sheet.name,
        sheet_part=sheet.part,
        header_row=header_row,
        columns=matched,
        headers=headers,
        duplicate_columns=duplicates,
        unmatched_headers=unmatched,
        hidden_columns=hidden_columns,
        hidden_rows=hidden_rows,
        merges=merges,
        header_candidates=[
            {
                "sheet": sheet.name,
                "row": item["row"],
                "required_hits": item["required_hits"],
                "optional_hits": item["optional_hits"],
            }
            for _, item in header_candidates
        ],
        warnings=warnings,
        mapping_source="explicit" if mapping else "default",
    )


# --------------------------------------------------------------------------- 结构识别
#: 汇总表相对旧模板的“结构性证据”。只靠品名/HS 无法区分，必须有至少两个
#: 汇总表特征列（供应商、英文品名、税收分类代码、报关单号、放行情况等）才自动认。
CONSOLIDATED_DISTINCTIVE_FIELDS = (
    "supplier",
    "english_name",
    "tax_classification_code",
    "declaration_no",
    "shipment_status",
    "customs_release_status",
)


def _looks_like_consolidated(workbook: Workbook, mapping: Mapping[str, Any] | None = None) -> bool:
    mapping = mapping or {}
    aliases = _field_aliases(mapping)
    for sheet in workbook.sheets:
        if mapping.get("sheet") and sheet.name != mapping["sheet"]:
            continue
        if not sheet.visible and not mapping.get("sheet"):
            continue
        for candidate in _candidate_header_rows(sheet, aliases, limit=int(mapping.get("header_row") or 30)):
            if mapping.get("header_row") and candidate["row"] != mapping["header_row"]:
                continue
            if candidate["required_hits"] != len(REQUIRED_FIELDS):
                continue
            matched = set(candidate["matched"])
            distinctive = len(matched & set(CONSOLIDATED_DISTINCTIVE_FIELDS))
            exact_chinese_name = "中文品名" in {
                _normalize_header(value) for value in candidate["headers"].values()
            }
            explicit_required = all(
                name in (mapping.get("fields") or {}) for name in REQUIRED_FIELDS
            )
            # 旧申报页也有供应商/英文品名，不能只凭这两列当成汇总表。
            if explicit_required or (distinctive >= 2 and ("declaration_no" in matched or exact_chinese_name)):
                return True
    return False


def _looks_like_legacy(workbook: Workbook) -> bool:
    legacy_sheet_markers = ("申报要素", "装箱明细")
    if any(
        marker in sheet.name
        for sheet in workbook.sheets
        for marker in legacy_sheet_markers
    ):
        return True
    for sheet in workbook.sheets:
        if not sheet.visible:
            continue
        max_row = min(_sheet_max_row(sheet), 20)
        for row in range(1, max_row + 1):
            headers = {
                _normalize_header(cell.value)
                for column in range(1, max(_sheet_max_col(sheet), 1) + 1)
                if (cell := sheet.cell_at(row, column)) is not None
            }
            if "品名" in headers and ("海关编码" in headers or "HS编码" in headers):
                if "申报要素" in headers:
                    return True
    return False


def _has_legacy_packing_sheet(workbook: Workbook) -> bool:
    return any("装箱明细" in sheet.name for sheet in workbook.sheets)


def detect_workbook_format(workbook: Workbook, mapping: Mapping[str, Any] | None = None) -> str:
    consolidated = _looks_like_consolidated(workbook, mapping)
    legacy = _looks_like_legacy(workbook)
    if consolidated and legacy:
        # 两套独立结构同时出现（包括不同页签）也是歧义，不按页签数量猜。
        raise ConsolidatedFormatError(
            "input_format_ambiguous",
            "工作簿同时像旧多页签模板和汇总表；请显式选择 --input-format",
            details={"sheets": [sheet.name for sheet in workbook.sheets]},
        )
    if consolidated:
        return FORMAT_CONSOLIDATED
    if legacy:
        return FORMAT_LEGACY
    raise ConsolidatedFormatError(
        "input_format_unknown",
        "无法识别输入格式；请显式选择 --input-format 或提供映射配置",
        details={"sheets": [sheet.name for sheet in workbook.sheets]},
    )


# --------------------------------------------------------------------------- 行/单元格读取
def _number_format_for(cell: Any, number_formats: Mapping[str, str]) -> str:
    style = getattr(cell, "style", None)
    if style is None:
        return ""
    return number_formats.get(str(style), "")


def _cell_issues_for_text(
    *,
    field_name: str,
    text: str,
    raw: str,
    cell: Any,
    number_format: str,
    sheet: str,
    row: int,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    ref = cell.ref if cell is not None else ""
    if text in FORMULA_ERROR_MARKERS or (text.startswith("#") and text.endswith(("!", "?", "A"))):
        issues.append(
            _issue(
                f"cell_error_value_{field_name}",
                f"{field_name} 单元格是 Excel 错误值 {text}",
                severity="error",
                blocking=field_name in REQUIRED_FIELDS,
                sheet=sheet,
                row=row,
                cell=ref,
                field_name=field_name,
            )
        )
    if cell is not None and getattr(cell, "kind", "") == "n" and re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", (raw or "").strip()
    ):
        issues.append(
            _issue(
                f"numeric_precision_risk_{field_name}",
                f"{field_name} 以科学计数法存储（{raw}），原始编码可能已失真；按原始值保留，不猜补",
                severity="warning",
                sheet=sheet,
                row=row,
                cell=ref,
                field_name=field_name,
            )
        )
    if (
        cell is not None
        and getattr(cell, "kind", "") == "n"
        and re.fullmatch(r"\d{16,}", (raw or "").strip())
        and field_name in {"hs", "item_no", "purchase_contract_no", "contract_no", "declaration_no", "tax_classification_code", "original_record_id"}
    ):
        issues.append(
            _issue(
                f"numeric_precision_risk_{field_name}",
                f"{field_name} 以超过15位的Excel数值存储，精度可能已丢失；保留包内原文，不猜还原",
                sheet=sheet, row=row, cell=ref, field_name=field_name,
                details={"raw": raw},
            )
        )
    if number_format and re.fullmatch(r"0+", number_format.strip()) and INTEGER_TEXT.match(raw or ""):
        expected_width = len(number_format.strip())
        if len(raw) < expected_width:
            issues.append(
                _issue(
                    f"number_format_leading_zero_{field_name}",
                    f"{field_name} 的数字格式要求 {expected_width} 位（0 填充），原值为 {text}；"
                    "按格式补零属于确定性显示，但原始值另存 raw",
                    severity="info",
                    sheet=sheet,
                    row=row,
                    cell=ref,
                    field_name=field_name,
                    details={"expected_width": expected_width, "raw": raw},
                )
            )
    if (raw or "").lstrip().startswith("'"):
        issues.append(
            _issue(
                f"text_marker_removed_{field_name}",
                f"{field_name} 原文带前导单引号（Excel 文本标记）；业务值去掉标记，raw 保留原文",
                severity="info",
                sheet=sheet,
                row=row,
                cell=ref,
                field_name=field_name,
                details={"raw": raw},
            )
        )
    return issues


def _read_cell(
    sheet: Sheet,
    *,
    row: int,
    column: int,
    field_name: str,
    number_formats: Mapping[str, str],
) -> CellRead:
    ref = f"{col_label(column)}{row}"
    cell = sheet.cell_at(row, column)
    merge = sheet.enclosing_merge(row, column)
    result = CellRead(field=field_name, ref=ref)
    if merge is not None:
        result.merge_ref = merge.ref
        result.merge_anchor = merge.anchor_ref
        is_anchor = row == merge.top and column == merge.left
        if not is_anchor:
            # 只按明确规则取值：纵向/横向合并的非锚点单元格保持为空，
            # 绝不把上一商品的 HS/要素/图片向下填。
            result.status = "merged_continuation"
            result.issues.append(
                _issue(
                    f"merged_continuation_{field_name}",
                    f"{field_name} 位于合并单元格 {merge.ref} 的非锚点位置；按规则留空，不借用 {merge.anchor_ref} 的值",
                    severity="warning",
                    sheet=sheet.name,
                    row=row,
                    cell=ref,
                    field_name=field_name,
                    details={"merge": merge.ref, "anchor": merge.anchor_ref},
                )
            )
            return result
    if cell is None:
        return result
    result.cell_kind = cell.kind
    result.raw = cell.value or ""
    result.formula = sheet.formulas.get(ref) or cell.formula or ""
    if result.formula:
        result.formula_has_cache = bool(cell.value)
        if not cell.value:
            result.status = "formula_cache_missing"
            result.issues.append(
                _issue(
                    f"formula_cache_missing_{field_name}",
                    f"{field_name} 是公式单元格且没有缓存值；不执行公式、不取外部链接，按缺失处理",
                    severity="error" if field_name in REQUIRED_FIELDS else "warning",
                    blocking=field_name in REQUIRED_FIELDS,
                    sheet=sheet.name,
                    row=row,
                    cell=ref,
                    field_name=field_name,
                    details={"formula": result.formula},
                )
            )
            return result
        result.issues.append(
            _issue(
                f"formula_cache_used_{field_name}",
                f"{field_name} 是公式单元格；使用工作簿缓存值，公式原文另存报告",
                severity="info",
                sheet=sheet.name,
                row=row,
                cell=ref,
                field_name=field_name,
                details={"formula": result.formula},
            )
        )
    if not result.raw:
        result.status = "empty"
        return result
    if merge is not None:
        result.issues.append(
            _issue(
                f"merged_anchor_{field_name}",
                f"{field_name} 取自合并单元格 {merge.ref} 的锚点 {merge.anchor_ref}",
                severity="info",
                sheet=sheet.name,
                row=row,
                cell=ref,
                field_name=field_name,
                details={"merge": merge.ref},
            )
        )
    result.number_format = _number_format_for(cell, number_formats)
    raw_text = str(result.raw)
    text = raw_text
    if INTEGER_TEXT.match(raw_text) and result.number_format and re.fullmatch(
        r"0+", result.number_format.strip()
    ):
        text = raw_text.zfill(len(result.number_format.strip()))
    result.text = text.strip()
    result.status = "ok"
    result.issues.extend(
        _cell_issues_for_text(
            field_name=field_name,
            text=result.text,
            raw=raw_text,
            cell=cell,
            number_format=result.number_format,
            sheet=sheet.name,
            row=row,
        )
    )
    return result


# --------------------------------------------------------------------------- 主解析
class ConsolidatedParser:
    def __init__(
        self,
        workbook: Workbook,
        *,
        path: Path,
        source_id: str,
        original_filename: str,
        dataset_kind: str = "real",
        mapping: Mapping[str, Any] | None = None,
        image_root: Path | None = None,
    ) -> None:
        self.workbook = workbook
        self.path = Path(path)
        self.source_id = source_id
        self.original_filename = original_filename
        self.dataset_kind = dataset_kind
        self.mapping = dict(mapping or {})
        self.image_root = Path(image_root).resolve() if image_root is not None else None
        self.layout = _select_layout(workbook, path=self.path, mapping=self.mapping)
        self.sheet = workbook.sheet(self.layout.sheet)
        self.metadata = _read_sheet_metadata(self.path, self.sheet.part)
        self.number_formats = self.metadata.get("number_formats", {}) or {}
        self.aliases = _field_aliases(self.mapping)
        self.task_issues: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.data_quality: dict[str, Any] = {}
        self.row_classification: dict[str, int] = Counter()

    # -- 读取一行 ---------------------------------------------------------
    def _read_row(self, row: int) -> ConsolidatedRow:
        cells = {
            name: _read_cell(
                self.sheet,
                row=row,
                column=match.column,
                field_name=name,
                number_formats=self.number_formats,
            )
            for name, match in self.layout.columns.items()
        }
        # 缺失列不是“空单元格”：显式记 column_missing，供报告区分。
        for name in CONSOLIDATED_FIELD_ALIASES:
            if name not in cells:
                cells[name] = CellRead(field=name, status="column_missing")
        values = {name: _value_for_p15(name, item) for name, item in cells.items()}
        raw_values = {name: item.raw for name, item in cells.items()}
        issues: list[dict[str, Any]] = []
        for item in cells.values():
            issues.extend(item.issues)
        date_cell = cells.get("recorded_at")
        if (
            date_cell is not None
            and date_cell.cell_kind == "n"
            and re.fullmatch(r"\d+(?:\.\d+)?", (date_cell.raw or "").strip())
        ):
            # Excel 日期序列号不转换成业务日期；保留原值到 raw，recorded_at 留空。
            values["recorded_at"] = ""
            date_issue = _issue(
                    "recorded_at_numeric_serial",
                    "申报日期列是 Excel 数值（可能是日期序列号）；不换算成业务日期，按缺失处理",
                    severity="warning",
                    sheet=self.layout.sheet,
                    row=row,
                    cell=date_cell.ref,
                    field_name="recorded_at",
                    details={"raw": date_cell.raw},
                )
            date_cell.issues.append(date_issue)
            issues.append(date_issue)
        source_cells = f"A{row}:{col_label(max(_sheet_max_col(self.sheet), 1))}{row}"
        row_obj = ConsolidatedRow(
            sheet=self.layout.sheet,
            row=row,
            values=values,
            raw_values=raw_values,
            cells=cells,
            issues=issues,
            source_cells=source_cells,
            expects_item=False,
        )
        self._apply_row_rules(row_obj)
        return row_obj

    def _apply_row_rules(self, row: ConsolidatedRow) -> None:
        name = row.values.get("name", "")
        hs = row.values.get("hs", "")
        if not name:
            row.issues.append(
                _issue(
                    "missing_name",
                    "中文品名为空，无法生成 P15 历史记录；该行保留在报告，不静默丢弃",
                    severity="error",
                    blocking=True,
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("name"),
                    field_name="name",
                )
            )
        if not hs:
            row.issues.append(
                _issue(
                    "missing_hs",
                    "HS 编码为空，无法生成 P15 历史记录；该行保留在报告，不静默丢弃",
                    severity="error",
                    blocking=True,
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("hs"),
                    field_name="hs",
                )
            )
        if hs and not re.fullmatch(r"[0-9]{10}", hs):
            code = "hs_non_ascii_digits" if any(char.isdecimal() and char not in "0123456789" for char in hs) else "hs_length_not_10"
            row.issues.append(
                _issue(
                    code,
                    f"HS 编码原文为 {hs!r}，不符合 10 位 ASCII 数字格式；按原文保留，不补零/不截断/不猜转换，标待核",
                    severity="warning",
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("hs"),
                    field_name="hs",
                    details={"hs": hs, "length": len(hs)},
                )
            )
            row.requires_review = True
        if not row.values.get("elements"):
            if row.cells.get("elements", CellRead("elements")).status == "column_missing":
                pass  # 文件级已报 column_missing，不逐行重复
            else:
                row.issues.append(
                    _issue(
                        "missing_elements",
                        "申报要素为空；原文留空，不从品名/规格编造，标待核",
                        severity="warning",
                        sheet=row.sheet,
                        row=row.row,
                        cell=row.cell("elements"),
                        field_name="elements",
                    )
                )
                row.requires_review = True
        if not row.values.get("purchase_contract_no") and not row.values.get("contract_no"):
            row.issues.append(
                _issue(
                    "missing_contract",
                    "采购合同号与合同号都为空；业务身份不足，标待核",
                    severity="error",
                    blocking=True,
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("purchase_contract_no"),
                    field_name="purchase_contract_no",
                )
            )
        elif not row.values.get("purchase_contract_no"):
            row.issues.append(
                _issue(
                    "missing_purchase_contract_no",
                    "采购合同号为空但合同号存在；保留可用身份，标待核",
                    severity="warning",
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("purchase_contract_no"),
                    field_name="purchase_contract_no",
                )
            )
            row.requires_review = True
        if row.row in set(self.layout.hidden_rows):
            row.issues.append(
                _issue(
                    "hidden_row",
                    f"第 {row.row} 行是隐藏行；仍识别为商品行，来源需人工确认",
                    severity="warning",
                    sheet=row.sheet,
                    row=row.row,
                )
            )
            row.requires_review = True
        if not row.values.get("recorded_at"):
            # P15 recorded_at 必填；沿用既有“未提供”占位符，不推断日期。
            pass
        # 放行情况只作原文证据；文件名“历史成功数据”不等于海关放行凭证。
        release = row.values.get("customs_release_status", "")
        if release:
            row.issues.append(
                _issue(
                    "customs_release_status_present",
                    f"放行情况原文为 {release!r}；只作原文记录，不据此认定已放行/已复核",
                    severity="info",
                    sheet=row.sheet,
                    row=row.row,
                    cell=row.cell("customs_release_status"),
                    field_name="customs_release_status",
                )
            )
        row.report_fields = {
            "supplier": row.values.get("supplier", ""),
            "purchase_contract_no": row.values.get("purchase_contract_no", ""),
            "sales_contract_no": row.values.get("contract_no", ""),
            "declaration_no": row.values.get("declaration_no", ""),
            "english_name": row.values.get("english_name", ""),
            "tax_classification_code": row.values.get("tax_classification_code", ""),
            "release_status": release,
            "elements_parse_source": "raw",
        }
        row.source_extra = {
            "source_file_id": self.source_id,
            "source_filename": self.original_filename,
            "source_format": FORMAT_CONSOLIDATED,
            "supplier": row.values.get("supplier", ""),
            "purchase_contract_no": row.values.get("purchase_contract_no", ""),
            "sales_contract_no": row.values.get("contract_no", ""),
            "declaration_no": row.values.get("declaration_no", ""),
            "english_name": row.values.get("english_name", ""),
        }
        row.notes_prefix = (
            "历史汇总表导入；仅作参考线索，不直接作为当前归类结论。"
            "文件名为“历史成功数据”不等于海关放行凭证，未独立核验。"
        )
        row.source_system = (
            "historical-consolidated-import"
            if self.dataset_kind == "real"
            else "synthetic-historical-consolidated"
        )

    # -- 行分类 -----------------------------------------------------------
    def _classify_row(self, row: int, row_obj: ConsolidatedRow) -> str:
        non_empty = [
            item
            for item in row_obj.cells.values()
            if item.status not in {"column_missing", "merged_continuation"} and item.text.strip()
        ]
        has_unresolved_identity = any(
            row_obj.cells[name].status in {"formula_cache_missing", "merged_continuation"}
            for name in REQUIRED_FIELDS
        )
        has_actual_content = any(
            cell.row == row and cell.has_content for cell in self.sheet.cells.values()
        )
        if not non_empty and not has_unresolved_identity and not has_actual_content:
            return "blank_row"
        normalized_headers = {
            _normalize_header(item.text) for item in row_obj.cells.values() if item.text.strip()
        }
        header_matches = sum(
            1
            for name in REQUIRED_FIELDS
            if any(
                _header_matches(header, alias)
                for header in normalized_headers
                for alias in self.aliases.get(name, ())
            )
        )
        if header_matches == len(REQUIRED_FIELDS):
            return "repeated_header"
        first_text = next(
            (item.text.strip() for item in row_obj.cells.values() if item.text.strip()), ""
        )
        if _compact(first_text) in {_compact(marker) for marker in TOTAL_MARKERS}:
            return "total_row"
        if not row_obj.values.get("name") and not row_obj.values.get("hs"):
            if not has_unresolved_identity and re.match(r"^(说明|备注|注[：:]|注意)", first_text):
                return "note_row"
            # 不能确认是说明行的非空行按商品候选保留并拒绝缺必填字段，不吞行。
            row_obj.issues.append(_issue(
                "row_kind_uncertain", "非空行缺少品名和HS，无法可靠认定为说明；保留为商品候选并报告拒绝",
                sheet=row_obj.sheet, row=row, severity="warning",
            ))
            row_obj.requires_review = True
        return "goods_row"

    # -- 身份 -------------------------------------------------------------
    def _identity_payload(self, row_obj: ConsolidatedRow, fields: Sequence[str]) -> str:
        # 不删除词内空格、不 casefold：规格“AB 12”与“AB1 2”、合同的大小写
        # 均可能有真实语义；宁可保留多条，也不通过危险规范化制造去重。
        return _json_key({name: row_obj.values.get(name, "").strip() for name in fields})

    def _assign_identity(self, records: list[ConsolidatedRow]) -> None:
        content_groups: dict[str, list[ConsolidatedRow]] = defaultdict(list)
        business_groups: dict[str, list[ConsolidatedRow]] = defaultdict(list)
        change_groups: dict[str, list[ConsolidatedRow]] = defaultdict(list)
        for record in records:
            content_hash = _sha256_text(self._identity_payload(record, IDENTITY_FIELDS))
            record.content_hash = content_hash
            content_groups[content_hash].append(record)
            business_groups[self._identity_payload(record, CONFLICT_FIELDS)].append(record)
            change_groups[self._identity_payload(record, CHANGE_FIELDS)].append(record)

        original_ids = [record.values.get("original_record_id", "") for record in records]
        use_original = bool(records) and all(original_ids) and len(set(original_ids)) == len(original_ids)
        if any(original_ids) and not use_original:
            duplicated = [
                item for item, count in Counter(original_ids).items() if item and count > 1
            ]
            self.task_issues.append(
                _issue(
                    "original_record_id_not_unique",
                    "原始记录ID列存在空值或重复值，不能作为唯一主键；退回内容哈希身份",
                    severity="warning",
                    sheet=self.layout.sheet,
                    field_name="original_record_id",
                    details={"duplicates": duplicated[:20]},
                )
            )

        business_occurrence: dict[str, int] = {}
        change_occurrence: dict[str, int] = {}
        # 先按内容哈希排序，再分配“同业务内容/同变更锚点”的序号；排序不改变身份。
        for key, group in business_groups.items():
            for index, record in enumerate(sorted(group, key=lambda item: item.content_hash)):
                record.business_occurrence = index
                business_occurrence[str(id(record))] = index
        for key, group in change_groups.items():
            for index, record in enumerate(sorted(group, key=lambda item: item.content_hash)):
                record.change_occurrence = index
                change_occurrence[str(id(record))] = index

        for content_hash, group in content_groups.items():
            for index, record in enumerate(group):
                record.content_occurrence = index
                record.identity_basis = "original_record_id" if use_original else "content"
                if use_original:
                    basis = f"{self.source_id}|{record.values['original_record_id']}"
                    record.record_id = RECORD_ID_PREFIX + _sha256_text(basis)[:20].upper()
                else:
                    basis = f"{self.source_id}|{content_hash}|{index}"
                    record.record_id = RECORD_ID_PREFIX + _sha256_text(basis)[:20].upper()
                record.source_key = (
                    f"consolidated|{self.source_id}|{self.layout.sheet}|{content_hash}|{index}"
                )
                record.change_key = _sha256_text(
                    f"{self.source_id}|{self._identity_payload(record, CHANGE_FIELDS)}"
                    f"|{record.change_occurrence}"
                )
                record.business_key = _sha256_text(
                    f"{self._identity_payload(record, CONFLICT_FIELDS)}"
                    f"|{record.business_occurrence}"
                )
                record.extra_attributes = {
                    "source_format": FORMAT_CONSOLIDATED,
                    "identity_version": CONSOLIDATED_IDENTITY_VERSION,
                    "identity_basis": record.identity_basis,
                    "content_hash": content_hash,
                    "content_occurrence": str(index),
                    "business_occurrence": str(record.business_occurrence),
                    "change_key": record.change_key,
                    "change_anchor": _sha256_text(
                        f"{self.source_id}|{self._identity_payload(record, CHANGE_FIELDS)}"
                    ),
                    "business_key": record.business_key,
                    "row_kind": "goods_row",
                }
                locator_parts = [
                    f"{self.layout.sheet}!第{record.row}行",
                    f"采购合同 {record.values.get('purchase_contract_no') or '未提供'}",
                    f"合同号 {record.values.get('contract_no') or '未提供'}",
                    f"报关单号 {record.values.get('declaration_no') or '未提供'}",
                    f"单元格 {record.source_cells}",
                ]
                if record.values.get("original_source_file") or record.values.get("original_source_row"):
                    locator_parts.append(
                        "原始来源 "
                        f"{record.values.get('original_source_file') or '未提供'}"
                        f"!{record.values.get('original_source_row') or '未提供'}"
                    )
                merged = sorted(
                    {
                        item.merge_ref
                        for item in record.cells.values()
                        if item.merge_ref and item.status == "merged_continuation"
                    }
                )
                if merged:
                    locator_parts.append("合并单元格 " + "、".join(merged))
                record.locator = "；".join(locator_parts)
                record.extra_attributes.update(
                    {
                        "supplier": record.values.get("supplier", ""),
                        "purchase_contract_no": record.values.get("purchase_contract_no", ""),
                        "sales_contract_no": record.values.get("contract_no", ""),
                        "declaration_no": record.values.get("declaration_no", ""),
                        "english_name": record.values.get("english_name", ""),
                        "english_name_raw": record.raw_values.get("english_name", ""),
                        "name_raw": record.raw_values.get("name", ""),
                        "hs_raw": record.raw_values.get("hs", ""),
                        "spec_raw": record.raw_values.get("spec", ""),
                        "material_raw": record.raw_values.get("material", ""),
                        "recorded_at_raw": record.raw_values.get("recorded_at", ""),
                        "purchase_contract_no_raw": record.raw_values.get("purchase_contract_no", ""),
                        "sales_contract_no_raw": record.raw_values.get("contract_no", ""),
                        "item_no_raw": record.raw_values.get("item_no", ""),
                        "tax_classification_code": record.values.get(
                            "tax_classification_code", ""
                        ),
                        "tax_classification_code_raw": record.raw_values.get(
                            "tax_classification_code", ""
                        ),
                        "input_tax_rate": record.values.get("input_tax_rate", ""),
                        "shipment_status": record.values.get("shipment_status", ""),
                        "container_no": record.values.get("container_no", ""),
                        "container_raw": record.raw_values.get("container_no", ""),
                        "bill_of_lading_no": record.values.get("bill_of_lading_no", ""),
                        "remark": record.values.get("remark", ""),
                        "customs_release_status": record.values.get(
                            "customs_release_status", ""
                        ),
                        "original_source_file": record.values.get(
                            "original_source_file", ""
                        ),
                        "original_source_row": record.values.get("original_source_row", ""),
                        "original_record_id": record.values.get("original_record_id", ""),
                    }
                )
                merged_ranges = sorted(
                    {
                        item.merge_ref
                        for item in record.cells.values()
                        if item.merge_ref
                    }
                )
                if merged_ranges:
                    record.extra_attributes["merged_ranges"] = ",".join(merged_ranges)
                if any(item.status == "merged_continuation" for item in record.cells.values()):
                    record.extra_attributes["merged_continuation_cells"] = ",".join(
                        sorted(
                            item.ref
                            for item in record.cells.values()
                            if item.status == "merged_continuation"
                        )
                    )
                if any(item.formula for item in record.cells.values()):
                    record.extra_attributes["formula_cells"] = ",".join(
                        sorted(item.ref for item in record.cells.values() if item.formula)
                    )

    # -- 图片 -------------------------------------------------------------
    def _extract_images(self, records: list[ConsolidatedRow]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        match = self.layout.columns.get("image")
        rows = [record.row for record in records]
        if match is None:
            ambiguous = next(
                (
                    item
                    for item in self.layout.duplicate_columns
                    if item.get("field") == "image"
                ),
                None,
            )
            if ambiguous is not None:
                return (
                    {
                        "column": "",
                        "column_status": IMAGE_COLUMN_AMBIGUOUS,
                        "counts": {IMAGE_COLUMN_AMBIGUOUS: len(rows)},
                        "reason": f"图片列存在多个候选：{ambiguous.get('columns')}；未做逐行归属",
                        "candidates": ambiguous.get("columns", []),
                    },
                    [
                        _issue(
                            "image_column_ambiguous",
                            "图片列存在多个候选，未擅自选择；图片状态记为 column_ambiguous",
                            severity="warning",
                            sheet=self.layout.sheet,
                            field_name="image",
                            details=ambiguous,
                        )
                    ],
                )
            return (
                {
                    "column": "",
                    "column_status": IMAGE_COLUMN_MISSING,
                    "counts": {IMAGE_COLUMN_MISSING: len(rows)},
                    "reason": "汇总表没有可识别的图片列；不能当作所有商品都无图",
                },
                [
                    _issue(
                        "image_column_missing",
                        "汇总表没有可识别的图片列；图片状态记为 column_missing，不假装从旧模板恢复",
                        severity="warning",
                        sheet=self.layout.sheet,
                        field_name="image",
                    )
                ],
            )
        try:
            extraction = extract_column_images(
                self.workbook,
                sheet_name=self.layout.sheet,
                column=match.letter,
                rows=rows,
                header_row=self.layout.header_row,
            )
        except Exception as exc:  # 图片元数据损坏也要报告，不把整份文件当无图
            return (
                {
                    "column": match.letter,
                    "column_status": IMAGE_COLUMN_AMBIGUOUS,
                    "counts": {IMAGE_COLUMN_AMBIGUOUS: len(rows)},
                    "reason": f"{type(exc).__name__}: {exc}",
                },
                [
                    _issue(
                        "image_extraction_failed",
                        f"图片列提取失败：{type(exc).__name__}: {exc}",
                        severity="warning",
                        sheet=self.layout.sheet,
                        field_name="image",
                    )
                ],
            )
        if extraction.column_status not in {"found", "provided"}:
            counts = {IMAGE_COLUMN_MISSING: len(rows)}
            return (
                {
                    "column": match.letter,
                    "column_status": extraction.column_status,
                    "counts": counts,
                    "reason": extraction.column_reason,
                },
                [
                    _issue(
                        f"image_column_{extraction.column_status}",
                        extraction.column_reason or "图片列不可用；不能当作无图",
                        severity="warning",
                        sheet=self.layout.sheet,
                        field_name="image",
                    )
                ],
            )
        by_row = {image.row: image for image in extraction.rows}
        counts: dict[str, int] = {
            STATUS_AVAILABLE: 0,
            STATUS_ABSENT: 0,
            STATUS_READ_FAILED: 0,
        }
        issues: list[dict[str, Any]] = []
        for record in records:
            image_cell = record.cells.get("image")
            if image_cell is not None and image_cell.status == "merged_continuation":
                record.row_image = None
                record.image_status_override = STATUS_READ_FAILED
                counts[STATUS_READ_FAILED] += 1
                record.issues.append(_issue(
                    "image_merged_continuation", "图片列位于合并续行，无法独立确定归属；拒绝跨行借图",
                    sheet=record.sheet, row=record.row, cell=image_cell.ref, field_name="image",
                ))
                continue
            image = by_row.get(record.row)
            if image is None:
                counts[IMAGE_ROW_MISSING] = counts.get(IMAGE_ROW_MISSING, 0) + 1
                record.row_image = None
                record.image_status_override = IMAGE_ROW_MISSING
                continue
            cell = record.cells.get("image")
            text = cell.text.strip() if cell is not None else ""
            if text.lower().startswith(("http://", "https://")):
                counts[IMAGE_EXTERNAL_URL_NOT_FETCHED] = (
                    counts.get(IMAGE_EXTERNAL_URL_NOT_FETCHED, 0) + 1
                )
                record.row_image = None
                record.image_status_override = IMAGE_EXTERNAL_URL_NOT_FETCHED
                record.issues.append(
                    _issue(
                        "image_external_url_not_fetched",
                        "图片列是外部 URL；默认不联网，未读取",
                        severity="warning",
                        sheet=record.sheet,
                        row=record.row,
                        cell=cell.ref if cell else "",
                        field_name="image",
                    )
                )
                continue
            if text and not image.available:
                looks_like_path = bool(
                    re.search(r"[\\/]|[A-Za-z]:|\.(?:png|jpe?g|gif|bmp|webp)$", text, re.I)
                )
                if not looks_like_path:
                    if text.strip() in {"无图", "无", "没有", "无图片", "N/A", "NA", "-"}:
                        counts[STATUS_ABSENT] += 1
                        record.row_image = RowImage(
                            sheet=record.sheet, row=record.row, cell=cell.ref if cell else "",
                            status=STATUS_ABSENT, code="text_says_absent", message="图片列文字明确表示无图",
                        )
                        record.issues.append(_issue(
                            "image_text_says_absent", f"图片列文字 {text!r} 表示无图；按 absent 记录",
                            severity="info", sheet=record.sheet, row=record.row,
                            cell=cell.ref if cell else "", field_name="image",
                        ))
                    else:
                        counts[IMAGE_TEXT_UNRECOGNIZED] = counts.get(IMAGE_TEXT_UNRECOGNIZED, 0) + 1
                        record.row_image = None
                        record.image_status_override = IMAGE_TEXT_UNRECOGNIZED
                        record.issues.append(_issue(
                            "image_text_unrecognized", f"图片列文字 {text!r} 不是可识别路径/图片线索；不读取也不当作无图",
                            severity="warning", sheet=record.sheet, row=record.row,
                            cell=cell.ref if cell else "", field_name="image",
                        ))
                    continue
                # 看起来是本地路径而不是 DISPIMG：只有显式给图片根目录才读取。
                if self.image_root is None:
                    counts[IMAGE_EXTERNAL_PATH_NOT_ENABLED] = (
                        counts.get(IMAGE_EXTERNAL_PATH_NOT_ENABLED, 0) + 1
                    )
                    record.row_image = None
                    record.image_status_override = IMAGE_EXTERNAL_PATH_NOT_ENABLED
                    record.issues.append(
                        _issue(
                            "image_external_path_not_enabled",
                            "图片列看起来是本地路径；未提供 --image-root，默认不读取",
                            severity="warning",
                            sheet=record.sheet,
                            row=record.row,
                            cell=cell.ref if cell else "",
                            field_name="image",
                        )
                    )
                    continue
                external = self._read_external_image(record, text)
                if external is not None:
                    counts[external.status] = counts.get(external.status, 0) + 1
                    record.issues.extend(external.issues)
                    record.row_image = external.image
                    record.image_status_override = "" if external.image is not None else external.status
                    continue
            counts[image.status] = counts.get(image.status, 0) + 1
            record.row_image = image
            record.issues.append(
                _issue(
                    f"image_{image.status}",
                    f"图片状态 {image.status}（{image.cell}）",
                    severity="warning" if image.status == STATUS_READ_FAILED else "info",
                    sheet=record.sheet,
                    row=record.row,
                    cell=image.cell,
                    field_name="image",
                )
            )
        return (
            {
                "column": match.letter,
                "column_status": "found",
                "counts": counts,
                "reason": "",
            },
            issues,
        )

    def _read_external_image(self, record: ConsolidatedRow, text: str):
        # 延迟导入，避免把图片依赖放到模块顶层。
        from adapters.files.dispimg import RowImage

        cell = record.cells.get("image")
        ref = cell.ref if cell is not None else ""
        raw = text.strip().replace("\\", "/")
        if raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:/", raw):
            return _ExternalImageResult(
                status=IMAGE_EXTERNAL_PATH_UNSAFE,
                issues=[
                    _issue(
                        "image_external_path_unsafe",
                        "图片路径是绝对路径；只允许 --image-root 内的相对路径",
                        severity="warning",
                        sheet=record.sheet,
                        row=record.row,
                        cell=ref,
                        field_name="image",
                    )
                ],
            )
        root = self.image_root
        assert root is not None
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return _ExternalImageResult(
                status=IMAGE_EXTERNAL_PATH_UNSAFE,
                issues=[
                    _issue(
                        "image_external_path_unsafe",
                        "图片路径越出 --image-root（含 .. 或符号链接越界）",
                        severity="warning",
                        sheet=record.sheet,
                        row=record.row,
                        cell=ref,
                        field_name="image",
                    )
                ],
            )
        if not candidate.is_file():
            return _ExternalImageResult(
                status=IMAGE_EXTERNAL_PATH_NOT_FOUND,
                issues=[
                    _issue(
                        "image_external_path_not_found",
                        f"图片根目录内找不到文件：{raw}",
                        severity="warning",
                        sheet=record.sheet,
                        row=record.row,
                        cell=ref,
                        field_name="image",
                    )
                ],
            )
        try:
            if candidate.stat().st_size > DEFAULT_MAX_IMAGE_BYTES:
                raise ValueError(f"外部图片超过大小上限 {DEFAULT_MAX_IMAGE_BYTES} bytes")
            data = candidate.read_bytes()
            if len(data) > DEFAULT_MAX_IMAGE_BYTES:
                raise ValueError("读取期间外部图片大小超过上限")
            media = _decode_media(
                MediaInfo(part=raw, status="resource_found", sha256=hashlib.sha256(data).hexdigest(), size=len(data)),
                data,
            )
            if not media.usable:
                raise ValueError(media.error or media.decode_status)
        except (OSError, ValueError) as exc:
            return _ExternalImageResult(
                status=STATUS_READ_FAILED,
                issues=[
                    _issue(
                        "image_read_failed",
                        f"图片文件读取失败：{type(exc).__name__}: {exc}",
                        severity="warning",
                        sheet=record.sheet,
                        row=record.row,
                        cell=ref,
                        field_name="image",
                    )
                ],
            )
        suffix = (media.format or "bin").lower()
        return _ExternalImageResult(
            status=STATUS_AVAILABLE,
            image=RowImage(
                sheet=record.sheet,
                row=record.row,
                cell=ref,
                status=STATUS_AVAILABLE,
                code="external_local",
                message="从显式图片根目录读取",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                image_format=suffix,
                data=data,
            ),
            issues=[],
        )

    # -- 数据质量统计 -----------------------------------------------------
    def _collect_data_quality(
        self,
        *,
        records: list[ConsolidatedRow],
        skipped_rows: list[dict[str, Any]],
        image_info: Mapping[str, Any],
    ) -> dict[str, Any]:
        hs_values = [record.values.get("hs", "") for record in records if record.values.get("hs")]
        tax_values = [
            record.raw_values.get("tax_classification_code", "")
            for record in records
            if record.raw_values.get("tax_classification_code")
        ]
        tax_normalized = [
            re.sub(r"[\s']", "", str(value)) for value in tax_values
        ]
        tax_lengths = Counter(len(value) for value in tax_normalized)
        duplicate_contents: dict[str, list[int]] = defaultdict(list)
        for record in records:
            duplicate_contents[record.content_hash].append(record.row)
        duplicate_groups = {
            key: rows for key, rows in duplicate_contents.items() if len(rows) > 1
        }
        formula_fields = Counter()
        formula_cache_missing = Counter()
        merged_continuations = Counter()
        for record in records:
            for name, cell in record.cells.items():
                if cell.formula:
                    formula_fields[name] += 1
                    if not cell.formula_has_cache:
                        formula_cache_missing[name] += 1
                if cell.status == "merged_continuation":
                    merged_continuations[f"{cell.field}@{cell.merge_ref}"] += 1
        release_values = Counter(
            record.values.get("customs_release_status", "") for record in records
        )
        release_values.pop("", None)
        return {
            "sheet": self.layout.sheet,
            "header_row": self.layout.header_row,
            "columns": {
                name: {
                    "header": match.header,
                    "column": match.letter,
                    "hidden": match.hidden,
                }
                for name, match in self.layout.columns.items()
            },
            "unmatched_headers": self.layout.unmatched_headers,
            "duplicate_columns": self.layout.duplicate_columns,
            "hidden_columns": self.layout.hidden_columns,
            "hidden_rows": self.layout.hidden_rows,
            "merged_ranges": self.layout.merges,
            "merged_range_count": len(self.layout.merges),
            "merged_cell_count": sum(
                (item["bottom"] - item["top"] + 1) * (item["right"] - item["left"] + 1)
                for item in self.layout.merges
            ),
            "merged_continuation_counts": dict(merged_continuations),
            "formula_cells": dict(formula_fields),
            "formula_cache_missing": dict(formula_cache_missing),
            "external_link_parts": sorted(
                name for name in self.workbook.part_names if "externalLink" in name
            ),
            "image_column_status": image_info.get("column_status", ""),
            "row_classification": dict(self.row_classification),
            "skipped_row_count": len(skipped_rows),
            "hs": {
                "non_empty": len(hs_values),
                "lengths": {str(key): value for key, value in Counter(len(value) for value in hs_values).items()},
                "non_10_digit_rows": [
                    {"row": record.row, "hs": record.values.get("hs", "")}
                    for record in records
                    if record.values.get("hs") and not re.fullmatch(r"[0-9]{10}", record.values["hs"])
                ],
                "text_cells": sum(
                    1
                    for record in records
                    if record.cells.get("hs") is not None
                    and record.cells["hs"].cell_kind == "s"
                ),
                "numeric_cells": sum(
                    1
                    for record in records
                    if record.cells.get("hs") is not None
                    and record.cells["hs"].cell_kind == "n"
                ),
            },
            "tax_classification_code": {
                "non_empty": len(tax_values),
                "normalized_lengths": {str(key): value for key, value in tax_lengths.items()},
                "non_19_digit_rows": [
                    {
                        "row": record.row,
                        "raw": record.raw_values.get("tax_classification_code", ""),
                    }
                    for record in records
                    if record.raw_values.get("tax_classification_code")
                    and len(
                        re.sub(
                            r"[\s']",
                            "",
                            str(record.raw_values.get("tax_classification_code", "")),
                        )
                    )
                    != 19
                ],
            },
            "duplicate_content_groups": len(duplicate_groups),
            "duplicate_content_extra_rows": sum(len(rows) - 1 for rows in duplicate_groups.values()),
            "duplicate_content_samples": [
                {"rows": rows} for rows in list(duplicate_groups.values())[:20]
            ],
            "empty_required": {
                "name": sum(1 for record in records if not record.values.get("name")),
                "hs": sum(1 for record in records if not record.values.get("hs")),
            },
            "empty_optional": {
                name: sum(1 for record in records if not record.values.get(name))
                for name in (
                    "elements",
                    "purchase_contract_no",
                    "contract_no",
                    "declaration_no",
                    "english_name",
                    "spec",
                    "material",
                    "recorded_at",
                    "image",
                )
                if name in self.layout.columns
            },
            "column_missing": {
                name: sum(
                    1
                    for record in records
                    if record.cells.get(name) is not None
                    and record.cells[name].status == "column_missing"
                )
                for name in CONSOLIDATED_FIELD_ALIASES
                if name not in self.layout.columns
            },
            "customs_release_status_values": dict(release_values),
        }

    # -- 入口 -------------------------------------------------------------
    def parse(self) -> ConsolidatedParseResult:
        max_row = _sheet_max_row(self.sheet)
        records: list[ConsolidatedRow] = []
        skipped_rows: list[dict[str, Any]] = []
        candidate_rows = 0
        for row in range(self.layout.header_row + 1, max_row + 1):
            row_obj = self._read_row(row)
            kind = self._classify_row(row, row_obj)
            self.row_classification[kind] += 1
            if kind == "blank_row":
                skipped_rows.append(row_obj.as_skipped_report(kind=kind, reason="空行"))
                continue
            if kind == "repeated_header":
                skipped_rows.append(
                    row_obj.as_skipped_report(kind=kind, reason="重复表头行，已跳过")
                )
                continue
            if kind == "total_row":
                skipped_rows.append(
                    row_obj.as_skipped_report(kind=kind, reason="合计/小计行，未当作商品")
                )
                continue
            if kind == "note_row":
                skipped_rows.append(
                    row_obj.as_skipped_report(kind=kind, reason="无品名/HS 的说明行，保留诊断")
                )
                continue
            candidate_rows += 1
            records.append(row_obj)

        self._assign_identity(records)
        image_info, image_issues = self._extract_images(records)
        self.data_quality = self._collect_data_quality(
            records=records, skipped_rows=skipped_rows, image_info=image_info
        )
        for warning in self.layout.warnings:
            self.task_issues.append(warning)
        if image_issues:
            self.task_issues.extend(image_issues)
        for record in records:
            self._apply_quality_review(record)
        return ConsolidatedParseResult(
            layout=self.layout,
            records=records,
            skipped_rows=skipped_rows,
            row_classification=dict(self.row_classification),
            data_quality=self.data_quality,
            image_info=image_info,
            image_issues=image_issues,
            task_issues=self.task_issues,
            warnings=list(self.warnings),
            source_file_sha256=self.workbook.source_sha256,
        )

    def _apply_quality_review(self, record: ConsolidatedRow) -> None:
        issue_codes = {issue.get("code", "") for issue in record.issues}
        review_codes = {
            "hs_length_not_10",
            "missing_elements",
            "missing_purchase_contract_no",
            "hidden_row",
        }
        if issue_codes & review_codes:
            record.requires_review = True
        if any(issue.get("blocking") for issue in record.issues):
            record.requires_review = True
        if any(
            str(issue.get("code", "")).startswith(("numeric_precision_risk_", "formula_cache_missing_"))
            for issue in record.issues
        ):
            record.requires_review = True


@dataclass
class _ExternalImageResult:
    status: str
    image: Any | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- 值映射
def _value_for_p15(field_name: str, cell: CellRead) -> str:
    if cell.status == "column_missing":
        return ""
    if cell.status == "merged_continuation":
        return ""
    if cell.status == "formula_cache_missing":
        return ""
    if field_name in REQUIRED_FIELDS and (
        cell.cell_kind == "e" or cell.text.strip() in FORMULA_ERROR_MARKERS
    ):
        return ""
    if field_name == "hs":
        return _normalize_hs(cell.text)
    if field_name in {
        "name",
        "english_name",
        "supplier",
        "purchase_contract_no",
        "contract_no",
        "declaration_no",
        "bill_of_lading_no",
        "item_no",
        "quantity",
        "unit",
        "refund_rate",
        "input_tax_rate",
        "spec",
        "material",
        "recorded_at",
        "original_source_file",
        "original_source_row",
        "original_record_id",
        "shipment_status",
        "remark",
        "customs_release_status",
    }:
        return normalized_value(cell.text)
    if field_name == "elements":
        return cell.raw.strip()
    if field_name == "container_no":
        return normalized_value(cell.text)
    if field_name == "tax_classification_code":
        return re.sub(r"[\s']", "", str(cell.text or ""))
    if field_name == "image":
        return cell.text.strip()
    return normalized_value(cell.text)


def _normalize_hs(text: str) -> str:
    value = normalized_value(text)
    if DECIMAL_TEXT.match(value):
        value = value.split(".", 1)[0]
    return value


def parse_consolidated_workbook(
    workbook: Workbook,
    *,
    path: Path,
    source_id: str,
    original_filename: str,
    dataset_kind: str = "real",
    mapping: Mapping[str, Any] | None = None,
    image_root: Path | None = None,
) -> ConsolidatedParseResult:
    return ConsolidatedParser(
        workbook,
        path=path,
        source_id=source_id,
        original_filename=original_filename,
        dataset_kind=dataset_kind,
        mapping=mapping,
        image_root=image_root,
    ).parse()


# --------------------------------------------------------------------------- 元素原文的补充解析
def parse_labeled_elements(raw: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """补充识别“1. 标签 [?] 值”这种真实汇总表写法。

    只识别有明确标签的行；位置式 ``0|1|值|值`` 不猜测标签，留给调用方报
    ``element_unstructured``。本函数不补值、不改写原文。
    """

    text = (raw or "").strip()
    if not text:
        return {}, []
    matches = DOT_LABEL_VALUE.findall(text)
    if len(matches) < 2:
        return {}, []
    elements: dict[str, str] = {}
    issues: list[dict[str, str]] = []
    for _, label, value in matches:
        label = _normalize(label)
        value = _normalize(value)
        if not label or not value:
            continue
        if label in elements:
            issues.append(
                {
                    "code": "element_duplicate_label",
                    "message": f"要素标签 {label} 重复；保留第一次的值，原文完整保留",
                }
            )
            continue
        elements[label] = value
    if not elements:
        return {}, []
    return elements, issues
