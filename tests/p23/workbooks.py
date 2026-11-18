"""P23 测试专用的合成 XLSX 构造器：用 ``zipfile`` 手写最小 OOXML 包。

约束（与 P04 的 ``tests/p04/fixtures.py`` 同一思路，但只保留 P23 需要的部分）：

- 多页签，页签名任意（含中文）；
- 表头可以不在第 1 行（例如第 9 行），前面放标题/元数据行；
- 文本单元格用 ``t="inlineStr"``；数值单元格只写 ``<v>``，并可指定 ``s=`` 样式索引；
- ``xl/styles.xml`` 固定提供两个自定义 ``numFmt``（``0.00%`` 与 ``0.00``），
  ``cellXfs`` 下标 0 = 没有任何 numFmt 的默认样式、1 = 百分数、2 = 两位小数；
- 不构造图片/合并/公式：P23 的导入流水线只读单元格文本、类型与数字格式。

本模块不携带任何真实业务数据：内容全部由调用方（``tests.p23.builders``）给出。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"

#: ``cellXfs`` 里固定下标：1 = ``0.00%``，2 = ``0.00``。
STYLE_PERCENT = 1
STYLE_DECIMAL = 2

#: 自定义 numFmtId；内置百分比格式是 9/10，这里刻意用自定义 ID 验证读取逻辑。
PERCENT_FORMAT_ID = 164
DECIMAL_FORMAT_ID = 165


def escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _column_label(ref: str) -> str:
    letters = "".join(char for char in ref if char.isalpha())
    return letters.upper()


def _row_number(ref: str) -> int:
    digits = "".join(char for char in ref if char.isdigit())
    return int(digits)


def inline_cell(ref: str, value: str, *, style: int | None = None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    return (
        f'<c r="{ref}" t="inlineStr"{style_attr}>'
        f'<is><t xml:space="preserve">{escape(value)}</t></is></c>'
    )


def numeric_cell(ref: str, value: str, *, style: int | None = None) -> str:
    style_attr = f' s="{style}"' if style is not None else ""
    return f'<c r="{ref}"{style_attr}><v>{escape(value)}</v></c>'


class Sheet:
    """一页签；``cells`` 的键是 ``A1`` 形式的引用，值是一段 ``<c>`` XML。"""

    def __init__(self, name: str):
        self.name = name
        self.cells: dict[str, str] = {}

    def text(self, ref: str, value: str) -> "Sheet":
        self.cells[ref.upper()] = inline_cell(ref.upper(), value)
        return self

    def number(
        self,
        ref: str,
        value: str | int | float,
        *,
        percent: bool | None = None,
        style: int | None = None,
    ) -> "Sheet":
        """数值单元格。

        ``percent=True`` 用 ``0.00%`` 样式；``False`` 用 ``0.00``；``None`` 不写 ``s``
        （对应 ``cellXfs[0]`` 这个没有任何 numFmt 的默认样式）。显式 ``style`` 优先。
        """

        chosen = style
        if chosen is None and percent is True:
            chosen = STYLE_PERCENT
        elif chosen is None and percent is False:
            chosen = STYLE_DECIMAL
        self.cells[ref.upper()] = numeric_cell(ref.upper(), str(value), style=chosen)
        return self

    def xml(self) -> str:
        rows: dict[int, list[str]] = {}
        for ref, cell in self.cells.items():
            rows.setdefault(_row_number(ref), []).append((_column_label(ref), cell))
        body = "".join(
            f'<row r="{number}">'
            + "".join(cell for _, cell in sorted(cells, key=lambda item: item[0]))
            + "</row>"
            for number, cells in sorted(rows.items())
        )
        return (
            f'<worksheet xmlns="{S}" xmlns:r="{R}">'
            f"<sheetData>{body}</sheetData></worksheet>"
        )


STYLES_XML = (
    f'<styleSheet xmlns="{S}">'
    f'<numFmts count="2">'
    f'<numFmt numFmtId="{PERCENT_FORMAT_ID}" formatCode="0.00%"/>'
    f'<numFmt numFmtId="{DECIMAL_FORMAT_ID}" formatCode="0.00"/>'
    f"</numFmts>"
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    f'<xf numFmtId="{PERCENT_FORMAT_ID}" fontId="0" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1"/>'
    f'<xf numFmtId="{DECIMAL_FORMAT_ID}" fontId="0" fillId="0" borderId="0" xfId="0" '
    'applyNumberFormat="1"/>'
    "</cellXfs>"
    "</styleSheet>"
)


class Package:
    """一个最小但结构完整的 XLSX 包（仅页签 + 单元格 + 数字格式）。"""

    def __init__(self):
        self._sheets: list[Sheet] = []

    def sheet(self, name: str) -> Sheet:
        sheet = Sheet(name)
        self._sheets.append(sheet)
        return sheet

    @property
    def sheet_names(self) -> list[str]:
        return [sheet.name for sheet in self._sheets]

    def _parts(self) -> dict[str, str]:
        content_types = (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.worksheet+xml"/>'
                for index in range(1, len(self._sheets) + 1)
            )
            + "</Types>"
        )
        workbook = (
            f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
            + "".join(
                f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="r{index}"/>'
                for index, sheet in enumerate(self._sheets, start=1)
            )
            + "</sheets></workbook>"
        )
        relationships = (
            f'<Relationships xmlns="{P}">'
            + "".join(
                f'<Relationship Id="r{index}" Type="{R}/worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, len(self._sheets) + 1)
            )
            + f'<Relationship Id="rStyles" Type="{R}/styles" Target="styles.xml"/>'
            + "</Relationships>"
        )
        parts = {
            "[Content_Types].xml": content_types,
            "_rels/.rels": (
                f'<Relationships xmlns="{P}">'
                f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/>'
                f"</Relationships>"
            ),
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": relationships,
            "xl/styles.xml": STYLES_XML,
        }
        for index, sheet in enumerate(self._sheets, start=1):
            parts[f"xl/worksheets/sheet{index}.xml"] = sheet.xml()
        return parts

    def build(self, path: str | Path) -> str:
        """写出 XLSX 并返回**文件字节**的 SHA-256（用于填来源清单）。"""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(target, "w", ZIP_DEFLATED) as archive:
            for name, text in self._parts().items():
                archive.writestr(name, text.encode("utf-8"))
        return hashlib.sha256(target.read_bytes()).hexdigest()


__all__ = [
    "DECIMAL_FORMAT_ID",
    "PERCENT_FORMAT_ID",
    "Package",
    "STYLE_DECIMAL",
    "STYLE_PERCENT",
    "Sheet",
    "escape",
    "inline_cell",
    "numeric_cell",
]
