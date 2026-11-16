"""历史模板导入 P15（最小可重复流水线）。

定位
----
把已经确认的历史业务 Excel 模板转换为 P15 能直接读取的
``p15.history.1`` JSON。解析复用 P04 的申报/装箱提取与 P05 的 DISPIMG
图片判定；本模块只做“历史模板 -> 历史记录”的装配、稳定标识、重复导入
处理和导入报告，不写数据库、不启服务、不调用模型，也不改动 P06 的上传
服务、业务 API、页面或公共模型。

诚实边界
--------
- 历史模板的“成功”来自 Jay 对第一批试点模板的人工声明，导入批次会原样
  记录这个声明；不伪造海关放行单据、报关日期、税则版本或模板中不存在的
  字段。
- 历史记录只是参考线索，不是当前归类结论；``HistoryRecord`` 本身已由 P15
  的结构约束（``historical_hs``/``reference_only``）防止被当成建议。
- 原模板没有申报日期时，不在文件名、合同号或归档日期里推断日期；P15 的
  ``recorded_at`` 必填，因此使用固定的“未提供”占位符并在报告与 notes 中
  明确标注缺失。字段本身应保持为空的（``hs_version``/``applicable_scope``/
  ``valid_until``）保持 ``None``。
- 图片严格按“页签 + 行号 + 图片列”关联；列缺失、无图、读取失败分别记录，
  不把读失败当成无图，也不跨行借用图片。

重复导入
--------
``record_id`` 由「来源文件标识 + 申报页签 + 原行号 + 合同号 + 项号」稳定
生成；``source_key`` 是同一组要素的可读键。重复运行默认读取已有输出，
同 ``record_id`` 同内容记为 ``duplicate`` 并跳过，同 ``source_key`` 但内容
变化记为 ``changed``（默认保留旧记录，``replace_changed=True`` 时替换）。
重复/变化都写入报告，不静默产生重复案例。

本模块不新增依赖；CLI 入口见 ``scripts/import_history_templates.py``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING

from adapters.files.declarations import SemanticLayoutError, extract_declarations
from adapters.files.dispimg import (
    STATUS_ABSENT,
    STATUS_AVAILABLE,
    STATUS_READ_FAILED,
    extract_column_images,
)
from adapters.files.ooxml import OoxmlError, Workbook
from adapters.files.packing_links import map_workbook

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查；运行时延迟到 Django 配置完成后导入
    from adapters.sources.history_search import HistoryRecord

SCHEMA_VERSION = "p15.history.1"
RECORD_ID_PREFIX = "HIST-"
MISSING_DATE = "未提供"
IMAGE_COLUMN_MISSING = "column_missing"
IMAGE_COLUMN_AMBIGUOUS = "column_ambiguous"
IMAGE_ROW_MISSING = "row_missing"

SUCCESS_OUTCOME = "approved"
SUCCESS_BASIS_REAL = (
    "Jay 人工声明（导入批次）：第一批试点历史模板可暂视为已通过、"
    "准确的历史案例；未独立核验海关放行凭证，仅作参考。"
)
SUCCESS_BASIS_SYNTHETIC = "合成样本：仅用于验证导入与检索链路，不是真实历史库。"

DEFAULT_DECLARATION_NOTE = (
    "Jay 确认第一批试点历史模板可暂视为已通过、准确的历史案例。"
    "本批次只记录该人工声明，不伪造海关放行单据、报关日期、税则版本或其他模板中不存在的信息。"
)

DATE_HEADER_ALIASES = ("申报日期", "报关日期", "业务日期", "出口日期", "申报时间", "报关时间")
MATERIAL_KEYS = ("材质", "材料")
SPEC_KEYS = ("型号", "规格型号", "规格", "型号规格")
ELEMENT_SPLIT = re.compile(r"[|｜]")
ELEMENT_COLON = re.compile(r"[:：]")
INDEXED_LABEL = re.compile(r"^(\d+)\s*[:：]\s*(.*)$")
VALUE_AND_NEXT_LABEL = re.compile(r"^(.*?)\s+(\d+)\s*[:：]\s*(.*)$")
PURE_NUMBER = re.compile(r"^\d+$")
SPACES = re.compile(r"\s+")


# --------------------------------------------------------------------------- 基础工具
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: Any) -> str:
    return SPACES.sub(" ", "" if text is None else str(text)).strip()


def _compact(text: Any) -> str:
    return SPACES.sub("", _normalize(text)).casefold()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def configure_django() -> None:
    """P15 DTO 通过 Django Ninja 定义；CLI 在导入前显式配置 settings。"""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    try:
        django.setup()
    except RuntimeError:
        # 已在测试/宿主进程中配置过 apps，不重复 setup。
        pass


def _history_dto():
    """延迟导入 P15 DTO，避免 CLI 在 Django settings 就绪前触发 LazySettings。"""

    from adapters.sources.history_search import HistoryRecord, HistorySourceRef

    return HistoryRecord, HistorySourceRef


# --------------------------------------------------------------------------- 申报要素
def parse_element_pairs(raw: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """把申报要素原文拆成 ``键 -> 值``。

    支持两类真实模板写法，且只做可核对的拆分，不补要素、不改写值：

    1. ``0:品牌类型|境内自主品牌 1:出口享惠情况|不确定 ...``
    2. ``0｜1｜用途：用于轮胎硫化机｜材质：丁腈橡胶 ...``

    拆不动的片段不丢：原文由调用方保存在 ``elements_raw``，本函数只返回空
    dict 和 ``element_unstructured`` 问题。
    """

    text = (raw or "").strip()
    if not text:
        return {}, []
    segments = [segment.strip() for segment in ELEMENT_SPLIT.split(text) if segment.strip()]
    if not segments:
        return {}, [{"code": "element_unstructured", "message": "申报要素为空"}]

    pairs: list[tuple[str, str]] = []
    issues: list[dict[str, str]] = []

    # 形态 1：编号 + 标签 + 值，值后面可能紧跟下一个编号。
    indexed = bool(INDEXED_LABEL.match(segments[0]))
    if indexed:
        current_label: str | None = None
        current_value: str | None = None

        def flush() -> None:
            nonlocal current_label, current_value
            if current_label is not None:
                pairs.append((_normalize(current_label), _normalize(current_value or "")))
            current_label, current_value = None, None

        for segment in segments:
            value_and_next = VALUE_AND_NEXT_LABEL.match(segment)
            label_only = INDEXED_LABEL.match(segment)
            if value_and_next:
                current_value = value_and_next.group(1)
                flush()
                current_label = value_and_next.group(3)
            elif label_only:
                flush()
                current_label = label_only.group(2)
            elif current_label is not None:
                current_value = segment
            else:
                issues.append(
                    {
                        "code": "element_unstructured",
                        "message": f"无法定位要素标签：{segment[:40]}",
                    }
                )
        flush()
    else:
        # 形态 2：标签：值；纯编号（0/1）是要素序号，跳过但不报错。
        no_separator = all(ELEMENT_COLON.search(segment) is None for segment in segments)
        for segment in segments:
            if PURE_NUMBER.match(segment):
                continue
            parts = ELEMENT_COLON.split(segment, maxsplit=1)
            if len(parts) == 2:
                label, value = parts[0], parts[1]
            else:
                label, value = segment, ""
            label = _normalize(label)
            value = _normalize(value or "")
            if label and value:
                pairs.append((label, value))
            elif label and not no_separator:
                issues.append(
                    {
                        "code": "element_value_missing",
                        "message": f"要素 {label} 没有值，原文保留但不进入 elements",
                    }
                )
        if no_separator:
            issues.append(
                {
                    "code": "element_unstructured",
                    "message": "申报要素没有可识别的“标签：值”分隔符；原文完整保留，elements 留空",
                }
            )

    elements: dict[str, str] = {}
    duplicates: dict[str, int] = {}
    for label, value in pairs:
        if not label or not value:
            continue
        if label in elements:
            duplicates[label] = duplicates.get(label, 1) + 1
            continue
        elements[label] = value
    for label, count in duplicates.items():
        issues.append(
            {
                "code": "element_duplicate_label",
                "message": f"要素标签 {label} 出现 {count} 次；保留第一次的值，原文仍完整保留",
            }
        )
    if not elements and not issues:
        issues.append(
            {
                "code": "element_unstructured",
                "message": "申报要素无法拆成键值；原文完整保留，elements 留空",
            }
        )
    return elements, issues


def derive_material(elements: Mapping[str, str]) -> str:
    for key in MATERIAL_KEYS:
        if key in elements and elements[key]:
            return elements[key]
    for key, value in elements.items():
        if any(alias in key for alias in MATERIAL_KEYS) and value:
            return value
    return ""


def derive_spec(elements: Mapping[str, str]) -> str:
    for key in SPEC_KEYS:
        if key in elements and elements[key]:
            return elements[key]
    for key, value in elements.items():
        if any(alias in key for alias in SPEC_KEYS) and value:
            return value
    return ""


def explicit_recorded_at(record: Any) -> tuple[str, str]:
    """只从模板里明确存在的日期列取值；找不到就返回空，不推断。"""

    for item in getattr(record, "unmapped", ()) or ():
        header = _normalize(item.get("header", ""))
        value = _normalize(item.get("value", ""))
        if not value:
            continue
        if header in DATE_HEADER_ALIASES or header == "日期":
            return value, _normalize(item.get("cell", ""))
    return "", ""


# --------------------------------------------------------------------------- 来源与批次
@dataclass(frozen=True)
class SourceMeta:
    source_id: str
    path: Path
    original_filename: str
    sha256: str
    bytes: int
    declaration_sheet: str | None = None
    declaration_header_row: int | None = None
    declaration_rows: tuple[int, ...] = ()
    manifest_entry: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HumanDeclaration:
    declared_by: str
    declared_at: str
    statement: str
    scope: tuple[str, ...]
    independently_verified: bool = False

    def as_dict(self, batch_id: str) -> dict[str, Any]:
        return {
            "declaration_id": "decl-" + _sha256_text(
                f"{batch_id}|{self.declared_by}|{self.statement}"
            )[:16],
            "declared_by": self.declared_by,
            "declared_at": self.declared_at,
            "statement": self.statement,
            "scope_source_ids": list(self.scope),
            "independently_verified": self.independently_verified,
            "not_fabricated": [
                "customs_release_document",
                "declaration_date",
                "tariff_version",
                "any_field_not_present_in_source",
            ],
            "reference_only": True,
        }


def load_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("manifest 顶层必须是 JSON 对象")
    return payload


def _manifest_root(manifest_path: Path) -> Path:
    """repository_path 是仓库相对路径；向上找到 .git 所在目录作为根。"""

    current = manifest_path.resolve().parent
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current


def _manifest_entry_for(manifest: Mapping[str, Any], path: Path) -> Mapping[str, Any] | None:
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        return None
    resolved = path.resolve()
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        repo_path = str(entry.get("repository_path", "")).strip()
        if not repo_path:
            continue
        candidate = (Path(manifest.get("_root", ".")) / repo_path).resolve()
        if candidate == resolved:
            return entry
    # 退一步按原文件名匹配；重名时仍由 SHA256 校验兜底。
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        names = {
            str(entry.get("original_filename", "")).strip(),
            Path(str(entry.get("repository_path", ""))).name,
        }
        if path.name in names:
            return entry
    return None


def build_source_meta(path: Path, entry: Mapping[str, Any] | None, *, verify_hash: bool = True) -> SourceMeta:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到历史模板：{path}")
    actual_sha = _file_sha256(path)
    actual_bytes = path.stat().st_size
    entry = entry or {}
    declared_sha = _normalize(entry.get("sha256", ""))
    if verify_hash and declared_sha and declared_sha.casefold() != actual_sha.casefold():
        raise ValueError(
            f"文件哈希与 manifest 不一致：{path.name}（manifest {declared_sha[:12]}… / 实际 {actual_sha[:12]}…）"
        )
    source_id = _normalize(entry.get("source_id", "")) or path.stem
    original = _normalize(entry.get("original_filename", "")) or path.name
    rows = entry.get("declaration_source_rows") or ()
    return SourceMeta(
        source_id=source_id,
        path=path,
        original_filename=original,
        sha256=actual_sha,
        bytes=actual_bytes,
        declaration_sheet=_normalize(entry.get("declaration_sheet", "")) or None,
        declaration_header_row=entry.get("declaration_header_row"),
        declaration_rows=tuple(int(row) for row in rows if isinstance(row, (int, str)) and str(row).isdigit()),
        manifest_entry=entry,
    )


def batch_id_for(source_metas: Sequence[SourceMeta], dataset_kind: str) -> str:
    """批次 ID：包含文件哈希，用于报告/声明追溯。"""

    digest = hashlib.sha256()
    digest.update(dataset_kind.encode("utf-8"))
    for meta in sorted(source_metas, key=lambda item: (item.source_id, item.original_filename)):
        digest.update(f"|{meta.source_id}|{meta.sha256}".encode("utf-8"))
    return f"historical-{dataset_kind}-{digest.hexdigest()[:12]}"


def storage_key_for(source_metas: Sequence[SourceMeta], dataset_kind: str) -> str:
    """默认输出目录键：只含来源身份，不含文件哈希。

    同一模板重新保存（业务内容不变）后仍落到同一目录，重复导入保护才能默认生效；
    文件哈希变化由 changed/duplicate 报告表达，而不是换一个目录重来。
    """

    digest = hashlib.sha256()
    digest.update(dataset_kind.encode("utf-8"))
    for meta in sorted(source_metas, key=lambda item: (item.source_id, item.original_filename)):
        digest.update(f"|{meta.source_id}|{meta.original_filename}".encode("utf-8"))
    return f"historical-{dataset_kind}-{digest.hexdigest()[:12]}"


# --------------------------------------------------------------------------- 导入记录
@dataclass
class ImportRequest:
    files: Sequence[Path]
    out_dir: Path
    manifest: Path | None = None
    dataset_kind: str = "real"  # real | synthetic
    declared_success: bool = False
    declared_by: str = "Jay"
    declaration_note: str = DEFAULT_DECLARATION_NOTE
    declared_at: str | None = None
    copy_images: bool = False
    replace_changed: bool = False
    keep_business_duplicates: bool = False
    fresh: bool = False
    skip_validation: bool = False
    declaration_label: str = "申报要素"
    packing_label: str = "装箱明细"
    dataset_id: str | None = None


@dataclass
class ImportOutcome:
    out_dir: Path
    records_path: Path
    report_json_path: Path
    report_md_path: Path
    validation_json_path: Path | None
    validation_md_path: Path | None
    report: dict[str, Any]


def _record_id(source_id: str, sheet: str, row: int, contract: str, item: str) -> str:
    return RECORD_ID_PREFIX + _sha256_text(
        "|".join((source_id, sheet, str(row), contract, item))
    )[:20].upper()


def _source_key(source_id: str, sheet: str, row: int, contract: str, item: str) -> str:
    return "|".join((source_id, sheet, str(row), contract or "", item or ""))


def _issue_dict(issue: Any) -> dict[str, Any]:
    if hasattr(issue, "as_dict"):
        return dict(issue.as_dict())
    if isinstance(issue, Mapping):
        return dict(issue)
    return {"code": "issue", "message": str(issue)}


def _image_note(status: str, cell: str, message: str) -> str:
    if status == STATUS_AVAILABLE:
        return f"图片可用（{cell}）；图片仅记录到本商品行，不与其他行共享。"
    if status == STATUS_ABSENT:
        return f"无图（{cell or '图片列空白'}）。"
    if status == STATUS_READ_FAILED:
        return f"图片读取失败（{cell or '图片列'}）：{message or '原因未记录'}；不能当作无图。"
    if status == IMAGE_COLUMN_MISSING:
        return "模板没有可识别的图片列（column_missing）；不能当作所有商品都无图。"
    if status == IMAGE_COLUMN_AMBIGUOUS:
        return "模板图片列存在多个候选（column_ambiguous），未做逐行归属。"
    return f"图片状态未取得：{status}（{message or '原因未记录'}）。"


def _build_image_index(
    workbook: Workbook,
    extraction: Any,
    rows: Sequence[int],
) -> tuple[dict[int, Any], dict[str, Any], list[dict[str, Any]]]:
    rows = tuple(rows)
    layout = extraction.layout
    if not rows:
        # 空申报页签（有表头但没有数据行）不是解析失败：0 条记录，图片列无需逐行判定。
        return (
            {},
            {"column": layout.column("image") or "", "column_status": "no_rows", "counts": {}},
            [
                {
                    "code": "no_declaration_rows",
                    "message": "申报页签没有可导入的数据行；按 0 条记录处理，不判为解析失败",
                    "sheet": layout.sheet,
                    "cell": "",
                    "row": None,
                    "field": "image",
                    "severity": "info",
                    "blocking": False,
                    "details": {},
                }
            ],
        )
    column = layout.column("image")
    if not column:
        return (
            {},
            {
                "column": "",
                "column_status": IMAGE_COLUMN_MISSING,
                "counts": {IMAGE_COLUMN_MISSING: len(rows)},
            },
            [
                {
                    "code": "image_column_missing",
                    "message": "申报要素页签没有可识别的图片列；图片状态记为 column_missing，不当作无图",
                    "sheet": layout.sheet,
                    "cell": "",
                    "row": None,
                    "field": "image",
                    "severity": "warning",
                    "blocking": False,
                    "details": {},
                }
            ],
        )
    images = extract_column_images(
        workbook,
        sheet_name=layout.sheet,
        column=column,
        rows=rows,
        header_row=layout.header_row,
    )
    if images.column_status != "found" and images.column_status != "provided":
        return (
            {},
            {
                "column": column,
                "column_status": IMAGE_COLUMN_MISSING
                if images.column_status == "missing"
                else IMAGE_COLUMN_AMBIGUOUS,
                "counts": {IMAGE_COLUMN_MISSING: len(rows)},
                "reason": images.column_reason,
            },
            [
                {
                    "code": f"image_column_{images.column_status}",
                    "message": images.column_reason or "图片列不可用；图片状态记为 column_missing",
                    "sheet": layout.sheet,
                    "cell": "",
                    "row": None,
                    "field": "image",
                    "severity": "warning",
                    "blocking": False,
                    "details": {"column": column},
                }
            ],
        )
    by_row = {row_image.row: row_image for row_image in images.rows}
    counts: dict[str, int] = {STATUS_AVAILABLE: 0, STATUS_ABSENT: 0, STATUS_READ_FAILED: 0}
    for row in rows:
        row_image = by_row.get(row)
        if row_image is None:
            counts[IMAGE_ROW_MISSING] = counts.get(IMAGE_ROW_MISSING, 0) + 1
        else:
            counts[row_image.status] = counts.get(row_image.status, 0) + 1
    return by_row, {"column": column, "column_status": "found", "counts": counts}, []


def _build_record(
    *,
    record: Any,
    link: Any,
    source: SourceMeta,
    batch_id: str,
    dataset_kind: str,
    success_basis: str,
    image: Any,
    image_column_status: str,
    image_output_path: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """把一条 P04 记录装配成 P15 ``HistoryRecord``；返回 payload、行报告与问题。"""

    HistoryRecord, HistorySourceRef = _history_dto()
    issues: list[dict[str, Any]] = []
    for p04_issue in record.issues:
        issues.append(_issue_dict(p04_issue))
    missing_fields: list[str] = []

    name = _normalize(record.value("name"))
    hs = _normalize(record.value("hs"))
    contract = _normalize(record.value("contract"))
    item = _normalize(record.value("item"))
    raw_elements = record.value("elements")
    elements, element_issues = parse_element_pairs(raw_elements)
    issues.extend(element_issues)
    material = derive_material(elements)
    spec = derive_spec(elements)
    recorded_at, date_cell = explicit_recorded_at(record)
    if not recorded_at:
        recorded_at = MISSING_DATE
        missing_fields.append("recorded_at")
        issues.append(
            {
                "code": "recorded_at_missing",
                "message": "原模板没有明确的申报日期/业务日期列；recorded_at 使用占位符“未提供”，不推断日期",
                "sheet": record.sheet,
                "cell": "",
                "row": record.row,
                "field": "recorded_at",
                "severity": "info",
                "blocking": False,
                "details": {},
            }
        )
    if not contract:
        missing_fields.append("contract_no")
    if not item:
        missing_fields.append("item_no")
    if not material:
        missing_fields.append("material")
    if not spec:
        missing_fields.append("spec")

    if not name:
        issues.append(
            {
                "code": "missing_name",
                "message": "缺少品名，无法生成 P15 历史记录",
                "sheet": record.sheet,
                "cell": record.cell("name"),
                "row": record.row,
                "field": "name",
                "severity": "error",
                "blocking": True,
                "details": {},
            }
        )
    if not hs:
        issues.append(
            {
                "code": "missing_hs",
                "message": "缺少原 HS，无法生成 P15 历史记录",
                "sheet": record.sheet,
                "cell": record.cell("hs"),
                "row": record.row,
                "field": "hs",
                "severity": "error",
                "blocking": True,
                "details": {},
            }
        )
    if record.status != "identified":
        issues.append(
            {
                "code": "p04_requires_review",
                "message": f"P04 记录状态为 {record.status}；仍导入但标记待核",
                "sheet": record.sheet,
                "cell": "",
                "row": record.row,
                "field": "",
                "severity": "warning",
                "blocking": False,
                "details": {},
            }
        )
    if link is not None:
        for link_issue in link.issues:
            issues.append(_issue_dict(link_issue))
        if link.conflicting_declaration_rows:
            issues.append(
                {
                    "code": "association_ambiguous",
                    "message": "同一合同项被多条申报行认领；本行只作候选，未擅自选边",
                    "sheet": record.sheet,
                    "cell": "",
                    "row": record.row,
                    "field": "contract/item",
                    "severity": "warning",
                    "blocking": False,
                    "details": {
                        "conflicting_declaration_rows": list(link.conflicting_declaration_rows)
                    },
                }
            )

    # P05 图片结果：严格按行匹配；列缺失与无图分开。
    if image is not None:
        image_status = image.status
        image_cell = image.cell
        image_message = image.message
        image_sha = image.sha256
        image_size = str(image.size)
        image_format = image.image_format
    elif image_column_status == "found":
        # 图片列存在，但这一行没有返回结果：不能记成“无图”。
        image_status = IMAGE_ROW_MISSING
        image_cell = ""
        image_message = ""
        image_sha = ""
        image_size = ""
        image_format = ""
    else:
        image_status = image_column_status
        image_cell = ""
        image_message = ""
        image_sha = ""
        image_size = ""
        image_format = ""
    image_note = _image_note(image_status, image_cell, image_message)

    source_key = _source_key(source.source_id, record.sheet, record.row, contract, item)
    record_id = _record_id(source.source_id, record.sheet, record.row, contract, item)
    locator_parts = [f"{record.sheet}!第{record.row}行"]
    locator_parts.append(f"合同 {contract or '未提供'}")
    locator_parts.append(f"项号 {item or '未提供'}")
    if record.source_cells:
        locator_parts.append(f"单元格 {record.source_cells}")
    locator = "；".join(locator_parts)

    attributes: dict[str, str] = {
        "source_key": source_key,
        "source_file_id": source.source_id,
        "source_filename": source.original_filename,
        "source_file_sha256": source.sha256,
        "source_sheet": record.sheet,
        "source_row": str(record.row),
        "source_cells": record.source_cells,
        "contract_no": contract,
        "item_no": item,
        "quantity": _normalize(record.value("quantity")),
        "unit": _normalize(record.value("unit")),
        "refund_rate": _normalize(record.value("refund_rate")),
        "elements_raw": raw_elements,
        "image_status": image_status,
        "image_cell": image_cell,
        "image_sha256": image_sha,
        "image_size": image_size,
        "image_format": image_format,
        "image_output_path": image_output_path,
        "batch_id": batch_id,
        "success_basis": success_basis,
        "dataset_kind": dataset_kind,
    }
    if date_cell:
        attributes["recorded_at_cell"] = date_cell
    if MISSING_DATE == recorded_at:
        attributes["recorded_at_missing"] = "true"
    issue_codes = sorted({str(issue.get("code", "")) for issue in issues if issue.get("code")})
    if issue_codes:
        attributes["import_issue_codes"] = ",".join(issue_codes)
    blocking_codes = sorted(
        {
            str(issue.get("code", ""))
            for issue in issues
            if issue.get("blocking") or issue.get("severity") == "error"
        }
    )
    if blocking_codes:
        attributes["blocking_issue_codes"] = ",".join(blocking_codes)
    attributes["needs_review"] = "true" if (blocking_codes or record.status != "identified") else "false"

    notes_parts = [
        "历史模板导入；仅作参考线索，不直接作为当前归类结论。",
        f"导入批次 {batch_id}；成功依据：{success_basis}",
        "原模板未提供税则版本/适用范围/有效期时保持为空，不推断。",
    ]
    if MISSING_DATE == recorded_at:
        notes_parts.append("原模板未提供申报日期/业务日期，recorded_at 为“未提供”占位符。")
    notes_parts.append(image_note)
    if issues:
        notes_parts.append("导入问题：" + "；".join(issue_codes))

    row_report: dict[str, Any] = {
        "record_id": record_id,
        "source_key": source_key,
        "source": {
            "file_id": source.source_id,
            "filename": source.original_filename,
            "sheet": record.sheet,
            "row": record.row,
            "contract_no": contract,
            "item_no": item,
            "source_cells": record.source_cells,
        },
        "fields": {
            "name": name,
            "hs": hs,
            "material": material,
            "spec": spec,
            "elements_count": len(elements),
            "recorded_at": recorded_at,
            "image_status": image_status,
        },
        "missing_fields": missing_fields,
        "issues": issues,
        "p04_status": record.status,
        "p04_issue_codes": [_issue_dict(issue).get("code", "") for issue in record.issues],
    }

    if not name or not hs:
        row_report["status"] = "skipped"
        row_report["reason"] = "缺少 P15 必填的品名或原 HS；已报告，未静默丢弃"
        return {}, row_report, issues

    try:
        history_record = HistoryRecord(
            record_id=record_id,
            historical_hs=hs,
            historical_name=name,
            elements=elements,
            material=material,
            spec=spec,
            attributes=attributes,
            recorded_at=recorded_at,
            outcome=SUCCESS_OUTCOME,
            hs_version=None,
            applicable_scope=None,
            valid_until=None,
            source=HistorySourceRef(
                document=f"{source.source_id}/{source.original_filename}",
                locator=locator,
                url=None,
                system="historical-template-import"
                if dataset_kind == "real"
                else "synthetic-history-template",
            ),
            notes=" ".join(notes_parts),
        )
    except Exception as exc:  # P15 契约拒绝时也要留报告，不静默丢行
        issues.append(
            {
                "code": "p15_contract_rejected",
                "message": f"记录不符合 P15 契约：{type(exc).__name__}: {exc}",
                "sheet": record.sheet,
                "cell": "",
                "row": record.row,
                "field": "",
                "severity": "error",
                "blocking": True,
                "details": {},
            }
        )
        row_report["issues"] = issues
        row_report["status"] = "failed"
        row_report["reason"] = "P15 契约校验失败"
        return {}, row_report, issues

    payload = history_record.model_dump()
    row_report["status"] = "imported"
    row_report["reason"] = ""
    return payload, row_report, issues


def _read_existing(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = _read_json(path)
    if isinstance(payload, list):
        return {"schema_version": SCHEMA_VERSION, "dataset": {}, "records": payload}
    if not isinstance(payload, dict):
        raise ValueError(f"已有输出不是 JSON 对象或数组：{path}")
    return payload


#: 判断“同一来源位置是否变化”时只看业务字段；文件哈希、批次号、输出路径属于
#: 采集/运行痕迹，文件被重新保存但业务内容没变时不应把所有记录都算成 changed。
BUSINESS_RECORD_KEYS = (
    "record_id",
    "historical_hs",
    "historical_name",
    "elements",
    "material",
    "spec",
    "recorded_at",
    "outcome",
    "hs_version",
    "applicable_scope",
    "valid_until",
    "source",
)
BUSINESS_ATTRIBUTE_KEYS = (
    "source_key",
    "source_file_id",
    "source_filename",
    "source_sheet",
    "source_row",
    "source_cells",
    "contract_no",
    "item_no",
    "quantity",
    "unit",
    "refund_rate",
    "elements_raw",
    "image_status",
    "image_cell",
    "image_sha256",
    "image_size",
    "image_format",
    "recorded_at_cell",
    "recorded_at_missing",
    "success_basis",
    "dataset_kind",
    "import_issue_codes",
    "blocking_issue_codes",
    "needs_review",
)


def _business_fingerprint(payload: Mapping[str, Any]) -> str:
    attributes = payload.get("attributes") or {}
    business = {key: payload.get(key) for key in BUSINESS_RECORD_KEYS}
    business["attributes"] = {
        key: attributes.get(key) for key in BUSINESS_ATTRIBUTE_KEYS if key in attributes
    }
    return json.dumps(business, ensure_ascii=False, sort_keys=True)


def _business_dedup_key(payload: Mapping[str, Any]) -> str:
    """跨来源位置的业务去重键：同名同码同要素同合同项视为同一条业务案例。

    这比 ``record_id`` 更严格：它能发现“同一模板被复制/改名后再次导入”造成的
    业务重复，而 record_id/source_key 只覆盖“同一来源位置”的重复。合同号与项号
    都为空时返回空串，表示身份不足以做跨来源业务去重，退回 record_id/source_key。
    """

    attributes = payload.get("attributes") or {}
    contract = _compact(attributes.get("contract_no", ""))
    item = _compact(attributes.get("item_no", ""))
    if not contract and not item:
        return ""
    parts = [
        _compact(payload.get("historical_hs", "")),
        _compact(payload.get("historical_name", "")),
        json.dumps(payload.get("elements") or {}, ensure_ascii=False, sort_keys=True),
        contract,
        item,
        _compact(payload.get("material", "")),
        _compact(payload.get("spec", "")),
    ]
    return "|".join(parts)


def _merge_records(
    existing: Mapping[str, Any] | None,
    new_records: Sequence[dict[str, Any]],
    *,
    replace_changed: bool,
    keep_business_duplicates: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """合并已有输出与本次记录；返回 (最终 records, 重复报告, 本次新写入报告)。

    同 record_id 同业务内容 -> duplicate；同 source_key 不同业务内容 -> changed；
    批内重复 -> duplicate_within_batch；不同来源位置但业务内容相同 ->
    duplicate_business_content（默认跳过，除非 keep_business_duplicates）。任何分支
    都写入报告，不静默。
    """

    final: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    existing_records = list(existing.get("records", [])) if existing else []
    by_id: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    by_business: dict[str, dict[str, Any]] = {}
    for record in existing_records:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id", ""))
        if record_id:
            by_id[record_id] = record
        source_key = str((record.get("attributes") or {}).get("source_key", ""))
        if source_key:
            by_source[source_key] = record
        business_key = _business_dedup_key(record)
        if business_key and business_key not in by_business:
            by_business[business_key] = record
        final.append(record)

    seen_new_ids: set[str] = set()
    seen_new_sources: set[str] = set()
    seen_new_business: set[str] = set()
    for payload in new_records:
        record_id = str(payload.get("record_id", ""))
        attributes = payload.get("attributes") or {}
        source_key = str(attributes.get("source_key", ""))
        business_key = _business_dedup_key(payload)
        duplicate = {
            "record_id": record_id,
            "source_key": source_key,
            "source": {
                "file_id": attributes.get("source_file_id", ""),
                "filename": attributes.get("source_filename", ""),
                "sheet": attributes.get("source_sheet", ""),
                "row": attributes.get("source_row", ""),
            },
        }
        if record_id in seen_new_ids or (source_key and source_key in seen_new_sources):
            duplicates.append(duplicate | {"kind": "duplicate_within_batch", "action": "skipped"})
            continue
        seen_new_ids.add(record_id)
        if source_key:
            seen_new_sources.add(source_key)
        if record_id in by_id:
            current = by_id[record_id]
            same = _business_fingerprint(current) == _business_fingerprint(payload)
            kind = "duplicate" if same else "changed"
            if same:
                duplicates.append(duplicate | {"kind": kind, "action": "kept"})
                continue
            if not replace_changed:
                duplicates.append(duplicate | {"kind": kind, "action": "kept_existing"})
                continue
            final = [payload if str(item.get("record_id", "")) == record_id else item for item in final]
            by_id[record_id] = payload
            if source_key:
                by_source[source_key] = payload
            duplicates.append(duplicate | {"kind": kind, "action": "replaced"})
            writes.append(payload)
            continue
        if source_key and source_key in by_source:
            current = by_source[source_key]
            same = _business_fingerprint(current) == _business_fingerprint(payload)
            kind = "duplicate" if same else "changed"
            if not replace_changed:
                duplicates.append(duplicate | {"kind": kind, "action": "kept_existing"})
                continue
            old_id = str(current.get("record_id", ""))
            final = [payload if str(item.get("record_id", "")) == old_id else item for item in final]
            by_id.pop(old_id, None)
            by_id[record_id] = payload
            by_source[source_key] = payload
            duplicates.append(duplicate | {"kind": kind, "action": "replaced"})
            writes.append(payload)
            continue
        if business_key and business_key in seen_new_business:
            if not keep_business_duplicates:
                duplicates.append(
                    duplicate
                    | {"kind": "duplicate_business_content", "action": "skipped", "business_key": business_key}
                )
                continue
            duplicates.append(
                duplicate
                | {"kind": "duplicate_business_content", "action": "kept", "business_key": business_key}
            )
        if business_key and business_key in by_business:
            # 不同来源位置但业务内容完全一致（复制/改名）：默认跳过并报告，
            # 只有显式允许时才保留多份来源。
            if not keep_business_duplicates:
                duplicates.append(
                    duplicate
                    | {
                        "kind": "duplicate_business_content",
                        "action": "skipped",
                        "existing_record_id": str(by_business[business_key].get("record_id", "")),
                    }
                )
                continue
            duplicates.append(
                duplicate | {"kind": "duplicate_business_content", "action": "kept"}
            )
        if business_key:
            seen_new_business.add(business_key)
        final.append(payload)
        by_id[record_id] = payload
        if source_key:
            by_source[source_key] = payload
        writes.append(payload)
    return final, duplicates, writes


def _dataset_payload(
    *,
    batch_id: str,
    dataset_kind: str,
    dataset_id: str | None,
    source_count: int,
    record_count: int,
    declaration: HumanDeclaration | None,
) -> dict[str, Any]:
    if dataset_kind == "real":
        description = (
            "真实历史模板导入（第一批试点）。成功状态来自 Jay 的人工声明，未独立核验海关放行凭证；"
            "历史成功码仅作参考，不能作为当前归类结论。缺失日期/版本/有效期保持为空或“未提供”，不推断。"
        )
        synthetic = False
    else:
        description = (
            "合成历史模板导入样本，由测试构造器生成，品名/合同/HS/图片均为虚构；"
            "仅用于验证 P15 导入与检索链路，不是真实历史库。"
        )
        synthetic = True
    return {
        "dataset_id": dataset_id or f"{batch_id}-dataset",
        "synthetic": synthetic,
        "description": description,
        "generated_at": _utc_now(),
        "import_batch_id": batch_id,
        "source_file_count": source_count,
        "record_count": record_count,
        "human_declaration": declaration.as_dict(batch_id) if declaration else None,
    }


# --------------------------------------------------------------------------- 主流程
def import_history_templates(request: ImportRequest) -> ImportOutcome:
    if request.dataset_kind not in {"real", "synthetic"}:
        raise ValueError("dataset_kind 只能是 real 或 synthetic")
    if request.dataset_kind == "real" and not request.declared_success:
        raise ValueError(
            "真实历史模板导入必须显式声明 --declared-success（Jay 人工声明试点案例通过）；"
            "没有声明时不能把记录写成 approved"
        )

    manifest = load_manifest(request.manifest)
    if request.manifest is not None:
        manifest["_root"] = str(_manifest_root(request.manifest))
    source_metas: list[SourceMeta] = []
    for path in request.files:
        entry = _manifest_entry_for(manifest, Path(path)) if manifest else None
        source_metas.append(build_source_meta(Path(path), entry))
    if not source_metas:
        raise ValueError("至少需要一个历史模板文件")

    batch_id = batch_id_for(source_metas, request.dataset_kind)
    declaration = None
    if request.dataset_kind == "real":
        declaration = HumanDeclaration(
            declared_by=request.declared_by,
            declared_at=request.declared_at or _utc_now(),
            statement=request.declaration_note or DEFAULT_DECLARATION_NOTE,
            scope=tuple(meta.source_id for meta in source_metas),
            independently_verified=False,
        )
    success_basis = SUCCESS_BASIS_REAL if declaration else SUCCESS_BASIS_SYNTHETIC

    request.out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = request.out_dir / "images"
    if request.copy_images:
        image_dir.mkdir(parents=True, exist_ok=True)

    file_reports: list[dict[str, Any]] = []
    all_new_records: list[dict[str, Any]] = []
    all_row_reports: list[dict[str, Any]] = []
    image_copy_failures: list[dict[str, Any]] = []

    for source in source_metas:
        file_report: dict[str, Any] = {
            "source_id": source.source_id,
            "filename": source.original_filename,
            "path": str(source.path),
            "sha256": source.sha256,
            "bytes": source.bytes,
            "status": "parsed",
            "declaration_sheet": None,
            "header_row": None,
            "source_record_count": 0,
            "imported_count": 0,
            "skipped_count": 0,
            "image": {},
            "layout_warnings": [],
            "workbook_sheets": [],
            "task_issues": [],
            "row_reports": [],
            "error": None,
        }
        workbook = None
        try:
            workbook = Workbook.open(source.path)
            file_report["workbook_sheets"] = [sheet.name for sheet in workbook.sheets]
            mapped = None
            extraction = None
            try:
                mapped = map_workbook(
                    workbook,
                    declaration_label=request.declaration_label,
                    packing_label=request.packing_label,
                )
                extraction = mapped.declaration
                if source.declaration_sheet and extraction.layout.sheet != source.declaration_sheet:
                    file_report["layout_warnings"].append(
                        f"manifest 声明页签为 {source.declaration_sheet}，实际定位为 {extraction.layout.sheet}"
                    )
            except SemanticLayoutError as mapping_error:
                # 历史模板可能只有申报页签（例如合成/简化模板）；不因此丢整份文件，
                # 退回只用 P04 申报提取，并把“装箱页签不可用”作为文件级问题报告。
                extraction = extract_declarations(
                    workbook, sheet_label=request.declaration_label
                )
                file_report["task_issues"].append(
                    {
                        "code": "packing_layout_unavailable",
                        "message": f"装箱页签未能定位，已退回仅申报提取：{mapping_error}",
                        "severity": "warning",
                        "blocking": False,
                        "details": {},
                    }
                )
            if extraction is None:
                raise RuntimeError("P04 未返回申报提取结果")
            file_report["declaration_sheet"] = extraction.layout.sheet
            file_report["header_row"] = extraction.layout.header_row
            file_report["layout_warnings"].extend(extraction.layout.warnings)
            if mapped is not None:
                for issue in mapped.issues:
                    file_report["task_issues"].append(_issue_dict(issue))
            records = extraction.records
            file_report["source_record_count"] = len(records)
            if source.declaration_rows and tuple(source.declaration_rows) != tuple(
                record.row for record in records
            ):
                file_report["layout_warnings"].append(
                    f"manifest 声明源行 {len(source.declaration_rows)} 条，"
                    f"P04 实际提取 {len(records)} 条，数量不一致，请核对"
                )
            rows = [record.row for record in records]
            image_by_row, image_info, image_issues = _build_image_index(
                workbook, extraction, rows
            )
            file_report["image"] = image_info
            file_report["task_issues"].extend(image_issues)
            for record in records:
                link = mapped.link(record.row) if mapped is not None else None
                row_image = image_by_row.get(record.row)
                image_output_path = ""
                has_required_identity = bool(
                    _normalize(record.value("name")) and _normalize(record.value("hs"))
                )
                if (
                    has_required_identity
                    and row_image is not None
                    and row_image.status == STATUS_AVAILABLE
                    and request.copy_images
                ):
                    record_id = _record_id(
                        source.source_id,
                        record.sheet,
                        record.row,
                        _normalize(record.value("contract")),
                        _normalize(record.value("item")),
                    )
                    suffix = row_image.image_format or "bin"
                    target = image_dir / f"{record_id}.{row_image.sha256[:8]}.{suffix}"
                    try:
                        if target.exists() and target.read_bytes() != row_image.data:
                            raise ValueError("同名图片已存在且内容不同，拒绝覆盖")
                        if not target.exists():
                            target.write_bytes(row_image.data)
                        image_output_path = str(target.relative_to(request.out_dir))
                    except (OSError, ValueError) as image_error:
                        image_copy_failures.append(
                            {
                                "record_id": record_id,
                                "source": f"{source.source_id}/{record.sheet}!{record.row}",
                                "message": str(image_error),
                            }
                        )
                payload, row_report, issues = _build_record(
                    record=record,
                    link=link,
                    source=source,
                    batch_id=batch_id,
                    dataset_kind=request.dataset_kind,
                    success_basis=success_basis,
                    image=row_image,
                    image_column_status=image_info.get("column_status", IMAGE_COLUMN_MISSING),
                    image_output_path=image_output_path,
                )
                row_report["source"]["filename"] = source.original_filename
                file_report["row_reports"].append(row_report)
                all_row_reports.append(row_report)
                if payload:
                    all_new_records.append(payload)
                    file_report["imported_count"] += 1
                else:
                    file_report["skipped_count"] += 1
        except (OoxmlError, SemanticLayoutError) as parse_error:
            file_report["status"] = "parse_failed"
            file_report["error"] = {
                "code": getattr(parse_error, "code", type(parse_error).__name__),
                "message": str(parse_error),
            }
        except Exception as unexpected:  # 文件级异常也要进报告，不让一条坏文件中断整批
            file_report["status"] = "parse_failed"
            file_report["error"] = {
                "code": type(unexpected).__name__,
                "message": str(unexpected),
                "traceback": traceback.format_exc(limit=5),
            }
        file_reports.append(file_report)

    records_path = request.out_dir / "history_records.json"
    existing_payload = None if request.fresh else _read_existing(records_path)
    if existing_payload and existing_payload.get("dataset"):
        previous_synthetic = (existing_payload.get("dataset") or {}).get("synthetic")
        current_synthetic = request.dataset_kind == "synthetic"
        if previous_synthetic is not None and bool(previous_synthetic) != current_synthetic:
            raise ValueError(
                "已有输出与本次 dataset_kind 不一致（真实/合成不能混在同一个历史 JSON）；"
                "请换 --out-dir，或显式 --fresh 覆盖"
            )

    final_records, duplicates, writes = _merge_records(
        existing_payload,
        all_new_records,
        replace_changed=request.replace_changed,
        keep_business_duplicates=request.keep_business_duplicates,
    )
    # 行报告状态同步为真实处理结果；重复记录不重复写 payload。
    duplicate_by_id = {
        str(item.get("record_id", "")): item for item in duplicates if item.get("kind") != "duplicate_within_batch"
    }
    duplicate_in_batch = [
        item for item in duplicates if item.get("kind") == "duplicate_within_batch"
    ]
    for row_report in all_row_reports:
        if row_report.get("status") != "imported":
            continue
        duplicate = duplicate_by_id.get(str(row_report.get("record_id", "")))
        if duplicate is None:
            continue
        action = duplicate.get("action")
        row_report["duplicate"] = duplicate
        if action == "replaced":
            row_report["status"] = "replaced"
        elif action in {"kept", "kept_existing"}:
            row_report["status"] = "duplicate"
        elif duplicate.get("kind") == "duplicate_business_content":
            row_report["status"] = "duplicate_business_content"
        elif duplicate.get("kind") == "changed":
            row_report["status"] = "changed_skipped"
    written_ids = {str(payload.get("record_id", "")) for payload in writes}
    for file_report in file_reports:
        file_report["importable_count"] = file_report["imported_count"]
        file_report["written_count"] = sum(
            1
            for row in file_report["row_reports"]
            if str(row.get("record_id", "")) in written_ids
        )

    dataset = _dataset_payload(
        batch_id=batch_id,
        dataset_kind=request.dataset_kind,
        dataset_id=request.dataset_id,
        source_count=len(source_metas),
        record_count=len(final_records),
        declaration=declaration,
    )
    records_payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "records": final_records,
    }
    _write_json(records_path, records_payload)

    image_totals: dict[str, int] = {}
    for file_report in file_reports:
        for key, value in (file_report.get("image", {}).get("counts") or {}).items():
            image_totals[key] = image_totals.get(key, 0) + int(value)
    report = {
        "schema_version": "p15.history-import.report.1",
        "batch_id": batch_id,
        "dataset_kind": request.dataset_kind,
        "generated_at": _utc_now(),
        "out_dir": str(request.out_dir),
        "records_path": str(records_path),
        "human_declaration": declaration.as_dict(batch_id) if declaration else None,
        "dataset": dataset,
        "source_files": [
            {
                "source_id": meta.source_id,
                "filename": meta.original_filename,
                "path": str(meta.path),
                "sha256": meta.sha256,
                "bytes": meta.bytes,
            }
            for meta in source_metas
        ],
        "files": file_reports,
        "totals": {
            "files": len(file_reports),
            "parsed_files": sum(1 for item in file_reports if item["status"] == "parsed"),
            "parse_failed_files": sum(1 for item in file_reports if item["status"] == "parse_failed"),
            "source_records": sum(item["source_record_count"] for item in file_reports),
            "records_imported_this_run": len(writes),
            "records_skipped": sum(item["skipped_count"] for item in file_reports),
            "records_duplicate": sum(1 for item in duplicates if item.get("kind") == "duplicate"),
            "records_changed": sum(1 for item in duplicates if item.get("kind") == "changed"),
            "records_duplicate_within_batch": len(duplicate_in_batch),
            "records_duplicate_business_content": sum(
                1 for item in duplicates if item.get("kind") == "duplicate_business_content"
            ),
            "records_total_in_output": len(final_records),
            "image_status": image_totals,
            "image_copy_failures": len(image_copy_failures),
        },
        "duplicates": duplicates,
        "duplicate_within_batch": duplicate_in_batch,
        "image_copy_failures": image_copy_failures,
        "policy": {
            "reference_only": True,
            "no_inference": [
                "declaration_date",
                "customs_release_document",
                "hs_version",
                "applicable_scope",
                "valid_until",
            ],
            "missing_date_sentinel": MISSING_DATE,
            "duplicate_policy": (
                "same record_id + same business fields -> duplicate/skip; business fields changed -> "
                "changed/keep existing unless --replace-changed; same business content across different "
                "sources (renamed/copied) -> duplicate_business_content/skip unless --keep-business-duplicates; "
                "file hash and batch id are provenance, not business fields"
            ),
            "database": "not_connected; output is a P15-compatible JSON snapshot for later DB/HistoryRecordStore integration",
        },
    }
    report_json_path = request.out_dir / "import_report.json"
    report_md_path = request.out_dir / "import_report.md"
    _write_json(report_json_path, report)
    report_md_path.write_text(render_markdown_report(report), encoding="utf-8")

    validation_json_path = None
    validation_md_path = None
    if not request.skip_validation:
        validation = validate_history_queries(records_path, out_dir=request.out_dir)
        validation_json_path = request.out_dir / "query_validation.json"
        validation_md_path = request.out_dir / "query_validation.md"
        report["validation"] = {
            "path": str(validation_json_path),
            "checks": validation["checks"],
        }
        _write_json(report_json_path, report)

    return ImportOutcome(
        out_dir=request.out_dir,
        records_path=records_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        validation_json_path=validation_json_path,
        validation_md_path=validation_md_path,
        report=report,
    )


# --------------------------------------------------------------------------- 查询验证
def _query_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("historical_name", ""),
        "elements": dict(record.get("elements") or {}),
        "material": record.get("material", ""),
        "spec": record.get("spec", ""),
    }


def generate_query_cases(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    hit_record = next(
        (record for record in records if record.get("historical_name") and record.get("historical_hs")),
        records[0],
    )
    hit_query = _query_from_record(hit_record)
    cases: list[dict[str, Any]] = [
        {
            "case_id": "hit-basic",
            "kind": "hit",
            "query": hit_query,
            "expected": {"outcome": "matched", "record_id": hit_record.get("record_id")},
            "description": "用导入记录自身的品名/要素/规格材质查询，应返回该历史参考",
        },
        {
            "case_id": "no-match",
            "kind": "no_match",
            "query": {
                "name": "完全不存在的量子咖啡杯",
                "elements": {"用途": "量子咖啡杯测试"},
                "material": "不存在的材质",
                "spec": "不存在的规格",
            },
            "expected": {"outcome": "no_match"},
            "description": "不存在的商品，应返回 no_match（不是 query_failed）",
        },
    ]
    diff_record = next(
        (
            record
            for record in records
            if record.get("material") or record.get("spec") or (record.get("elements") or {})
        ),
        None,
    )
    if diff_record is not None:
        diff_query = _query_from_record(diff_record)
        diff_query["material"] = "关键属性不同的材质"
        diff_query["spec"] = "关键属性不同的规格"
        if diff_query.get("elements"):
            diff_query["elements"] = dict(diff_query["elements"])
            if "材质" in diff_query["elements"]:
                diff_query["elements"]["材质"] = "关键属性不同的材质"
            if "规格" in diff_query["elements"]:
                diff_query["elements"]["规格"] = "关键属性不同的规格"
        cases.append(
            {
                "case_id": "key-attribute-diff",
                "kind": "key_attribute_diff",
                "query": diff_query,
                "expected": {
                    "outcome": "matched",
                    "key_attribute_conflict": True,
                    "record_id": diff_record.get("record_id"),
                },
                "description": "同品名但材质/规格不同；应命中但标出差异与关键属性冲突",
            }
        )
    return cases


def validate_history_queries(
    records_path: Path,
    *,
    out_dir: Path | None = None,
    queries_path: Path | None = None,
) -> dict[str, Any]:
    configure_django()
    from adapters.sources.history_search import HistoryQuery, HistorySearchSource

    payload = _read_json(records_path)
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    cases = _read_json(queries_path) if queries_path else generate_query_cases(records)
    if isinstance(cases, dict):
        cases = cases.get("queries", [])
    source = HistorySearchSource.load(records_path)
    results: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    for case in cases:
        case_id = str(case.get("case_id", "case"))
        expected = case.get("expected") or {}
        query_payload = case.get("query") or {}
        try:
            query = HistoryQuery(**query_payload)
        except Exception as exc:
            results.append(
                {
                    "case_id": case_id,
                    "kind": case.get("kind", ""),
                    "query": query_payload,
                    "outcome": "invalid_query",
                    "error": f"{type(exc).__name__}: {exc}",
                    "passed": False,
                }
            )
            checks[case_id] = False
            continue
        result = source.search(query)
        passed = result.outcome.value == expected.get("outcome")
        if expected.get("record_id") and result.outcome.value == "matched":
            passed = passed and any(
                candidate.record_id == expected["record_id"] for candidate in result.candidates
            )
        if expected.get("key_attribute_conflict"):
            passed = passed and any(
                candidate.key_attribute_conflict for candidate in result.candidates
            )
        results.append(
            {
                "case_id": case_id,
                "kind": case.get("kind", ""),
                "description": case.get("description", ""),
                "query": query_payload,
                "expected": expected,
                "outcome": result.outcome.value,
                "evidence_status": result.evidence_status.value
                if result.evidence_status
                else None,
                "candidate_count": len(result.candidates),
                "candidates": [
                    {
                        "record_id": candidate.record_id,
                        "historical_hs": candidate.historical_hs,
                        "historical_name": candidate.historical_name,
                        "score": candidate.score,
                        "matched_keys": candidate.matched_keys,
                        "diff_keys": candidate.diff_keys,
                        "key_attribute_conflict": candidate.key_attribute_conflict,
                        "version_status": candidate.version_status.value,
                        "reference_only": candidate.reference_only,
                    }
                    for candidate in result.candidates
                ],
                "reason": result.reason,
                "passed": passed,
            }
        )
        checks[case_id] = passed

    required_case_kinds = ("hit", "no_match", "key_attribute_diff")
    present_kinds = {str(case.get("kind", "")) for case in cases}
    missing_required_cases = sorted(set(required_case_kinds) - present_kinds)
    validation = {
        "schema_version": "p15.history-import.validation.1",
        "generated_at": _utc_now(),
        "records_path": str(records_path),
        "dataset": payload.get("dataset") if isinstance(payload, dict) else None,
        "cases": results,
        "checks": checks,
        "required_case_kinds": list(required_case_kinds),
        "missing_required_cases": missing_required_cases,
        "all_passed": (
            bool(checks) and all(checks.values()) and not missing_required_cases
        ),
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(out_dir / "query_validation.json", validation)
        (out_dir / "query_validation.md").write_text(
            render_validation_markdown(validation), encoding="utf-8"
        )
    return validation


# --------------------------------------------------------------------------- 报告
def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def render_markdown_report(report: Mapping[str, Any]) -> str:
    totals = report.get("totals", {})
    declaration = report.get("human_declaration")
    lines = [
        f"# 历史模板导入报告（{report.get('batch_id')}）",
        "",
        f"- 数据集类型：`{report.get('dataset_kind')}`（真实/合成标记写入 P15 dataset.synthetic）",
        f"- 生成时间：{report.get('generated_at')}",
        f"- 输出记录：`{report.get('records_path')}`",
        f"- 数据库：未接入；本次输出是 P15 可读取的 JSON 快照，后续可通过 `HistoryRecordStore` 接库。",
        "",
        "## 人工声明",
        "",
    ]
    if declaration:
        lines.extend(
            [
                f"- 声明人：{declaration.get('declared_by')}",
                f"- 声明时间：{declaration.get('declared_at')}",
                f"- 声明内容：{declaration.get('statement')}",
                f"- 独立核验：{declaration.get('independently_verified')}（未伪造海关放行单据/报关日期/税则版本）",
            ]
        )
    else:
        lines.append("- 合成样本：没有真实历史成功声明；不得当成真实历史库。")
    lines.extend(
        [
            "",
            "## 汇总",
            "",
            _md_table(
                ["指标", "值"],
                [
                    ("文件数", totals.get("files", 0)),
                    ("解析成功文件", totals.get("parsed_files", 0)),
                    ("解析失败文件", totals.get("parse_failed_files", 0)),
                    ("P04 源记录", totals.get("source_records", 0)),
                    ("本次新写入", totals.get("records_imported_this_run", 0)),
                    ("跳过", totals.get("records_skipped", 0)),
                    ("重复", totals.get("records_duplicate", 0)),
                    ("跨来源业务重复", totals.get("records_duplicate_business_content", 0)),
                    ("变化", totals.get("records_changed", 0)),
                    ("批内重复", totals.get("records_duplicate_within_batch", 0)),
                    ("输出总记录", totals.get("records_total_in_output", 0)),
                    ("图片状态", totals.get("image_status", {})),
                ],
            ),
            "",
            "## 文件与图片",
            "",
            _md_table(
                ["source_id", "文件", "状态", "源记录", "可导入", "本次写入", "跳过", "图片状态"],
                [
                    (
                        item.get("source_id"),
                        item.get("filename"),
                        item.get("status"),
                        item.get("source_record_count"),
                        item.get("imported_count"),
                        item.get("written_count", 0),
                        item.get("skipped_count"),
                        item.get("image", {}).get("counts", {}),
                    )
                    for item in report.get("files", [])
                ],
            ),
            "",
            "## 重复/变化处理",
            "",
        ]
    )
    duplicates = report.get("duplicates") or []
    if duplicates:
        lines.append(
            _md_table(
                ["record_id", "kind", "action", "source"],
                [
                    (
                        item.get("record_id"),
                        item.get("kind"),
                        item.get("action"),
                        item.get("source", {}),
                    )
                    for item in duplicates
                ],
            )
        )
    else:
        lines.append("无重复或变化记录。")
    lines.extend(["", "## 文件级问题", ""])
    file_issue_rows = []
    for file_report in report.get("files", []):
        for issue in file_report.get("task_issues", []):
            file_issue_rows.append(
                (
                    file_report.get("source_id"),
                    issue.get("code"),
                    issue.get("severity", ""),
                    issue.get("message", ""),
                )
            )
    if file_issue_rows:
        lines.append(_md_table(["source_id", "问题", "严重度", "说明"], file_issue_rows))
    else:
        lines.append("无。")
    lines.extend(["", "## 逐行异常/缺失（节选）", ""])
    issue_rows = []
    for file_report in report.get("files", []):
        for row in file_report.get("row_reports", []):
            if row.get("issues") or row.get("status") != "imported":
                issues = row.get("issues", [])
                if any(issue.get("blocking") for issue in issues):
                    severity, rank = "error", 0
                elif any(issue.get("severity") in {"error", "warning"} for issue in issues):
                    severity, rank = "warning", 1
                else:
                    severity, rank = "info", 2
                issue_rows.append(
                    (
                        rank,
                        (
                            row.get("record_id"),
                            f"{row.get('source', {}).get('filename')}!{row.get('source', {}).get('sheet')}@{row.get('source', {}).get('row')}",
                            row.get("status"),
                            severity,
                            "; ".join(str(issue.get("code")) for issue in issues),
                            ",".join(row.get("missing_fields", [])),
                        ),
                    )
                )
    issue_rows.sort(key=lambda item: item[0])
    issue_rows = [row for _, row in issue_rows]
    if issue_rows:
        lines.append(
            _md_table(["record_id", "来源", "状态", "严重度", "问题", "缺字段"], issue_rows[:80])
        )
        if len(issue_rows) > 80:
            lines.append(f"\n（仅显示前 80 条，完整见 `{report.get('records_path')}` 同目录的 import_report.json）")
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- 历史成功记录只作参考，不直接作为当前归类结论。",
            "- 原模板缺失日期/税则版本/适用范围/有效期时保留为空或“未提供”，不推断。",
            "- 图片按“页签+行号+图片列”精确关联；列缺失 ≠ 无图，读取失败 ≠ 无图。",
            "- 重复导入按稳定 record_id/source_key 与业务指纹识别；重复、变化、批内重复和跨来源业务重复均写入本报告。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_validation_markdown(validation: Mapping[str, Any]) -> str:
    lines = [
        "# P15 历史记录查询验证",
        "",
        f"- 记录文件：`{validation.get('records_path')}`",
        f"- 生成时间：{validation.get('generated_at')}",
        f"- 全部通过：{validation.get('all_passed')}",
        f"- 缺失必需用例：{validation.get('missing_required_cases') or '无'}",
        "",
        _md_table(
            ["case", "类型", "结果", "候选数", "通过", "说明"],
            [
                (
                    case.get("case_id"),
                    case.get("kind"),
                    case.get("outcome"),
                    case.get("candidate_count", 0),
                    case.get("passed"),
                    case.get("reason", ""),
                )
                for case in validation.get("cases", [])
            ],
        ),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="历史业务 Excel -> P15 可读取历史记录的导入流水线（MVP，不写数据库）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="导入历史模板并生成 P15 JSON、导入报告与查询验证")
    imp.add_argument("--files", nargs="+", required=True, help="历史 Excel 文件（可多份）")
    imp.add_argument("--manifest", help="可选 manifest.json：用于 source_id、原始文件名、SHA256 与页签核对")
    imp.add_argument("--out-dir", help="输出目录；默认 data/private/p15-import/<batch_id>")
    imp.add_argument("--dataset-kind", choices=("real", "synthetic"), default="real")
    imp.add_argument("--dataset-id", help="覆盖 P15 dataset_id")
    imp.add_argument("--declared-success", action="store_true", help="真实模板必须显式声明：Jay 已确认试点案例可视为通过")
    imp.add_argument("--declared-by", default="Jay")
    imp.add_argument("--declaration-note", default=DEFAULT_DECLARATION_NOTE)
    imp.add_argument("--declared-at", help="声明时间；默认当前 UTC")
    imp.add_argument("--copy-images", action="store_true", help="把可用图片写到输出目录 images/（不提交 Git）")
    imp.add_argument("--replace-changed", action="store_true", help="同 source_key 内容变化时替换旧记录")
    imp.add_argument("--keep-business-duplicates", action="store_true", help="不同来源文件业务内容相同时仍保留多份来源")
    imp.add_argument("--fresh", action="store_true", help="忽略已有输出，重新开始")
    imp.add_argument("--skip-validation", action="store_true", help="不执行命中/无命中/关键差异查询验证")
    imp.add_argument("--declaration-label", default="申报要素")
    imp.add_argument("--packing-label", default="装箱明细")

    val = sub.add_parser("validate", help="用 P15 检索适配器验证已导入记录")
    val.add_argument("--records", required=True, help="P15 历史记录 JSON")
    val.add_argument("--queries", help="可选固定查询 JSON；默认从记录自动生成 hit/no-match/key-attribute-diff")
    val.add_argument("--out-dir", help="验证报告输出目录；默认与记录同目录")
    return parser


def _print_summary(outcome: ImportOutcome) -> None:
    totals = outcome.report["totals"]
    print(f"batch: {outcome.report['batch_id']}")
    print(f"records: {outcome.records_path}")
    print(f"report: {outcome.report_json_path}")
    if outcome.validation_json_path:
        print(f"validation: {outcome.validation_json_path}")
    print(
        "summary: files={files} parsed={parsed_files} source_records={source_records} "
        "written={records_imported_this_run} duplicate={records_duplicate} "
        "business_duplicate={records_duplicate_business_content} "
        "changed={records_changed} skipped={records_skipped} total={records_total_in_output}".format(
            **totals
        )
    )
    checks = (outcome.report.get("validation") or {}).get("checks") or {}
    if checks:
        print("checks: " + ", ".join(f"{key}={value}" for key, value in checks.items()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    configure_django()
    if args.command == "validate":
        records_path = Path(args.records)
        out_dir = Path(args.out_dir) if args.out_dir else records_path.parent
        validation = validate_history_queries(
            records_path,
            out_dir=out_dir,
            queries_path=Path(args.queries) if args.queries else None,
        )
        print(json.dumps(validation["checks"], ensure_ascii=False, indent=2))
        return 0 if validation["all_passed"] else 1

    files = [Path(item) for item in args.files]
    requested_out = Path(args.out_dir) if args.out_dir else None
    manifest = Path(args.manifest) if args.manifest else None
    if requested_out is None:
        # 批 ID 需要先读 manifest/哈希；这里先算一次元数据，再定默认目录。
        manifest_payload = load_manifest(manifest)
        if manifest is not None:
            manifest_payload["_root"] = str(_manifest_root(manifest))
        metas = [
            build_source_meta(
                item,
                _manifest_entry_for(manifest_payload, item) if manifest_payload else None,
            )
            for item in files
        ]
        requested_out = Path("data/private/p15-import") / storage_key_for(metas, args.dataset_kind)
    request = ImportRequest(
        files=files,
        out_dir=requested_out,
        manifest=manifest,
        dataset_kind=args.dataset_kind,
        declared_success=args.declared_success,
        declared_by=args.declared_by,
        declaration_note=args.declaration_note,
        declared_at=args.declared_at,
        copy_images=args.copy_images,
        replace_changed=args.replace_changed,
        keep_business_duplicates=args.keep_business_duplicates,
        fresh=args.fresh,
        skip_validation=args.skip_validation,
        declaration_label=args.declaration_label,
        packing_label=args.packing_label,
        dataset_id=args.dataset_id,
    )
    try:
        outcome = import_history_templates(request)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(outcome)
    totals = outcome.report["totals"]
    if totals["parse_failed_files"] or totals["records_total_in_output"] == 0:
        return 1
    checks = (outcome.report.get("validation") or {}).get("checks") or {}
    if checks and not all(checks.values()):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI 入口
    raise SystemExit(main())