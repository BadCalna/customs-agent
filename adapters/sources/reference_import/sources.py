"""来源清单、原件核对与只读工作簿访问（P23）。

三件事分开做，避免“读到一半就当成功”：

1. **来源清单**（``SourceManifest``）：这批到底登记了哪些原件、属于哪个数据类别、
   本批次是真实还是合成；清单里没有的类别不允许在报告里写成“已导入”。
2. **原件核对**（``SourceFile``）：导入前后各算一次 SHA-256 与字节数，与清单声明逐字比对；
   不一致就失败，不“修一下继续”。
3. **只读访问**（``WorkbookAccess``）：复用 P04/P05 的 ``adapters.files.ooxml``，
   不通过 openpyxl 另存；额外读取 ``styles.xml`` 取得单元格数字格式，用来判断
   “百分比列到底是百分数格式还是被写成了普通小数”——这决定了能不能换算退税率。

字段映射一律**按表头文字定位**，不按列序号、不按页签名猜结构；找不到必需表头或
同一表头出现多次，直接拒绝该文件并报告，不猜一个“最像”的列。
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from zipfile import BadZipFile, ZipFile

from adapters.files.ooxml import Cell, OoxmlError, Sheet, Workbook

from adapters.sources.reference_import.model import (
    SOURCES_SCHEMA_VERSION,
    DatasetId,
    DatasetKind,
    DatasetStatus,
    InputKind,
    SourceFile,
    parse_iso_date,
    read_json,
    sha256_file,
    strip_invisible,
    visible_text,
)

#: XML 命名空间（Transitional 与 Strict 两种写法在这里是同一个 URI）。
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS = {"s": _S}

#: 内置百分比数字格式 ID（ECMA-376 §18.8.30）；自定义格式从 ``numFmts`` 读。
_BUILTIN_PERCENT_FORMAT_IDS = frozenset({9, 10})

#: 表头文字归一：去所有空白并做 NFKC，避免“HS 编码”与“HS编码”被当成两列。
_SPACES = re.compile(r"[\s\u3000]+")


def normalize_header(text: Any) -> str:
    import unicodedata

    if text is None:
        return ""
    return _SPACES.sub("", unicodedata.normalize("NFKC", str(text))).strip()


class SourceError(RuntimeError):
    """来源清单或原件不可用；调用方据此给出“缺失输入/无法解析”，不静默降级。"""


class LayoutError(RuntimeError):
    """表头/页签结构无法按契约识别。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 数字格式
# ---------------------------------------------------------------------------


class NumberFormats:
    """``xl/styles.xml`` → 单元格样式索引 → 数字格式代码。

    只回答一个问题：某个单元格自己的数字格式是不是百分数。**不能**因为整列
    看起来像百分比就替所有单元格换算；格式与值必须逐格对应。
    """

    def __init__(self, styles_xml: bytes | None):
        self._codes: dict[int, str] = {}
        self._cell_xf_format: dict[int, int] = {}
        self.available = styles_xml is not None
        if styles_xml is None:
            return
        try:
            root = ET.fromstring(styles_xml)
        except ET.ParseError:
            self.available = False
            return
        for element in root.findall("s:numFmts/s:numFmt", _NS):
            try:
                self._codes[int(element.get("numFmtId", ""))] = element.get("formatCode", "")
            except ValueError:
                continue
        xfs = root.find("s:cellXfs", _NS)
        if xfs is None:
            return
        for index, element in enumerate(xfs.findall("s:xf", _NS)):
            try:
                self._cell_xf_format[index] = int(element.get("numFmtId", "0"))
            except ValueError:
                self._cell_xf_format[index] = 0

    def format_code(self, style_index: str | None) -> str | None:
        """返回该样式对应的格式代码；无法判定时返回 ``None``（不猜）。"""

        if not self.available:
            return None
        try:
            index = int(style_index) if style_index not in (None, "") else 0
        except (TypeError, ValueError):
            return None
        fmt_id = self._cell_xf_format.get(index)
        if fmt_id is None:
            return None
        if fmt_id in self._codes:
            return self._codes[fmt_id]
        # 内置格式：只声明我们关心的两种；其余返回 None，由调用方按“未声明”处理。
        if fmt_id in _BUILTIN_PERCENT_FORMAT_IDS:
            return "0%" if fmt_id == 9 else "0.00%"
        return f"builtin:{fmt_id}"

    def is_percent(self, style_index: str | None) -> bool | None:
        """``True``=百分数格式；``False``=确定不是；``None``=读不出格式。"""

        code = self.format_code(style_index)
        if code is None:
            return None
        return "%" in code


