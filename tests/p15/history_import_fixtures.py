"""P15 历史模板导入测试用合成工作簿。

所有内容都是构造出来的占位值，不含真实商品、合同号、编码或图片。
复用 P04 的 OOXML 合成构造器，覆盖：有图 / 无图 / 读失败 / 图片列缺失 /
缺原 HS / 缺日期 / 正常三行。
"""
from __future__ import annotations

from pathlib import Path

from tests.p04.fixtures import Package, png

DECLARATION_SHEET = "申报要素"
PACKING_SHEET = "装箱明细"


def _declaration(package: Package, *, with_image_column: bool = True, name: str = "合成法兰盘") -> None:
    sheet = package.sheet(DECLARATION_SHEET)
    headers = [
        "采购合同号",
        "合同项号",
        "品名",
        "海关编码",
        "申报要素",
        "退税率",
        "附图" if with_image_column else "备注",
        "申报日期",
    ]
    for index, header in enumerate(headers, start=1):
        sheet.text(f"{chr(64 + index)}1", header)
    rows = [
        (
            "SYN-CONTRACT-A",
            "1",
            name,
            "7307210000",
            "材质：不锈钢|用途：管道连接|型号：DN50",
            "0.13",
            '_xlfn.DISPIMG("ID_A",1)' if with_image_column else "无",
            "2025-01-15",
        ),
        (
            "SYN-CONTRACT-A",
            "2",
            name,
            "7307990000",
            "材质：碳钢|用途：管道连接|型号：DN80",
            "0.13",
            None if with_image_column else "无",
            "2025-01-15",
        ),
        (
            "SYN-CONTRACT-B",
            "1",
            "合成弯头",
            "7307930000",
            "材质：碳钢|用途：管子连接|型号：DN100",
            "0",
            '_xlfn.DISPIMG("ID_CORRUPT",1)' if with_image_column else "无",
            None,
        ),
        (
            "SYN-CONTRACT-B",
            "2",
            "合成缺编码件",
            "",
            "材质：塑料|用途：测试",
            "0",
            None,
            None,
        ),
    ]
    for row_index, values in enumerate(rows, start=2):
        for column_index, value in enumerate(values, start=1):
            if value is None:
                continue
            ref = f"{chr(64 + column_index)}{row_index}"
            if column_index in (6,) and value.replace(".", "", 1).isdigit():
                sheet.number(ref, value)
            elif column_index == 7 and with_image_column and "DISPIMG" in str(value):
                sheet.formula(ref, value)
            else:
                sheet.text(ref, value)


def _packing(package: Package) -> None:
    sheet = package.sheet(PACKING_SHEET)
    headers = ("合同号", "合同项号", "合同第一条品名", "数量", "单位")
    for index, header in enumerate(headers, start=1):
        sheet.text(f"{chr(64 + index)}1", header)
    rows = (
        ("SYN-CONTRACT-A", "1", "合成法兰盘", "10", "PCS"),
        ("SYN-CONTRACT-A", "2", "合成法兰盘", "20", "PCS"),
        ("SYN-CONTRACT-B", "1", "合成弯头", "30", "PCS"),
        ("SYN-CONTRACT-B", "2", "合成缺编码件", "40", "PCS"),
    )
    for row_index, values in enumerate(rows, start=2):
        for column_index, value in enumerate(values, start=1):
            sheet.text(f"{chr(64 + column_index)}{row_index}", value)


def build_synthetic_workbook(path: Path, *, with_image_column: bool = True, name: str = "合成法兰盘") -> Path:
    package = Package()
    _declaration(package, with_image_column=with_image_column, name=name)
    _packing(package)
    if with_image_column:
        package.cell_image("ID_A", "xl/media/a.png")
        package.image("xl/media/a.png", png(b"\x00\x80\xff"))
        package.cell_image("ID_CORRUPT", "xl/media/corrupt.png")
        package.image("xl/media/corrupt.png", b"not a png")
    package.build(path)
    return path

def build_empty_workbook(path: Path) -> Path:
    """只有表头、没有数据行的合成模板：用于验证“0 条记录”不等于“解析失败”。"""

    package = Package()
    declaration = package.sheet(DECLARATION_SHEET)
    for index, header in enumerate(
        ("采购合同号", "合同项号", "品名", "海关编码", "申报要素", "退税率", "附图", "申报日期"),
        start=1,
    ):
        declaration.text(f"{chr(64 + index)}1", header)
    packing = package.sheet(PACKING_SHEET)
    for index, header in enumerate(("合同号", "合同项号", "合同第一条品名"), start=1):
        packing.text(f"{chr(64 + index)}1", header)
    package.build(path)
    return path
