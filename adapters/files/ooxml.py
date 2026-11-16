"""只读读取 XLSX/OOXML：页签、单元格、公式与图片资源（P04/P05 公共地基）。

设计口径：

- **只读**。不通过 openpyxl 等库整体载入再另存，避免丢掉 WPS 的 ``cellimages`` 等扩展部件；
  所有内容按原始部件读取，原件字节不落盘、不改写。
- **读不出就报错**。损坏、加密、动态/外部引用等给出带 ``code`` 的 :class:`OoxmlError`，
  不静默跳过——R11 要求“读不出”与“确实没有”分开。
- **异常是数据**。共享公式主公式缺失、图片定义重复、资源损坏等由调用方决定如何处理，
  本模块只给状态和来源，不替业务选择。
- 安全边界：拒绝 DTD/ENTITY（XXE）、重复 ZIP 条目、越界的关系目标与超限解压体积，
  媒体解码用 Pillow 做真实像素加载，未安装 Pillow 时明确报告 ``not_run_Pillow_unavailable``。

图片机制（D2“附图与 DISPIMG”）：单元格里只有 ``=_xlfn.DISPIMG("ID_…",1)`` 公式，
图片字节在 ``xl/media/``，由 ``xl/cellimages.xml`` 及其 rels 建立 ID → 部件的映射；
普通浮图则由页签 drawing 的关系与锚点表示。两种都可能在真实模板中同时出现（P03 观察）。
"""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zipfile import BadZipFile, ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"s": S, "r": R}

#: 解压后总大小的拒绝阈值（含图片）；超过即认为是异常输入或压缩炸弹。
MAX_EXPANDED_BYTES = 300 * 1024 * 1024
#: ZIP 条目数上限；真实模板通常几十到几百个部件。
MAX_PARTS = 20_000
#: 单个部件大小上限；目前最大的是 sharedStrings 与媒体。
MAX_PART_BYTES = 100 * 1024 * 1024

_DISPIMG = re.compile(r'(?:_xlfn\.)?DISPIMG\s*\(\s*"([^"\r\n]+)"\s*,\s*[0-9]+\s*\)', re.I)
_CELLIMAGES_PART = re.compile(r"(?:^|/)cellimages[^/]*\.xml$", re.I)

#: 单元格共享公式与普通 A1 引用的平移都只支持静态引用；其余显式拒绝。
_REF_TOKEN = re.compile(
    r'"(?:[^"]|"")*"|'
    r"(?<![\w.$])(?:(?P<sheet>'(?:[^']|'')+'|[\w\u0080-\uffff]+)!)?"
    r"(?P<ref>\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?"
    r"|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}|\$?[1-9][0-9]*:\$?[1-9][0-9]*)"
    r"(?![\w.(])"
)


class OoxmlError(ValueError):
    """输入不受支持或已损坏；调用方据此区分“解析失败”与“没有内容”。"""

    def __init__(self, code: str, message: str = "", *, sheet: str | None = None,
                 cell: str | None = None):
        self.code = code
        self.sheet = sheet
        self.cell = cell
        super().__init__(message or code)

    def as_issue(self) -> dict:
        issue = {"code": self.code, "message": str(self)}
        if self.sheet:
            issue["sheet"] = self.sheet
        if self.cell:
            issue["cell"] = self.cell
        return issue


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def col_number(label: str) -> int:
    value = 0
    letters = label.strip().upper()
    if not letters:
        raise OoxmlError("invalid_column", f"空列标：{label!r}")
    for char in letters:
        if char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            raise OoxmlError("invalid_column", f"非法列标：{label!r}")
        value = value * 26 + ord(char) - 64
    if not 1 <= value <= 16384:
        raise OoxmlError("column_out_of_range", f"列标超出 XLSX 上限：{label!r}")
    return value


def col_label(number: int) -> str:
    if not 1 <= number <= 16384:
        raise OoxmlError("column_out_of_range", f"列号超出 XLSX 上限：{number}")
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def cell_position(ref: str) -> tuple[int, int]:
    """``B7`` → ``(2, 7)``；行列为 1 起始。"""
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)", ref or "")
    if not match:
        raise OoxmlError("invalid_cell_reference", f"非法单元格引用：{ref!r}")
    return col_number(match[1]), int(match[2])