def _stylesheet_bytes(data: bytes) -> bytes | None:
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            return archive.read("xl/styles.xml")
    except (BadZipFile, KeyError, OSError):
        return None


# ---------------------------------------------------------------------------
# 只读工作簿访问
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderLayout:
    """一份页签凭表头识别出来的列位置。"""

    sheet: str
    header_row: int
    columns: Mapping[str, int]
    extras: Mapping[str, int] = field(default_factory=dict)

    def column(self, name: str) -> int | None:
        return self.columns.get(name)


@dataclass(frozen=True)
class HeaderSpec:
    """一类页签的表头契约：必需列 + 可选列。"""

    label: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()

    @property
    def all_headers(self) -> tuple[str, ...]:
        return self.required + self.optional


class WorkbookAccess:
    """只读工作簿 + 数字格式 + 行缓存；原件字节只读一次。"""

    def __init__(self, source: SourceFile):
        self.source = source
        try:
            data = source.path.read_bytes()
        except OSError as exc:
            raise SourceError(f"原件不可读：{source.source_id}（{type(exc).__name__}）") from exc
        for label, expected in (("sha256", source.sha256), ("bytes", source.size)):
            actual = sha256_file(source.path) if label == "sha256" else len(data)
            if actual != expected:
                raise SourceError(
                    f"原件在本次读取中被改动：{source.source_id} 的 {label} 与清单不一致"
                )
        try:
            self.workbook = Workbook(data, source_name=source.path.name)
        except OoxmlError as exc:
            raise SourceError(
                f"原件无法按 XLSX 只读解析：{source.source_id}（{exc.code}：{exc}）"
            ) from exc
        self.formats = NumberFormats(_stylesheet_bytes(data))
        self._row_cache: dict[str, dict[int, dict[int, Cell]]] = {}

    # ------------------------------------------------------------- 页签与行
    @property
    def sheets(self) -> tuple[Sheet, ...]:
        return self.workbook.sheets

    @property
    def sheet_names(self) -> list[str]:
        return [sheet.name for sheet in self.sheets]

    def sheet(self, name: str) -> Sheet:
        try:
            return self.workbook.sheet(name)
        except OoxmlError as exc:
            raise LayoutError("sheet_not_found", f"找不到页签：{name!r}（{exc}）") from exc

    def _rows(self, sheet: Sheet) -> dict[int, dict[int, Cell]]:
        cached = self._row_cache.get(sheet.name)
        if cached is not None:
            return cached
        rows: dict[int, dict[int, Cell]] = {}
        for cell in sheet.cells.values():
            rows.setdefault(cell.row, {})[cell.column] = cell
        self._row_cache[sheet.name] = rows
        return rows

    def row_numbers(self, sheet: Sheet) -> list[int]:
        return sorted(self._rows(sheet))

    def row_cells(self, sheet: Sheet, row: int) -> dict[int, Cell]:
        """一行内的单元格：``列号 → Cell``；不存在时返回空字典。"""

        return dict(self._rows(sheet).get(row, {}))

    def cell(self, sheet: Sheet, row: int, column: int) -> Cell | None:
        return self._rows(sheet).get(row, {}).get(column)

    def text(self, sheet: Sheet, row: int, column: int | None) -> str:
        if column is None:
            return ""
        cell = self.cell(sheet, row, column)
        return "" if cell is None else cell.value

    def kind(self, sheet: Sheet, row: int, column: int | None) -> str:
        if column is None:
            return ""
        cell = self.cell(sheet, row, column)
        return "" if cell is None else cell.kind

    def number_format(self, sheet: Sheet, row: int, column: int | None) -> str | None:
        if column is None:
            return None
        cell = self.cell(sheet, row, column)
        if cell is None:
            return None
        return self.formats.format_code(cell.style)

    def is_percent_cell(self, sheet: Sheet, row: int, column: int | None) -> bool | None:
        if column is None:
            return None
        cell = self.cell(sheet, row, column)
        if cell is None:
            return None
        return self.formats.is_percent(cell.style)

    def locator(self, sheet: Sheet, row: int, column: int | None = None) -> str:
        """来源定位：``页签!单元格``；只给行时给行范围起点。"""

        from adapters.files.ooxml import col_label

        if column is None:
            return f"{sheet.name}!第 {row} 行"
        return f"{sheet.name}!{col_label(column)}{row}"

    # ------------------------------------------------------------- 表头识别
    def locate_header(
        self,
        spec: HeaderSpec,
        *,
        sheet_name: str | None = None,
        scan_rows: int = 40,
    ) -> HeaderLayout:
        """按表头文字定位页签与表头行；找不到/不唯一/必需列缺一即报错。"""

        candidates: list[tuple[str, int, dict[str, int], dict[str, int]]] = []
        sheets = self.sheets if sheet_name is None else (self.sheet(sheet_name),)
        required = {normalize_header(name) for name in spec.required}
        wanted = {normalize_header(name) for name in spec.all_headers}
        for sheet in sheets:
            rows = self._rows(sheet)
            for row_number in sorted(rows)[:scan_rows]:
                mapping: dict[str, int] = {}
                extras: dict[str, int] = {}
                duplicates: set[str] = set()
                for column, cell in sorted(rows[row_number].items()):
                    header = normalize_header(cell.value)
                    if not header:
                        continue
                    if header in mapping:
                        if header in wanted:
                            # 必需列与可选列同名重复都属于“结构有歧义”，不能静默取左列。
                            duplicates.add(header)
                        continue
                    if header in wanted:
                        mapping[header] = column
                    else:
                        extras[header] = column
                if not required.issubset(mapping):
                    continue
                if duplicates:
                    raise LayoutError(
                        "header_ambiguous",
                        f"页签 {sheet.name} 第 {row_number} 行表头重复：{'、'.join(sorted(duplicates))}",
                    )
                # 同一组必需列可能被多个页签同时满足（例如要素明细也有“HS编码/商品名称/适用前缀”），
                # 用可选列的命中数区分；仍然打平就报歧义，不猜。
                score = len(mapping)
                candidates.append((sheet.name, row_number, mapping, extras, score))
        if not candidates:
            where = "任意页签" if sheet_name is None else f"页签 {sheet_name}"
            raise LayoutError(
                "header_not_found",
                f"{where} 未找到 {spec.label} 表头（需要列：{'、'.join(spec.required)}）",
            )
        # 命中多处时取“可选列命中最多”的那一处；仍然打平才报歧义。
        best = max(candidates, key=lambda item: item[4])
        top = [item for item in candidates if item[4] == best[4]]
        if len(top) > 1:
            detail = "、".join(f"{name}!第 {row} 行" for name, row, _, _, _ in top[:4])
            raise LayoutError(
                "header_ambiguous",
                f"{spec.label} 表头在多个位置出现且无法区分（{detail}），不猜映射",
            )
        sheet_name, row_number, mapping, extras, _score = best
        named: dict[str, int] = {}
        for name in spec.all_headers:
            key = normalize_header(name)
            if key in mapping:
                named[name] = mapping[key]
        missing = [name for name in spec.required if name not in named]
        if missing:  # pragma: no cover - 上面的子集判断已覆盖
            raise LayoutError(
                "header_not_found", f"{spec.label} 缺少必需列：{'、'.join(missing)}"
            )
        return HeaderLayout(
            sheet=sheet_name, header_row=row_number, columns=named, extras=extras
        )


