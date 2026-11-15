import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from experiments.excel_fidelity.cli import run
from experiments.excel_fidelity.conclusion import append_precheck_column
from experiments.excel_fidelity.insert import audit, insert_columns
from experiments.excel_fidelity.mapping import detect_layout, map_workbook
from experiments.excel_fidelity.references import transform_formula
from experiments.excel_fidelity.workbook import FidelityError, Workbook
from .fixtures import cell, formula, make_fixture


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = make_fixture(self.root / 'source.xlsx')

    def edit(self, part, change):
        with ZipFile(self.source) as archive:
            parts = {info.filename: archive.read(info) for info in archive.infolist()}
        parts[part] = change(parts[part].decode('utf-8')).encode('utf-8')
        make_fixture(self.source, parts)

    def layout(self):
        return detect_layout(Workbook(self.source), 'declaration')

    def test_incomplete_products_are_retained_with_missing_fields(self):
        rows = ''.join(f'<row r="{number}">{contents}</row>' for number, contents in (
            (8, cell('D8', 'SYNTHETIC_HS')),
            (9, cell('F9', '合成申报字段')),
            (10, formula('E10', '_xlfn.DISPIMG("ID_SYNTHETIC",1)', '')),
            (11, cell('B11', '1')),
            (12, cell('C12', '合成品名')),
            (13, cell('A13', 'TEST-CONTRACT') + cell('F13', '合成字段')),
            (14, '<c r="D14" s="0"/>'),
            (15, cell('A15', '发运备注')),
            (16, cell('C16', '合计') + cell('H16', '3')),
            (17, cell('H17', '13%')),
            (18, cell('H18', '整机类型')),
            (19, formula('D19', 'SUM(100,200)', '300')),
            (20, formula('F20', 'SUM(1,2)', '3')),
        ))
        self.edit('xl/worksheets/sheet2.xml', lambda text: text.replace('</sheetData>', rows + '</sheetData>'))
        mapping = map_workbook(Workbook(self.source))
        layout = mapping['declaration']
        self.assertEqual(layout['data_rows'], [2, 3, 4, 8, 9, 10, 11, 12, 13, 17, 19, 20])
        records = {record['row']: record for record in layout['records']}
        for number in range(8, 14):
            self.assertEqual(records[number]['status'], 'requires_review')
        self.assertEqual(set(records[8]['issues']), {'missing_contract', 'missing_item', 'missing_name'})
        self.assertEqual(set(records[13]['issues']), {'missing_item', 'missing_name'})
        self.assertEqual({row['row'] for row in layout['non_item_rows']}, {6, 15, 16, 18})
        links = {link['declaration_row']: link for link in mapping['associations']}
        self.assertTrue(all(links[number]['status'] == 'requires_review' for number in range(8, 14)))

    def test_packing_quantity_and_unit_keep_zero_and_source(self):
        self.edit('xl/worksheets/sheet1.xml', lambda text:
                  text.replace('</row>', cell('I1', '数量') + cell('J1', '单位') + '</row>', 1)
                      .replace(cell('G2', '不作为型号') + '</row>',
                               cell('G2', '不作为型号') + cell('I2', '0') + cell('J2', '件') + '</row>', 1))
        packing = map_workbook(Workbook(self.source))['packing']
        self.assertEqual(packing['fields']['quantity'], 'I')
        self.assertEqual(packing['records'][0]['facts']['quantity'], {'value': '0', 'cell': 'I2'})
        self.assertEqual(packing['records'][0]['facts']['unit'], {'value': '件', 'cell': 'J2'})

    def test_duplicate_members_mark_every_affected_declaration(self):
        self.edit('xl/worksheets/sheet2.xml', lambda text: text.replace(cell('B3', '2'), cell('B3', '1')))
        links = {link['declaration_row']: link for link in map_workbook(Workbook(self.source))['associations']}
        for row, other in ((2, 3), (3, 2)):
            self.assertEqual(links[row]['status'], 'requires_review')
            self.assertEqual(links[row]['conflicting_declaration_rows'], [other])
            self.assertEqual(links[row]['members'][0]['packing_row'], 2)
        self.assertEqual(links[4]['status'], 'linked_by_contract_item')

    def test_cross_sheet_validation_and_conditional_formulas_are_shifted(self):
        with ZipFile(self.source) as archive:
            parts = {info.filename: archive.read(info) for info in archive.infolist()}
        parts = {name: (data.replace('申报要素'.encode(), '申报要素Decl'.encode())
                       if name.endswith(('.xml', '.rels')) else data) for name, data in parts.items()}
        parts['xl/worksheets/sheet2.xml'] = parts['xl/worksheets/sheet2.xml'].replace(
            '<t>申报要素Decl</t>'.encode(), '<t>申报要素</t>'.encode())
        make_fixture(self.source, parts)
        extra = ('<dataValidations count="1"><dataValidation type="list" sqref="B2:B4">'
                 '<formula1>\'申报要素dEcL\'!$G$2:$G$4</formula1></dataValidation></dataValidations>'
                 '<conditionalFormatting sqref="B2:B4"><cfRule type="expression" priority="1">'
                 '<formula>\'申报要素dEcL\'!$H$2&gt;0</formula></cfRule></conditionalFormatting>')
        self.edit('xl/worksheets/sheet3.xml', lambda text: text.replace('</worksheet>', extra + '</worksheet>'))
        destination = self.root / 'cross-sheet.xlsx'
        report = insert_columns(self.source, destination, self.layout())
        self.assertEqual(report['status'], 'passed')
        result = Workbook(destination)
        raw = result.parts['xl/worksheets/sheet3.xml'].decode()
        self.assertIn("'申报要素dEcL'!$I$2:$I$4", raw)
        self.assertIn("'申报要素dEcL'!$J$2&gt;0", raw)
        self.assertIn('sqref="B2:B4"', raw)
        self.assertEqual(transform_formula('dEcL!F2+F2', 'Other', 'Decl', 5), 'dEcL!H2+F2')
        corrupt = self.root / 'corrupt.xlsx'
        with ZipFile(destination) as archive, ZipFile(corrupt, 'w') as output:
            for info in archive.infolist():
                data = archive.read(info)
                if info.filename == 'xl/worksheets/sheet3.xml':
                    data = data.replace(b'$J$2&gt;', b'$H$2&gt;')
                output.writestr(info, data)
        self.assertIn('sheet_metadata_changed:其他页',
                      audit(Workbook(self.source), Workbook(corrupt), '申报要素Decl', 5, [1, 2, 3, 4])['failures'])

    def test_unsafe_metadata_formulas_refuse_output(self):
        extra = ('<dataValidations count="1"><dataValidation sqref="B2">'
                 '<formula1>INDIRECT("申报要素!H2")</formula1></dataValidation></dataValidations>')
        self.edit('xl/worksheets/sheet3.xml', lambda text: text.replace('</worksheet>', extra + '</worksheet>'))
        destination = self.root / 'unsafe.xlsx'
        with self.assertRaisesRegex(FidelityError, 'Dynamic'):
            insert_columns(self.source, destination, self.layout())
        self.assertFalse(destination.exists())

    def test_extended_metadata_formula_refuses_output(self):
        extra = ('<extLst><ext uri="synthetic"><x:f xmlns:x="urn:synthetic">'
                 '申报要素!H2</x:f></ext></extLst>')
        self.edit('xl/worksheets/sheet3.xml', lambda text: text.replace('</worksheet>', extra + '</worksheet>'))
        destination = self.root / 'extended.xlsx'
        with self.assertRaisesRegex(FidelityError, 'Extended validation/conditional formula'):
            insert_columns(self.source, destination, self.layout())
        self.assertFalse(destination.exists())

    def test_image_requires_pixel_decoding_for_cell_and_floating_resources(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow unavailable; decode cannot be accepted')
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8), 'red').save(buffer, format='JPEG')
        truncated = buffer.getvalue()[:-2]
        with Image.open(io.BytesIO(truncated)) as image:
            image.verify()
        with self.assertRaises(OSError), Image.open(io.BytesIO(truncated)) as image:
            image.load()
        make_fixture(self.source, {'xl/media/test.png': truncated})
        book = Workbook(self.source)
        self.assertEqual(book.image_records()[0]['status'], 'corrupt_resource')
        self.assertNotEqual(book.image_records()[0]['decode_status'], 'passed')
        self.assertEqual(book.floating_records()[0]['images'][0]['status'], 'corrupt_resource')

    def test_conditional_image_is_not_accepted_as_displayed(self):
        self.edit('xl/worksheets/sheet2.xml', lambda text:
                  text.replace('_xlfn.DISPIMG("ID_SYNTHETIC",1)',
                               'IF(FALSE,_xlfn.DISPIMG("ID_SYNTHETIC",1),"")'))
        book = Workbook(self.source)
        self.assertTrue(all(image['status'] == 'requires_review' for image in book.image_records()))
        self.assertEqual(book.image_records()[0]['candidates'][0]['status'], 'resource_found')
        self.assertEqual(map_workbook(book)['image_mapping']['rows'][0]['status'], 'requires_review')

    def test_result_labels_in_history_do_not_block_insertion(self):
        for row, label in ((2, 'AI推荐HScode'), (3, '预审结论'), (4, '置信度')):
            self.edit('xl/worksheets/sheet2.xml', lambda text, row=row, label=label:
                      text.replace(cell(f'G{row}', '历史说明，不是本轮AI结论'), cell(f'G{row}', label)))
        destination = self.root / 'history.xlsx'
        self.assertEqual(insert_columns(self.source, destination, self.layout())['status'], 'passed')
        result = Workbook(destination).sheet('申报要素')
        self.assertEqual(result['cells']['I2']['value'], 'AI推荐HScode')
        self.assertEqual(result['cells']['I3']['value'], '预审结论')

    def test_append_conclusion_preserves_existing_result_columns_and_images(self):
        additions = cell('M1', 'AI推荐hscode') + cell('N1', '业务员确认HScode') + cell('O1', '置信度')
        self.edit('xl/worksheets/sheet2.xml', lambda text: text.replace('</row>', additions + '</row>', 1))
        source = Workbook(self.source)
        report = run(self.source, self.root / 'append', 'SYNTHETIC', add_precheck=True)
        self.assertEqual(report['audit']['column'], 'P')
        self.assertEqual(report['audit']['changed_parts'], ['xl/worksheets/sheet2.xml'])
        result = Workbook(report['output_path'])
        self.assertEqual(detect_layout(result, 'declaration')['fields']['precheck_conclusion'], 'P')
        for old in source.sheets:
            new = result.sheet(old['name'])
            self.assertTrue(all(new['cells'][ref] == cell for ref, cell in old['cells'].items()))
        self.assertEqual(source.image_records(), result.image_records())
        self.assertEqual(source.floating_records(), result.floating_records())
        self.assertEqual(Workbook(self.source).source_hash, source.source_hash)
        self.assertEqual(result.sheet('申报要素')['cells']['P2']['value'], '')
        with self.assertRaisesRegex(FidelityError, 'already present'):
            append_precheck_column(report['output_path'], self.root / 'duplicate.xlsx',
                                   detect_layout(result, 'declaration'))
        self.assertFalse((self.root / 'duplicate.xlsx').exists())

    def test_append_conclusion_ignores_history_and_preserves_existing_files(self):
        self.edit('xl/worksheets/sheet2.xml', lambda text:
                  text.replace(cell('G2', '历史说明，不是本轮AI结论'), cell('G2', '预审结论')))
        destination = self.root / 'append-history.xlsx'
        report = append_precheck_column(self.source, destination, self.layout())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(Workbook(destination).sheet('申报要素')['cells']['G2']['value'], '预审结论')
        content = destination.read_bytes()
        with self.assertRaises(FidelityError):
            append_precheck_column(self.source, destination, self.layout())
        self.assertEqual(destination.read_bytes(), content)
        with self.assertRaises(FidelityError):
            append_precheck_column(self.source, self.source, self.layout())

    def test_uniform_formatting_to_last_column_is_bounded_during_insertion(self):
        self.edit('xl/worksheets/sheet2.xml', lambda text:
                  text.replace('min="1" max="8" width="20"', 'min="1" max="16384" width="20"'))
        destination = self.root / 'tail-insert.xlsx'
        self.assertEqual(insert_columns(self.source, destination, self.layout())['status'], 'passed')
        self.assertIn('max="16384"', Workbook(destination).parts['xl/worksheets/sheet2.xml'].decode())
        self.assertNotIn('max="16386"', Workbook(destination).parts['xl/worksheets/sheet2.xml'].decode())

    def test_append_uses_logical_end_and_splits_trailing_column_format(self):
        self.edit('xl/worksheets/sheet2.xml', lambda text:
                  text.replace('min="1" max="8" width="20"', 'min="1" max="16384" width="20"'))
        destination = self.root / 'tail-append.xlsx'
        report = append_precheck_column(self.source, destination, self.layout())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['column'], 'I')
        raw = Workbook(destination).parts['xl/worksheets/sheet2.xml'].decode()
        self.assertIn('min="10" max="16384" width="20"', raw)
        self.assertEqual(Workbook(destination).sheet('申报要素')['cells']['I1']['value'], '预审结论')

    def put_floating_picture_on_row_eight(self):
        self.edit('xl/drawings/drawing1.xml', lambda text:
                  text.replace('<xdr:col>5</xdr:col>', '<xdr:col>4</xdr:col>')
                      .replace('<xdr:row>1</xdr:row>', '<xdr:row>7</xdr:row>')
                      .replace('<xdr:row>2</xdr:row>', '<xdr:row>7</xdr:row>'))

    def assert_floating_only_row_is_pending(self):
        mapping = map_workbook(Workbook(self.source))
        self.assertIn(8, mapping['declaration']['data_rows'])
        record = next(r for r in mapping['declaration']['records'] if r['row'] == 8)
        self.assertEqual(record['status'], 'requires_review')
        self.assertEqual(set(record['issues']), {'missing_contract', 'missing_item', 'missing_name'})
        link = next(link for link in mapping['associations'] if link['declaration_row'] == 8)
        self.assertEqual(link['status'], 'requires_review')
        picture_row = next(r for r in mapping['image_mapping']['rows'] if r['row'] == 8)
        self.assertEqual(len(picture_row['floating_images']), 1)
        self.assertEqual(picture_row['status'], 'requires_review')
        self.assertFalse(mapping['image_mapping']['unassigned'])

    def test_floating_only_row_beyond_last_cell_is_retained(self):
        self.put_floating_picture_on_row_eight()
        self.assertFalse(any(ref.endswith('8') for ref in Workbook(self.source).sheet('申报要素')['cells']))
        self.assert_floating_only_row_is_pending()

    def test_floating_only_row_with_blank_styled_cell_is_retained(self):
        self.put_floating_picture_on_row_eight()
        self.edit('xl/worksheets/sheet2.xml', lambda text:
                  text.replace('</sheetData>', '<row r="8" ht="30" customHeight="1"><c r="E8" s="0"/></row></sheetData>'))
        self.assert_floating_only_row_is_pending()

    def test_outside_or_ambiguous_floating_pictures_remain_unassigned(self):
        for kind in ('wrong_column', 'spans_rows', 'header_row'):
            with self.subTest(kind=kind):
                make_fixture(self.source)
                self.put_floating_picture_on_row_eight()
                if kind == 'wrong_column':
                    self.edit('xl/drawings/drawing1.xml', lambda text:
                              text.replace('<xdr:col>4</xdr:col>', '<xdr:col>6</xdr:col>'))
                elif kind == 'spans_rows':
                    self.edit('xl/drawings/drawing1.xml', lambda text:
                              text.replace('<xdr:row>7</xdr:row>', '<xdr:row>6</xdr:row>', 1))
                else:
                    self.edit('xl/drawings/drawing1.xml', lambda text:
                              text.replace('<xdr:row>7</xdr:row>', '<xdr:row>0</xdr:row>'))
                mapping = map_workbook(Workbook(self.source))
                self.assertEqual(mapping['declaration']['data_rows'], [2, 3, 4])
                self.assertEqual(len(mapping['image_mapping']['unassigned']), 1)

    def test_floating_only_rows_survive_result_exports(self):
        for row_xml in ('', '<row r="8" ht="30" customHeight="1"/>'):
            with self.subTest(row_xml=row_xml):
                make_fixture(self.source)
                self.put_floating_picture_on_row_eight()
                if row_xml:
                    self.edit('xl/worksheets/sheet2.xml', lambda text:
                              text.replace('</sheetData>', row_xml + '</sheetData>'))
                source = Workbook(self.source)
                insertion = self.root / ('insert-empty-row-' + str(bool(row_xml)) + '.xlsx')
                self.assertEqual(insert_columns(self.source, insertion, self.layout())['status'], 'passed')
                inserted = Workbook(insertion)
                self.assertIn(8, detect_layout(inserted, 'declaration')['data_rows'])
                self.assertEqual(inserted.sheet('申报要素')['cells']['F8']['value'], '')
                self.assertIn('ref="A1:J8"', inserted.parts['xl/worksheets/sheet2.xml'].decode())
                appended_path = self.root / ('append-empty-row-' + str(bool(row_xml)) + '.xlsx')
                self.assertEqual(append_precheck_column(self.source, appended_path, self.layout())['status'], 'passed')
                appended = Workbook(appended_path)
                self.assertEqual(appended.sheet('申报要素')['cells']['I8']['value'], '')
                self.assertIn('ref="A1:I8"', appended.parts['xl/worksheets/sheet2.xml'].decode())
                self.assertEqual(source.floating_records(), appended.floating_records())
                self.assertEqual(Workbook(self.source).source_hash, source.source_hash)

    def test_unlabelled_totals_are_separate_from_incomplete_products(self):
        for role, part in (('packing', 'xl/worksheets/sheet1.xml'),
                           ('declaration', 'xl/worksheets/sheet2.xml')):
            for kind in ('totals', 'single_quantity', 'extra_text', 'floating_picture'):
                if kind == 'floating_picture' and role == 'packing':
                    continue
                with self.subTest(role=role, kind=kind):
                    make_fixture(self.source)
                    values = formula('I8', 'SUM(I2:I4)', '6')
                    if kind != 'single_quantity':
                        values += formula('J8', 'SUM(J2:J4)', '9')
                    if kind == 'extra_text':
                        values += cell('K8', '待核备注')
                    self.edit(part, lambda text:
                              text.replace('</row>', cell('I1', '数量') + '</row>', 1)
                                  .replace('</sheetData>', f'<row r="8">{values}</row></sheetData>'))
                    if kind == 'floating_picture':
                        self.put_floating_picture_on_row_eight()
                    layout = detect_layout(Workbook(self.source), role)
                    if kind == 'totals':
                        self.assertNotIn(8, layout['data_rows'])
                        self.assertTrue(any(row['row'] == 8 and row['reason'].startswith('summary_formula_only')
                                            for row in layout['non_item_rows']))
                    else:
                        self.assertIn(8, layout['data_rows'])
                        self.assertEqual(next(record for record in layout['records']
                                              if record['row'] == 8)['status'], 'requires_review')