def relationship_part(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def resolve_part(base: str, target: str) -> str:
    """把关系目标解析为包内部件名；越界或含反斜杠直接拒绝。"""
    if not target:
        raise OoxmlError("empty_relationship_target", f"空关系目标（来源部件 {base}）")
    if "\\" in target:
        raise OoxmlError("unsafe_relationship_target", f"关系目标含反斜杠：{target!r}")
    result = posixpath.normpath(
        target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target)
    )
    if result.startswith("../") or result == "..":
        raise OoxmlError("unsafe_relationship_target", f"关系目标越出 OOXML 包：{target!r}")
    return result


def _xml(data: bytes, *, part: str) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise OoxmlError("unsupported_xml_declaration", f"不允许 DTD/ENTITY：{part}", )
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OoxmlError("malformed_xml", f"部件 XML 无法解析：{part}（{exc}）") from exc


def _text(element: ET.Element | None) -> str:
    """取文本节点；富文本只拼接 t 与 r/t，忽略拼音注音（rPh）。"""
    if element is None:
        return ""
    return "".join(
        child.text or "" if child.tag == f"{{{S}}}t" else child.findtext(f"{{{S}}}t", "") or ""
        for child in element
        if child.tag in (f"{{{S}}}t", f"{{{S}}}r")
    )


def shift_formula_to(formula: str, origin: str, destination: str) -> str:
    """把共享公式从主单元格平移到从属单元格（只处理静态 A1 引用）。"""
    old_col, old_row = cell_position(origin)
    new_col, new_row = cell_position(destination)

    def replace(match: re.Match) -> str:
        if match.group(0).startswith('"'):
            return match.group(0)

        def move(endpoint: str) -> str:
            cell = re.fullmatch(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)", endpoint)
            if not cell:
                raise OoxmlError(
                    "shared_formula_needs_native_editor",
                    f"共享公式含整列/整行引用，需原生编辑器：{formula!r}",
                )
            column = col_number(cell.group(2)) + (0 if cell.group(1) else new_col - old_col)
            row = int(cell.group(4)) + (0 if cell.group(3) else new_row - old_row)
            if not 1 <= row <= 1_048_576 or not 1 <= column <= 16384:
                raise OoxmlError(
                    "shared_formula_out_of_range",
                    f"共享公式平移后越界：{formula!r} → {destination}",
                )
            return f"{cell.group(1)}{col_label(column)}{cell.group(3)}{row}"

        ref = ":".join(move(part) for part in match.group("ref").split(":"))
        return ((match.group("sheet") + "!") if match.group("sheet") else "") + ref

    return _REF_TOKEN.sub(replace, formula)


@dataclass(frozen=True)
class Cell:
    ref: str
    row: int
    column: int
    value: str = ""
    kind: str = "n"
    formula: str | None = None
    formula_attributes: Mapping[str, str] = field(default_factory=dict)
    style: str | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.value) or self.formula is not None

    @property
    def has_formula(self) -> bool:
        return self.formula is not None



@dataclass(frozen=True)
class Merge:
    ref: str
    top: int
    left: int
    bottom: int
    right: int

    @property
    def anchor_ref(self) -> str:
        return f"{col_label(self.left)}{self.top}"

    @property
    def vertical(self) -> bool:
        return self.left == self.right and self.bottom > self.top

    def contains(self, row: int, column: int) -> bool:
        return self.top <= row <= self.bottom and self.left <= column <= self.right


@dataclass(frozen=True)
class Sheet:
    name: str
    part: str
    state: str
    cells: Mapping[str, Cell]
    merges: tuple[Merge, ...] = ()
    dimension: str | None = None
    formulas: Mapping[str, str] = field(default_factory=dict)

    @property
    def visible(self) -> bool:
        return self.state not in ("hidden", "veryHidden")

    @property
    def occupied_rows(self) -> tuple[int, ...]:
        return tuple(sorted({cell.row for cell in self.cells.values() if cell.has_content}))

    def cell(self, ref: str) -> Cell | None:
        return self.cells.get(ref.upper())

    def cell_at(self, row: int, column: int) -> Cell | None:
        return self.cells.get(f"{col_label(column)}{row}")

    def enclosing_merge(self, row: int, column: int) -> Merge | None:
        for merge in self.merges:
            if merge.contains(row, column):
                return merge
        return None

    def value_at(self, row: int, column: int) -> tuple[str, str, bool]:
        """返回 ``(文本, 来源单元格, 是否取自纵向合并的锚点)``。

        横向合并且本格为空时不借用相邻列内容——那会把别的字段当成这一列的值。
        """
        ref = f"{col_label(column)}{row}"
        cell = self.cells.get(ref)
        if cell is not None and cell.value:
            return cell.value, ref, False
        merge = self.enclosing_merge(row, column)
        if merge is not None and merge.vertical:
            anchor = self.cells.get(merge.anchor_ref)
            if anchor is not None and anchor.value:
                return anchor.value, merge.anchor_ref, True
        return (cell.value if cell is not None else ""), ref, False