# ---------------------------------------------------------------------------
# 工作簿缓存
# ---------------------------------------------------------------------------

#: 同一份原件在一次运行中可能同时喂给目录、要素、退税三个转换器。
#: 缓存按「绝对路径 + 原件哈希」建键：原件被换掉不会命中旧缓存。
_ACCESS_CACHE: dict[tuple[str, str], "WorkbookAccess"] = {}
#: 缓存上限（个）；超过就整体清空，代价是重新解析，换来内存可控。
_MAX_CACHED_WORKBOOKS = 8


def open_workbook(source: SourceFile) -> "WorkbookAccess":
    """取得（或复用）只读工作簿；原件哈希变化会重新解析。"""

    key = (str(source.path.resolve()), source.sha256)
    cached = _ACCESS_CACHE.get(key)
    if cached is not None:
        return cached
    access = WorkbookAccess(source)
    if len(_ACCESS_CACHE) >= _MAX_CACHED_WORKBOOKS:
        # 长驻进程反复导入不同批次时不能无限堆积解析后的工作簿。
        _ACCESS_CACHE.clear()
    _ACCESS_CACHE[key] = access
    return access


def clear_workbook_cache() -> None:
    """释放工作簿缓存（测试与长驻进程用；不改变任何已写出的文件）。"""

    _ACCESS_CACHE.clear()


