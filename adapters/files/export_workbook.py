"""P20：固定结果集写回模板副本；不改原件、不执行归类或人工确认。

插列和静态图片转换复用 P03 已验收的保真实现。结果填充仅修改明确的两列，
其余单元格、页签、媒体和已有格式逐项审计后才返回文件字节。
"""
from __future__ import annotations

import copy
import hashlib
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZipFile

from apps.api.schemas import ComparisonStatus, IssueEntry, ResultMode, RunKind
from .declarations import extract_declarations
from .ooxml import NS, S, Workbook, col_number
from experiments.excel_fidelity.compat_images import export_excel2016_images
from experiments.excel_fidelity.conclusion import append_precheck_column
from experiments.excel_fidelity.insert import canonical, insert_columns
from experiments.excel_fidelity.workbook import FidelityError, Workbook as FidelityWorkbook


class ExportError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _xml(data: bytes, *, part: str = "部件") -> ET.Element:
    """解析原件或导出结果的 XML 部件。

    原件里的 XML 可能损坏（例如样式表被替换成畸形内容）。这类问题属于输入文件问题，
    必须转成结构化校验错误（422），不能让 ``ET.ParseError`` 冒到入口变成 500。
    """
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ExportError("source_workbook_invalid",
                          f"原件中的 {part} 不是合法 XML，无法安全导出（{exc}）") from exc
    except LookupError as exc:
        # 未知编码（encoding="unknown-codec" 等）：ElementTree 抛 LookupError，同样按输入问题处理。
        raise ExportError("source_workbook_invalid",
                          f"原件中的 {part} 声明了不支持的编码，无法安全导出（{exc}）") from exc


