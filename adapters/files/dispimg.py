"""WPS ``DISPIMG`` 图片引用的逐行提取（P05、D2“附图与 DISPIMG”、R11）。

设计口径：

- **逐行判定**。调用方给出 ``(页签, 行号集合, 图片列)``，本模块对每个申报行返回
  ``available`` / ``absent`` / ``read_failed``，与 :class:`apps.reviews.models.ItemImage.Status`
  的取值一一对应；提取结果不写数据库，由 ``apps.reviews.services.files`` 决定落库与版本归属。
- **共享公式逐行解析**。``<f t="shared">`` 的从属行本身没有公式文本，按 ``ooxml.Sheet.formulas``
  平移后的有效公式解析；若从属行的缓存值里还带着本行自己的 ``DISPIMG`` 调用（WPS 的写法），
  以缓存值为本行实际 ID，并标记来源 ``shared_formula_cached``，供人工追溯。
- **读不出不是没有图**。缺定义、重复定义、缺资源、坏资源、外部链接、未做像素解码、
  条件/表达式公式、一个单元格多个引用各给不同 ``code``，统一是 ``read_failed``；
  只有“单元格里确实没有图片公式”才是 ``absent``，且区分空白、文本、其它公式三种来源。
- **不推断显示结果**。条件/动态公式（如 ``IF(...,DISPIMG("ID",1),"")``）在离线解析里
  无法确定实际显示哪张图，返回 ``read_failed`` + ``dispimg_expression_not_evaluated``，
  不按任一候选图片入库。
- 生产代码只依赖 :mod:`adapters.files.ooxml` 与标准库，不引用 ``experiments``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .ooxml import OoxmlError, Sheet, Workbook, col_label, col_number

#: 与 ``ItemImage.Status`` 对齐的三种图片状态（可用字符串直接落库）。
STATUS_AVAILABLE = "available"
STATUS_ABSENT = "absent"
STATUS_READ_FAILED = "read_failed"

#: 提取成功；其余 code 都用于解释 absent / read_failed 的具体来源。
CODE_OK = "ok"
CODE_BLANK_CELL = "blank_cell"
CODE_TEXT_CONTENT = "text_content"
CODE_OTHER_FORMULA = "other_formula"
CODE_FORMULA_TEXT_ONLY = "dispimg_formula_text_only"
CODE_DEFINITION_MISSING = "dispimg_definition_missing"
CODE_DEFINITION_DUPLICATE = "dispimg_definition_duplicate"
CODE_MEDIA_MISSING = "dispimg_media_missing"
CODE_MEDIA_CORRUPT = "dispimg_media_corrupt"
CODE_MEDIA_EXTERNAL = "dispimg_media_external"
CODE_MEDIA_UNAVAILABLE = "dispimg_media_unavailable"
CODE_MEDIA_TOO_LARGE = "dispimg_media_too_large"
CODE_DECODE_NOT_RUN = "dispimg_decode_not_run"
CODE_EXPRESSION = "dispimg_expression_not_evaluated"
CODE_MULTIPLE_REFERENCES = "dispimg_multiple_references"
CODE_UNSUPPORTED_FORMULA = "dispimg_formula_unsupported"

#: 结构性错误（页签/列/行参数本身有问题），与逐行状态分开，避免把“列没识别出来”写成“无图片”。
CODE_SHEET_MISSING = "sheet_missing"
CODE_COLUMN_INVALID = "image_column_invalid"
CODE_HEADER_ROW_INVALID = "header_row_invalid"
CODE_INVALID_ROW = "invalid_row"
CODE_NO_ROWS = "no_rows"

#: 图片列表头识别：真实模板写作“附图”，其它写法按同义关键词兜底，不做模糊猜测列字母。
DEFAULT_IMAGE_HEADER_KEYWORDS = ("附图", "图片", "照片", "image", "picture", "photo")
#: 单张图片字节上限（解压后的媒体部件）；超过按 read_failed 处理，由文件服务决定是否放宽。
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 直接图片调用 ``DISPIMG("ID_…", mode)``；mode 只影响客户端显示，不改变取到的资源。
_DISPIMG_CALL = re.compile(
    r'(?:_xlfn\.)?DISPIMG\s*\(\s*"([^"\r\n]+)"\s*,\s*[0-9]+\s*\)', re.I,
)
_SPACES = re.compile(r"\s+")


class DispimgError(ValueError):
    """页签、图片列或行号参数本身不成立；调用方据此区分“参数/结构问题”和“逐行读失败”。"""

    def __init__(self, code: str, message: str = "", *, sheet: str | None = None,
                 column: str | None = None):
        self.code = code
        self.sheet = sheet
        self.column = column
        super().__init__(message or code)

    def as_issue(self) -> dict:
        issue = {"code": self.code, "message": str(self)}
        if self.sheet:
            issue["sheet"] = self.sheet
        if self.column:
            issue["column"] = self.column
        return issue


@dataclass(frozen=True)
class ColumnMatch:
    """图片列识别结果；``missing`` / ``ambiguous`` 时不能当成“无图片”。"""

    status: str
    column: str = ""
    header: str = ""
    candidates: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RowImage:
    """一个申报行的图片提取结果；``data`` 只在 ``available`` 时有内容。"""

    sheet: str
    row: int
    cell: str
    status: str
    code: str = ""
    message: str = ""
    dispimg_id: str = ""
    formula: str = ""
    formula_status: str = ""
    id_source: str = ""
    media_part: str = ""
    sha256: str = ""
    size: int = 0
    image_format: str = ""
    data: bytes = field(default=b"", repr=False, compare=False)
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == STATUS_AVAILABLE

    def as_dict(self) -> dict:
        """落库/报告用；不含图片字节（字节由存储层按 sha256 落盘）。"""
        return {
            "sheet": self.sheet,
            "row": self.row,
            "cell": self.cell,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "dispimg_id": self.dispimg_id,
            "formula": self.formula,
            "formula_status": self.formula_status,
            "id_source": self.id_source,
            "media_part": self.media_part,
            "sha256": self.sha256,
            "size": self.size,
            "image_format": self.image_format,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ImageExtraction:
    """一次按列提取的结果；``column_status`` 必须原样报告给调用方。"""

    sheet: str
    column: str
    column_status: str
    column_header: str = ""
    column_candidates: tuple[str, ...] = ()
    column_reason: str = ""
    rows: tuple[RowImage, ...] = ()
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES

    @property
    def counts(self) -> dict:
        counts = {STATUS_AVAILABLE: 0, STATUS_ABSENT: 0, STATUS_READ_FAILED: 0}
        for row_image in self.rows:
            counts[row_image.status] = counts.get(row_image.status, 0) + 1
        return counts

    def as_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "column": self.column,
            "column_status": self.column_status,
            "column_header": self.column_header,
            "column_candidates": list(self.column_candidates),
            "column_reason": self.column_reason,
            "max_image_bytes": self.max_image_bytes,
            "counts": self.counts,
            "rows": [row_image.as_dict() for row_image in self.rows],
        }


# --------------------------------------------------------------------- 图片列识别
def _normalize_header(text: str) -> str:
    return _SPACES.sub("", text or "").casefold()


def find_image_column(
    workbook: Workbook,
    sheet_name: str,
    *,
    header_row: int = 1,
    keywords: Iterable[str] = DEFAULT_IMAGE_HEADER_KEYWORDS,
) -> ColumnMatch:
    """按表头关键词识别图片列；返回 ``found`` / ``missing`` / ``ambiguous``。

    只在 ``header_row`` 里按表头文字匹配，不按固定列字母猜——真实模板的附图列并不固定
    （P03 观察：不同模板在 M、Y 等列）。多个表头命中时返回 ``ambiguous`` 交由人工核对。
    """
    sheet = _sheet(workbook, sheet_name)
    _check_header_row(header_row, sheet_name)
    normalized_keywords = tuple(_normalize_header(keyword) for keyword in keywords if keyword)
    matches = []
    for cell in sheet.cells.values():
        if cell.row != header_row or not cell.has_content:
            continue
        header = _normalize_header(cell.value)
        if not header:
            continue
        if any(keyword in header for keyword in normalized_keywords):
            matches.append((col_label(cell.column), cell.value))
    matches.sort(key=lambda item: col_number(item[0]))
    if len(matches) == 1:
        return ColumnMatch(status="found", column=matches[0][0], header=matches[0][1])
    if not matches:
        return ColumnMatch(
            status="missing",
            reason=f"表头第 {header_row} 行没有命中图片列关键词：{'、'.join(keywords)}",
        )
    candidates = tuple(column for column, _ in matches)
    return ColumnMatch(
        status="ambiguous", candidates=candidates,
        reason=f"表头第 {header_row} 行命中多个图片列候选：{'、'.join(candidates)}，需人工核对",
    )


# --------------------------------------------------------------------- 逐行提取
def extract_images(
    workbook: Workbook,
    *,
    sheet_name: str,
    rows: Iterable[int],
    column: str | None = None,
    header_row: int = 1,
    keywords: Iterable[str] = DEFAULT_IMAGE_HEADER_KEYWORDS,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> ImageExtraction:
    """按 ``(页签, 行号集合, 图片列)`` 提取每个申报行的图片。

    ``column`` 为 ``None`` 时按表头关键词识别；识别不出或出现多个候选时返回
    ``column_status=missing/ambiguous`` 且不产生逐行结果——调用方必须据此报错，
    不能把它写成“无图片”。
    """
    _check_header_row(header_row, sheet_name)
    sheet = _sheet(workbook, sheet_name)
    row_numbers = _row_numbers(rows)
    if column is None:
        match = find_image_column(
            workbook, sheet_name, header_row=header_row, keywords=keywords,
        )
        if match.status != "found":
            return ImageExtraction(
                sheet=sheet_name, column="", column_status=match.status,
                column_candidates=match.candidates, column_reason=match.reason,
                max_image_bytes=max_image_bytes,
            )
        label, column_status = match.column, "found"
        match_header = match.header
    else:
        label = _column_label(column, sheet_name)
        column_status = "provided"
        header_cell = sheet.cell(f"{label}{header_row}")
        match_header = header_cell.value if header_cell is not None else ""

    images = tuple(
        _resolve_row(workbook, sheet, row, column_label=label, max_image_bytes=max_image_bytes)
        for row in row_numbers
    )
    return ImageExtraction(
        sheet=sheet_name, column=label, column_status=column_status,
        column_header=match_header, rows=images, max_image_bytes=max_image_bytes,
    )


def extract_column_images(
    workbook: Workbook,
    *,
    sheet_name: str,
    column: str,
    rows: Iterable[int],
    header_row: int = 1,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> ImageExtraction:
    """显式指定图片列的便捷入口（调用方已由 P04 语义映射确定列）。"""
    return extract_images(
        workbook, sheet_name=sheet_name, rows=rows, column=column,
        header_row=header_row, max_image_bytes=max_image_bytes,
    )


def _check_header_row(header_row: int, sheet_name: str) -> None:
    if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
        raise DispimgError(
            CODE_HEADER_ROW_INVALID, f"表头行号必须是正整数：{header_row!r}", sheet=sheet_name,
        )


def _sheet(workbook: Workbook, sheet_name: str) -> Sheet:
    try:
        return workbook.sheet(sheet_name)
    except OoxmlError as exc:
        raise DispimgError(CODE_SHEET_MISSING, f"找不到页签：{sheet_name!r}（{exc}）", sheet=sheet_name) from exc


def _column_label(column: str, sheet_name: str) -> str:
    try:
        return col_label(col_number(column))
    except OoxmlError as exc:
        raise DispimgError(
            CODE_COLUMN_INVALID, f"图片列标不合法：{column!r}", sheet=sheet_name, column=str(column),
        ) from exc


def _row_numbers(rows: Iterable[int]) -> tuple[int, ...]:
    if isinstance(rows, int):
        raise DispimgError(CODE_INVALID_ROW, "行号集合应是可迭代对象，不能传单个整数")
    numbers = []
    for row in rows:
        if isinstance(row, bool) or not isinstance(row, int) or row < 1:
            raise DispimgError(CODE_INVALID_ROW, f"行号必须是正整数：{row!r}")
        numbers.append(row)
    unique = tuple(sorted(set(numbers)))
    if not unique:
        raise DispimgError(CODE_NO_ROWS, "没有可提取的申报行")
    return unique


def _resolve_row(
    workbook: Workbook, sheet: Sheet, row: int, *, column_label: str, max_image_bytes: int,
) -> RowImage:
    cell_ref = f"{column_label}{row}"
    cell = sheet.cell(cell_ref)
    formula = sheet.formulas.get(cell_ref)

    if formula is None:
        return _absent_without_formula(sheet.name, row, cell_ref, cell)

    stripped = formula.strip()
    if stripped.startswith("="):
        stripped = stripped[1:].strip()
    direct = _DISPIMG_CALL.fullmatch(stripped)
    if direct is not None:
        image_id = direct.group(1)
        id_source = "formula"
        notes: tuple[str, ...] = ()
        attributes = dict(cell.formula_attributes) if cell is not None else {}
        if attributes.get("t") == "shared" and not (cell.formula or "").strip():
            # 共享公式从属行：有效公式由主公式平移得到；WPS 可能在缓存值里保留本行 ID。
            cached_id = _cached_dispimg_id(cell.value) if cell is not None else ""
            if cached_id and cached_id != image_id:
                image_id, id_source = cached_id, "shared_formula_cached"
                notes = ("从属行缓存值与共享主公式不同，按本行缓存值取图；需在客户端核对",)
            else:
                id_source = "shared_formula"
                notes = ("共享公式从属行，按主公式平移解析本行图片 ID",)
        return _resolve_definition(
            workbook, sheet.name, row, cell_ref, image_id=image_id, formula=formula,
            formula_status="direct_image_call", id_source=id_source,
            max_image_bytes=max_image_bytes, notes=notes,
        )

    ids = [match.group(1) for match in _DISPIMG_CALL.finditer(stripped)]
    if not ids:
        if "DISPIMG" in stripped.upper():
            # 公式里出现了 DISPIMG 关键字但不是可解析的调用形态；不猜，报失败。
            return _failed(
                sheet.name, row, cell_ref, CODE_UNSUPPORTED_FORMULA,
                "图片公式形态不受支持（DISPIMG 调用参数不完整或含动态内容），需人工核对",
                formula=formula, formula_status="unsupported_formula",
            )
        return RowImage(
            sheet=sheet.name, row=row, cell=cell_ref, status=STATUS_ABSENT,
            code=CODE_OTHER_FORMULA,
            message="该单元格是其它公式、不是图片公式，按无图片处理",
            formula=formula, formula_status="other_formula",
        )
    if len(ids) > 1:
        return _failed(
            sheet.name, row, cell_ref, CODE_MULTIPLE_REFERENCES,
            f"一个单元格出现多个 DISPIMG 引用（{'、'.join(sorted(set(ids)))}），无法判断显示哪张图，需人工核对",
            formula=formula, formula_status="expression_display_not_evaluated",
            notes=(f"候选图片 ID：{'、'.join(sorted(set(ids)))}",),
        )
    return _failed(
        sheet.name, row, cell_ref, CODE_EXPRESSION,
        "条件/表达式图片公式，离线解析不能确定实际显示哪张图，需在客户端核对",
        formula=formula, formula_status="expression_display_not_evaluated",
        dispimg_id=ids[0],
        notes=(f"候选图片 ID：{ids[0]}，未按候选图片入库",),
    )


def _absent_without_formula(sheet: str, row: int, cell_ref: str, cell) -> RowImage:
    """没有图片公式时的 absent 细分：空白、纯文本、其它公式。"""
    if cell is None or not (cell.value or "").strip():
        return RowImage(
            sheet=sheet, row=row, cell=cell_ref, status=STATUS_ABSENT,
            code=CODE_BLANK_CELL, message="", formula_status="no_formula",
        )
    text = (cell.value or "").strip()
    if "DISPIMG" in text.upper():
        # 只有公式文本、没有 <f> 定义：文件可能被工具改写，不能当“无图片”。
        return _failed(
            sheet, row, cell_ref, CODE_FORMULA_TEXT_ONLY,
            "单元格只有 DISPIMG 公式文本、没有公式定义，疑似文件被改写或损坏，需人工核对",
            formula=text, formula_status="text_only",
        )
    return RowImage(
        sheet=sheet, row=row, cell=cell_ref, status=STATUS_ABSENT,
        code=CODE_TEXT_CONTENT,
        message="单元格有文本内容但不是 DISPIMG 图片公式，按无图片处理",
        formula_status="text_only",
        notes=("图片列出现了非图片内容，建议人工确认该行是否漏图",),
    )


def _cached_dispimg_id(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("="):
        text = text[1:].strip()
    match = _DISPIMG_CALL.fullmatch(text)
    return match.group(1) if match is not None else ""


def _resolve_definition(
    workbook: Workbook,
    sheet: str,
    row: int,
    cell_ref: str,
    *,
    image_id: str,
    formula: str,
    formula_status: str,
    id_source: str,
    max_image_bytes: int,
    notes: tuple[str, ...],
) -> RowImage:
    resources = workbook.image_definitions().get(image_id, [])
    if not resources:
        return _failed(
            sheet, row, cell_ref, CODE_DEFINITION_MISSING,
            f"DISPIMG 图片 ID 在 cellimages 中没有定义：{image_id}",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, notes=notes,
        )
    if len(resources) > 1:
        parts = "、".join(sorted({resource.media.part or "（无部件）" for resource in resources}))
        return _failed(
            sheet, row, cell_ref, CODE_DEFINITION_DUPLICATE,
            f"同一 DISPIMG 图片 ID 有 {len(resources)} 条重复定义（{parts}），无法确定使用哪张，需人工核对",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, notes=notes,
        )

    resource = resources[0]
    media = resource.media
    if media.status == "external_unsupported":
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_EXTERNAL,
            "图片指向外部链接，离线解析不取外链，需人工核对",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, notes=notes,
        )
    if media.status == "missing_resource":
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_MISSING,
            f"图片资源部件不在包内：{media.part or '（缺少关系目标）'}",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", notes=notes,
        )
    if media.status == "corrupt_resource":
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_CORRUPT,
            f"图片资源无法解码：{media.error or media.decode_status or '未知原因'}",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", sha256=media.sha256,
            size=media.size, notes=notes,
        )
    if media.decode_status == "not_run_Pillow_unavailable":
        return _failed(
            sheet, row, cell_ref, CODE_DECODE_NOT_RUN,
            "未做像素解码校验（缺少 Pillow），不能报告图片可读",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", sha256=media.sha256,
            size=media.size, notes=notes,
        )
    if not media.usable:
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_UNAVAILABLE,
            f"图片资源状态不可用：{media.status}/{media.decode_status or '未解码'}",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", sha256=media.sha256,
            size=media.size, notes=notes,
        )
    if media.size > max_image_bytes:
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_TOO_LARGE,
            f"图片体积 {media.size} 字节超过上限 {max_image_bytes} 字节；需人工确认是否放宽",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", sha256=media.sha256,
            size=media.size, notes=notes,
        )
    try:
        data = workbook.media_bytes(media.part)
    except OoxmlError as exc:
        return _failed(
            sheet, row, cell_ref, CODE_MEDIA_MISSING,
            f"读取图片资源失败：{exc}",
            formula=formula, formula_status=formula_status, id_source=id_source,
            dispimg_id=image_id, media_part=media.part or "", sha256=media.sha256,
            size=media.size, notes=notes,
        )
    return RowImage(
        sheet=sheet, row=row, cell=cell_ref, status=STATUS_AVAILABLE, code=CODE_OK,
        dispimg_id=image_id, formula=formula, formula_status=formula_status,
        id_source=id_source, media_part=media.part or "", sha256=media.sha256,
        size=media.size, image_format=media.format or "", data=data, notes=notes,
    )


def _failed(
    sheet: str,
    row: int,
    cell_ref: str,
    code: str,
    message: str,
    *,
    formula: str = "",
    formula_status: str = "",
    id_source: str = "",
    dispimg_id: str = "",
    media_part: str = "",
    sha256: str = "",
    size: int = 0,
    notes: tuple[str, ...] = (),
) -> RowImage:
    # 统一补上错误码前缀，方便页面与日志按 code 检索，同时保留中文原因。
    if message and not message.startswith(code):
        message = f"{code}：{message}"
    return RowImage(
        sheet=sheet, row=row, cell=cell_ref, status=STATUS_READ_FAILED, code=code,
        message=message, formula=formula, formula_status=formula_status,
        id_source=id_source, dispimg_id=dispimg_id, media_part=media_part,
        sha256=sha256, size=size, notes=notes,
    )


__all__ = [
    "STATUS_AVAILABLE",
    "STATUS_ABSENT",
    "STATUS_READ_FAILED",
    "ColumnMatch",
    "DispimgError",
    "ImageExtraction",
    "RowImage",
    "extract_column_images",
    "extract_images",
    "find_image_column",
]
