"""完全合成的 OOXML 工作簿构造器（P04/P05 测试共用）。

约束：
- 所有商品、合同号、编码、图片都是构造出来的占位内容，**不含真实业务数据**；
- 真实模板放在 git 忽略的 ``docs/参考模板/``，测试只读其结构，不把真实值写进断言；
- 可以精确构造缺表头、空白、重复项号、合并分箱、无匹配、共享公式图片、缺资源、
  坏资源等结构，用来验证“异常是数据，不是猜测”的口径。
"""

from __future__ import annotations

import posixpath
import struct
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

from adapters.files.ooxml import P, R, S

DRAW = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
ET = "http://www.wps.cn/officeDocument/2017/etCustomData"


def png(pixel: bytes = b"\x00\x80\xff") -> bytes:
    """最小合法 PNG（1×1 真彩色）。"""

    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
            + chunk(b"IEND", b""))


def escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def inline(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def numeric(ref: str, value: str) -> str:
    return f'<c r="{ref}"><v>{escape(value)}</v></c>'


def formula(ref: str, text: str, cached: str = "", attrs: str = "") -> str:
    cached_xml = f"<v>{escape(cached)}</v>" if cached else ""
    return f'<c r="{ref}"><f{attrs}>{escape(text)}</f>{cached_xml}</c>'


class Sheet:
    """一页签；``cells`` 以 ``A1`` 为键，值是一段 ``<c>`` XML。"""

    def __init__(self, name: str):
        self.name = name
        self.cells: dict[str, str] = {}
        self.merges: list[str] = []
        self.dimension: str | None = None
        self.extra = ""
        self.drawing_rel: str | None = None

    def text(self, ref: str, value: str) -> "Sheet":
        self.cells[ref.upper()] = inline(ref.upper(), value)
        return self

    def number(self, ref: str, value: str) -> "Sheet":
        self.cells[ref.upper()] = numeric(ref.upper(), value)
        return self

    def formula(self, ref: str, text: str, cached: str = "") -> "Sheet":
        self.cells[ref.upper()] = formula(ref.upper(), text, cached)
        return self

    def shared(self, ref: str, text: str | None, si: int, span: str | None = None) -> "Sheet":
        attrs = f' t="shared" si="{si}"' + (f' ref="{span}"' if span else "")
        self.cells[ref.upper()] = formula(ref.upper(), text or "", "", attrs)
        return self

    def merge(self, *refs: str) -> "Sheet":
        self.merges.extend(refs)
        return self

    def xml(self) -> str:
        rows: dict[int, list[str]] = {}
        for ref, xml in self.cells.items():
            row = int("".join(ch for ch in ref if ch.isdigit()))
            rows.setdefault(row, []).append(xml)
        body = "".join(
            f'<row r="{number}">{"".join(rows[number])}</row>' for number in sorted(rows)
        )
        extra = ""
        if self.merges:
            cells = "".join(f'<mergeCell ref="{ref}"/>' for ref in self.merges)
            extra += f'<mergeCells count="{len(self.merges)}">{cells}</mergeCells>'
        if self.drawing_rel:
            extra += f'<drawing r:id="{self.drawing_rel}"/>'
        dimension = f'<dimension ref="{self.dimension}"/>' if self.dimension else ""
        return (f'<worksheet xmlns="{S}" xmlns:r="{R}">{dimension}'
                f"<sheetData>{body}</sheetData>{extra}{self.extra}</worksheet>")


class Package:
    """构造一个最小但结构完整的 XLSX 包。"""

    def __init__(self):
        self.sheets: list[Sheet] = []
        self.parts: dict[str, bytes | str] = {}
        self.media: dict[str, bytes] = {}
        self.cellimages: list[tuple[str, str]] = []
        self.drawings: dict[str, str] = {}
        self.drawing_media: dict[str, str] = {}
        self.shared_strings: list[str] = []

    def sheet(self, name: str) -> Sheet:
        sheet = Sheet(name)
        self.sheets.append(sheet)
        return sheet

    def image(self, part: str, data: bytes | None = None) -> str:
        """登记媒体部件；``data=None`` 时不写实体，用于“缺资源”样例。"""
        if data is not None:
            self.media[part] = data
        return part

    def cell_image(self, image_id: str, media_part: str) -> None:
        self.cellimages.append((image_id, media_part))

    def attach_drawing(self, sheet_name: str, drawing_xml: str, image_part: str) -> None:
        """给页签挂普通浮图绘图部件；图片实体另用 :meth:`image` 登记。"""
        self.drawings[sheet_name] = drawing_xml
        self.drawing_media[sheet_name] = image_part.rsplit("/", 1)[-1]

    def _drawing_media(self, sheet_name: str) -> str:
        return self.drawing_media[sheet_name]

    def build(self, path) -> str:
        parts: dict[str, bytes | str] = {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
            ),
            "xl/workbook.xml": (
                f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                + "".join(
                    f'<sheet name="{escape(sheet.name)}" sheetId="{index + 1}" r:id="r{index + 1}"/>'
                    for index, sheet in enumerate(self.sheets)
                )
                + "</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                f'<Relationships xmlns="{P}">'
                + "".join(
                    f'<Relationship Id="r{index + 1}" Type="{R}/worksheet" '
                    f'Target="worksheets/sheet{index + 1}.xml"/>'
                    for index in range(len(self.sheets))
                )
                + "</Relationships>"
            ),
        }
        drawing_index = 0
        for index, sheet in enumerate(self.sheets):
            if sheet.name in self.drawings:
                drawing_index += 1
                sheet.drawing_rel = "rIdDraw"
                parts[f"xl/worksheets/_rels/sheet{index + 1}.xml.rels"] = (
                    f'<Relationships xmlns="{P}">'
                    f'<Relationship Id="rIdDraw" Type="{R}/drawing" '
                    f'Target="../drawings/drawing{drawing_index}.xml"/></Relationships>'
                )
                parts[f"xl/drawings/drawing{drawing_index}.xml"] = self.drawings[sheet.name]
                parts[f"xl/drawings/_rels/drawing{drawing_index}.xml.rels"] = (
                    f'<Relationships xmlns="{P}">'
                    f'<Relationship Id="rImage1" Type="{R}/image" '
                    f'Target="../media/{self._drawing_media(sheet.name)}"/></Relationships>'
                )
            parts[f"xl/worksheets/sheet{index + 1}.xml"] = sheet.xml()
        for part, data in self.media.items():
            parts[part] = data
        if self.cellimages:
            pictures = "".join(
                f'<etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{index + 1}" '
                f'name="{escape(image_id)}"/></xdr:nvPicPr><xdr:blipFill>'
                f'<a:blip r:embed="rId{index + 1}"/></xdr:blipFill></xdr:pic></etc:cellImage>'
                for index, (image_id, _) in enumerate(self.cellimages)
            )
            parts["xl/cellimages.xml"] = (
                f'<etc:cellImages xmlns:etc="{ET}" xmlns:xdr="{DRAW}" xmlns:a="{A}" xmlns:r="{R}">'
                f"{pictures}</etc:cellImages>"
            )
            parts["xl/_rels/cellimages.xml.rels"] = (
                f'<Relationships xmlns="{P}">'
                + "".join(
                    f'<Relationship Id="rId{index + 1}" Type="{R}/image" '
                    f'Target="{posixpath.relpath(media_part, "xl")}"/>'
                    for index, (_, media_part) in enumerate(self.cellimages)
                )
                + "</Relationships>"
            )
        parts.update(self.parts)
        if self.shared_strings:
            parts["xl/sharedStrings.xml"] = (
                f'<sst xmlns="{S}" count="{len(self.shared_strings)}" '
                f'uniqueCount="{len(self.shared_strings)}">'
                + "".join(f"<si><t>{escape(value)}</t></si>" for value in self.shared_strings)
                + "</sst>"
            )
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, value in parts.items():
                archive.writestr(name, value.encode("utf-8") if isinstance(value, str) else value)
        return path