# ---------------------------------------------------------------------------
# 来源清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredDataset:
    """清单里对一份数据类别的声明。"""

    dataset: DatasetId
    status: DatasetStatus
    reason: str = ""
    inputs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "dataset": self.dataset.value,
            "status": self.status.value,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.inputs:
            payload["inputs"] = list(self.inputs)
        return payload


@dataclass(frozen=True)
class UnresolvedInput:
    """清单登记了但本机取不到的原件；报告里必须显式出现。"""

    source_id: str
    kind: InputKind
    declared_path: str | None
    local_path_hint: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "repository_path": self.declared_path,
            "local_path_hint": self.local_path_hint,
            "status": "input_missing",
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SourceManifest:
    """一份已解析的来源清单：登记了什么、实际取到哪些、哪份数据类别没提供。"""

    batch_id: str
    kind: DatasetKind
    prepared_at: str
    manifest_path: Path
    declared: tuple[DeclaredDataset, ...]
    sources: tuple[SourceFile, ...]
    unresolved: tuple[UnresolvedInput, ...]
    defaults: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""

    def dataset(self, dataset: DatasetId) -> DeclaredDataset | None:
        return next((item for item in self.declared if item.dataset is dataset), None)

    def sources_for(self, dataset: DatasetId) -> tuple[SourceFile, ...]:
        declared = self.dataset(dataset)
        if declared is None:
            return ()
        wanted = set(declared.inputs)
        return tuple(item for item in self.sources if item.source_id in wanted)

    def source(self, source_id: str) -> SourceFile | None:
        return next((item for item in self.sources if item.source_id == source_id), None)

    @property
    def input_sha256(self) -> dict[str, str]:
        return {item.source_id: item.sha256 for item in self.sources}


_KIND_BY_LABEL = {
    "hs_element_mapping_workbook": InputKind.HS_ELEMENT_WORKBOOK,
    "hs_element_tax_rate_workbook": InputKind.TAX_RATE_WORKBOOK,
    "commentary_json": InputKind.COMMENTARY_JSON,
    "commentary_workbook": InputKind.COMMENTARY_WORKBOOK,
}

_DATASET_BY_LABEL = {item.value: item for item in DatasetId}
_STATUS_BY_LABEL = {item.value: item for item in DatasetStatus}


def _require_mapping(payload: Any, *, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise SourceError(f"{what}必须是对象")
    return payload


def detect_repository_root(start: str | Path) -> Path:
    """从清单位置向上找仓库根（有 ``.git`` 或 ``manage.py`` 的那一层）。

    来源清单通常放在批次子目录里，而 ``repository_path`` 是仓库根相对路径；
    只按清单所在目录解析会把 ``data/...`` 解析成 ``data/import-basis/.../data/...``。
    """

    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / "manage.py").is_file():
            return candidate
    return current