def _decode_utf8(data: bytes, *, part: str) -> str:
    """把 XML 部件解码为 UTF-8；非 UTF-8 文本属于输入工作簿格式问题。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportError("source_workbook_invalid",
                          f"原件中的 {part} 不是有效 UTF-8 文本，无法安全导出（{exc}）") from exc


@dataclass(frozen=True)
class ExportLine:
    item_id: str
    version_id: str
    run_id: str
    sheet: str
    row: int
    suggested_hs: str | None = None
    comparison_status: str | None = None
    reason: str = ""
    confidence: float | None = None
    confidence_label: str = ""
    confidence_note: str = ""
    issues: tuple[IssueEntry, ...] = ()
    status: str = "succeeded"
    error_message: str = ""


@dataclass(frozen=True)
class ExportSelection:
    task_id: str
    version_id: str
    run_id: str
    source_sha256: str
    lines: tuple[ExportLine, ...]
    result_mode: str = "mock"
    run_kind: str = "precheck"
    submission_id: str | None = None
    decision_id: str | None = None
    version_number: int = 1

    def validate(self, source: bytes) -> None:
        if not all(isinstance(v, str) and v.strip() for v in (self.task_id, self.version_id, self.run_id)):
            raise ExportError("missing_result_reference", "导出必须明确指定任务、文件版本和执行")
        if hashlib.sha256(source).hexdigest() != self.source_sha256:
            raise ExportError("source_mismatch", "文件内容与选定结果集的原件摘要不一致")
        if self.result_mode not in {v.value for v in ResultMode} or self.run_kind not in {v.value for v in RunKind}:
            raise ExportError("invalid_result_mode", "未知的结果来源或执行类型")
        if self.run_kind == RunKind.EXPERT_CHECK and not self.submission_id:
            raise ExportError("submission_required", "专家核查导出必须绑定提交快照")
        if self.run_kind == RunKind.PRECHECK and (self.submission_id or self.decision_id):
            raise ExportError("result_reference_mismatch", "预审结果不能混入专家提交或决定")
        if not self.lines:
            raise ExportError("empty_result_set", "没有明确的商品结果集可供导出")
        ids, positions = set(), set()
        for line in self.lines:
            if line.version_id != self.version_id or line.run_id != self.run_id:
                raise ExportError("result_reference_mismatch", "商品结果混用了文件版本或执行")
            if not line.item_id or line.item_id in ids or (line.sheet, line.row) in positions:
                raise ExportError("duplicate_result", "商品或源位置重复，不能重复写入")
            ids.add(line.item_id)
            positions.add((line.sheet, line.row))
            if isinstance(line.row, bool) or not isinstance(line.row, int) or line.row < 1:
                raise ExportError("invalid_source_row", "商品源行无效")
            if line.suggested_hs is not None and not re.fullmatch(r"[0-9]{10}", line.suggested_hs):
                raise ExportError("invalid_suggested_hs", "推荐 HS 必须是十位数字文本或空值")
            if line.comparison_status is not None and line.comparison_status not in {v.value for v in ComparisonStatus}:
                raise ExportError("invalid_comparison", "未知的预审比较结论")
            if line.confidence is not None and (not math.isfinite(line.confidence) or not 0 <= line.confidence <= 1):
                raise ExportError("invalid_confidence", "置信度数值必须介于 0 和 1")
            if line.confidence_label not in ("", "高", "中", "低"):
                raise ExportError("invalid_confidence", "置信度等级必须由上游明确提供高、中、低")
            if line.status not in ("succeeded", "failed", "queued", "running"):
                raise ExportError("invalid_row_status", "未知的执行行状态")
            for issue in line.issues:
                if not isinstance(issue, IssueEntry):
                    raise ExportError("invalid_issue", "问题必须符合已发布的问题契约")


@dataclass(frozen=True)
class ExportArtifact:
    content: bytes = field(repr=False)
    filename: str
    manifest: dict


def source_records(source: bytes, selection: ExportSelection):
    """源位置验证与项号格式无关；导出不重复执行上传阶段的项号校验。"""
    selection.validate(source)
    book = Workbook(source)
    extracted = extract_declarations(book)
    records = {(r.sheet, r.row): r for r in extracted.records}
    if set(records) != {(line.sheet, line.row) for line in selection.lines}:
        raise ExportError("incomplete_result_set", "选定结果必须与该文件版本的申报商品逐行对应，不能串行或漏行")
    return book, extracted, records


LABELS = {
    ComparisonStatus.ASSOCIATION_ERROR: "资料对应待确认",
    ComparisonStatus.SUPPLIER_MISSING: "供应商原码未填",
    ComparisonStatus.SUPPLIER_INVALID: "供应商原码异常",
    ComparisonStatus.NEEDS_CONFIRMATION: "待确认",
    ComparisonStatus.CONSISTENT: "推荐码与原码一致",
    ComparisonStatus.INCONSISTENT: "推荐码与原码不一致",
}


def conclusion_segments(selection: ExportSelection, line: ExportLine) -> list[tuple[str, bool]]:
    segments = []
    if selection.result_mode == ResultMode.MOCK:
        segments.append(("【测试结果，供导出验收】", False))
    if line.status != "succeeded":
        text = "技术执行失败：" + (line.error_message or "未提供失败原因") if line.status == "failed" else "尚未完成核查"
        segments.append((text, False))
        return segments
    segments.append((LABELS.get(line.comparison_status, "尚未得出比较结论"), False))
    segments.append(("依据：" + (line.reason or "未提供判断依据，需人工核对"), False))
    if line.confidence_label:
        segments.append(("预审置信度：" + line.confidence_label, False))
    elif line.confidence is not None:
        segments.append((f"置信度数值：{line.confidence:g}（等级未提供）", False))
    if line.confidence_note:
        segments.append(("置信度说明：" + line.confidence_note, False))
    for issue in line.issues:
        segments.append(("问题：" + issue.message, False))
        if issue.missing:
            segments.append(("需补充/核对：" + issue.missing, True))
    return segments


def manifest(selection: ExportSelection, kind: str) -> dict:
    return {"kind": kind, "task_id": selection.task_id, "version_id": selection.version_id,
            "version_number": selection.version_number, "run_id": selection.run_id,
            "submission_id": selection.submission_id, "decision_id": selection.decision_id,
            "run_kind": selection.run_kind, "result_mode": selection.result_mode,
            "source_sha256": selection.source_sha256, "item_count": len(selection.lines)}


def text_value(text: str) -> str:
    if not isinstance(text, str) or len(text) > 32767 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", text):
        raise ExportError("invalid_cell_text", "结果文本含不支持字符或超过 Excel 单格长度，不能截断导出")
    return escape(text)


def _prefix(raw: str, tag: str) -> str:
    m = re.search(r"<((?:[A-Za-z_][\w.-]*:)?)" + re.escape(tag) + r"\b", raw)
    if not m:
        raise ExportError("unsupported_xml", "工作簿部件结构不支持安全填充")
    return m[1]


def _append(raw: str, tag: str, fragments: list[str], count: int) -> str:
    prefix = _prefix(raw, tag)
    # All supported style containers are ordinary paired elements.
    match = re.search(rf'<{prefix}{tag}\b[^>]*>.*?</{prefix}{tag}>', raw, re.S)
    if not match:
        raise ExportError("unsupported_styles", "样式容器缺失或使用不支持的结构")
    value = match[0]
    opening = value[:value.index(">") + 1]
    opening = re.sub(r'\s+count="[^"]*"', "", opening)
    opening = opening[:-1] + f' count="{count}">'
    value = opening + value[value.index(">") + 1:]
    value = value.replace(f'</{prefix}{tag}>', ''.join(fragments) + f'</{prefix}{tag}>')
    return raw[:match.start()] + value + raw[match.end():]


def _style_index(raw: str | None, *, label: str) -> int:
    if raw is None:
        return 0
    try:
        index = int(raw)
    except (TypeError, ValueError) as exc:
        raise ExportError("invalid_style", f"{label}无效：{raw!r}") from exc
    if index < 0:
        raise ExportError("invalid_style", f"{label}不能为负数：{index}")
    return index


def _styles(raw: bytes, requested: set[tuple[int, str]]) -> tuple[bytes, dict]:
    root = _xml(raw, part="xl/styles.xml")
    fonts, fills, xfs = (root.find("s:" + tag, NS) for tag in ("fonts", "fills", "cellXfs"))
    if any(node is None or not len(node) for node in (fonts, fills, xfs)):
        raise ExportError("unsupported_styles", "模板缺少基础样式，不能安全应用结果标色")
    appended = {"fonts": [], "fills": [], "cellXfs": []}
    result = {}
    for base, level in sorted(requested):
        if not 0 <= base < len(xfs):
            raise ExportError("invalid_style", "结果单元格引用了不存在的样式")
        xf = copy.deepcopy(xfs[base])
        alignment = xf.find("s:alignment", NS)
        if alignment is None:
            alignment = ET.Element(f"{{{S}}}alignment")
            position = next((i for i, node in enumerate(xf) if node.tag in (f"{{{S}}}protection", f"{{{S}}}extLst")), len(xf))
            xf.insert(position, alignment)
        alignment.set("wrapText", "1")
        xf.set("applyAlignment", "1")
        if level in ("低", "中"):
            font_id = _style_index(xf.get("fontId"), label="模板字体索引")
            if not 0 <= font_id < len(fonts):
                raise ExportError("invalid_style", "模板字体索引无效")
            font = copy.deepcopy(fonts[font_id])
            for node in list(font.findall("s:color", NS)):
                font.remove(node)
            ET.SubElement(font, f"{{{S}}}color", rgb="FF9C0006" if level == "低" else "FF9C6500")
            fill = ET.Element(f"{{{S}}}fill")
            pattern = ET.SubElement(fill, f"{{{S}}}patternFill", patternType="solid")
            ET.SubElement(pattern, f"{{{S}}}fgColor", rgb="FFFFC7CE" if level == "低" else "FFFFF2CC")
            ET.SubElement(pattern, f"{{{S}}}bgColor", indexed="64")
            xf.set("fontId", str(len(fonts) + len(appended["fonts"])))
            xf.set("fillId", str(len(fills) + len(appended["fills"])))
            xf.set("applyFont", "1")
            xf.set("applyFill", "1")
            appended["fonts"].append(ET.tostring(font, encoding="unicode"))
            appended["fills"].append(ET.tostring(fill, encoding="unicode"))
        result[(base, level)] = len(xfs) + len(appended["cellXfs"])
        appended["cellXfs"].append(ET.tostring(xf, encoding="unicode"))
    value = _decode_utf8(raw, part="xl/styles.xml")
    for tag, old in (("fonts", fonts), ("fills", fills), ("cellXfs", xfs)):
        if appended[tag]:
            value = _append(value, tag, appended[tag], len(old) + len(appended[tag]))
    return value.encode("utf-8"), result


def _cell(ref: str, segments: list[tuple[str, bool]], style: int, prefix: str) -> str:
    text_value("\n".join(text for text, _ in segments))
    content = []
    for index, (text, red) in enumerate(segments):
        properties = f'<{prefix}rPr><{prefix}color rgb="FF9C0006"/></{prefix}rPr>' if red else ""
        content.append(f'<{prefix}r>{properties}<{prefix}t xml:space="preserve">'
                       + text_value(("\n" if index else "") + text) + f'</{prefix}t></{prefix}r>')
    return f'<{prefix}c r="{ref}" s="{style}" t="inlineStr"><{prefix}is>' + ''.join(content) + f'</{prefix}is></{prefix}c>'


def _fill_sheet(raw: bytes, writes: dict[str, str]) -> bytes:
    value = _decode_utf8(raw, part="工作表")
    row_prefix = _prefix(value, "row")
    cell_prefix = _prefix(value, "c")
    grouped: dict[int, dict[str, str]] = {}
    for ref, xml in writes.items():
        row = int(re.search(r"[0-9]+$", ref)[0])
        grouped.setdefault(row, {})[ref] = xml
    visited = set()
    row_pattern = rf'(<{row_prefix}row\b[^>]*?)(?:/>|>(.*?)</{row_prefix}row>)'
    cell_pattern = rf'<{cell_prefix}c\b[^>]*?(?:/>|>.*?</{cell_prefix}c>)'

    def replace_row(match):
        row_attr = re.search(r'\br="([0-9]+)"', match[1])
        if not row_attr:
            raise ExportError("unsupported_xml", "工作表行缺少 r 属性，不能安全填充")
        number = int(row_attr[1])
        if number not in grouped:
            return match[0]
        visited.add(number)
        body = match[2] or ""
        pending = dict(grouped[number])
        pieces, end = [], 0
        for cell in re.finditer(cell_pattern, body, re.S):
            ref_attr = re.search(r'\br="([A-Za-z]+[0-9]+)"', cell[0])
            if not ref_attr:
                raise ExportError("unsupported_xml", "工作表单元格缺少 r 属性，不能安全填充")
            ref = ref_attr[1].upper()
            before = sorted((r for r in pending if col_number(re.match(r"[A-Z]+", r)[0]) < col_number(re.match(r"[A-Z]+", ref)[0])),
                            key=lambda r: col_number(re.match(r"[A-Z]+", r)[0]))
            pieces.append(body[end:cell.start()])
            pieces.extend(pending.pop(r) for r in before)
            pieces.append(pending.pop(ref, cell[0]))
            end = cell.end()
        pieces.append(body[end:])
        pieces.extend(pending[r] for r in sorted(pending, key=lambda r: col_number(re.match(r"[A-Z]+", r)[0])))
        return match[1] + ">" + ''.join(pieces) + f'</{row_prefix}row>'

    value = re.sub(row_pattern, replace_row, value, flags=re.S)
    if visited != set(grouped):
        raise ExportError("source_row_missing", "模板中没有指定商品行，不能凭空写入")
    return value.encode("utf-8")


def audit_fill(before: bytes, after: bytes, sheet_name: str, writes: dict[str, str]) -> None:
    left, right = Workbook(before), Workbook(after)
    sheet = left.sheet(sheet_name)
    if left.part_names != right.part_names:
        raise ExportError("fidelity_failed", "结果写入改变了非预期的工作簿部件")
    for part in left.part_names - {sheet.part, "xl/styles.xml"}:
        if left.media_bytes(part) != right.media_bytes(part):
            raise ExportError("fidelity_failed", "原页签、图片或其他部件发生非预期变化")
    a, b = _xml(left.media_bytes(sheet.part), part=sheet.part), _xml(right.media_bytes(sheet.part), part=sheet.part)
    for root in (a, b):
        for row in root.findall("s:sheetData/s:row", NS):
            for cell in list(row):
                if cell.get("r") in writes:
                    row.remove(cell)
    if canonical(a) != canonical(b):
        raise ExportError("fidelity_failed", "结果列以外的单元格或页签元数据发生变化")
    actual = right.sheet(sheet_name)
    for ref, expected in writes.items():
        prefix = _prefix(expected, "c")
        declaration = f' xmlns:{prefix[:-1]}="{S}"' if prefix else ""
        node = ET.fromstring('<root xmlns="' + S + '"' + declaration + '>' + expected + '</root>')[0]
        found = _xml(right.media_bytes(sheet.part), part=sheet.part).find(f'.//s:c[@r="{ref}"]', NS)
        if found is None or canonical(found) != canonical(node) or actual.cell(ref).formula is not None:
            raise ExportError("fidelity_failed", "结果文本或标色未完整写入")
    old_styles = _xml(left.media_bytes("xl/styles.xml"), part="xl/styles.xml")
    new_styles = _xml(right.media_bytes("xl/styles.xml"), part="xl/styles.xml")
    for tag in ("fonts", "fills", "cellXfs"):
        old, new = old_styles.find("s:" + tag, NS), new_styles.find("s:" + tag, NS)
        if new is None or len(new) < len(old) or any(canonical(x) != canonical(y) for x, y in zip(old, new)):
            raise ExportError("fidelity_failed", "已有格式发生变化")
        for child in list(new)[len(old):]:
            new.remove(child)
        if old.get("count") is None:
            new.attrib.pop("count", None)
        else:
            new.set("count", old.get("count"))
    if canonical(old_styles) != canonical(new_styles):
        raise ExportError("fidelity_failed", "已有样式元数据发生变化")


def _zip_replace(source: bytes, changes: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with ZipFile(io.BytesIO(source)) as original, ZipFile(output, "w") as archive:
        archive.comment = original.comment
        for info in original.infolist():
            archive.writestr(info, changes.get(info.filename, original.read(info.filename)))
    return output.getvalue()


def normalize_export_views(source: bytes) -> tuple[bytes, dict]:
    """Open exported sheets in normal view; retain cells, panes and print settings."""
    book = Workbook(source)
    changes, views = {}, []
    for sheet in book.sheets:
        raw = book.media_bytes(sheet.part)
        expected = _xml(raw, part=sheet.part)
        changed = []
        for node in expected.findall("s:sheetViews/s:sheetView", NS):
            previous = node.get("view")
            if previous in ("pageBreakPreview", "pageLayout"):
                node.set("view", "normal")
                changed.append({"sheet": sheet.name, "workbook_view_id": node.get("workbookViewId"),
                                "before": previous, "after": "normal"})
        if not changed:
            continue
        value = _decode_utf8(raw, part=sheet.part)
        container_prefix = _prefix(value, "sheetViews")
        container = re.compile(rf'<{container_prefix}sheetViews\b[^>]*>.*?</{container_prefix}sheetViews>', re.S)
        match = container.search(value)
        if not match:
            raise ExportError("unsupported_sheet_view", "页签视图结构不支持安全调整")
        opening = re.compile(r'<(?:[A-Za-z_][\w.-]*:)?sheetView\b[^>]*>')
        view_attribute = re.compile(r'''(\sview\s*=\s*)(["'])(pageBreakPreview|pageLayout)\2''')
        replacement = opening.sub(lambda tag: view_attribute.sub(lambda attr: attr[1] + attr[2] + "normal" + attr[2], tag[0]), match[0])
        updated = (value[:match.start()] + replacement + value[match.end():]).encode("utf-8")
        if canonical(_xml(updated, part=sheet.part)) != canonical(expected):
            raise ExportError("fidelity_failed", "视图调整改变了非预期的页签内容")
        changes[sheet.part] = updated
        views.extend(changed)
    output = _zip_replace(source, changes) if changes else source
    return output, {"operation": "normal_sheet_views", "status": "passed", "views": views,
                    "changed_parts": sorted(changes), "source_sha256": hashlib.sha256(source).hexdigest(),
                    "output_sha256": hashlib.sha256(output).hexdigest()}


def export_precheck(source: bytes, selection: ExportSelection, *, excel2016_images: bool = True) -> ExportArtifact:
    original_book, extracted, _ = source_records(source, selection)
    if any("vba" in part.lower() or "_xmlsignatures/" in part.lower() for part in original_book.part_names):
        raise ExportError("unsupported_package", "宏或签名工作簿需要额外验证，不能直接导出为 XLSX")
    if any(column.field in ("ai_hs", "precheck_conclusion") for column in extracted.layout.ambiguous_columns):
        raise ExportError("ambiguous_result_columns", "结果列存在重名，不能任选一列写入")
    if selection.run_kind != RunKind.PRECHECK:
        raise ExportError("expert_layout_pending", "专家结果模板布局尚未确认，请先导出独立问题清单")
    preprocessing = []
    with tempfile.TemporaryDirectory(prefix="p20-") as directory:
        current = Path(directory) / "source.xlsx"
        current.write_bytes(source)
        fields = extracted.layout.columns
        layout = {"sheet": extracted.layout.sheet, "header_row": extracted.layout.header_row,
                  "data_rows": [r.row for r in extracted.records], "fields": dict(fields)}
        has_ai, has_conclusion = "ai_hs" in fields, "precheck_conclusion" in fields
        if not has_ai and not has_conclusion and not any(k in fields for k in ("confirmed_hs", "confidence")):
            destination = Path(directory) / "columns.xlsx"
            preprocessing.append(insert_columns(current, destination, layout))
            current = destination
        elif has_ai and not has_conclusion:
            destination = Path(directory) / "conclusion.xlsx"
            preprocessing.append(append_precheck_column(current, destination, layout))
            current = destination
        elif not has_ai:
            raise ExportError("missing_result_columns", "模板缺少 AI 推荐列且已有其他结果列，请使用已确认的结果列模板")
        prepared = current.read_bytes()
        book = Workbook(prepared)
        extracted = extract_declarations(book)
        for column in extracted.layout.ambiguous_columns:
            if column.field in ("ai_hs", "precheck_conclusion"):
                raise ExportError("ambiguous_result_columns", "结果列存在重名，不能任选一列写入")
        if excel2016_images and FidelityWorkbook(current).image_records():
            layout = {"sheet": extracted.layout.sheet, "header_row": extracted.layout.header_row,
                      "data_rows": [r.row for r in extracted.records], "fields": dict(extracted.layout.columns)}
            destination = Path(directory) / "images.xlsx"
            preprocessing.append(export_excel2016_images(current, destination, layout))
            prepared = destination.read_bytes()
    book = Workbook(prepared)
    sheet = book.sheet(extracted.layout.sheet)
    root = _xml(book.media_bytes(sheet.part), part=sheet.part)
    if root.find("s:sheetProtection", NS) is not None:
        raise ExportError("protected_sheet", "申报要素页受保护，不能写入")
    requested, cells = set(), {}
    prefix = _prefix(_decode_utf8(book.media_bytes(sheet.part), part=sheet.part), "worksheet")
    for line in selection.lines:
        for key in ("ai_hs", "precheck_conclusion"):
            if key not in extracted.layout.columns:
                raise ExportError("missing_result_columns", "工作表缺少必要的结果列，不能安全写入")
            ref = extracted.layout.columns[key] + str(line.row)
            column = col_number(extracted.layout.columns[key])
            if sheet.enclosing_merge(line.row, column):
                raise ExportError("merged_result_cell", "结果格存在合并，不能把多个商品结果写入同一格")
            old = sheet.cell(ref)
            if old and old.formula is not None:
                raise ExportError("formula_in_result_cell", "结果格含公式，不能静默覆盖")
            base = _style_index(old.style, label="结果单元格样式索引") if old else 0
            level = line.confidence_label if line.status == "succeeded" else ""
            requested.add((base, level))
            segments = ([(line.suggested_hs or "", False)] if key == "ai_hs" and line.status == "succeeded"
                        else [("", False)] if key == "ai_hs" else conclusion_segments(selection, line))
            cells[ref] = (segments, base, level)
    styles, mapping = _styles(book.media_bytes("xl/styles.xml"), requested)
    writes = {ref: _cell(ref, segments, mapping[(base, level)], prefix)
              for ref, (segments, base, level) in cells.items()}
    output = _zip_replace(prepared, {sheet.part: _fill_sheet(book.media_bytes(sheet.part), writes), "xl/styles.xml": styles})
    audit_fill(prepared, output, sheet.name, writes)
    output, view_report = normalize_export_views(output)
    details = manifest(selection, "precheck")
    details.update(output_sha256=hashlib.sha256(output).hexdigest(), result_columns={
        k: extracted.layout.columns[k] for k in ("ai_hs", "precheck_conclusion")},
        preprocessing=preprocessing, view_adjustment=view_report, changed_cells=sorted(writes),
        client_wps="pending", client_excel="pending")
    return ExportArtifact(output, "预审结果" + ("-测试结果" if selection.result_mode == ResultMode.MOCK else "") + ".xlsx", details)
