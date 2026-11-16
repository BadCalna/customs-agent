"""P05 合成工作簿：覆盖有图/无图/共享公式/缺资源/坏资源/条件公式/重复定义/图片列缺失。

全部商品、合同号与图片都是构造出来的占位内容，**不含真实业务数据**；真实模板只在
本地只读核验（``data/private/p05/``），不写进测试断言。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from adapters.files.ooxml import P, R
from tests.p04.fixtures import DRAW, A, Package, formula, png, standard_package

#: 与 P04 样例一致的页签名；商品行按该页签的行号建立。
DECLARATION_SHEET = "申报要素"
#: 合成图片列的默认列标（表头写作“附图”）。
IMAGE_COLUMN = "E"


def _data_row(sheet, row: int) -> None:
    sheet.text(f"A{row}", "TEST-CONTRACT")
    sheet.text(f"B{row}", str(row - 1))
    sheet.text(f"C{row}", "合成部件")
    sheet.text(f"D{row}", "0000000000")
    sheet.text(f"F{row}", "合成要素：合成值")


def _declaration_sheet(package: Package, *, image_header: str = "附图"):
    sheet = package.sheet(DECLARATION_SHEET)
    for column, header in zip("ABCDEF", ("采购合同号", "合同项号", "品名", "海关编码", image_header, "申报要素")):
        sheet.text(f"{column}1", header)
    for row in range(2, 17):
        _data_row(sheet, row)
    return sheet


def _register_media(package: Package) -> None:
    """ID_A / ID_B / ID_DUP / ID_NO_MEDIA / ID_CORRUPT 的 cellimages 与媒体部件。"""
    package.cell_image("ID_A", "xl/media/a.png")
    package.image("xl/media/a.png", png(b"\x00\x80\xff"))
    package.cell_image("ID_B", "xl/media/b.png")
    package.image("xl/media/b.png", png(b"\x10\x20\x30"))
    package.cell_image("ID_DUP", "xl/media/dup1.png")
    package.image("xl/media/dup1.png", png(b"\x01\x02\x03"))
    package.cell_image("ID_DUP", "xl/media/dup2.png")
    package.image("xl/media/dup2.png", png(b"\x04\x05\x06"))
    # 登记的媒体部件故意不写实体：模拟“缺资源”。
    package.image("xl/media/missing.png", None)
    package.cell_image("ID_NO_MEDIA", "xl/media/missing.png")
    package.cell_image("ID_CORRUPT", "xl/media/corrupt.png")
    package.image("xl/media/corrupt.png", b"not a png")


def mixed_package() -> Package:
    """15 行覆盖：有图(3)、无图(4)、读失败(8)，另含共享公式从属行。"""
    package = Package()
    sheet = _declaration_sheet(package)
    # E2 主公式；E3 普通从属行；E4 从属行缓存值带本行自己的 ID。
    sheet.shared("E2", '_xlfn.DISPIMG("ID_A",1)', si=0, span="E2:E4")
    sheet.shared("E3", None, si=0)
    sheet.cells["E4"] = formula("E4", "", cached='=DISPIMG("ID_B",1)', attrs=' t="shared" si="0"')
    # E5 不建单元格：列存在但本行空白。
    sheet.text("E6", "无")                                   # 纯文本
    sheet.formula("E7", '_xlfn.DISPIMG("ID_MISSING",1)')     # 缺定义
    sheet.formula("E8", '_xlfn.DISPIMG("ID_DUP",1)')         # 重复定义
    sheet.formula("E9", '_xlfn.DISPIMG("ID_NO_MEDIA",1)')    # 缺资源
    sheet.formula("E10", '_xlfn.DISPIMG("ID_CORRUPT",1)')    # 坏资源
    sheet.formula("E11", 'IF(A2="","",_xlfn.DISPIMG("ID_A",1))')          # 条件/表达式
    sheet.formula("E12", '_xlfn.DISPIMG("ID_A",1)&_xlfn.DISPIMG("ID_B",1)')  # 多引用
    sheet.text("E13", '=DISPIMG("ID_A",1)')                  # 只有公式文本，没有公式定义
    sheet.formula("E14", "A2")                               # 其它公式
    sheet.formula("E15", '_xlfn.DISPIMG("ID_A")')            # 形态不支持（缺 mode 参数）
    _register_media(package)
    return package


def external_package() -> Package:
    """图片定义指向外部链接：离线解析不取外链，按读取失败处理。"""
    package = Package()
    sheet = _declaration_sheet(package)
    sheet.formula("E2", '_xlfn.DISPIMG("ID_EXT",1)')
    package.parts["xl/cellimages.xml"] = (
        '<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" '
        'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="{R}"><etc:cellImage><xdr:pic><xdr:nvPicPr>'
        '<xdr:cNvPr id="1" name="ID_EXT"/></xdr:nvPicPr><xdr:blipFill>'
        '<a:blip r:embed="rExt"/></xdr:blipFill></xdr:pic></etc:cellImage></etc:cellImages>'
    )
    package.parts["xl/_rels/cellimages.xml.rels"] = (
        f'<Relationships xmlns="{P}"><Relationship Id="rExt" Type="{R}/image" '
        'Target="https://example.invalid/image.png" TargetMode="External"/></Relationships>'
    )
    return package


def no_image_column_package() -> Package:
    """表头里没有“附图/图片”等关键词：图片列缺失，不能当成全部无图。"""
    package = Package()
    _declaration_sheet(package, image_header="备注")
    return package


def _two_cell_anchor(rel_id: str, *, row: int, column: int, bottom_row: int | None = None,
                     bottom_column: int | None = None) -> str:
    """一个起止点明确的普通浮图；行列均 0 起始（与 OOXML 锚点一致）。"""
    bottom_row = row if bottom_row is None else bottom_row
    bottom_column = column if bottom_column is None else bottom_column

    def marker(tag: str, at_row: int, at_column: int) -> str:
        return (
            f"<xdr:{tag}><xdr:col>{at_column}</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>{at_row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:{tag}>"
        )

    return (
        "<xdr:twoCellAnchor>"
        + marker("from", row, column) + marker("to", bottom_row, bottom_column)
        + "<xdr:pic><xdr:nvPicPr><xdr:cNvPr id=\"1\" name=\"SYNTHETIC\"/></xdr:nvPicPr>"
        + f'<xdr:blipFill><a:blip r:embed="{rel_id}"/></xdr:blipFill></xdr:pic>'
        + "<xdr:clientData/></xdr:twoCellAnchor>"
    )


def floating_drawing(anchors) -> str:
    """把若干 ``(rel_id, 关键字参数)`` 组装成一个 drawing 部件。"""
    body = "".join(_two_cell_anchor(rel_id, **markers) for rel_id, markers in anchors)
    return (
        f'<xdr:wsDr xmlns:xdr="{DRAW}" xmlns:a="{A}" xmlns:r="{R}">'
        f"{body}</xdr:wsDr>"
    )


def floating_package() -> Package:
    """附图列只有普通浮图或浮图异常的行：单格可用/多张/坏资源/缺资源/跨行/公式+浮图。"""
    package = Package()
    sheet = _declaration_sheet(package)
    for row in range(2, 9):
        _data_row(sheet, row)
    # E7 同时有 DISPIMG 公式和同一单元格的浮图：以公式为准，浮图进 notes 待核。
    sheet.formula("E7", '_xlfn.DISPIMG("ID_A",1)')
    anchors = [
        ("rOk", {"row": 1, "column": 4}),                      # E2 单张可用
        ("rOk", {"row": 2, "column": 4}),                      # E3 两张
        ("rOk", {"row": 2, "column": 4}),
        ("rCorrupt", {"row": 3, "column": 4}),                 # E4 坏资源
        ("rMissing", {"row": 4, "column": 4}),                 # E5 缺资源
        ("rOk", {"row": 5, "column": 4, "bottom_row": 6}),     # E6 跨行：不参与归属
        ("rOk", {"row": 6, "column": 4}),                      # E7 公式 + 浮图
    ]
    package.cell_image("ID_A", "xl/media/float-ok.png")
    package.image("xl/media/float-ok.png", png(b"\x00\x80\xff"))
    package.image("xl/media/float-corrupt.png", b"not a png")
    package.attach_drawing(DECLARATION_SHEET, floating_drawing(anchors), "xl/media/float-ok.png")
    package.parts["xl/drawings/_rels/drawing1.xml.rels"] = (
        f'<Relationships xmlns="{P}">'
        f'<Relationship Id="rOk" Type="{R}/image" Target="../media/float-ok.png"/>'
        f'<Relationship Id="rCorrupt" Type="{R}/image" Target="../media/float-corrupt.png"/>'
        f'<Relationship Id="rMissing" Type="{R}/image" Target="../media/float-missing.png"/>'
        "</Relationships>"
    )
    return package


def compose_package() -> Package:
    """P04→P05 组合样例：P04 标准两页签 + 共享公式图片行 + 单格浮图行（含只有浮图的行）。"""
    package = standard_package()
    package.attach_drawing(
        "节点①申报要素",
        floating_drawing([
            ("rImage1", {"row": 3, "column": 4}),   # 第 4 行：有其它字段，附图格是浮图
            ("rImage1", {"row": 5, "column": 4}),   # 第 6 行：只有浮图，没有文字
        ]),
        "xl/media/image1.png",
    )
    return package


def ambiguous_image_column_package() -> Package:
    """表头同时出现“附图”和“图片”：图片列有歧义，需人工核对。"""
    package = Package()
    sheet = _declaration_sheet(package)
    sheet.text("F1", "图片")
    return package


def package_bytes(package: Package) -> bytes:
    """把合成包落成 xlsx 字节；每次生成的时间戳不同，测试内请复用同一份字节。"""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.xlsx"
        package.build(path)
        return path.read_bytes()


__all__ = [
    "DECLARATION_SHEET",
    "IMAGE_COLUMN",
    "ambiguous_image_column_package",
    "compose_package",
    "external_package",
    "floating_drawing",
    "floating_package",
    "mixed_package",
    "no_image_column_package",
    "package_bytes",
]
