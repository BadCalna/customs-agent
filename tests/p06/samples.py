"""完全合成，真实样本仅通过环境变量传入，不提交。"""
import tempfile
from pathlib import Path
from tests.p04.builders import build_package, sheet_of, DECLARATION_SHEET
from tests.p04.fixtures import png


def sample(*, order=(1, 2, 3), missing_sheet=False, missing_key=False, ambiguous=False, blank_raw=False, image_column=True):
    declarations = [["SYNTHETIC-C", str(n), f"合成部件{n}", "" if blank_raw else "0000000000",
                     "" if blank_raw else "用途：合成试验", "" if blank_raw else "13%", None, "历史备注原文"] for n in order]
    if missing_key:
        declarations[0][0] = None
    if ambiguous:
        declarations[1][1] = declarations[0][1]
    headers=None if image_column else ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "退税率", "其他", "历史意见")
    pkg=build_package(declaration_rows=declarations, packing_rows=[
        ["SYNTHETIC-C", str(n), f"合成部件{n}", "", "SYNTHETIC-SPEC", "M", "", 1, "件"] for n in range(1,4)
    ], include_packing=not missing_sheet, declaration_headers=headers)
    sheet=sheet_of(pkg,DECLARATION_SHEET)
    if image_column:
        for row,n in enumerate(order,2):
            if n==1: sheet.formula(f"G{row}", '_xlfn.DISPIMG("ID_OK",1)')
            if n==3: sheet.formula(f"G{row}", '_xlfn.DISPIMG("ID_MISSING",1)')
        pkg.cell_image("ID_OK", "xl/media/synthetic.png")
        pkg.image("xl/media/synthetic.png",png(b'\x10\x80\x20'))
    with tempfile.TemporaryDirectory() as tmp:
        return Path(pkg.build(Path(tmp)/"synthetic.xlsx")).read_bytes()


def broken_drawing(data, *, sheet_number=1):
    """仅损坏 optional drawing 部件，工作簿核心/商品/成员保持完整。"""
    import io
    from zipfile import ZipFile, ZIP_DEFLATED
    with ZipFile(io.BytesIO(data)) as source:
        parts={name:source.read(name) for name in source.namelist()}
    sheet=f'xl/worksheets/sheet{sheet_number}.xml'
    parts[sheet]=parts[sheet].replace(b'</worksheet>',b'<drawing xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="BrokenDrawing"/></worksheet>')
    parts[f'xl/worksheets/_rels/sheet{sheet_number}.xml.rels']=b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="BrokenDrawing" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/broken.xml"/></Relationships>'
    parts['xl/drawings/broken.xml']=b'<broken'
    output=io.BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for name,content in parts.items():archive.writestr(name,content)
    return output.getvalue()
