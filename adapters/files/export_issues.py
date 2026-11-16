"""P20 独立问题/补料清单：仅导出已有问题，不从查询失败推导供应商必填项。"""
from __future__ import annotations

import hashlib
import io
import json
from zipfile import ZIP_DEFLATED, ZipFile

from apps.api.schemas import ResultMode
from .export_workbook import ExportArtifact, ExportSelection, manifest, source_records, text_value
from .ooxml import P, R, S, col_label

HEADERS = ("合同号", "项号", "品名", "原位置", "问题", "需补充信息", "建议处理人", "反馈")
ROLES = {"supplier": "供应商", "operator": "业务员", "expert": "物流/专家"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

STYLES = f'''<styleSheet xmlns="{S}"><fonts count="2"><font><sz val="11"/><name val="微软雅黑"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="微软雅黑"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFB91C1C"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="49" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''


def _text_cell(ref: str, value: str, style: int = 0) -> str:
    return (f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">'
            + text_value(value) + '</t></is></c>')


def export_issues(source: bytes, selection: ExportSelection) -> ExportArtifact:
    _, _, records = source_records(source, selection)
    rows = []
    for line in sorted(selection.lines, key=lambda value: (value.sheet, value.row)):
        record = records[(line.sheet, line.row)]
        common = [record.value("contract"), record.value("item"), record.value("name"),
                  f"{line.sheet}!第{line.row}行"]
        for issue in line.issues:
            rows.append(common + [issue.message, issue.missing or "",
                                  ROLES.get(issue.suggested_role, issue.suggested_role or "待人工指定"), ""])
        if line.status == "failed":
            rows.append(common + ["技术执行失败：" + (line.error_message or "未提供原因"),
                                  "检查执行记录并重试；核实需要后再联系供应商", "物流/专家", ""])
        elif line.status in ("queued", "running"):
            rows.append(common + ["尚未完成核查", "等待执行完成后重新导出", "待人工指定", ""])
    mock = selection.result_mode == ResultMode.MOCK
    title = "独立补料与核对清单" + ("（测试结果）" if mock else "")
    meta = f"文件版本：V{selection.version_number}；阶段：{'业务员预审' if selection.run_kind == 'precheck' else '专家核查'}；问题数：{len(rows)}；建议处理人由人工确认。"
    body = [f'<row r="1" ht="28" customHeight="1">{_text_cell("A1", title, 1)}</row>',
            f'<row r="2" ht="36" customHeight="1">{_text_cell("A2", meta, 3)}</row>',
            '<row r="4" ht="30" customHeight="1">' + ''.join(_text_cell(f"{col_label(i)}4", header, 1)
                                                                       for i, header in enumerate(HEADERS, 1)) + '</row>']
    for number, values in enumerate(rows, 5):
        body.append(f'<row r="{number}" ht="90" customHeight="1">' + ''.join(
            _text_cell(f"{col_label(index)}{number}", value, 2 if index == 8 else 0)
            for index, value in enumerate(values, 1)) + '</row>')
    if not rows:
        body.append('<row r="5" ht="36" customHeight="1">' + _text_cell("A5", "本结果集未返回问题，不表示已经人工审核通过。") + '</row>')
    last = max(5, 4 + len(rows))
    columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
                      for i, width in enumerate((30, 16, 28, 28, 48, 48, 20, 42), 1))
    sheet = (f'<worksheet xmlns="{S}"><dimension ref="A1:H{last}"/><sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             '<sheetFormatPr defaultRowHeight="18"/><cols>' + columns + '</cols><sheetData>' + ''.join(body)
             + f'</sheetData><autoFilter ref="A4:H{max(4, 4 + len(rows))}"/><mergeCells count="2">'
             '<mergeCell ref="A1:H1"/><mergeCell ref="A2:H2"/></mergeCells></worksheet>')
    details = manifest(selection, "issues")
    details["issue_count"] = len(rows)
    props_ns = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
    parts = {
        '[Content_Types].xml': f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/></Types>',
        '_rels/.rels': f'<Relationships xmlns="{P}"><Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="{R}/custom-properties" Target="docProps/custom.xml"/></Relationships>',
        'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="补料清单" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{R}/styles" Target="styles.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': sheet,
        'xl/styles.xml': STYLES,
        'docProps/custom.xml': f'<Properties xmlns="{props_ns}" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property fmtid="{{D5CDD505-2E9C-101B-9397-08002B2CF9AE}}" pid="2" name="ExportSource"><vt:lpwstr>' + text_value(json.dumps(details, ensure_ascii=False)) + '</vt:lpwstr></property></Properties>',
    }
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content)
    data = output.getvalue()
    details["output_sha256"] = hashlib.sha256(data).hexdigest()
    return ExportArtifact(data, "独立补料清单" + ("-测试结果" if mock else "") + ".xlsx", details)
