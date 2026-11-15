"""Synthetic, invented workbook; no supplier data or real classification answers."""

import struct
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

from experiments.excel_fidelity.workbook import P, R, S

DRAW = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def png():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x80\xff")) + chunk(b"IEND", b""))


def cell(ref, value):
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def formula(ref, value, cached="2", attrs=""):
    return f'<c r="{ref}"><f{attrs}>{value}</f><v>{cached}</v></c>'


def worksheet(body, extra=""):
    return f'<worksheet xmlns="{S}" xmlns:r="{R}">{body}{extra}</worksheet>'


def make_fixture(path, overrides=None):
    packed_headers = ["合同号", "合同项号", "合同第一条品名", "附件清单品名", "规格", "厂家提供型号", "合并申报"]
    declared_headers = ["采购合同号", "合同项号", "品名", "海关编码", "附图", "申报要素", "历史意见", "退税率%"]
    pack = '<sheetData><row r="1">' + ''.join(cell(f'{chr(65+i)}1', h) for i, h in enumerate(packed_headers)) + '</row>'
    for i in range(2, 5):
        pack += f'<row r="{i}">' + ''.join(cell(f'{chr(65+j)}{i}', value) for j, value in enumerate(
            ["TEST-CONTRACT", str(i - 1), "合成部件", "合成别名", "SPEC-X", "MODEL-X", "不作为型号"])) + '</row>'
    pack += '</sheetData>'
    declaration = '<dimension ref="A1:H6"/><sheetViews><sheetView workbookViewId="0"><pane xSplit="4" topLeftCell="E2" state="frozen"/><selection activeCell="G2" sqref="G2:G4"/></sheetView></sheetViews><cols><col min="1" max="8" width="20" customWidth="1"/></cols><sheetData>'
    declaration += '<row r="1" spans="1:8">' + ''.join(cell(f'{chr(65+i)}1', h) for i, h in enumerate(declared_headers)) + '</row>'
    for i, item in ((2, "1"), (3, "2"), (4, "3")):
        declaration += f'<row r="{i}" spans="1:8">' + cell(f'A{i}', 'TEST-CONTRACT') + cell(f'B{i}', item) + cell(f'C{i}', '合成部件') + cell(f'D{i}', 'TEST_HS')
        declaration += formula(f'E{i}', '_xlfn.DISPIMG("ID_SYNTHETIC",1)' if i == 2 else '', '',
                               ' t="shared" si="0" ref="E2:E4"' if i == 2 else ' t="shared" si="0"')
        declaration += cell(f'F{i}', '合成字段：合成值') + cell(f'G{i}', '历史说明，不是本轮AI结论') + cell(f'H{i}', '0') + '</row>'
    declaration += '<row r="6">' + cell('C6', '合计') + formula('F6', 'SUM(H2:H4)') + '</row></sheetData><autoFilter ref="A1:H4"/><mergeCells count="1"><mergeCell ref="F6:G6"/></mergeCells><dataValidations count="1"><dataValidation type="whole" sqref="G2:G4"><formula1>H2</formula1></dataValidation></dataValidations><conditionalFormatting sqref="G2:G4"><cfRule type="expression" priority="1"><formula>H2&gt;0</formula></cfRule></conditionalFormatting>'
    declaration += '<drawing r:id="rIdDraw"/><legacyDrawing r:id="rIdVml"/>'
    other = '<sheetData><row r="1">' + formula('A1', "'申报要素'!$H$2+申报要素!G2") + cell('B1', 'H2 must stay literal') + formula('C1', 'IF(A1=&quot;H2&quot;,1,0)') + '</row></sheetData>'
    drawing = f'<xdr:wsDr xmlns:xdr="{DRAW}" xmlns:a="{A}" xmlns:r="{R}"><xdr:twoCellAnchor><xdr:from><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="SYNTHETIC"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rImage"/></xdr:blipFill></xdr:pic><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>'
    parts = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="装箱明细" sheetId="1" r:id="r1"/><sheet name="申报要素" sheetId="2" r:id="r2"/><sheet name="其他页" sheetId="3" r:id="r3"/></sheets><definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="1">申报要素!$A$1:$H$4</definedName><definedName name="LOCAL_RANGE" localSheetId="1">$G$2:$H$4</definedName></definedNames></workbook>',
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{P}">' + ''.join(f'<Relationship Id="r{i}" Type="{R}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1,2,3)) + '</Relationships>',
        'xl/worksheets/sheet1.xml': worksheet(pack),
        'xl/worksheets/sheet2.xml': worksheet(declaration),
        'xl/worksheets/sheet3.xml': worksheet(other),
        'xl/worksheets/_rels/sheet2.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="rIdDraw" Type="{R}/drawing" Target="../drawings/drawing1.xml"/><Relationship Id="rIdComment" Type="{R}/comments" Target="../comments/comment1.xml"/><Relationship Id="rIdVml" Type="{R}/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/></Relationships>',
        'xl/drawings/drawing1.xml': drawing,
        'xl/drawings/_rels/drawing1.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="rImage" Type="{R}/image" Target="../media/test.png"/></Relationships>',
        'xl/comments/comment1.xml': f'<comments xmlns="{S}"><authors><author>Synthetic author</author></authors><commentList><comment ref="G2" authorId="0"><text><t>合成批注</t></text></comment></commentList></comments>',
        'xl/drawings/vmlDrawing1.vml': '<xml xmlns:x="urn:schemas-microsoft-com:office:excel"><x:ClientData><x:Column>6</x:Column><x:Row>1</x:Row><x:Anchor>6, 0, 1, 0, 8, 0, 3, 0</x:Anchor></x:ClientData></xml>',
        'xl/cellimages.xml': f'<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" xmlns:xdr="{DRAW}" xmlns:a="{A}" xmlns:r="{R}"><etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="ID_SYNTHETIC"/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rCellImage"/></xdr:blipFill></xdr:pic></etc:cellImage></etc:cellImages>',
        'xl/_rels/cellimages.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="rCellImage" Type="{R}/image" Target="media/test.png"/></Relationships>',
        'xl/media/test.png': png(),
        'customXml/unknown.xml': '<untouched xmlns="urn:synthetic">preserve byte-for-byte</untouched>',
    }
    parts.update(overrides or {})
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            if value is not None:
                archive.writestr(name, value.encode('utf-8') if isinstance(value,str) else value)
    return path
