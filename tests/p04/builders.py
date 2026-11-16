"""P04 自有合成样例构造助手（在 ``tests/p04/fixtures.py`` 地基上拼表头与数据行）。

约束与地基一致：所有内容都是构造出来的占位值，**不含真实商品、合同号、编码或图片**。
这里只提供“按表头顺序写一行”的便捷方法，具体合并、公式、浮图由各测试自行补充，
以便精确构造缺表头、缺列、空白行、只有图片的行、纵向合并等结构。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adapters.files.ooxml import Workbook, col_label
from tests.p04.fixtures import Package

DECLARATION_SHEET = "节点①申报要素"
PACKING_SHEET = "节点①装箱明细"

#: 表头按列序排列；某项为 ``None`` 表示该列不写表头（构造“无表头但有内容”的列）。
DECLARATION_HEADERS = (
    "采购合同号",
    "合同项号",
    "品名",
    "海关编码",
    "申报要素",
    "退税率",
    "附图",
    "历史意见",
)
PACKING_HEADERS = (
    "合同号",
    "合同项号",
    "合同第一条品名",
    "附件清单品名",
    "规格",
    "厂家提供型号",
    "物料代码",
    "数量",
    "单位",
    "合并申报",
)


def write_headers(sheet, headers, row: int):
    """在第 ``row`` 行写表头；``None`` 跳过该列。"""
    for index, header in enumerate(headers, start=1):
        if header is None:
            continue
        sheet.text(f"{col_label(index)}{row}", header)
    return sheet


def write_rows(sheet, headers, rows, start_row: int):
    """按表头列序写数据行；``None`` 表示该格不写（留空），数字写成数值格。"""
    for offset, values in enumerate(rows):
        row = start_row + offset
        for index, value in enumerate(values, start=1):
            if value is None:
                continue
            ref = f"{col_label(index)}{row}"
            if isinstance(value, (int, float)):
                sheet.number(ref, str(value))
            else:
                sheet.text(ref, value)
    return sheet


def build_package(
    *,
    declaration_rows=(),
    packing_rows=(),
    declaration_headers=None,
    packing_headers=None,
    declaration_header_row: int = 1,
    packing_header_row: int = 4,
    declaration_sheet: str = DECLARATION_SHEET,
    packing_sheet: str = PACKING_SHEET,
    include_declaration: bool = True,
    include_packing: bool = True,
) -> Package:
    """构造两页签的合成工作簿包（不落盘）。"""
    package = Package()
    if include_declaration:
        headers = tuple(declaration_headers or DECLARATION_HEADERS)
        sheet = package.sheet(declaration_sheet)
        write_headers(sheet, headers, declaration_header_row)
        write_rows(sheet, headers, declaration_rows, declaration_header_row + 1)
    if include_packing:
        headers = tuple(packing_headers or PACKING_HEADERS)
        sheet = package.sheet(packing_sheet)
        write_headers(sheet, headers, packing_header_row)
        write_rows(sheet, headers, packing_rows, packing_header_row + 1)
    return package


def sheet_of(package: Package, name: str):
    return next(sheet for sheet in package.sheets if sheet.name == name)


class WorkbookTestCase(unittest.TestCase):
    """把合成包写成临时 xlsx 再按只读方式打开。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._built = 0

    def build(self, package: Package, name: str | None = None) -> Workbook:
        self._built += 1
        path = self.root / (name or f"sample-{self._built}.xlsx")
        return Workbook.open(Path(package.build(path)))