@dataclass(frozen=True)
class MediaInfo:
    part: str | None
    status: str
    sha256: str = ""
    size: int = 0
    format: str | None = None
    decode_status: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "resource_found" and self.decode_status == "passed"


@dataclass(frozen=True)
class ImageResource:
    image_id: str
    definition_part: str
    relationship_id: str
    media: MediaInfo
    external: bool = False


@dataclass(frozen=True)
class ImageReference:
    """一个含 DISPIMG 公式的单元格。"""

    sheet: str
    cell: str
    formula: str
    formula_status: str
    image_id: str | None
    resources: tuple[ImageResource, ...]
    status: str
    reason: str = ""


@dataclass(frozen=True)
class AnchorImage:
    relationship_id: str
    media: MediaInfo
    external: bool = False


@dataclass(frozen=True)
class PictureAnchor:
    """普通浮动图片锚点（含精确到单元格的 from/to 标记）。"""

    sheet: str
    part: str
    index: int
    kind: str
    top_row: int | None
    left_column: int | None
    bottom_row: int | None
    right_column: int | None
    images: tuple[AnchorImage, ...] = ()

    def exact_cell(self, column: int) -> int | None:
        """起止锚点都落在同一单元格时返回该行（1 起始）；否则返回 ``None``。"""
        if (self.top_row is not None and self.bottom_row == self.top_row
                and self.left_column == column - 1 and self.right_column == column - 1):
            return self.top_row + 1
        return None