def _resolve_input_path(
    raw_path: str, repository_root: Path, *, local_path_hint: str | None
) -> Path | None:
    """repo 相对路径按仓库根解析；本地提示路径依次尝试仓库内与仓库同级目录。"""

    candidates: list[Path] = []
    if raw_path:
        path = Path(raw_path).expanduser()
        candidates.append(path if path.is_absolute() else repository_root / path)
    if local_path_hint:
        hint = Path(local_path_hint).expanduser()
        if hint.is_absolute():
            candidates.append(hint)
        else:
            # 原件常常放在工作区根目录（仓库是工作区里的一个 worktree），
            # 因此依次尝试仓库内、上一级、上两级。
            for ancestor in (
                repository_root,
                repository_root.parent,
                repository_root.parent.parent,
            ):
                candidates.append(ancestor / hint)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_source_manifest(
    path: str | Path,
    *,
    manifest_root: Path | None = None,
    kind: DatasetKind | None = None,
) -> SourceManifest:
    """读取并核对来源清单；原件缺失只登记，不在这里抛错。"""

    manifest_path = Path(path)
    try:
        payload = read_json(manifest_path)
    except OSError as exc:
        raise SourceError(f"来源清单不可读：{manifest_path.name}（{type(exc).__name__}）") from exc
    except ValueError as exc:
        raise SourceError(f"来源清单不是合法 JSON：{manifest_path.name}（{exc}）") from exc
    payload = _require_mapping(payload, what="来源清单顶层")
    schema_version = payload.get("schema_version")
    if schema_version != SOURCES_SCHEMA_VERSION:
        raise SourceError(
            f"来源清单结构版本不受支持：{schema_version!r}；需要 {SOURCES_SCHEMA_VERSION}"
        )
    root = (manifest_root or detect_repository_root(manifest_path.parent)).resolve()
    batch_id = visible_text(payload.get("batch_id"))
    if not batch_id:
        raise SourceError("来源清单缺少 batch_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", batch_id):
        # batch_id 会拼进快照目录名；出现 ../ 或分隔符可能写到输出根之外。
        raise SourceError(
            f"batch_id 只能是字母/数字/._- 且不超过 64 个字符：{batch_id!r}"
        )
    kind_label = visible_text(payload.get("dataset_kind")) or DatasetKind.REAL.value
    try:
        declared_kind = DatasetKind(kind_label)
    except ValueError as exc:
        raise SourceError(
            f"来源清单的 dataset_kind 未知：{kind_label!r}；只能是 real 或 synthetic"
        ) from exc
    if kind is not None and kind is not declared_kind:
        raise SourceError(
            f"命令行声明的批次性质（{kind.value}）与来源清单（{declared_kind.value}）不一致；"
            "不允许把真实批次当合成批次、或反过来"
        )

    declared: list[DeclaredDataset] = []
    datasets_payload = payload.get("datasets")
    if not isinstance(datasets_payload, list) or not datasets_payload:
        raise SourceError("来源清单缺少非空 datasets 数组：不知道要产出哪些数据类别")
    for index, item in enumerate(datasets_payload):
        item = _require_mapping(item, what=f"第 {index + 1} 个 datasets 条目")
        label = visible_text(item.get("dataset"))
        dataset = _DATASET_BY_LABEL.get(label)
        if dataset is None:
            raise SourceError(f"第 {index + 1} 个 datasets 条目的 dataset 未知：{label!r}")
        status_label = visible_text(item.get("status"))
        status = _STATUS_BY_LABEL.get(status_label)
        if status is None:
            raise SourceError(f"数据类别 {label} 的 status 未知：{status_label!r}")
        inputs = item.get("inputs") or []
        if not isinstance(inputs, list):
            raise SourceError(f"数据类别 {label} 的 inputs 必须是数组")
        declared.append(
            DeclaredDataset(
                dataset=dataset,
                status=status,
                reason=visible_text(item.get("reason")),
                inputs=tuple(visible_text(entry) for entry in inputs if visible_text(entry)),
            )
        )
    known = {item.dataset for item in declared}
    if len(known) != len(declared):
        raise SourceError("来源清单里同一数据类别重复声明，无法判断以哪条为准")

    sources: list[SourceFile] = []
    unresolved: list[UnresolvedInput] = []
    inputs_payload = payload.get("inputs")
    if not isinstance(inputs_payload, list):
        raise SourceError("来源清单缺少 inputs 数组")
    seen_ids: set[str] = set()
    for index, item in enumerate(inputs_payload):
        item = _require_mapping(item, what=f"第 {index + 1} 个 inputs 条目")
        source_id = visible_text(item.get("source_id"))
        if not source_id:
            raise SourceError(f"第 {index + 1} 个 inputs 条目缺少 source_id")
        if source_id in seen_ids:
            raise SourceError(f"来源清单出现重复 source_id：{source_id}")
        seen_ids.add(source_id)
        kind_label = visible_text(item.get("kind"))
        input_kind = _KIND_BY_LABEL.get(kind_label)
        if input_kind is None:
            raise SourceError(f"来源 {source_id} 的 kind 未知：{kind_label!r}")
        declared_path = visible_text(item.get("repository_path"))
        local_hint = visible_text(item.get("local_path_hint"))
        if not declared_path and not local_hint:
            raise SourceError(f"来源 {source_id} 既没有 repository_path 也没有 local_path_hint")
        declared_sha = visible_text(item.get("sha256")) or None
        if declared_sha is not None and not re.fullmatch(r"[0-9a-f]{64}", declared_sha):
            raise SourceError(f"来源 {source_id} 的 sha256 不是 64 位小写十六进制")
        declared_bytes = item.get("bytes")
        if declared_bytes is not None and not isinstance(declared_bytes, int):
            raise SourceError(f"来源 {source_id} 的 bytes 必须是整数")
        retrieved_at = visible_text(item.get("retrieved_at")) or None
        if retrieved_at is not None and parse_iso_date(retrieved_at) is None:
            raise SourceError(f"来源 {source_id} 的 retrieved_at 不是 ISO 日期/时间")
        roles = item.get("roles") or []
        if not isinstance(roles, list):
            raise SourceError(f"来源 {source_id} 的 roles 必须是数组")
        resolved = _resolve_input_path(
            declared_path, root, local_path_hint=local_hint or None
        )
        if resolved is None:
            unresolved.append(
                UnresolvedInput(
                    source_id=source_id,
                    kind=input_kind,
                    declared_path=declared_path or None,
                    local_path_hint=local_hint or None,
                    reason="本机找不到该原件（未随仓库分发或路径不在当前工作区）",
                )
            )
            continue
        size = resolved.stat().st_size
        actual_sha = sha256_file(resolved)
        sources.append(
            SourceFile(
                source_id=source_id,
                kind=input_kind,
                path=resolved,
                sha256=actual_sha,
                size=size,
                roles=tuple(visible_text(entry) for entry in roles if visible_text(entry)),
                declared_sha256=declared_sha,
                declared_bytes=declared_bytes,
                retrieved_at=retrieved_at,
                recorded_source=item.get("recorded_source") or {},
                original_filename=visible_text(item.get("original_filename")),
                notes=visible_text(item.get("notes")),
                in_git=bool(declared_path),
                declared_path=declared_path.replace("\\", "/") if declared_path else "",
                local_path_hint=local_hint or None,
            )
        )
    defaults: dict[str, str] = {}
    defaults_payload = payload.get("defaults") or {}
    if not isinstance(defaults_payload, Mapping):
        raise SourceError("来源清单的 defaults 必须是对象")
    for key, value in defaults_payload.items():
        if not isinstance(value, str):
            raise SourceError(f"来源清单 defaults.{key} 必须是字符串（版本号、日期都按文本保存）")
        defaults[visible_text(key)] = visible_text(value)
    return SourceManifest(
        batch_id=batch_id,
        kind=declared_kind,
        prepared_at=visible_text(payload.get("prepared_at")),
        manifest_path=manifest_path,
        declared=tuple(declared),
        sources=tuple(sources),
        unresolved=tuple(unresolved),
        defaults=defaults,
        notes=visible_text(payload.get("notes")),
    )