def drawing_with_anchor(image_part: str, *, row: int, column: int, rel_id: str = "rImage1") -> str:
    """精确落在单个单元格上的普通浮图（行列 0 起始，与 OOXML 锚点一致）。"""

    def marker(name: str) -> str:
        tag = "from" if name == "from" else "to"
        return (f"<xdr:{tag}><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff>"
                f"<xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:{tag}>")

    return (
        f'<xdr:wsDr xmlns:xdr="{DRAW}" xmlns:a="{A}" xmlns:r="{R}">'
        f"<xdr:twoCellAnchor>{marker('from')}{marker('to')}"
        f"<xdr:pic><xdr:nvPicPr><xdr:cNvPr id=\"1\" name=\"SYNTHETIC\"/></xdr:nvPicPr>"
        f'<xdr:blipFill><a:blip r:embed="{rel_id}"/></xdr:blipFill></xdr:pic>'
        f"<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
    )


# --------------------------------------------------------------------- 常用样例
PACKING_HEADERS = ["合同号", "合同项号", "合同第一条品名", "规格", "厂家提供型号", "数量", "单位"]


def standard_package() -> Package:
    """正常两页签 + 一条共享公式图片 + 一条普通浮图；用于基础读取与关联。"""
    package = Package()
    packing = package.sheet("节点①装箱明细")
    for column, header in zip("ABCDEFG", PACKING_HEADERS):
        packing.text(f"{column}4", header)
    for index, (item, name, quantity) in enumerate(
        (("1", "合成部件甲", "10"), ("2", "合成部件乙", "20"), ("2", "合成部件乙", "5")), start=5
    ):
        packing.text(f"A{index}", "TEST-CONTRACT").text(f"B{index}", item)
        packing.text(f"C{index}", name).text(f"D{index}", "SPEC-X")
        packing.text(f"E{index}", "MODEL-X").number(f"F{index}", quantity).text(f"G{index}", "个")

    declaration = package.sheet("节点①申报要素")
    for column, header in zip("ABCDEFG", ("采购合同号", "合同项号", "品名", "海关编码", "附图", "申报要素", "历史意见")):
        declaration.text(f"{column}1", header)
    for index, item in ((2, "1"), (3, "2"), (4, "3"), (5, "4")):
        declaration.text(f"A{index}", "TEST-CONTRACT").text(f"B{index}", item)
        declaration.text(f"C{index}", "合成部件").text(f"D{index}", "0000000000")
        declaration.text(f"F{index}", "合成要素：合成值").text(f"G{index}", "历史意见，不是本轮结论")
    declaration.shared("E2", '_xlfn.DISPIMG("ID_SYNTHETIC_A",1)', si=0, span="E2:E3")
    declaration.shared("E3", None, si=0)
    declaration.text("E5", "")
    declaration.merge("A6:C6")
    declaration.extra = ""
    package.cell_image("ID_SYNTHETIC_A", "xl/media/image1.png")
    package.image("xl/media/image1.png", png())

    extra = package.sheet("节点①箱件汇总")
    extra.text("A1", "与商品行无关的汇总页")
    return package