class Workbook:
    """已完整读取到内存的工作簿；构造后不可变，可重复查询。"""

    def __init__(self, data: bytes, *, source_name: str = "",
                 max_expanded_bytes: int = MAX_EXPANDED_BYTES):
        if not data:
            raise OoxmlError("empty_file", "空文件")
        self.source_name = source_name
        self.source_sha256 = sha256(data)
        self.source_size = len(data)
        self._parts: dict[str, bytes] = {}
        self._media_cache: dict[str, MediaInfo] = {}
        self._image_definitions: dict[str, list[tuple[str, str, MediaInfo, bool]]] | None = None

        try:
            with ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(set(names)) != len(names):
                    raise OoxmlError("duplicate_zip_entry", "ZIP 存在重名条目")
                if len(infos) > MAX_PARTS:
                    raise OoxmlError("too_many_parts", f"ZIP 条目数超过上限 {MAX_PARTS}")
                total = sum(info.file_size for info in infos)
                if total > max_expanded_bytes:
                    raise OoxmlError("expanded_size_exceeded", f"解压后体积超过上限 {max_expanded_bytes} 字节")
                if archive.testzip() is not None:
                    raise OoxmlError("crc_mismatch", "ZIP CRC 校验失败")
                for info in infos:
                    if info.file_size > MAX_PART_BYTES:
                        raise OoxmlError("part_too_large", f"部件过大：{info.filename}")
                    self._parts[info.filename] = archive.read(info.filename)
        except BadZipFile as exc:
            raise OoxmlError("unreadable_package", "不是可读的 XLSX/ZIP 包") from exc

        if "xl/workbook.xml" not in self._parts:
            raise OoxmlError("not_spreadsheet", "缺少 xl/workbook.xml，不是 Excel 工作簿")

        self.shared_strings = self._read_shared_strings()
        self.sheets = tuple(self._read_sheets())

    # ------------------------------------------------------------------ 载入
    @classmethod
    def open(cls, path: str | Path, **kwargs) -> "Workbook":
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise OoxmlError("unreadable_file", f"无法读取文件：{path.name}（{exc}）") from exc
        return cls(data, source_name=path.name, **kwargs)

    def _read_shared_strings(self) -> tuple[str, ...]:
        data = self._parts.get("xl/sharedStrings.xml")
        if data is None:
            return ()
        return tuple(_text(element) for element in _xml(data, part="xl/sharedStrings.xml"))

    def _relationships(self, part: str) -> dict[str, dict[str, str]]:
        data = self._parts.get(relationship_part(part))
        if data is None:
            return {}
        return {
            element.attrib["Id"]: dict(element.attrib)
            for element in _xml(data, part=relationship_part(part))
            if "Id" in element.attrib
        }

    def _read_sheets(self) -> list[Sheet]:
        root = _xml(self._parts["xl/workbook.xml"], part="xl/workbook.xml")
        relationships = self._relationships("xl/workbook.xml")
        sheets = []
        for element in root.findall("s:sheets/s:sheet", NS):
            name = element.get("name") or ""
            rel_id = element.get(f"{{{R}}}id")
            relation = relationships.get(rel_id or "")
            if relation is None:
                raise OoxmlError("missing_sheet_relationship", f"页签 {name} 缺少关系", sheet=name)
            if relation.get("TargetMode") == "External":
                raise OoxmlError("external_sheet_unsupported", f"页签 {name} 指向外部工作簿", sheet=name)
            part = resolve_part("xl/workbook.xml", relation.get("Target", ""))
            if part not in self._parts:
                raise OoxmlError("missing_sheet_part", f"页签 {name} 的部件缺失：{part}", sheet=name)
            sheets.append(self._read_sheet(name, part, element.get("state", "visible")))
        if not sheets:
            raise OoxmlError("no_worksheets", "工作簿没有页签")
        return sheets

    def _read_sheet(self, name: str, part: str, state: str) -> Sheet:
        root = _xml(self._parts[part], part=part)
        cells: dict[str, Cell] = {}
        for element in root.findall("s:sheetData/s:row/s:c", NS):
            ref = (element.get("r") or "").upper()
            if not ref:
                raise OoxmlError("cell_without_reference", "单元格缺少 r 属性", sheet=name)
            column, row = cell_position(ref)
            formula_element = element.find("s:f", NS)
            kind = element.get("t", "n")
            raw = element.findtext("s:v", "", NS)
            if kind == "s":
                try:
                    value = self.shared_strings[int(raw)] if raw else ""
                except (ValueError, IndexError) as exc:
                    raise OoxmlError(
                        "broken_shared_string", f"共享字符串索引越界：{ref}", sheet=name, cell=ref
                    ) from exc
            elif kind == "inlineStr":
                value = _text(element.find("s:is", NS))
            elif kind == "b":
                value = "TRUE" if raw == "1" else ("FALSE" if raw else "")
            else:
                value = raw or ""
            cells[ref] = Cell(
                ref=ref, row=row, column=column, value=value, kind=kind,
                formula=None if formula_element is None else (formula_element.text or ""),
                formula_attributes={} if formula_element is None else dict(formula_element.attrib),
                style=element.get("s"),
            )
        merges = []
        for element in root.findall("s:mergeCells/s:mergeCell", NS):
            ref = element.get("ref") or ""
            start, _, end = ref.partition(":")
            if not end:
                end = start
            left, top = cell_position(start)
            right, bottom = cell_position(end)
            merges.append(Merge(ref=ref, top=top, left=left, bottom=bottom, right=right))
        dimension_element = root.find("s:dimension", NS)
        return Sheet(
            name=name, part=part, state=state, cells=cells, merges=tuple(merges),
            dimension=None if dimension_element is None else dimension_element.get("ref"),
            formulas=_effective_formulas(name, cells),
        )

    # ------------------------------------------------------------------ 查询
    @property
    def part_names(self) -> frozenset[str]:
        return frozenset(self._parts)

    def sheet(self, name: str) -> Sheet:
        exact = [sheet for sheet in self.sheets if sheet.name == name]
        if exact:
            return exact[0]
        folded = [sheet for sheet in self.sheets if sheet.name.casefold() == name.casefold()]
        if len(folded) == 1:
            return folded[0]
        raise OoxmlError("sheet_not_found", f"找不到唯一页签：{name!r}")

    def find_sheet(self, label: str) -> Sheet:
        """按页签名包含 ``label`` 匹配；真实模板的页签常带“节点①”等前缀。"""
        matches = [sheet for sheet in self.sheets if label in sheet.name]
        if not matches:
            raise OoxmlError("sheet_not_found", f"找不到页签：{label!r}")
        if len(matches) > 1:
            found = "、".join(sheet.name for sheet in matches)
            raise OoxmlError(
                "sheet_ambiguous", f"页签“{label}”应唯一匹配，实际匹配：{found}"
            )
        return matches[0]

    # ------------------------------------------------------------------ 媒体
    def media_bytes(self, part: str | None) -> bytes:
        if not part:
            raise OoxmlError("missing_media_part", "图片缺少媒体部件名")
        data = self._parts.get(part)
        if data is None:
            raise OoxmlError("missing_media_part", f"媒体部件不存在：{part}")
        return data

    def media(self, part: str | None, *, external: bool = False) -> MediaInfo:
        if external:
            return MediaInfo(part=None, status="external_unsupported",
                             error="图片指向外部链接，离线解析不取外链")
        if not part:
            return MediaInfo(part=None, status="missing_resource", error="缺少关系目标")
        if part in self._media_cache:
            return self._media_cache[part]
        data = self._parts.get(part)
        if data is None:
            info = MediaInfo(part=part, status="missing_resource", error="媒体部件不在包内")
        else:
            info = MediaInfo(
                part=part, status="resource_found", sha256=sha256(data), size=len(data),
            )
            info = _decode_media(info, data)
        self._media_cache[part] = info
        return info

    def image_definitions(self) -> dict[str, list[ImageResource]]:
        """WPS ``cellimages`` 中“图片 ID → 资源”的定义表；同一 ID 可有多个定义。"""
        if self._image_definitions is not None:
            return {key: list(value) for key, value in self._image_definitions.items()}
        definitions: dict[str, list[ImageResource]] = {}
        for part in sorted(self._parts):
            if not _CELLIMAGES_PART.search(part):
                continue
            relationships = self._relationships(part)
            for picture in _xml(self._parts[part], part=part).iter():
                if picture.tag.rsplit("}", 1)[-1] != "pic":
                    continue
                props = next((e for e in picture.iter() if e.tag.endswith("}cNvPr")), None)
                blip = next((e for e in picture.iter() if e.tag.endswith("}blip")), None)
                if props is None or blip is None:
                    continue
                image_id = props.get("name") or ""
                rel_id = blip.get(f"{{{R}}}embed", "")
                relation = relationships.get(rel_id, {})
                external = relation.get("TargetMode") == "External"
                target = None
                if relation.get("Target"):
                    target = resolve_part(part, relation["Target"])
                definitions.setdefault(image_id, []).append(ImageResource(
                    image_id=image_id, definition_part=part, relationship_id=rel_id,
                    media=self.media(target, external=external), external=external,
                ))
        self._image_definitions = definitions
        return {key: list(value) for key, value in definitions.items()}

    def image_references(self) -> tuple[ImageReference, ...]:
        """所有含 DISPIMG 的单元格；含条件/表达式形式的待核记录。"""
        references = []
        for sheet in self.sheets:
            for ref, formula in sorted(sheet.formulas.items()):
                if "DISPIMG" not in formula.upper():
                    continue
                stripped = formula.strip()
                if stripped.startswith("="):
                    stripped = stripped[1:]
                match = _DISPIMG.fullmatch(stripped.strip())
                if match:
                    image_id = match.group(1)
                    resources = self.image_definitions().get(image_id, [])
                    if not resources:
                        status = "missing_definition"
                    elif len(resources) > 1:
                        status = "ambiguous_definition"
                    else:
                        status = "resolved"
                    references.append(ImageReference(
                        sheet=sheet.name, cell=ref, formula=formula,
                        formula_status="direct_image_call", image_id=image_id,
                        resources=tuple(resources), status=status,
                    ))
                    continue
                candidates = []
                for candidate in _DISPIMG.finditer(stripped):
                    candidates.extend(self.image_definitions().get(candidate.group(1), []))
                references.append(ImageReference(
                    sheet=sheet.name, cell=ref, formula=formula,
                    formula_status="expression_display_not_evaluated", image_id=None,
                    resources=tuple(candidates), status="requires_review",
                    reason="条件或动态图片公式，离线无法确认实际显示哪张图；需在客户端核对",
                ))
        return tuple(references)

    def picture_anchors(self) -> tuple[PictureAnchor, ...]:
        anchors = []
        for sheet in self.sheets:
            for relation in self._relationships(sheet.part).values():
                if not (relation.get("Type") or "").endswith("/drawing"):
                    continue
                if relation.get("TargetMode") == "External":
                    continue
                part = resolve_part(sheet.part, relation.get("Target", ""))
                if part not in self._parts:
                    raise OoxmlError("missing_drawing_part", f"绘图部件缺失：{part}", sheet=sheet.name)
                relationships = self._relationships(part)
                for index, anchor in enumerate(_xml(self._parts[part], part=part)):
                    kind = anchor.tag.rsplit("}", 1)[-1]
                    markers: dict[str, dict[str, int]] = {}
                    for marker in anchor:
                        local = marker.tag.rsplit("}", 1)[-1]
                        if local in ("from", "to"):
                            markers[local] = {
                                e.tag.rsplit("}", 1)[-1]: int(e.text or 0) for e in marker
                                if (e.text or "").strip().lstrip("-").isdigit()
                            }
                    images = []
                    for element in anchor.iter():
                        if not element.tag.endswith("}blip"):
                            continue
                        rel_id = element.get(f"{{{R}}}embed", "")
                        relation = relationships.get(rel_id, {})
                        external = relation.get("TargetMode") == "External"
                        target = resolve_part(part, relation["Target"]) if relation.get("Target") else None
                        images.append(AnchorImage(
                            relationship_id=rel_id, external=external,
                            media=self.media(target, external=external),
                        ))
                    anchors.append(PictureAnchor(
                        sheet=sheet.name, part=part, index=index, kind=kind,
                        top_row=markers.get("from", {}).get("row"),
                        left_column=markers.get("from", {}).get("col"),
                        bottom_row=markers.get("to", {}).get("row"),
                        right_column=markers.get("to", {}).get("col"),
                        images=tuple(images),
                    ))
        return tuple(anchors)


