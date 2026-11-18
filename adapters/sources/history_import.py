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
import tempfile
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
from adapters.sources.history_publish import PublishError, publish_staging
from adapters.sources.history_consolidated import (
    CONSOLIDATED_IDENTITY_VERSION,
    FORMAT_AUTO,
    FORMAT_CHOICES,
    FORMAT_CONSOLIDATED,
    FORMAT_LEGACY,
    IMAGE_COLUMN_AMBIGUOUS,
    IMAGE_COLUMN_MISSING,
    IMAGE_EXTERNAL_PATH_NOT_ENABLED,
    IMAGE_EXTERNAL_PATH_NOT_FOUND,
    IMAGE_TEXT_UNRECOGNIZED,
    IMAGE_EXTERNAL_PATH_UNSAFE,
    IMAGE_EXTERNAL_URL_NOT_FETCHED,
    IMAGE_ROW_MISSING,
    ConsolidatedFormatError,
    detect_workbook_format,
    load_mapping_config,
    parse_consolidated_workbook,
    parse_labeled_elements,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查；运行时延迟到 Django 配置完成后导入
    from adapters.sources.history_search import HistoryRecord

SCHEMA_VERSION = "p15.history.1"
RECORD_ID_PREFIX = "HIST-"
MISSING_DATE = "未提供"

SUCCESS_OUTCOME = "approved"
SUCCESS_BASIS_REAL = (
    "Jay 人工声明（导入批次）：历史成功数据可暂视为已通过、准确的历史案例参考；"
    "文件名“历史成功数据”不等于海关放行凭证，未独立核验，仅作参考。"
)
SUCCESS_BASIS_SYNTHETIC = "合成样本：仅用于验证导入与检索链路，不是真实历史库。"

DEFAULT_DECLARATION_NOTE = (
    "Jay 确认历史成功数据可暂视为已通过、准确的历史案例参考。"
    "本批次只记录该人工声明；文件名“历史成功数据”不等同海关放行凭证，"
    "不伪造海关放行单据、报关日期、税则版本或其他表中不存在的信息。"
)

DATE_HEADER_ALIASES = ("申报日期", "报关日期", "业务日期", "出口日期", "申报时间", "报关时间")
MATERIAL_KEYS = ("材质", "材料")
SPEC_KEYS = ("型号", "规格型号", "规格", "型号规格")
ELEMENT_SPLIT = re.compile(r"[|｜丨]")
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
    # 真实汇总表存在“1. 标签 [?] 值”的逐行写法；只有标签明确时才解析。
    labeled, labeled_issues = parse_labeled_elements(text)
    if labeled:
        return labeled, labeled_issues
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
    #: auto / legacy-template / consolidated；auto 只在结构明确时自动判断。
    input_format: str = FORMAT_AUTO
    #: 汇总表输入的可选显式映射、页签、表头行与图片根目录。
    mapping: Path | None = None
    sheet: str | None = None
    header_row: int | None = None
    image_root: Path | None = None
    #: 业务键相同、内容不同时默认保留旧记录；显式允许才同时保留新记录。
    accept_conflicts: bool = False
    #: --fresh 覆盖已存在输出前，若来源数据集不同则要求显式允许。
    allow_dataset_replace: bool = False


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
    if status == IMAGE_ROW_MISSING:
        return "图片列存在但该行没有返回结果（row_missing）；按未取得处理，不当作无图。"
    if status == IMAGE_EXTERNAL_URL_NOT_FETCHED:
        return "图片列是外部 URL；默认不联网，未读取，也不当作无图。"
    if status == IMAGE_EXTERNAL_PATH_NOT_ENABLED:
        return "图片列是本地路径；未提供显式图片根目录，默认不读取。"
    if status == IMAGE_EXTERNAL_PATH_UNSAFE:
        return "图片路径越出显式图片根目录（含目录穿越/符号链接越界），已拒绝读取。"
    if status == IMAGE_EXTERNAL_PATH_NOT_FOUND:
        return "显式图片根目录内找不到图片文件；不能当作无图。"
    if status == IMAGE_TEXT_UNRECOGNIZED:
        return "图片列文字不是可识别路径/图片线索；未读取，也不当作无图。"
    return f"图片状态未取得：{status}（{message or '原因未记录'}）。"


def _record_value(record: Any, field_name: str) -> str:
    try:
        return record.value(field_name)
    except (AttributeError, KeyError, TypeError):
        return ""


def _record_attr(record: Any, name: str, default: Any) -> Any:
    value = getattr(record, name, None)
    return default if value is None else value


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
    if (_record_attr(record, "extra_attributes", {}) or {}).get("source_format") == FORMAT_CONSOLIDATED:
        if raw_elements.strip() and not elements:
            issues = [issue for issue in issues if issue.get("code") != "element_unstructured"]
            issues.append({
                "code": "element_unstructured", "severity": "warning", "blocking": False,
                "message": "申报要素原文非空但未取得可靠键值；原文保留，默认检索排除待核记录",
                "sheet": record.sheet, "row": record.row, "cell": record.cell("elements"),
            })
            record.requires_review = True
        embedded = [
            key for key, value in elements.items()
            if re.search(r"(?:材质|材料|种类|加工方法|规格|型号|用途|品牌|成分含量)\s*[:：]", value)
        ]
        if embedded:
            issues.append({
                "code": "element_embedded_label", "severity": "warning", "blocking": False,
                "message": "要素值内嵌其他已知标签，可能缺分隔符；保留原文不猜拆，标待核",
                "sheet": record.sheet, "row": record.row, "cell": record.cell("elements"),
                "details": {"keys": embedded},
            })
            record.requires_review = True
    # 汇总表可能有独立的规格/材质列；有则优先使用，没有才从申报要素键值推导。
    material = _normalize(_record_value(record, "material")) or derive_material(elements)
    spec = _normalize(_record_value(record, "spec")) or derive_spec(elements)
    recorded_at = _normalize(_record_attr(record, "recorded_at_value", ""))
    date_cell = _normalize(_record_attr(record, "recorded_at_cell", ""))
    if not recorded_at:
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
    if not item and _record_attr(record, "expects_item", True):
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
    if _record_attr(record, "requires_review", False):
        review_codes = sorted(
            {
                str(issue.get("code", ""))
                for issue in issues
                if issue.get("code") and issue.get("severity") in {"warning", "error"}
            }
        )
        issues.append(
            {
                "code": "import_requires_review",
                "message": "该行有数据质量疑点，导入后标待核；默认检索会排除，审计时可显式包含",
                "sheet": record.sheet,
                "cell": "",
                "row": record.row,
                "field": "",
                "severity": "warning",
                "blocking": False,
                "details": {"review_codes": review_codes},
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

    source_key = _normalize(_record_attr(record, "source_key", "")) or _source_key(
        source.source_id, record.sheet, record.row, contract, item
    )
    record_id = _normalize(_record_attr(record, "record_id", "")) or _record_id(
        source.source_id, record.sheet, record.row, contract, item
    )
    locator = _normalize(_record_attr(record, "locator", ""))
    if not locator:
        locator_parts = [f"{record.sheet}!第{record.row}行"]
        locator_parts.append(f"合同 {contract or '未提供'}")
        locator_parts.append(f"项号 {item or '未提供'}")
        if record.source_cells:
            locator_parts.append(f"单元格 {record.source_cells}")
        locator = "；".join(locator_parts)
    source_system = _normalize(_record_attr(record, "source_system", "")) or (
        "historical-template-import" if dataset_kind == "real" else "synthetic-history-template"
    )

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
    for key, value in (_record_attr(record, "extra_attributes", {}) or {}).items():
        attributes.setdefault(str(key), str(value))
    if missing_fields:
        attributes.setdefault("missing_fields", ",".join(sorted(set(missing_fields))))
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
    review_codes = sorted(
        {
            str(issue.get("code", ""))
            for issue in issues
            if issue.get("code") and issue.get("severity") in {"warning", "error"}
        }
    )
    if review_codes and (
        blocking_codes or _record_attr(record, "requires_review", False)
    ):
        attributes["review_issue_codes"] = ",".join(review_codes)
    attributes["needs_review"] = (
        "true"
        if (blocking_codes or record.status != "identified" or _record_attr(record, "requires_review", False))
        else "false"
    )

    notes_parts = [
        _normalize(_record_attr(record, "notes_prefix", ""))
        or "历史模板导入；仅作参考线索，不直接作为当前归类结论。",
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
        "record_status": record.status,
        "p04_status": record.status,
        "p04_issue_codes": [_issue_dict(issue).get("code", "") for issue in record.issues],
    }
    extra_source = _record_attr(record, "source_extra", {}) or {}
    if extra_source:
        row_report["source"].update(extra_source)
    extra_fields = _record_attr(record, "report_fields", {}) or {}
    if extra_fields:
        row_report["fields"].update(extra_fields)
    extra_attributes = _record_attr(record, "extra_attributes", {}) or {}
    if extra_attributes:
        row_report["identity"] = {
            key: extra_attributes.get(key)
            for key in (
                "identity_basis",
                "content_hash",
                "content_occurrence",
                "change_key",
                "business_key",
                "source_format",
            )
            if key in extra_attributes
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
                system=source_system,
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


def _read_existing(path: Path, *, data: bytes | None = None) -> dict[str, Any] | None:
    if data is None and not path.is_file():
        return None
    payload = json.loads(data) if data is not None else _read_json(path)
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
)
BUSINESS_ATTRIBUTE_KEYS = (
    "source_key",
    "source_sheet",
    "contract_no",
    "item_no",
    "quantity",
    "unit",
    "refund_rate",
    "elements_raw",
    "recorded_at_cell",
    "recorded_at_missing",
    "dataset_kind",
)


def _business_fingerprint(payload: Mapping[str, Any]) -> str:
    attributes = payload.get("attributes") or {}
    business = {key: payload.get(key) for key in BUSINESS_RECORD_KEYS}
    if attributes.get("source_format") == FORMAT_CONSOLIDATED:
        # 内容哈希覆盖所有真实业务列（含备注/箱号等）；原始ID身份下这些字段
        # 变化同样必须报告 changed。文件名、坐标、批次和图片补拷不构成业务变化。
        keys = ("content_hash", "dataset_kind")
    else:
        keys = BUSINESS_ATTRIBUTE_KEYS
    business["attributes"] = {
        key: attributes.get(key) for key in keys if key in attributes
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


def _provenance_updates(
    current: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, str]:
    """重复记录只更新“采集痕迹”字段，不覆盖业务字段。

    典型场景：第一次导入没加 ``--copy-images``，记录里 ``image_output_path`` 为空；
    第二次补拷后图片文件已生成，但业务指纹相同，不能把整条记录当 changed，
    也不能继续保留旧空路径。排序/重新保存后行号、单元格范围、文件哈希变化也只
    更新溯源位置，不改变业务身份。
    """

    # 校验结论、问题码、身份锚点和来源坐标都属于被校验的那份内容。
    # changed/kept_existing 保留旧事实时，不能把新输入的“干净”标记或位置
    # 拼到旧记录上；显式替换才允许整条采用新内容与新结论。
    # 内容相同时仍允许重验标记及补图更新，兼容重复导入与图片消失处理。
    if _business_fingerprint(current) != _business_fingerprint(payload):
        return {}
    old_attributes = current.get("attributes") or {}
    new_attributes = payload.get("attributes") or {}
    updates: dict[str, str] = {}
    for key in (
        "image_output_path",
        "source_row",
        "source_cells",
        "source_file_sha256",
        "source_file_id",
        "source_filename",
        "batch_id",
        "success_basis",
        "image_status",
        "image_cell",
        "image_sha256",
        "image_size",
        "image_format",
        "import_issue_codes",
        "blocking_issue_codes",
        "review_issue_codes",
        "needs_review",
    ):
        new_value = str(new_attributes.get(key, "") or "")
        old_value = str(old_attributes.get(key, "") or "")
        clearable = key in {
            "image_cell", "image_sha256", "image_size", "image_format",
            "import_issue_codes", "blocking_issue_codes", "review_issue_codes",
        }
        if key == "image_output_path":
            clearable = new_attributes.get("image_status") != STATUS_AVAILABLE or (
                old_attributes.get("image_sha256") != new_attributes.get("image_sha256")
            )
        if (new_value or clearable) and new_value != old_value:
            updates[key] = new_value
    # 合并阶段产生的身份不确定性不是解析器可解除的结论。
    # 同内容再次输入不能把旧的 identity_uncertain 自动洗成干净。
    previous_review_codes = set(filter(None, str(old_attributes.get("review_issue_codes", "")).split(",")))
    if "identity_uncertain" in previous_review_codes:
        updates["needs_review"] = "true"
        for key in ("import_issue_codes", "review_issue_codes"):
            codes = set(filter(None, str(new_attributes.get(key, "")).split(",")))
            codes.add("identity_uncertain")
            updates[key] = ",".join(sorted(codes))
    # 旧快照在此增补稳定锚点，避免下一次变更退回旧的序号猜配。
    for key in ("change_anchor", "change_key", "business_key"):
        if new_attributes.get(key) and new_attributes.get(key) != old_attributes.get(key):
            updates[key] = str(new_attributes[key])
    return updates


def _enforce_stored_hs_review(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验最终实际保留的 HS，恢复受旧版清标缺陷影响的汇总记录。

    不改编码、身份或业务字段；只允许把不合 ASCII 格式的记录加回待核，
    不自动解除任何已有待核。修复逐条进入报告，旧 generation 保持不变。
    """
    repairs: list[dict[str, Any]] = []
    for record in records:
        attributes = record.get("attributes") or {}
        if attributes.get("source_format") != FORMAT_CONSOLIDATED:
            continue
        hs = str(record.get("historical_hs", ""))
        if re.fullmatch(r"[0-9]{10}", hs):
            continue
        code = "hs_non_ascii_digits" if any(char.isdecimal() and char not in "0123456789" for char in hs) else "hs_length_not_10"
        before = {key: attributes.get(key, "") for key in (
            "needs_review", "import_issue_codes", "review_issue_codes")}
        attributes["needs_review"] = "true"
        for key in ("import_issue_codes", "review_issue_codes"):
            codes = set(filter(None, str(attributes.get(key, "")).split(",")))
            codes.add(code)
            attributes[key] = ",".join(sorted(codes))
        record["attributes"] = attributes
        if before != {key: attributes.get(key, "") for key in before}:
            repairs.append({
                "record_id": record.get("record_id", ""), "code": code,
                "action": "restored_needs_review", "historical_hs": hs,
                "message": "按最终保留的HS恢复待核；未替换旧业务内容，默认检索排除",
            })
    return repairs


def _with_provenance(
    current: Mapping[str, Any],
    updates: Mapping[str, str],
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    updated = json.loads(json.dumps(current, ensure_ascii=False))
    updated.setdefault("attributes", {})
    updated["attributes"].update(updates)
    if payload is not None and (
        "source_row" in updates or "source_cells" in updates or "source_filename" in updates
    ):
        new_source = payload.get("source")
        if isinstance(new_source, Mapping):
            updated["source"] = json.loads(json.dumps(new_source, ensure_ascii=False))
    return updated


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
                updates = _provenance_updates(current, payload)
                if updates:
                    updated = _with_provenance(current, updates, payload)
                    final = [
                        updated if str(item.get("record_id", "")) == record_id else item
                        for item in final
                    ]
                    by_id[record_id] = updated
                    if source_key:
                        by_source[source_key] = updated
                    if business_key and by_business.get(business_key) is current:
                        by_business[business_key] = updated
                    duplicates.append(
                        duplicate
                        | {
                            "kind": kind,
                            "action": "provenance_updated",
                            "provenance_fields": sorted(updates),
                        }
                    )
                else:
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
            if same:
                updates = _provenance_updates(current, payload)
                if updates:
                    updated = _with_provenance(current, updates, payload)
                    old_id = str(current.get("record_id", ""))
                    final = [
                        updated if str(item.get("record_id", "")) == old_id else item
                        for item in final
                    ]
                    by_id[old_id] = updated
                    by_source[source_key] = updated
                    if business_key and by_business.get(business_key) is current:
                        by_business[business_key] = updated
                    duplicates.append(
                        duplicate
                        | {
                            "kind": kind,
                            "action": "provenance_updated",
                            "provenance_fields": sorted(updates),
                        }
                    )
                else:
                    duplicates.append(duplicate | {"kind": kind, "action": "kept"})
                continue
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


def _consolidated_record_source(payload: Mapping[str, Any]) -> dict[str, Any]:
    attributes = payload.get("attributes") or {}
    return {
        "file_id": attributes.get("source_file_id", ""),
        "filename": attributes.get("source_filename", ""),
        "sheet": attributes.get("source_sheet", ""),
        "row": attributes.get("source_row", ""),
        "purchase_contract_no": attributes.get("purchase_contract_no", ""),
        "sales_contract_no": attributes.get("sales_contract_no", ""),
        "declaration_no": attributes.get("declaration_no", ""),
    }


def _consolidated_record_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "record_id": payload.get("record_id", ""),
        "historical_hs": payload.get("historical_hs", ""),
        "historical_name": payload.get("historical_name", ""),
        "source": _consolidated_record_source(payload),
        "content_hash": (payload.get("attributes") or {}).get("content_hash", ""),
        "change_key": (payload.get("attributes") or {}).get("change_key", ""),
        "business_key": (payload.get("attributes") or {}).get("business_key", ""),
    }


def _merge_consolidated_records(
    existing: Mapping[str, Any] | None,
    new_records: Sequence[dict[str, Any]],
    *,
    replace_changed: bool,
    keep_business_duplicates: bool,
    accept_conflicts: bool,
    current_source_ids: set[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    set[str],
]:
    """汇总表合并：内容身份 + 变更配对 + 冲突/消失报告。

    返回 ``(final, duplicates, writes, conflicts, missing, actions, added_ids)``。
    默认不覆盖旧记录、不物理删除历史记录；只有 ``replace_changed`` 才替换。
    """

    final: list[dict[str, Any]] = [
        record for record in (existing.get("records", []) if existing else []) if isinstance(record, dict)
    ]
    duplicates: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    actions: dict[str, str] = {}
    added_ids: set[str] = set()
    paired_old_ids: set[str] = set()

    by_id: dict[str, dict[str, Any]] = {}
    by_change: dict[str, dict[str, Any]] = {}
    by_business: dict[str, dict[str, Any]] = {}
    for record in final:
        record_id = str(record.get("record_id", ""))
        if record_id:
            by_id[record_id] = record
        attributes = record.get("attributes") or {}
        change_key = str(attributes.get("change_key", ""))
        if change_key:
            by_change[change_key] = record
        business_key = str(attributes.get("business_key", ""))
        if business_key:
            by_business[business_key] = record

    # 先确定完整的 incoming ID 集合，不能按遍历顺序把稍后出现的原记录
    # 当成“已消失”并与新增商品配对。只有同锚点恰好一个旧缺失、一个新内容时
    # 才报告 changed；多个候选绝不按排序/行号猜配，保留 missing+added。
    incoming_ids = {str(item.get("record_id", "")) for item in new_records}
    original_ids = set(by_id)
    old_by_anchor: dict[str, list[dict[str, Any]]] = {}
    new_by_anchor: dict[str, list[dict[str, Any]]] = {}
    for old_record in final:
        anchor = str((old_record.get("attributes") or {}).get("change_anchor", ""))
        if anchor and str(old_record.get("record_id", "")) not in incoming_ids:
            old_by_anchor.setdefault(anchor, []).append(old_record)
    for new_record in new_records:
        anchor = str((new_record.get("attributes") or {}).get("change_anchor", ""))
        if anchor and str(new_record.get("record_id", "")) not in original_ids:
            new_by_anchor.setdefault(anchor, []).append(new_record)

    def replace_record(record: dict[str, Any], updated: dict[str, Any]) -> None:
        old_id = str(record.get("record_id", ""))
        final[:] = [updated if str(item.get("record_id", "")) == old_id else item for item in final]
        by_id[old_id] = updated
        old_attributes = record.get("attributes") or {}
        for key in ("change_key", "business_key"):
            value = str(old_attributes.get(key, ""))
            if value and by_change.get(value) is record:
                by_change.pop(value, None)
            if key == "business_key" and value and by_business.get(value) is record:
                by_business.pop(value, None)
        new_attributes = updated.get("attributes") or {}
        for key in ("change_key", "business_key"):
            value = str(new_attributes.get(key, ""))
            if value:
                (by_change if key == "change_key" else by_business)[value] = updated

    def remove_record(record: dict[str, Any]) -> None:
        record_id = str(record.get("record_id", ""))
        final[:] = [item for item in final if str(item.get("record_id", "")) != record_id]
        by_id.pop(record_id, None)
        attributes = record.get("attributes") or {}
        change_key = str(attributes.get("change_key", ""))
        if change_key and by_change.get(change_key) is record:
            by_change.pop(change_key, None)
        business_key = str(attributes.get("business_key", ""))
        if business_key and by_business.get(business_key) is record:
            by_business.pop(business_key, None)

    def append_record(record: dict[str, Any]) -> None:
        final.append(record)
        record_id = str(record.get("record_id", ""))
        by_id[record_id] = record
        attributes = record.get("attributes") or {}
        change_key = str(attributes.get("change_key", ""))
        if change_key:
            by_change[change_key] = record
        business_key = str(attributes.get("business_key", ""))
        if business_key:
            by_business[business_key] = record

    seen_new_ids: set[str] = set()
    seen_new_business: dict[str, dict[str, Any]] = {}
    for payload in new_records:
        record_id = str(payload.get("record_id", ""))
        attributes = payload.get("attributes") or {}
        change_key = str(attributes.get("change_key", ""))
        business_key = str(attributes.get("business_key", ""))
        duplicate = {
            "record_id": record_id,
            "source_key": str(attributes.get("source_key", "")),
            "source": _consolidated_record_source(payload),
        }
        if record_id in seen_new_ids:
            duplicates.append(duplicate | {"kind": "duplicate_within_batch", "action": "skipped"})
            actions[record_id] = "duplicate_within_batch"
            continue
        seen_new_ids.add(record_id)

        current = by_id.get(record_id)
        if current is not None:
            same = _business_fingerprint(current) == _business_fingerprint(payload)
            if not same:
                # 仅 original_record_id 身份会出现“同一 ID 内容变化”。
                if not replace_changed:
                    updates = _provenance_updates(current, payload)
                    if updates:
                        replace_record(current, _with_provenance(current, updates, payload))
                    duplicate["kind"] = "changed"
                    duplicate["action"] = "kept_existing"
                    duplicates.append(duplicate)
                    actions[record_id] = "changed_kept_existing"
                    continue
                remove_record(current)
                append_record(payload)
                writes.append(payload)
                duplicate["kind"] = "changed"
                duplicate["action"] = "replaced"
                duplicates.append(duplicate)
                actions[record_id] = "replaced"
                continue
            updates = _provenance_updates(current, payload)
            if updates:
                updated = _with_provenance(current, updates, payload)
                replace_record(current, updated)
                duplicates.append(
                    duplicate
                    | {
                        "kind": "duplicate",
                        "action": "provenance_updated",
                        "provenance_fields": sorted(updates),
                    }
                )
                actions[record_id] = "duplicate"
            else:
                duplicates.append(duplicate | {"kind": "duplicate", "action": "kept"})
                actions[record_id] = "duplicate"
            continue

        anchor = str(attributes.get("change_anchor", ""))
        if anchor:
            old_candidates = old_by_anchor.get(anchor, [])
            new_candidates = new_by_anchor.get(anchor, [])
            old = old_candidates[0] if len(old_candidates) == len(new_candidates) == 1 else None
            if old_candidates and old is None:
                conflicts.append(
                    duplicate | {
                        "kind": "identity_uncertain",
                        "action": "kept_both",
                        "message": "同业务锚点有多个新增/消失候选，无法可靠配对；保留新增并报告消失，不猜覆盖",
                        "old_candidate_ids": [str(item.get("record_id", "")) for item in old_candidates],
                    }
                )
                attributes["needs_review"] = "true"
                attributes["review_issue_codes"] = ",".join(filter(None, [
                    str(attributes.get("review_issue_codes", "")), "identity_uncertain"
                ]))
        else:
            # 旧快照没有 change_anchor 时不按位置序号猜配；先以重复运行增补
            # 锚点，未能配对的旧记录报告 missing，新内容保留，绝不误覆盖。
            old = None
        old_id = str(old.get("record_id", "")) if old else ""
        if old is not None and old_id and old_id not in incoming_ids and old_id not in paired_old_ids:
            paired_old_ids.add(old_id)
            if replace_changed:
                remove_record(old)
                append_record(payload)
                writes.append(payload)
                duplicates.append(
                    duplicate
                    | {
                        "kind": "changed",
                        "action": "replaced",
                        "previous_record_id": old_id,
                        "previous": _consolidated_record_summary(old),
                    }
                )
                actions[record_id] = "replaced"
            else:
                updates = _provenance_updates(old, payload)
                if updates:
                    replace_record(old, _with_provenance(old, updates, payload))
                duplicates.append(
                    duplicate
                    | {
                        "kind": "changed",
                        "action": "kept_existing",
                        "previous_record_id": old_id,
                        "previous": _consolidated_record_summary(old),
                    }
                )
                actions[record_id] = "changed_kept_existing"
            continue

        existing_business = by_business.get(business_key) if business_key else None
        if existing_business is not None:
            if not anchor or not old_by_anchor.get(anchor) or len(old_by_anchor.get(anchor, [])) == len(new_by_anchor.get(anchor, [])) == 1:
                paired_old_ids.add(str(existing_business.get("record_id", "")))
            same = _business_fingerprint(existing_business) == _business_fingerprint(payload)
            if same and not keep_business_duplicates:
                duplicates.append(
                    duplicate
                    | {
                        "kind": "duplicate_business_content",
                        "action": "skipped",
                        "existing_record_id": str(existing_business.get("record_id", "")),
                    }
                )
                actions[record_id] = "duplicate_business_content"
                continue
            if not same and not accept_conflicts:
                conflicts.append(
                    duplicate
                    | {
                        "kind": "business_key_conflict",
                        "action": "kept_existing",
                        "existing": _consolidated_record_summary(existing_business),
                        "incoming": _consolidated_record_summary(payload),
                    }
                )
                actions[record_id] = "conflict_kept_existing"
                continue
            if not same:
                conflicts.append(
                    duplicate
                    | {
                        "kind": "business_key_conflict",
                        "action": "kept_both",
                        "existing": _consolidated_record_summary(existing_business),
                        "incoming": _consolidated_record_summary(payload),
                    }
                )
                actions[record_id] = "conflict_kept_both"

        previous_new = seen_new_business.get(business_key) if business_key else None
        if previous_new is not None:
            paired_old_ids.add(str(previous_new.get("record_id", "")))
            same = _business_fingerprint(previous_new) == _business_fingerprint(payload)
            if same and not keep_business_duplicates:
                duplicates.append(
                    duplicate
                    | {
                        "kind": "duplicate_business_content",
                        "action": "skipped",
                        "existing_record_id": str(previous_new.get("record_id", "")),
                    }
                )
                actions[record_id] = "duplicate_business_content"
                continue
            if not same:
                # 同一业务键的多个变体（例如同一商品不同箱号/备注）不是冲突：
                # 它们内容不同、身份不同，必须保留，只在报告里标注。
                conflicts.append(
                    duplicate
                    | {
                        "kind": "business_key_variant",
                        "action": "kept_both",
                        "existing": _consolidated_record_summary(previous_new),
                        "incoming": _consolidated_record_summary(payload),
                    }
                )
                actions[record_id] = "business_key_variant"
        if business_key:
            seen_new_business.setdefault(business_key, payload)
        append_record(payload)
        writes.append(payload)
        added_ids.add(record_id)
        actions.setdefault(record_id, "added")

    current_ids = {
        str(payload.get("record_id", "")) for payload in new_records if payload.get("record_id")
    }
    for record in list(final):
        attributes = record.get("attributes") or {}
        source_file_id = str(attributes.get("source_file_id", ""))
        if source_file_id not in current_source_ids:
            continue
        if str(attributes.get("source_format", "")) != FORMAT_CONSOLIDATED:
            continue
        record_id = str(record.get("record_id", ""))
        if not record_id or record_id in current_ids or record_id in paired_old_ids:
            continue
        if record_id in added_ids:
            continue
        missing.append(
            _consolidated_record_summary(record)
            | {
                "kind": "missing_from_source",
                "action": "kept",
                "message": "该记录在本次输入中不再出现；默认保留旧记录，不自动物理删除",
            }
        )
    return final, duplicates, writes, conflicts, missing, actions, added_ids


def _dataset_payload(
    *,
    batch_id: str,
    dataset_kind: str,
    dataset_id: str | None,
    source_count: int,
    record_count: int,
    declaration: HumanDeclaration | None,
    input_formats: Sequence[str] = (),
    source_files: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if dataset_kind == "real":
        description = (
            "真实历史数据导入。成功状态来自 Jay 的人工声明，未独立核验海关放行凭证；"
            "文件名“历史成功数据”不等于海关放行凭证。历史成功码仅作参考，不能作为当前归类结论。"
            "缺失日期/版本/有效期保持为空或“未提供”，不推断。"
        )
        synthetic = False
    else:
        description = (
            "合成历史数据导入样本，由测试构造器生成，品名/合同/HS/图片均为虚构；"
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
        "input_formats": sorted(set(input_formats)),
        "identity_version": CONSOLIDATED_IDENTITY_VERSION if FORMAT_CONSOLIDATED in input_formats else "legacy-row.1",
        "source_files": [dict(item) for item in source_files],
        "human_declaration": declaration.as_dict(batch_id) if declaration else None,
    }


def _clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _partition_merged_records(
    final_records: Sequence[Mapping[str, Any]],
    *,
    writes: Sequence[Mapping[str, Any]],
    duplicates: Sequence[Mapping[str, Any]],
    new_meta: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """区分合并后仍保留旧来源的记录与本次新采纳/更新溯源的记录。"""

    written_ids = {str(item.get("record_id", "")) for item in writes}
    provenance_updated_ids = {
        str(item.get("record_id", ""))
        for item in duplicates
        if item.get("action") == "provenance_updated"
    }
    unchanged_duplicate_ids = {
        str(item.get("record_id", ""))
        for item in duplicates
        if item.get("kind") == "duplicate" and item.get("action") == "kept"
    }
    new_sha_by_source = {
        str(item.get("source_id", "")): str(item.get("sha256", ""))
        for item in (new_meta.get("source_files") or [])
        if isinstance(item, Mapping) and item.get("source_id")
    }
    retained: list[Mapping[str, Any]] = []
    adopted: list[Mapping[str, Any]] = []
    for record in final_records:
        record_id = str(record.get("record_id", ""))
        attributes = record.get("attributes") or {}
        source_id = str(attributes.get("source_file_id", ""))
        record_sha = str(attributes.get("source_file_sha256", ""))
        new_sha = new_sha_by_source.get(source_id, "")
        if record_id in written_ids or record_id in provenance_updated_ids:
            adopted.append(record)
        elif record_id in unchanged_duplicate_ids and not (
            record_sha and new_sha and record_sha != new_sha
        ):
            adopted.append(record)
        else:
            retained.append(record)
    return retained, adopted


def _declaration_entry(meta: Mapping[str, Any]) -> dict[str, Any] | None:
    declaration = meta.get("human_declaration")
    if not isinstance(declaration, Mapping):
        return None
    entry = _clone_json(dict(declaration))
    batch_id = meta.get("import_batch_id")
    if isinstance(batch_id, str) and batch_id.strip():
        entry.setdefault("import_batch_id", batch_id)
    return entry


def _merge_dataset_metadata(
    old_meta: Mapping[str, Any] | None,
    new_meta: Mapping[str, Any],
    final_records: Sequence[Mapping[str, Any]],
    *,
    writes: Sequence[Mapping[str, Any]],
    duplicates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """保留旧记录时同步保留原来源哈希与原人工声明，不被新批次覆盖。

    - 全量采纳新批次（含首次导入、``replace_changed=True`` 全量替换）：使用新批次元数据；
    - 仅保留旧记录（新批次未采纳任何新增/替换/同内容溯源更新）：保留旧数据集元数据；
    - 混合保留旧记录与新批次记录：主 ``source_files`` / ``human_declaration`` 保持保留记录的
      原来源与原声明，同时在 ``active_source_files`` / ``active_declarations`` 保留所有仍在
      ``final_records`` 中有效记录对应的文件哈希与声明，供按批次/哈希双向回查。
    """

    if not old_meta:
        merged = _clone_json(dict(new_meta))
        merged["record_count"] = len(final_records)
        return merged

    retained, adopted = _partition_merged_records(
        final_records, writes=writes, duplicates=duplicates, new_meta=new_meta
    )
    if not retained:
        merged = _clone_json(dict(new_meta))
        merged["record_count"] = len(final_records)
        return merged
    if not adopted:
        merged = _clone_json(dict(old_meta))
        merged["record_count"] = len(final_records)
        return merged

    merged = _clone_json(dict(old_meta))
    merged["record_count"] = len(final_records)

    old_sources_by_id = {
        str(item.get("source_id", "")): _clone_json(dict(item))
        for item in (old_meta.get("source_files") or [])
        if isinstance(item, Mapping) and item.get("source_id")
    }
    new_sources_by_id = {
        str(item.get("source_id", "")): _clone_json(dict(item))
        for item in (new_meta.get("source_files") or [])
        if isinstance(item, Mapping) and item.get("source_id")
    }
    retained_sids = {
        str((record.get("attributes") or {}).get("source_file_id", ""))
        for record in retained
    }
    if "" in retained_sids:
        retained_sids.update(old_sources_by_id)
    active_file_keys = {
        (
            str((record.get("attributes") or {}).get("source_file_id", "")),
            str((record.get("attributes") or {}).get("source_file_sha256", "")),
        )
        for record in final_records
    }
    sids_without_sha = {sid for sid, sha in active_file_keys if not sha}
    candidate_sources = [
        *(old_meta.get("active_source_files") or old_meta.get("source_files") or []),
        *(new_meta.get("source_files") or []),
    ]
    active_sources: list[dict[str, Any]] = []
    seen_source_keys: set[tuple[str, str]] = set()
    for item in candidate_sources:
        if not isinstance(item, Mapping):
            continue
        sid = str(item.get("source_id", ""))
        sha = str(item.get("sha256", ""))
        if not sid or (sid, sha) in seen_source_keys:
            continue
        if (sid, sha) in active_file_keys or sid in sids_without_sha or not sha:
            seen_source_keys.add((sid, sha))
            active_sources.append(_clone_json(dict(item)))
    if active_sources:
        merged["active_source_files"] = active_sources

    active_sources_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in active_sources:
        active_sources_by_id.setdefault(str(item.get("source_id", "")), []).append(item)

    merged_sources: list[dict[str, Any]] = []
    ordered_sids = list(dict.fromkeys([*old_sources_by_id, *new_sources_by_id]))
    for sid in ordered_sids:
        if sid in retained_sids:
            old_entry = old_sources_by_id.get(sid)
            old_sha = str(old_entry.get("sha256", "")) if old_entry else ""
            sid_active = active_sources_by_id.get(sid, [])
            if old_entry and (not old_sha or (sid, old_sha) in active_file_keys or sid in sids_without_sha):
                merged_sources.append(old_entry)
            elif sid_active:
                merged_sources.append(_clone_json(sid_active[0]))
            elif old_entry:
                merged_sources.append(old_entry)
        elif sid in new_sources_by_id:
            merged_sources.append(new_sources_by_id[sid])
        elif sid in old_sources_by_id:
            merged_sources.append(old_sources_by_id[sid])
    if merged_sources:
        merged["source_files"] = merged_sources

    active_batch_ids = {
        str((record.get("attributes") or {}).get("batch_id", ""))
        for record in final_records
    }
    has_records_without_batch = "" in active_batch_ids
    candidate_declarations = [
        *(old_meta.get("active_declarations") or []),
        *([_declaration_entry(old_meta)] if not old_meta.get("active_declarations") else []),
        _declaration_entry(new_meta),
    ]
    active_declarations: list[dict[str, Any]] = []
    seen_decl_keys: set[tuple[str, str, str, str]] = set()
    for item in candidate_declarations:
        if not isinstance(item, Mapping):
            continue
        batch_key = str(item.get("import_batch_id", ""))
        if batch_key and not has_records_without_batch and batch_key not in active_batch_ids:
            continue
        dedup_key = (
            str(item.get("declaration_id", "")),
            batch_key,
            str(item.get("declared_by", "")),
            str(item.get("statement", "")),
        )
        if dedup_key in seen_decl_keys:
            continue
        seen_decl_keys.add(dedup_key)
        active_declarations.append(_clone_json(dict(item)))
    if active_declarations:
        merged["active_declarations"] = active_declarations
        old_batch_id = str(old_meta.get("import_batch_id", "") or "")
        if old_batch_id and not has_records_without_batch and old_batch_id not in active_batch_ids:
            fallback = _clone_json(active_declarations[0])
            fallback_batch = fallback.pop("import_batch_id", None)
            merged["human_declaration"] = fallback
            if fallback_batch:
                merged["import_batch_id"] = fallback_batch

    return merged


# --------------------------------------------------------------------------- 主流程
class ImportIncompleteError(ValueError):
    """导入未完整完成；旧输出必须保持可用。"""

    def __init__(self, message: str, *, diagnostics_path: Path | None = None):
        self.diagnostics_path = diagnostics_path
        super().__init__(message)


def _copy_row_image(row_image: Any, record_id: str, image_dir: Path) -> str:
    """把可用图片写进暂存目录；同名异内容拒绝覆盖。"""

    if row_image is None or getattr(row_image, "status", "") != STATUS_AVAILABLE:
        return ""
    data = getattr(row_image, "data", b"") or b""
    sha = getattr(row_image, "sha256", "") or hashlib.sha256(data).hexdigest()
    suffix = str(getattr(row_image, "image_format", "") or "bin").lower()
    if not re.fullmatch(r"[a-z0-9]{1,10}", suffix):
        suffix = "bin"
    target = image_dir / f"{record_id}.{sha[:8]}.{suffix}"
    if target.exists() and target.read_bytes() != data:
        raise ValueError(f"同名图片已存在且内容不同，拒绝覆盖：{target.name}")
    if not target.exists():
        image_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return str(target.relative_to(image_dir.parent))


def _publish_staging(
    staging: Path, out_dir: Path, files: Sequence[Path], *, expected_records_sha256: str | None = None
) -> None:
    """完整 generation + 单指针提交；CAS 防止并发合并丢更新。"""
    try:
        publish_staging(staging, out_dir, files, expected_records_sha256=expected_records_sha256)
    except PublishError as exc:
        raise ImportIncompleteError(f"发布输出失败，旧输出未改动：{exc}") from exc


def _parse_legacy_source(
    *,
    source: SourceMeta,
    workbook: Workbook,
    request: ImportRequest,
    batch_id: str,
    success_basis: str,
    image_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """旧多页签模板解析：保持既有 P04/P05 行为。"""

    file_report: dict[str, Any] = {
        "source_id": source.source_id,
        "filename": source.original_filename,
        "path": str(source.path),
        "sha256": source.sha256,
        "bytes": source.bytes,
        "format": FORMAT_LEGACY,
        "status": "parsed",
        "declaration_sheet": None,
        "header_row": None,
        "source_record_count": 0,
        "imported_count": 0,
        "skipped_count": 0,
        "ignored_row_count": 0,
        "image": {},
        "layout_warnings": [],
        "workbook_sheets": [sheet.name for sheet in workbook.sheets],
        "task_issues": [],
        "row_reports": [],
        "error": None,
    }
    records_out: list[dict[str, Any]] = []
    row_reports_out: list[dict[str, Any]] = []
    image_copy_failures: list[dict[str, Any]] = []
    try:
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
            extraction = extract_declarations(workbook, sheet_label=request.declaration_label)
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
        image_by_row, image_info, image_issues = _build_image_index(workbook, extraction, rows)
        file_report["image"] = image_info
        file_report["task_issues"].extend(image_issues)
        for record in records:
            link = mapped.link(record.row) if mapped is not None else None
            row_image = image_by_row.get(record.row)
            image_output_path = ""
            record_id = _record_id(
                source.source_id,
                record.sheet,
                record.row,
                _normalize(record.value("contract")),
                _normalize(record.value("item")),
            )
            if (
                _normalize(record.value("name"))
                and _normalize(record.value("hs"))
                and request.copy_images
                and row_image is not None
                and row_image.status == STATUS_AVAILABLE
            ):
                try:
                    image_output_path = _copy_row_image(row_image, record_id, image_dir)
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
            row_reports_out.append(row_report)
            if payload:
                records_out.append(payload)
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
    return file_report, records_out, row_reports_out, image_copy_failures


def _parse_consolidated_source(
    *,
    source: SourceMeta,
    workbook: Workbook,
    request: ImportRequest,
    mapping: Mapping[str, Any],
    batch_id: str,
    success_basis: str,
    image_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    file_report: dict[str, Any] = {
        "source_id": source.source_id,
        "filename": source.original_filename,
        "path": str(source.path),
        "sha256": source.sha256,
        "bytes": source.bytes,
        "format": FORMAT_CONSOLIDATED,
        "status": "parsed",
        "declaration_sheet": None,
        "header_row": None,
        "source_record_count": 0,
        "imported_count": 0,
        "skipped_count": 0,
        "ignored_row_count": 0,
        "image": {},
        "layout_warnings": [],
        "workbook_sheets": [sheet.name for sheet in workbook.sheets],
        "task_issues": [],
        "row_reports": [],
        "skipped_rows": [],
        "mapping": {},
        "data_quality": {},
        "row_classification": {},
        "error": None,
    }
    records_out: list[dict[str, Any]] = []
    row_reports_out: list[dict[str, Any]] = []
    image_copy_failures: list[dict[str, Any]] = []
    try:
        parsed = parse_consolidated_workbook(
            workbook,
            path=source.path,
            source_id=source.source_id,
            original_filename=source.original_filename,
            dataset_kind=request.dataset_kind,
            mapping=mapping,
            image_root=Path(request.image_root) if request.image_root is not None else None,
        )
    except ConsolidatedFormatError as format_error:
        file_report["status"] = "parse_failed"
        file_report["error"] = {
            "code": format_error.code,
            "message": str(format_error),
            "details": format_error.details,
        }
        return file_report, records_out, row_reports_out, image_copy_failures
    except OoxmlError as parse_error:
        file_report["status"] = "parse_failed"
        file_report["error"] = {
            "code": getattr(parse_error, "code", type(parse_error).__name__),
            "message": str(parse_error),
        }
        return file_report, records_out, row_reports_out, image_copy_failures
    except Exception as unexpected:
        file_report["status"] = "parse_failed"
        file_report["error"] = {
            "code": type(unexpected).__name__,
            "message": str(unexpected),
            "traceback": traceback.format_exc(limit=5),
        }
        return file_report, records_out, row_reports_out, image_copy_failures

    file_report["declaration_sheet"] = parsed.layout.sheet
    file_report["header_row"] = parsed.layout.header_row
    file_report["mapping"] = {
        "sheet": parsed.layout.sheet,
        "header_row": parsed.layout.header_row,
        "mapping_source": parsed.layout.mapping_source,
        "columns": {
            name: {
                "header": match.header,
                "column": match.letter,
                "hidden": match.hidden,
            }
            for name, match in parsed.layout.columns.items()
        },
        "duplicate_columns": parsed.layout.duplicate_columns,
        "unmatched_headers": parsed.layout.unmatched_headers,
        "hidden_columns": parsed.layout.hidden_columns,
        "hidden_rows": parsed.layout.hidden_rows,
        "header_candidates": parsed.layout.header_candidates,
        "merged_ranges": parsed.layout.merges,
    }
    file_report["data_quality"] = parsed.data_quality
    file_report["row_classification"] = parsed.row_classification
    file_report["image"] = parsed.image_info
    file_report["task_issues"] = list(parsed.task_issues)
    file_report["layout_warnings"] = [
        str(issue.get("message", "")) for issue in parsed.task_issues
    ]
    file_report["skipped_rows"] = list(parsed.skipped_rows)
    file_report["source_record_count"] = len(parsed.records)
    file_report["ignored_row_count"] = len(parsed.skipped_rows)
    for skipped in parsed.skipped_rows:
        file_report["row_reports"].append(skipped)
        row_reports_out.append(skipped)

    for record in parsed.records:
        row_image = record.row_image
        image_status = record.image_status_override or parsed.image_info.get(
            "column_status", IMAGE_COLUMN_MISSING
        )
        image_output_path = ""
        if (
            request.copy_images
            and row_image is not None
            and getattr(row_image, "status", "") == STATUS_AVAILABLE
        ):
            try:
                image_output_path = _copy_row_image(row_image, record.record_id, image_dir)
            except (OSError, ValueError) as image_error:
                image_copy_failures.append(
                    {
                        "record_id": record.record_id,
                        "source": f"{source.source_id}/{record.sheet}!{record.row}",
                        "message": str(image_error),
                    }
                )
        payload, row_report, issues = _build_record(
            record=record,
            link=None,
            source=source,
            batch_id=batch_id,
            dataset_kind=request.dataset_kind,
            success_basis=success_basis,
            image=row_image,
            image_column_status=image_status,
            image_output_path=image_output_path,
        )
        row_report["source"]["filename"] = source.original_filename
        file_report["row_reports"].append(row_report)
        row_reports_out.append(row_report)
        if payload:
            records_out.append(payload)
            file_report["imported_count"] += 1
        else:
            file_report["skipped_count"] += 1
    return file_report, records_out, row_reports_out, image_copy_failures


def import_history_templates(request: ImportRequest) -> ImportOutcome:
    """Build a private candidate and always clean staging; failed runs never publish."""
    request.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=request.out_dir) as raw_staging:
        staging = Path(raw_staging)
        try:
            return _import_history_templates(request, staging)
        except Exception as exc:
            diagnostic = request.out_dir / f"import_failure-{staging.name.removeprefix('.staging-')}.json"
            detail = staging / "import_report.partial.json"
            _write_json(diagnostic, {
                "status": "failed", "published": False, "generated_at": _utc_now(),
                "error": {"code": type(exc).__name__, "message": str(exc)},
                "details": _read_json(detail) if detail.is_file() else None,
            })
            raise


def _import_history_templates(request: ImportRequest, staging: Path) -> ImportOutcome:
    if request.dataset_kind not in {"real", "synthetic"}:
        raise ValueError("dataset_kind 只能是 real 或 synthetic")
    if request.input_format not in FORMAT_CHOICES:
        raise ValueError(f"input_format 只能是 {FORMAT_CHOICES}")
    if request.dataset_kind == "real" and not request.declared_success:
        raise ValueError(
            "真实历史数据导入必须显式声明 --declared-success（Jay 人工声明历史成功案例可作为参考）；"
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
        raise ValueError("至少需要一个历史数据文件")
    source_ids = [meta.source_id for meta in source_metas]
    if len(set(source_ids)) != len(source_ids):
        duplicated = sorted({item for item in source_ids if source_ids.count(item) > 1})
        raise ValueError(
            f"同一批文件 source_id 重复：{duplicated}；请用 manifest 固定 source_id 或改名后分批导入"
        )

    mapping = load_mapping_config(request.mapping)
    if request.sheet:
        mapping["sheet"] = request.sheet
    if request.header_row:
        mapping["header_row"] = int(request.header_row)

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

    image_dir = staging / "images"
    if request.copy_images:
        image_dir.mkdir(parents=True, exist_ok=True)

    # 先打开工作簿并识别格式；格式有歧义时拒绝并提示显式选择，不能猜。
    file_inputs: list[tuple[SourceMeta, Workbook | None, str, dict[str, Any]]] = []
    file_reports_pre: dict[str, dict[str, Any]] = {}
    for source in source_metas:
        file_report = {
            "source_id": source.source_id,
            "filename": source.original_filename,
            "path": str(source.path),
            "sha256": source.sha256,
            "bytes": source.bytes,
            "format": "",
            "status": "parsed",
            "declaration_sheet": None,
            "header_row": None,
            "source_record_count": 0,
            "imported_count": 0,
            "skipped_count": 0,
            "ignored_row_count": 0,
            "image": {},
            "layout_warnings": [],
            "workbook_sheets": [],
            "task_issues": [],
            "row_reports": [],
            "error": None,
        }
        try:
            workbook = Workbook.open(source.path)
            file_report["workbook_sheets"] = [sheet.name for sheet in workbook.sheets]
            detected: str | None = None
            try:
                detected = detect_workbook_format(workbook, mapping)
            except ConsolidatedFormatError:
                detected = None
            if request.input_format == FORMAT_AUTO:
                if detected is None:
                    raise ConsolidatedFormatError(
                        "input_format_unknown",
                        "无法自动识别输入格式；请显式选择 --input-format legacy-template 或 consolidated",
                    )
                chosen = detected
            else:
                chosen = request.input_format
                if detected is not None and detected != chosen:
                    raise ConsolidatedFormatError(
                        "input_format_mismatch",
                        f"显式指定 {chosen}，但文件结构更像 {detected}；请核对 --input-format",
                    )
            file_report["format"] = chosen
            file_inputs.append((source, workbook, chosen, {}))
        except ConsolidatedFormatError as format_error:
            raise ValueError(f"{source.original_filename}: {format_error}") from format_error
        except OoxmlError as parse_error:
            file_report["status"] = "parse_failed"
            file_report["error"] = {
                "code": getattr(parse_error, "code", type(parse_error).__name__),
                "message": str(parse_error),
            }
            file_reports_pre[source.source_id] = file_report
            file_inputs.append((source, None, request.input_format, file_report))
        except Exception as unexpected:
            file_report["status"] = "parse_failed"
            file_report["error"] = {
                "code": type(unexpected).__name__,
                "message": str(unexpected),
                "traceback": traceback.format_exc(limit=5),
            }
            file_reports_pre[source.source_id] = file_report
            file_inputs.append((source, None, request.input_format, file_report))

    parsed_formats = {fmt for _, workbook, fmt, _ in file_inputs if workbook is not None}
    if len(parsed_formats) > 1:
        raise ValueError(
            "一次导入只支持一种输入格式（legacy-template 或 consolidated）；"
            "请分开输出目录分别导入，避免两套身份语义混在一个数据集"
        )
    input_format = next(iter(parsed_formats), request.input_format)
    if input_format == FORMAT_LEGACY and (request.mapping or request.sheet or request.header_row or request.image_root):
        raise ValueError("--mapping/--sheet/--header-row/--image-root 仅用于 consolidated，旧模板不静默忽略这些参数")

    file_reports: list[dict[str, Any]] = []
    all_new_records: list[dict[str, Any]] = []
    all_row_reports: list[dict[str, Any]] = []
    image_copy_failures: list[dict[str, Any]] = []
    parsed_source_ids: set[str] = set()
    for source, workbook, chosen, pre_report in file_inputs:
        if workbook is None:
            file_reports.append(pre_report)
            continue
        if chosen == FORMAT_CONSOLIDATED:
            file_report, records, row_reports, copy_failures = _parse_consolidated_source(
                source=source,
                workbook=workbook,
                request=request,
                mapping=mapping,
                batch_id=batch_id,
                success_basis=success_basis,
                image_dir=image_dir,
            )
        else:
            file_report, records, row_reports, copy_failures = _parse_legacy_source(
                source=source,
                workbook=workbook,
                request=request,
                batch_id=batch_id,
                success_basis=success_basis,
                image_dir=image_dir,
            )
        # 真实文件导入前后 SHA-256 必须相同；导入期间被改动则整份文件拒绝。
        try:
            after_sha = _file_sha256(source.path)
        except OSError as exc:
            after_sha = ""
            file_report["sha256_after"] = ""
            file_report["task_issues"].append(
                {
                    "code": "source_recheck_failed",
                    "message": f"导入后无法重新计算文件哈希：{exc}",
                    "severity": "error",
                    "blocking": True,
                }
            )
        if not after_sha or after_sha != source.sha256 or workbook.source_sha256 != source.sha256:
            file_report["status"] = "parse_failed"
            file_report["sha256_unchanged"] = False
            file_report["error"] = {
                "code": "source_changed_during_import" if after_sha else "source_recheck_failed",
                "message": "无法证明输入文件在导入过程中未变化；本文件记录被丢弃，旧输出未改动",
                "sha256_before": source.sha256,
                "sha256_after": after_sha,
            }
            records = []
            row_reports = []
            image_copy_failures.extend(copy_failures)
        else:
            file_report["sha256_after"] = after_sha or source.sha256
            file_report["sha256_unchanged"] = (after_sha or source.sha256) == source.sha256
            image_copy_failures.extend(copy_failures)
            if file_report["status"] == "parsed":
                parsed_source_ids.add(source.source_id)
        file_report["importable_count"] = file_report.get("imported_count", 0)
        file_report["written_count"] = 0
        file_reports.append(file_report)
        all_new_records.extend(records)
        all_row_reports.extend(row_reports)

    if not any(item["status"] == "parsed" for item in file_reports):
        diagnostics = request.out_dir / f"import_failure-{_utc_now().replace(':', '').replace('+', '_')}.json"
        _write_json(
            diagnostics,
            {
                "batch_id": batch_id,
                "generated_at": _utc_now(),
                "input_format": input_format,
                "files": file_reports,
                "message": "所有输入文件解析失败；旧输出未改动",
            },
        )
        raise ImportIncompleteError(
            f"所有输入文件解析失败；诊断已写入 {diagnostics}；旧输出未改动",
            diagnostics_path=diagnostics,
        )

    if request.copy_images and image_copy_failures:
        diagnostics = staging / "import_report.partial.json"
        _write_json(
            diagnostics,
            {
                "batch_id": batch_id,
                "generated_at": _utc_now(),
                "image_copy_failures": image_copy_failures,
                "files": file_reports,
                "message": "图片复制失败；为避免留下被误认为完整成功的数据集，本次不发布",
            },
        )
        raise ImportIncompleteError(
            f"图片复制失败 {len(image_copy_failures)} 个；本次不发布，旧输出未改动"
        )

    records_path = request.out_dir / "history_records.json"
    # 同一次读取的字节同时用于解析与CAS；不能读旧payload后重新读新hash。
    existing_bytes = records_path.read_bytes() if records_path.is_file() else None
    expected_records_sha = hashlib.sha256(existing_bytes).hexdigest() if existing_bytes is not None else ""
    existing_snapshot = _read_existing(records_path, data=existing_bytes)
    existing_payload = None if request.fresh else existing_snapshot
    previous_total = 0 if request.fresh else (len(existing_snapshot.get("records", [])) if existing_snapshot else 0)
    previous_dataset = (existing_snapshot or {}).get("dataset") or {}
    if existing_snapshot and previous_dataset:
        previous_synthetic = previous_dataset.get("synthetic")
        current_synthetic = request.dataset_kind == "synthetic"
        if previous_synthetic is not None and bool(previous_synthetic) != current_synthetic and not (
            request.fresh and request.allow_dataset_replace
        ):
            raise ValueError(
                "已有输出与本次 dataset_kind 不一致（真实/合成不能混在同一个历史 JSON）；"
                "请换 --out-dir，或显式 --fresh --allow-dataset-replace 覆盖"
            )
        previous_formats = previous_dataset.get("input_formats")
        if not previous_formats and previous_dataset:
            previous_formats = [FORMAT_LEGACY]
        current_formats = sorted(
            {
                str((report.get("format") or input_format))
                for report in file_reports
                if report.get("status") == "parsed"
            }
        )
        if previous_formats and current_formats and sorted(set(previous_formats)) != current_formats:
            if not request.fresh:
                raise ValueError(
                    f"已有输出的输入格式 {previous_formats} 与本次 {current_formats} 不一致；"
                    "请换 --out-dir，或显式 --fresh --allow-dataset-replace"
                )
        previous_ids = {str(item.get("source_id", "")) for item in previous_dataset.get("source_files", []) if isinstance(item, dict)}
        current_ids = {meta.source_id for meta in source_metas}
        if not request.fresh and previous_ids and previous_ids != current_ids:
            raise ValueError("显式输出目录已有不同来源文件集合；不能静默丢弃来源清单，请换目录或 --fresh --allow-dataset-replace")
        if request.fresh and not request.allow_dataset_replace:
            previous_sources = {
                str(item.get("source_id", "")): str(item.get("filename", ""))
                for item in previous_dataset.get("source_files", [])
                if isinstance(item, dict)
            }
            current_sources = {
                meta.source_id: meta.original_filename for meta in source_metas
            }
            if previous_sources and previous_sources != current_sources:
                raise ValueError(
                    "--fresh 会覆盖不同来源数据集；如确认替换，请加 --allow-dataset-replace"
                )

    if existing_payload and input_format == FORMAT_CONSOLIDATED and existing_payload.get("records"):
        prior_identity_version = previous_dataset.get("identity_version")
        record_versions = {
            (record.get("attributes") or {}).get("identity_version")
            for record in existing_payload.get("records", [])
        }
        if prior_identity_version != CONSOLIDATED_IDENTITY_VERSION or record_versions != {CONSOLIDATED_IDENTITY_VERSION}:
            raise ValueError(
                "已有汇总数据身份版本缺失或不兼容；拒绝普通merge，避免整批新增。"
                "请保留旧快照后显式 --fresh 重建（不同来源另需 --allow-dataset-replace）"
            )

    duplicate_in_batch: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    actions: dict[str, str] = {}
    added_ids: set[str] = set()
    if input_format == FORMAT_CONSOLIDATED:
        final_records, duplicates, writes, conflicts, missing, actions, added_ids = (
            _merge_consolidated_records(
                existing_payload,
                all_new_records,
                replace_changed=request.replace_changed,
                keep_business_duplicates=request.keep_business_duplicates,
                accept_conflicts=request.accept_conflicts,
                current_source_ids=parsed_source_ids,
            )
        )
        duplicate_in_batch = [
            item for item in duplicates if item.get("kind") == "duplicate_within_batch"
        ]
        duplicate_by_id = {
            str(item.get("record_id", "")): item
            for item in duplicates
            if item.get("kind") != "duplicate_within_batch"
        }
        for row_report in all_row_reports:
            if row_report.get("status") != "imported":
                continue
            record_id = str(row_report.get("record_id", ""))
            action = actions.get(record_id)
            duplicate = duplicate_by_id.get(record_id)
            if action is None:
                continue
            row_report["merge"] = {"action": action}
            if duplicate is not None:
                row_report["duplicate"] = duplicate
            if action == "replaced":
                row_report["status"] = "replaced"
            elif action == "duplicate":
                row_report["status"] = "duplicate"
            elif action == "duplicate_business_content":
                row_report["status"] = "duplicate_business_content"
            elif action == "duplicate_within_batch":
                row_report["status"] = "duplicate"
            elif action == "changed_kept_existing":
                row_report["status"] = "changed_skipped"
            elif action == "conflict_kept_existing":
                row_report["status"] = "conflict_skipped"
            elif action == "business_key_variant":
                row_report["status"] = "imported"
    else:
        final_records, duplicates, writes = _merge_records(
            existing_payload,
            all_new_records,
            replace_changed=request.replace_changed,
            keep_business_duplicates=request.keep_business_duplicates,
        )
        duplicate_by_id = {
            str(item.get("record_id", "")): item
            for item in duplicates
            if item.get("kind") != "duplicate_within_batch"
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
            elif action == "provenance_updated":
                row_report["status"] = "duplicate"
            elif action in {"kept", "kept_existing"}:
                row_report["status"] = "duplicate"
            elif duplicate.get("kind") == "duplicate_business_content":
                row_report["status"] = "duplicate_business_content"
            elif duplicate.get("kind") == "changed":
                row_report["status"] = "changed_skipped"
        added_ids = {
            str(payload.get("record_id", ""))
            for payload in writes
            if not any(
                item.get("action") == "replaced"
                and str(item.get("record_id", "")) == str(payload.get("record_id", ""))
                for item in duplicates
            )
        }
    review_state_repairs = _enforce_stored_hs_review(final_records)
    written_ids = {str(payload.get("record_id", "")) for payload in writes}
    for file_report in file_reports:
        file_report["importable_count"] = file_report.get("imported_count", 0)
        file_report["written_count"] = sum(
            1
            for row in file_report["row_reports"]
            if str(row.get("record_id", "")) in written_ids
        )

    records_added = len(added_ids)
    records_replaced = sum(1 for item in duplicates if item.get("action") == "replaced")
    records_conflicts = sum(
        1 for item in conflicts if item.get("kind") == "business_key_conflict"
    )
    business_key_variants = sum(
        1 for item in conflicts if item.get("kind") == "business_key_variant"
    )
    records_rejected = sum(item.get("skipped_count", 0) for item in file_reports)
    records_ignored = sum(item.get("ignored_row_count", 0) for item in file_reports)
    records_needs_review = sum(
        1
        for record in final_records
        if str((record.get("attributes") or {}).get("needs_review", "")).casefold() == "true"
    )
    reconciliation_ok = previous_total + records_added == len(final_records)
    status = "complete"
    if any(item["status"] != "parsed" for item in file_reports):
        status = "partial"
    if records_rejected or records_conflicts:
        status = "partial"

    input_dataset = _dataset_payload(
        batch_id=batch_id,
        dataset_kind=request.dataset_kind,
        dataset_id=request.dataset_id,
        source_count=len(source_metas),
        record_count=len(final_records),
        declaration=declaration,
        input_formats=[input_format],
        source_files=[
            {
                "source_id": meta.source_id,
                "filename": meta.original_filename,
                "sha256": meta.sha256,
                "bytes": meta.bytes,
                "path": str(meta.path),
            }
            for meta in source_metas
        ],
    )
    dataset = _merge_dataset_metadata(
        (existing_payload or {}).get("dataset") if existing_payload and existing_payload.get("records") else None,
        input_dataset,
        final_records,
        writes=writes,
        duplicates=duplicates,
    )
    records_payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "records": final_records,
    }
    staging_records = staging / "history_records.json"
    _write_json(staging_records, records_payload)

    if not request.skip_validation:
        validation = validate_history_queries(staging_records)
        validation["status"] = "executed"
        validation["records_path"] = str(records_path)
        if not validation.get("all_passed"):
            status = "partial"
    else:
        # 新快照明确未运行；不能把上一版成功验证带到新数据集。
        validation = {
            "schema_version": "p15.history-import.validation.1",
            "status": "skipped", "generated_at": _utc_now(),
            "records_path": str(records_path), "dataset": dataset,
            "checks": {}, "cases": [], "all_passed": False,
            "missing_required_cases": ["hit", "no_match", "key_attribute_diff"],
        }

    image_totals: dict[str, int] = {}
    for file_report in file_reports:
        for key, value in (file_report.get("image", {}).get("counts") or {}).items():
            image_totals[key] = image_totals.get(key, 0) + int(value)
    report = {
        "schema_version": "p15.history-import.report.1",
        "batch_id": batch_id,
        "dataset_kind": request.dataset_kind,
        "input_format": input_format,
        "status": status,
        "generated_at": _utc_now(),
        "out_dir": str(request.out_dir),
        "records_path": str(records_path),
        "human_declaration": declaration.as_dict(batch_id) if declaration else None,
        "input_dataset": input_dataset,
        "dataset": dataset,
        "source_files": [
            {
                "source_id": meta.source_id,
                "filename": meta.original_filename,
                "path": str(meta.path),
                "sha256": meta.sha256,
                "bytes": meta.bytes,
                "format": next(
                    (item["format"] for item in file_reports if item["source_id"] == meta.source_id),
                    input_format,
                ),
            }
            for meta in source_metas
        ],
        "files": file_reports,
        "totals": {
            "files": len(file_reports),
            "parsed_files": sum(1 for item in file_reports if item["status"] == "parsed"),
            "parse_failed_files": sum(1 for item in file_reports if item["status"] == "parse_failed"),
            "source_records": sum(item.get("source_record_count", 0) for item in file_reports),
            "records_imported_this_run": len(writes),
            "records_added": records_added,
            "records_replaced": records_replaced,
            "records_skipped": records_rejected,
            "records_ignored": records_ignored,
            "records_rejected": records_rejected,
            "records_needs_review": records_needs_review,
            "records_review_state_repaired": len(review_state_repairs),
            "records_conflicts": records_conflicts,
            "records_business_key_variants": business_key_variants,
            "records_missing_from_source": len(missing),
            "records_duplicate": sum(1 for item in duplicates if item.get("kind") == "duplicate"),
            "records_changed": sum(1 for item in duplicates if item.get("kind") == "changed"),
            "records_duplicate_within_batch": len(duplicate_in_batch),
            "records_duplicate_business_content": sum(
                1 for item in duplicates if item.get("kind") == "duplicate_business_content"
            ),
            "records_provenance_updated": sum(
                1 for item in duplicates if item.get("action") == "provenance_updated"
            ),
            "previous_total_in_output": previous_total,
            "records_total_in_output": len(final_records),
            "reconciliation_ok": reconciliation_ok,
            "image_status": image_totals,
            "image_copy_failures": len(image_copy_failures),
        },
        "review_state_repairs": review_state_repairs,
        "duplicates": duplicates,
        "duplicate_within_batch": duplicate_in_batch,
        "conflicts": conflicts,
        "missing_from_source": missing,
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
                "content identity + occurrence; same content -> duplicate/skip; same change anchor with "
                "different content -> changed/keep existing unless --replace-changed; same business key "
                "with different content -> conflict/keep existing unless --accept-conflicts; records "
                "missing from source are reported and kept, never physically deleted automatically"
            ),
            "identity_policy": (
                "consolidated: business-field content hash + occurrence, not row number and not file hash; "
                "optional unique original_record_id takes precedence; legacy: source file + sheet + row + "
                "contract + item"
            ),
            "merged_cells": "only anchor value is read; continuation cells stay empty and are reported",
            "formulas": "cached value is used when present; missing cache is reported and never evaluated",
            "images": "DISPIMG/floating images reuse P05; external URL never fetched; external local path "
            "only under explicit --image-root with traversal/symlink checks",
            "database": "not_connected; output is a P15-compatible JSON snapshot for later DB/HistoryRecordStore integration",
        },
    }
    report_json_path = staging / "import_report.json"
    report_md_path = staging / "import_report.md"
    _write_json(report_json_path, report)
    report_md_path.write_text(render_markdown_report(report), encoding="utf-8")

    validation_json_path = None
    validation_md_path = None
    publish_files: list[Path] = [staging_records, report_json_path, report_md_path]
    if validation is not None:
        validation_json_path = staging / "query_validation.json"
        validation_md_path = staging / "query_validation.md"
        _write_json(validation_json_path, validation)
        validation_md_path.write_text(
            render_validation_markdown(validation), encoding="utf-8"
        )
        report["validation"] = {
            "path": str(request.out_dir / "query_validation.json"),
            "status": validation.get("status", "executed"),
            "checks": validation["checks"],
        }
        _write_json(report_json_path, report)
        publish_files.extend([validation_json_path, validation_md_path])
    if image_dir.is_dir():
        publish_files.extend(sorted(image_dir.rglob("*")))

    try:
        _publish_staging(staging, request.out_dir, publish_files, expected_records_sha256=expected_records_sha)
    except Exception as publish_error:
        try:
            import shutil

            shutil.rmtree(staging)
        except OSError:
            pass
        if isinstance(publish_error, ImportIncompleteError):
            raise
        raise ImportIncompleteError(
            f"发布输出失败，旧输出已回滚：{type(publish_error).__name__}: {publish_error}"
        ) from publish_error
    try:
        import shutil

        shutil.rmtree(staging)
    except OSError:
        pass

    final_validation_json = request.out_dir / "query_validation.json" if validation else None
    final_validation_md = request.out_dir / "query_validation.md" if validation else None
    return ImportOutcome(
        out_dir=request.out_dir,
        records_path=records_path,
        report_json_path=request.out_dir / "import_report.json",
        report_md_path=request.out_dir / "import_report.md",
        validation_json_path=final_validation_json,
        validation_md_path=final_validation_md,
        report=report,
    )


# --------------------------------------------------------------------------- 查询验证
def _record_needs_review(record: Mapping[str, Any]) -> bool:
    attributes = record.get("attributes") or {}
    if str(attributes.get("needs_review", "")).strip().casefold() in {"true", "1", "yes", "y", "是"}:
        return True
    return bool(str(attributes.get("blocking_issue_codes", "")).strip())


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
    # 默认检索会排除 needs_review 记录；验证用例也必须从可命中记录中选，
    # 否则会把“被排除”误判成 hit-basic 失败。
    usable_records = [record for record in records if not _record_needs_review(record)]
    if not usable_records:
        usable_records = []
    hit_record = next(
        (
            record
            for record in usable_records
            if record.get("historical_name") and record.get("historical_hs")
        ),
        usable_records[0] if usable_records else None,
    )
    if hit_record is None:
        return []
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
            for record in usable_records
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

    stable_records_path = records_path.resolve()
    records_bytes = stable_records_path.read_bytes()
    validation_records_sha = hashlib.sha256(records_bytes).hexdigest()
    payload = json.loads(records_bytes)
    records = payload.get("records", []) if isinstance(payload, dict) else payload
    cases = _read_json(queries_path) if queries_path else generate_query_cases(records)
    if isinstance(cases, dict):
        cases = cases.get("queries", [])
    source = HistorySearchSource.load(stable_records_path)
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
                        "needs_review": candidate.needs_review,
                        "blocking_issue_codes": candidate.blocking_issue_codes,
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
        # standalone validate 也必须发布新 generation，不能穿透稳定 symlink
        # 改写当前快照中的 Markdown（破坏不可变性）。
        with tempfile.TemporaryDirectory(prefix=".validation-", dir=out_dir) as raw:
            candidate = Path(raw)
            _write_json(candidate / "query_validation.json", validation)
            (candidate / "query_validation.md").write_text(
                render_validation_markdown(validation), encoding="utf-8"
            )
            is_same_output = records_path.absolute() == (out_dir / "history_records.json").absolute()
            _publish_staging(candidate, out_dir, [
                candidate / "query_validation.json", candidate / "query_validation.md",
            ], expected_records_sha256=validation_records_sha if is_same_output else None)
    return validation


# --------------------------------------------------------------------------- 报告
def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _mapping_lines(file_report: Mapping[str, Any]) -> list[str]:
    mapping = file_report.get("mapping") or {}
    quality = file_report.get("data_quality") or {}
    if not mapping and not quality:
        return []
    lines: list[str] = []
    if mapping:
        lines.extend(
            [
                f"### {file_report.get('source_id')} / {mapping.get('sheet')}",
                "",
                f"- 表头行：{mapping.get('header_row')}；映射来源：{mapping.get('mapping_source')}",
                f"- 未匹配表头：{mapping.get('unmatched_headers') or '无'}",
                f"- 重复列：{mapping.get('duplicate_columns') or '无'}",
                f"- 隐藏列：{mapping.get('hidden_columns') or '无'}；隐藏行：{mapping.get('hidden_rows') or '无'}",
                "",
                _md_table(
                    ["字段", "真实列名", "列", "隐藏"],
                    [
                        (name, item.get("header"), item.get("column"), item.get("hidden"))
                        for name, item in (mapping.get("columns") or {}).items()
                    ],
                ),
                "",
            ]
        )
    if quality:
        hs = quality.get("hs") or {}
        tax = quality.get("tax_classification_code") or {}
        lines.extend(
            [
                "### 数据质量盘点",
                "",
                _md_table(
                    ["指标", "值"],
                    [
                        ("行分类", quality.get("row_classification", {})),
                        ("合并单元格范围/单元格数", f"{quality.get('merged_range_count', 0)} / {quality.get('merged_cell_count', 0)}"),
                        ("公式列", quality.get("formula_cells", {})),
                        ("公式缺缓存", quality.get("formula_cache_missing", {})),
                        ("外部链接部件", quality.get("external_link_parts", [])),
                        ("HS 长度分布", hs.get("lengths", {})),
                        ("HS 非 10 位行", hs.get("non_10_digit_rows", [])),
                        ("HS 文本/数值单元格", f"{hs.get('text_cells', 0)} / {hs.get('numeric_cells', 0)}"),
                        ("税收分类代码长度分布", tax.get("normalized_lengths", {})),
                        ("税收分类代码非 19 位行数", len(tax.get("non_19_digit_rows", []))),
                        ("完全同内容组/多出行", f"{quality.get('duplicate_content_groups', 0)} / {quality.get('duplicate_content_extra_rows', 0)}"),
                        ("空必填字段", quality.get("empty_required", {})),
                        ("空可选字段", quality.get("empty_optional", {})),
                        ("缺失列", quality.get("column_missing", {})),
                        ("放行情况原文值", quality.get("customs_release_status_values", {})),
                        ("图片列状态", quality.get("image_column_status", "")),
                    ],
                ),
                "",
            ]
        )
        if tax.get("non_19_digit_rows"):
            lines.append(
                "税收分类代码格式异常（前 10 条；只报告，不用于身份、不猜补）："
            )
            lines.append("")
            lines.append(
                _md_table(
                    ["行", "原文"],
                    [
                        (item.get("row"), item.get("raw"))
                        for item in tax["non_19_digit_rows"][:10]
                    ],
                )
            )
            lines.append("")
        if quality.get("duplicate_content_samples"):
            lines.append("完全同内容行组（前 10 组；保留多行并报告，不做危险合并）：")
            lines.append("")
            lines.append(
                _md_table(
                    ["行号"],
                    [(", ".join(str(row) for row in item.get("rows", [])),) for item in quality["duplicate_content_samples"][:10]],
                )
            )
            lines.append("")
    return lines


def render_markdown_report(report: Mapping[str, Any]) -> str:
    totals = report.get("totals", {})
    declaration = report.get("human_declaration")
    fmt = report.get("input_format", "")
    source_label = "汇总表商品行" if fmt == FORMAT_CONSOLIDATED else "P04 源记录"
    lines = [
        f"# 历史数据导入报告（{report.get('batch_id')}）",
        "",
        f"- 输入格式：`{fmt}`；批次状态：`{report.get('status')}`",
        f"- 数据集类型：`{report.get('dataset_kind')}`（真实/合成标记写入 P15 dataset.synthetic）",
        f"- 生成时间：{report.get('generated_at')}",
        f"- 输出记录：`{report.get('records_path')}`",
        f"- 对账：上一版 {totals.get('previous_total_in_output', 0)} + 本次新增 "
        f"{totals.get('records_added', 0)} = 输出 {totals.get('records_total_in_output', 0)}"
        f"（reconciliation_ok={totals.get('reconciliation_ok')}）",
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
                    (source_label, totals.get("source_records", 0)),
                    ("本次新增", totals.get("records_added", 0)),
                    ("替换", totals.get("records_replaced", 0)),
                    ("重复", totals.get("records_duplicate", 0)),
                    ("跨来源业务重复", totals.get("records_duplicate_business_content", 0)),
                    ("变化", totals.get("records_changed", 0)),
                    ("冲突", totals.get("records_conflicts", 0)),
                    ("业务键变体", totals.get("records_business_key_variants", 0)),
                    ("来源中消失（保留旧记录）", totals.get("records_missing_from_source", 0)),
                    ("拒绝/跳过", totals.get("records_rejected", 0)),
                    ("忽略行（空/合计/说明/重复表头）", totals.get("records_ignored", 0)),
                    ("待核记录", totals.get("records_needs_review", 0)),
                    ("按保留HS恢复待核", totals.get("records_review_state_repaired", 0)),
                    ("批内重复", totals.get("records_duplicate_within_batch", 0)),
                    ("采集痕迹更新", totals.get("records_provenance_updated", 0)),
                    ("输出总记录", totals.get("records_total_in_output", 0)),
                    ("图片状态", totals.get("image_status", {})),
                ],
            ),
            "",
            "## 文件与图片",
            "",
            _md_table(
                ["source_id", "文件", "格式", "状态", "源记录", "可导入", "本次写入", "拒绝", "忽略", "图片状态"],
                [
                    (
                        item.get("source_id"),
                        item.get("filename"),
                        item.get("format", fmt),
                        item.get("status"),
                        item.get("source_record_count", 0),
                        item.get("importable_count", 0),
                        item.get("written_count", 0),
                        item.get("skipped_count", 0),
                        item.get("ignored_row_count", 0),
                        item.get("image", {}).get("counts", {}),
                    )
                    for item in report.get("files", [])
                ],
            ),
            "",
        ]
    )
    for file_report in report.get("files", []):
        lines.extend(_mapping_lines(file_report))
    lines.extend(["## 重复/变化处理", ""])
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
                    for item in duplicates[:40]
                ],
            )
        )
        if len(duplicates) > 40:
            lines.append(
                f"\n（仅显示前 40 条，共 {len(duplicates)} 条；完整见 `import_report.json`）"
            )
    else:
        lines.append("无重复或变化记录。")
    conflicts = report.get("conflicts") or []
    lines.extend(["", "## 冲突", ""])
    if conflicts:
        lines.append(
            _md_table(
                ["kind", "action", "record_id", "说明"],
                [
                    (
                        item.get("kind"),
                        item.get("action"),
                        item.get("record_id"),
                        "业务键相同但内容不同；默认保留旧记录，不静默覆盖",
                    )
                    for item in conflicts[:40]
                ],
            )
        )
        if len(conflicts) > 40:
            lines.append(f"\n（仅显示前 40 条，共 {len(conflicts)} 条）")
    else:
        lines.append("无。")
    missing = report.get("missing_from_source") or []
    lines.extend(["", "## 来源中消失的记录（保留旧记录）", ""])
    if missing:
        lines.append(
            _md_table(
                ["record_id", "历史HS", "品名", "来源"],
                [
                    (
                        item.get("record_id"),
                        item.get("historical_hs"),
                        item.get("historical_name"),
                        item.get("source", {}),
                    )
                    for item in missing[:40]
                ],
            )
        )
        if len(missing) > 40:
            lines.append(f"\n（仅显示前 40 条，共 {len(missing)} 条）")
    else:
        lines.append("无。")
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
                            "; ".join(
                                dict.fromkeys(str(issue.get("code")) for issue in issues)
                            ),
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
            lines.append(f"\n（仅显示前 80 条，完整见 import_report.json）")
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 口径",
            "",
            "- 历史成功记录只作参考，不直接作为当前归类结论。",
            "- 文件名“历史成功数据”不等于海关放行凭证；成功口径来自人工声明，未独立核验。",
            "- 汇总表缺失日期/税则版本/适用范围/有效期时保留为空或“未提供”，不推断。",
            "- 合并单元格只取锚点值；续行留空并报告，不向下填 HS/要素/图片。",
            "- 图片按“页签+行号+图片列”精确关联；列缺失 ≠ 无图，读取失败 ≠ 无图；外部 URL 不联网。",
            "- 汇总表身份使用业务字段内容哈希 + 同内容出现序号；排序/插行/重新保存不改变身份；",
            "  来源中消失的记录只报告保留，不自动物理删除；显式 --replace-changed 才替换。",
        ]
    )
    return "\n".join(lines) + "\n"


def render_validation_markdown(validation: Mapping[str, Any]) -> str:
    lines = [
        "# P15 历史记录查询验证",
        "",
        f"- 记录文件：`{validation.get('records_path')}`",
        f"- 生成时间：{validation.get('generated_at')}",
        f"- 执行状态：{validation.get('status', 'executed')}",
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
    imp.add_argument("--out-dir", help="输出目录；默认 data/private/p15-import/<storage_key>")
    imp.add_argument(
        "--input-format",
        choices=FORMAT_CHOICES,
        default=FORMAT_AUTO,
        help="输入格式：legacy-template=旧多页签模板；consolidated=历史汇总表；auto=仅在结构明确时自动判断",
    )
    imp.add_argument("--mapping", help="汇总表列映射 JSON；可指定 sheet/header_row/fields 别名")
    imp.add_argument("--sheet", help="汇总表页签；默认自动选择唯一可识别页签")
    imp.add_argument("--header-row", type=int, help="汇总表表头行；默认自动识别唯一候选")
    imp.add_argument("--image-root", help="显式允许读取的本地图片根目录；URL 仍不联网")
    imp.add_argument("--dataset-kind", choices=("real", "synthetic"), default="real")
    imp.add_argument("--dataset-id", help="覆盖 P15 dataset_id")
    imp.add_argument("--declared-success", action="store_true", help="真实模板必须显式声明：Jay 已确认试点案例可视为通过")
    imp.add_argument("--declared-by", default="Jay")
    imp.add_argument("--declaration-note", default=DEFAULT_DECLARATION_NOTE)
    imp.add_argument("--declared-at", help="声明时间；默认当前 UTC")
    imp.add_argument("--copy-images", action="store_true", help="把可用图片写到输出目录 images/（不提交 Git）")
    imp.add_argument("--replace-changed", action="store_true", help="同 source_key 内容变化时替换旧记录")
    imp.add_argument("--keep-business-duplicates", action="store_true", help="不同来源文件业务内容相同时仍保留多份来源")
    imp.add_argument("--fresh", action="store_true", help="忽略已有输出，重新开始（不同来源需 --allow-dataset-replace）")
    imp.add_argument("--allow-dataset-replace", action="store_true", help="允许 --fresh 覆盖不同来源数据集")
    imp.add_argument("--accept-conflicts", action="store_true", help="业务键相同但内容不同的记录也保留（默认保留旧记录）")
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
    print(f"status: {outcome.report.get('status')}")
    print(f"format: {outcome.report.get('input_format')}")
    print(f"records: {outcome.records_path}")
    print(f"report: {outcome.report_json_path}")
    if outcome.validation_json_path:
        print(f"validation: {outcome.validation_json_path}")
    print(
        "summary: files={files} parsed={parsed_files} source_records={source_records} "
        "added={records_added} replaced={records_replaced} duplicate={records_duplicate} "
        "business_duplicate={records_duplicate_business_content} "
        "changed={records_changed} conflicts={records_conflicts} missing={records_missing_from_source} "
        "rejected={records_rejected} needs_review={records_needs_review} "
        "total={records_total_in_output} previous={previous_total_in_output}".format(
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
    try:
        mapping = load_mapping_config(Path(args.mapping) if args.mapping else None)
        if args.sheet:
            mapping["sheet"] = args.sheet
        if args.header_row:
            mapping["header_row"] = args.header_row
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
        detected_formats = set()
        try:
            for item in files:
                workbook = Workbook.open(item)
                detected = (
                    detect_workbook_format(workbook, mapping)
                    if args.input_format == FORMAT_AUTO
                    else args.input_format
                )
                detected_formats.add(detected)
        except (ConsolidatedFormatError, OoxmlError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        default_key = storage_key_for(metas, args.dataset_kind)
        if FORMAT_CONSOLIDATED in detected_formats:
            default_key += "-consolidated"
        requested_out = Path("data/private/p15-import") / default_key
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
        input_format=args.input_format,
        mapping=Path(args.mapping) if args.mapping else None,
        sheet=args.sheet,
        header_row=args.header_row,
        image_root=Path(args.image_root) if args.image_root else None,
        accept_conflicts=args.accept_conflicts,
        allow_dataset_replace=args.allow_dataset_replace,
    )
    try:
        outcome = import_history_templates(request)
    except ImportIncompleteError as exc:
        print(f"incomplete: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(outcome)
    totals = outcome.report["totals"]
    if outcome.report.get("status") != "complete":
        return 1
    if totals["records_total_in_output"] == 0:
        return 1
    checks = (outcome.report.get("validation") or {}).get("checks") or {}
    if checks and not all(checks.values()):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI 入口
    raise SystemExit(main())