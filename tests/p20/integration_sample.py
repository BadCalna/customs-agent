"""Invented multi-sheet upload with an image, formula and prior opinion; no business data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from tests.p04.builders import build_package, sheet_of, DECLARATION_SHEET
from tests.p04.fixtures import drawing_with_anchor, png
from tests.p20.test_exports import STYLES


def upload_sample(label='V1'):
    pkg = build_package(declaration_headers=(
        '采购合同号','合同项号','品名','海关编码','申报要素','附图','历史意见','AI推荐HScode','AI预审结论'),
        declaration_rows=[['SYNTHETIC', '001', f'合成商品-{label}', '0000000000', '用途：合成验证', None, '原意见保留', '', '']],
        packing_rows=[['SYNTHETIC','001', f'合成商品-{label}', '', 'SPEC', 'MODEL', '', 1, '件']])
    sheet = sheet_of(pkg, DECLARATION_SHEET)
    sheet.formula('J2', '1+2', '3')
    pkg.parts['xl/styles.xml'] = STYLES
    pkg.image('xl/media/integration.png', png(b'\x10\x80\x20'))
    pkg.attach_drawing(DECLARATION_SHEET, drawing_with_anchor('xl/media/integration.png', row=1, column=5), 'xl/media/integration.png')
    with TemporaryDirectory() as directory:
        return Path(pkg.build(Path(directory)/'source.xlsx')).read_bytes()