def _effective_formulas(sheet_name: str, cells: Mapping[str, Cell]) -> dict[str, str]:
    masters: dict[str, tuple[str, str]] = {}
    for ref, cell in cells.items():
        if cell.formula is None:
            continue
        if cell.formula_attributes.get("t") == "shared" and cell.formula:
            key = cell.formula_attributes.get("si")
            if key in masters:
                raise OoxmlError(
                    "ambiguous_shared_formula_master",
                    f"共享公式有多个主公式：si={key}", sheet=sheet_name, cell=ref,
                )
            masters[key] = (ref, cell.formula)
    result: dict[str, str] = {}
    for ref, cell in cells.items():
        if cell.formula is None:
            continue
        if cell.formula_attributes.get("t") != "shared":
            result[ref] = cell.formula
            continue
        key = cell.formula_attributes.get("si")
        master = masters.get(key)
        if master is None:
            raise OoxmlError(
                "shared_formula_master_missing",
                f"共享公式缺少主公式：si={key}", sheet=sheet_name, cell=ref,
            )
        if master[0] == ref:
            result[ref] = master[1]
        else:
            result[ref] = shift_formula_to(master[1], master[0], ref)
    return result


def _decode_media(info: MediaInfo, data: bytes) -> MediaInfo:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖里已有 Pillow，缺库时明确报告
        return MediaInfo(
            part=info.part, status=info.status, sha256=info.sha256, size=info.size,
            decode_status="not_run_Pillow_unavailable",
            error="未安装 Pillow，未做像素解码校验",
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            # verify() 只校验容器，必须再 load() 真正解码像素。
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return MediaInfo(
            part=info.part, status=info.status, sha256=info.sha256, size=info.size,
            format=image_format, decode_status="passed",
        )
    except Exception as exc:  # noqa: BLE001 - 任何解码异常都归为损坏资源
        return MediaInfo(
            part=info.part, status="corrupt_resource", sha256=info.sha256, size=info.size,
            decode_status=f"failed:{type(exc).__name__}",
            error=f"图片资源无法解码：{type(exc).__name__}",
        )