def verify_sources_unchanged(sources: Iterable[SourceFile]) -> list[str]:
    """导入结束后再核一次原件哈希；返回不一致说明（空表示全部保持）。"""

    problems: list[str] = []
    for source in sources:
        if not source.path.is_file():
            problems.append(f"{source.source_id}：原件已不存在")
            continue
        actual = sha256_file(source.path)
        if actual != source.sha256:
            problems.append(
                f"{source.source_id}：导入后 SHA-256 变化（{source.sha256[:12]}→{actual[:12]}）"
            )
    return problems


def locate_dataset_inputs(
    manifest: SourceManifest, dataset: DatasetId
) -> tuple[tuple[SourceFile, ...], list[str]]:
    """取某数据类别的原件；清单声明了但缺件的 input 会返回原因，不静默忽略。"""

    declared = manifest.dataset(dataset)
    if declared is None:
        return (), [f"来源清单未声明数据类别 {dataset.value}"]
    reasons: list[str] = []
    available: list[SourceFile] = []
    for source_id in declared.inputs:
        source = manifest.source(source_id)
        if source is not None:
            available.append(source)
            continue
        missing = next(
            (item for item in manifest.unresolved if item.source_id == source_id), None
        )
        reasons.append(
            f"{source_id}：{missing.reason if missing else '未在清单 inputs 中找到'}"
        )
    return tuple(available), reasons


__all__ = [
    "DeclaredDataset",
    "detect_repository_root",
    "clear_workbook_cache",
    "open_workbook",
    "HeaderLayout",
    "HeaderSpec",
    "LayoutError",
    "NumberFormats",
    "SourceError",
    "SourceManifest",
    "UnresolvedInput",
    "WorkbookAccess",
    "load_source_manifest",
    "locate_dataset_inputs",
    "normalize_header",
    "strip_invisible",
    "verify_sources_unchanged",
]
