import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from experiments.excel_fidelity.cli import run
from experiments.excel_fidelity.insert import insert_columns
from experiments.excel_fidelity.mapping import detect_layout, map_workbook, parse_items
from experiments.excel_fidelity.references import effective_formulas, transform_formula
from experiments.excel_fidelity.workbook import FidelityError, NS, Workbook, sha256, xml
from .fixtures import cell, make_fixture


class FidelityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = make_fixture(self.root / 'source.xlsx')

    def layout(self):
        return detect_layout(Workbook(self.source), 'declaration')

    def edit_part(self, part, function):
        with ZipFile(self.source) as archive:
            original = archive.read(part).decode('utf-8')
        make_fixture(self.source, {part: function(original)})

    def test_member_ranges_and_ambiguity(self):
        self.assertEqual(parse_items('25-28、31-32、34'), ['25','26','27','28','31','32','34'])
        self.assertEqual(parse_items('01-03'), ['01','02','03'])
        for value in ('55-27', '1-1005', '1、1', '1-3、3', '1/2', ''):
            with self.subTest(value=value), self.assertRaises(FidelityError):
                parse_items(value)

    def test_semantic_headers_and_no_fixed_model_column(self):
        self.edit_part('xl/worksheets/sheet1.xml', lambda text: text.replace('厂家提供型号', '特别注意'))
        mapping = map_workbook(Workbook(self.source))
        self.assertNotIn('manufacturer_model', mapping['packing']['fields'])
        self.assertEqual(mapping['packing']['fields']['merge_hint'], 'G')
        self.assertEqual([a['members'][0]['packing_row'] for a in mapping['associations']], [2,3,4])

    def test_result_columns_located_by_labels_at_different_positions(self):
        labels = (' AI推荐 hsCode ', '业务员确认HS编码', '置信度', '预审结论')
        fields = ('ai_hs', 'confirmed_hs', 'confidence', 'precheck_conclusion')
        values = ('SYNTHETIC_AI', 'SYNTHETIC_CONFIRM', '中', '需核对合成参数')
        for columns in (('AF', 'AG', 'AH', 'AI'), ('M', 'N', 'O', 'Q'), ('J', 'AF', 'N', 'Z')):
            with self.subTest(columns=columns):
                make_fixture(self.source)
                headers = ''.join(cell(f'{column}1', label) for column, label in zip(columns, labels))
                data = ''.join(cell(f'{column}2', value) for column, value in zip(columns, values))
                self.edit_part('xl/worksheets/sheet2.xml', lambda text:
                    text.replace('</row>', headers + '</row>', 1)
                        .replace(cell('H2', '0') + '</row>', cell('H2', '0') + data + '</row>', 1))
                before_hash = sha256(self.source.read_bytes())
                layout = self.layout()
                self.assertEqual(layout['fields']['hs'], 'D')
                for field, column, value in zip(fields, columns, values):
                    self.assertEqual(layout['fields'][field], column)
                    self.assertEqual(layout['records'][0]['facts'][field],
                                     {'value': value, 'cell': f'{column}2'})
                destination = self.root / ('existing-' + columns[0] + '.xlsx')
                with self.assertRaisesRegex(FidelityError, 'Result columns already present'):
                    insert_columns(self.source, destination, layout)
                self.assertFalse(destination.exists())
                self.assertEqual(sha256(self.source.read_bytes()), before_hash)

    def test_missing_result_columns_reported_and_duplicate_labels_rejected(self):
        layout = self.layout()
        for field in ('ai_hs', 'confirmed_hs', 'confidence', 'precheck_conclusion'):
            self.assertNotIn(field, layout['fields'])
            self.assertIn(f'missing_column:{field}', layout['warnings'])
        self.edit_part('xl/worksheets/sheet2.xml', lambda text:
            text.replace('</row>', cell('M1', 'AI推荐hscode') + cell('AF1', 'AI推荐 HS编码') + '</row>', 1))
        with self.assertRaisesRegex(FidelityError, 'Duplicate semantic header'):
            self.layout()

    def test_missing_sheet_and_duplicate_headers_fail(self):
        self.edit_part('xl/workbook.xml', lambda text: text.replace('装箱明细', '其他数据'))
        with self.assertRaises(FidelityError):
            map_workbook(Workbook(self.source))
        make_fixture(self.source)
        self.edit_part('xl/worksheets/sheet1.xml', lambda text: text.replace('厂家提供型号', '合同号'))
        with self.assertRaises(FidelityError):
            map_workbook(Workbook(self.source))

    def test_reverse_member_does_not_guess_endpoints(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('<t>1</t>', '<t>55-27</t>', 1))
        association = map_workbook(Workbook(self.source))['associations'][0]
        self.assertEqual(association['members'], [])
        self.assertIn('ambiguous_or_excessive_item_range', association['issues'])

    def test_duplicate_declaration_ownership_is_flagged(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('<t>2</t>', '<t>1</t>', 1))
        issues = map_workbook(Workbook(self.source))['issues']
        self.assertTrue(any(i['type'] == 'member_claimed_by_multiple_declarations' for i in issues))

    def test_footer_is_retained_but_not_an_item(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('</sheetData>',
            '<row r="8"><c r="A8" t="inlineStr"><is><t>合同出货备注</t></is></c></row></sheetData>'))
        layout = self.layout()
        self.assertEqual(layout['data_rows'], [2,3,4])
        self.assertEqual(layout['non_item_rows'][0]['row'], 8)

    def test_missing_and_corrupt_images_differ_from_no_image(self):
        make_fixture(self.source, {'xl/media/test.png': None})
        self.assertEqual(len(Workbook(self.source).image_records()), 3)
        self.assertEqual({r['status'] for r in Workbook(self.source).image_records()}, {'missing_resource'})
        make_fixture(self.source, {'xl/media/test.png': b'not an image'})
        records = Workbook(self.source).image_records()
        if records[0].get('decode_status') == 'not_run_Pillow_unavailable':
            self.skipTest('Optional Pillow unavailable; resource decode is explicitly unverified')
        self.assertEqual(records[0]['status'], 'corrupt_resource')

    def test_formula_transform_respects_sheet_and_string_context(self):
        self.assertEqual(transform_formula('SUM($D$2:F4)+Other!F2+IF(A1="F2",1,0)', 'Decl', 'Decl', 5),
                         'SUM($D$2:H4)+Other!F2+IF(A1="F2",1,0)')
        self.assertEqual(transform_formula("'Decl'!$F2+Decl!E3+F2", 'Other', 'Decl', 5),
                         "'Decl'!$H2+Decl!G3+F2")
        self.assertEqual(transform_formula('LOG10(A1)+F:F', 'Decl', 'Decl', 5), 'LOG10(A1)+H:H')
        for formula in ('INDIRECT("F2")', 'OFFSET(A1,1,1)', '[book]Decl!F2', 'Sheet1:Sheet3!F2', 'SUM(Table1[Column])'):
            with self.subTest(formula=formula), self.assertRaises(FidelityError):
                transform_formula(formula, 'Decl', 'Decl', 5)

    def test_insert_preserves_values_and_translates_dependencies(self):
        before_hash = sha256(self.source.read_bytes())
        destination = self.root / 'result.xlsx'
        report = insert_columns(self.source, destination, self.layout())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(sha256(self.source.read_bytes()), before_hash)
        source, result = Workbook(self.source), Workbook(destination)
        decl = result.sheet('申报要素')
        self.assertEqual(decl['cells']['D2']['value'], 'TEST_HS')
        self.assertEqual(decl['cells']['E1']['value'], 'AI推荐HScode')
        self.assertEqual(decl['cells']['F1']['value'], '预审结论')
        self.assertEqual(decl['cells']['I2']['value'], '历史说明，不是本轮AI结论')
        self.assertEqual(effective_formulas(decl)['H6'], 'SUM(J2:J4)')
        self.assertEqual(effective_formulas(result.sheet('其他页'))['A1'], "'申报要素'!$J$2+申报要素!I2")
        self.assertEqual(effective_formulas(result.sheet('其他页'))['C1'], 'IF(A1="H2",1,0)')
        self.assertEqual(result.parts['xl/worksheets/sheet1.xml'], source.parts['xl/worksheets/sheet1.xml'])
        self.assertEqual(result.parts['customXml/unknown.xml'], source.parts['customXml/unknown.xml'])
        self.assertEqual(result.parts['xl/media/test.png'], source.parts['xl/media/test.png'])
        self.assertEqual(len(result.image_records()),3)
        self.assertEqual([r['cell'] for r in result.image_records()], ['G2','G3','G4'])
        self.assertIn('ref="H6:I6"', result.parts[decl['part']].decode())
        self.assertIn('sqref="I2:I4"', result.parts[decl['part']].decode())
        self.assertIn('<formula1>J2</formula1>', result.parts[decl['part']].decode())
        self.assertIn('topLeftCell="G2"', result.parts[decl['part']].decode())
        self.assertIn('xSplit="6"', result.parts[decl['part']].decode())
        self.assertIn('ref="I2"', result.parts['xl/comments/comment1.xml'].decode())
        self.assertIn('<x:Column>8</x:Column>', result.parts['xl/drawings/vmlDrawing1.vml'].decode())
        self.assertIn('<x:Anchor>8, 0, 1, 0, 10, 0, 3, 0</x:Anchor>', result.parts['xl/drawings/vmlDrawing1.vml'].decode())
        self.assertIn('<xdr:col>6</xdr:col>', result.parts['xl/drawings/drawing1.xml'].decode())
        self.assertIn('$I$2:$J$4', result.parts['xl/workbook.xml'].decode())
        self.assertIn('申报要素!$A$1:$J$4', result.parts['xl/workbook.xml'].decode())
        for part, data in result.parts.items():
            if part.endswith(('.xml','.rels')):
                xml(data)

    def test_never_overwrites_input_or_existing_output(self):
        with self.assertRaises(FidelityError):
            insert_columns(self.source, self.source, self.layout())
        destination = self.root / 'exists.xlsx'
        destination.write_bytes(b'existing data')
        with self.assertRaises(FidelityError):
            insert_columns(self.source, destination, self.layout())
        self.assertEqual(destination.read_bytes(), b'existing data')

    def test_self_closing_styled_cells_do_not_consume_next_cell(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('<row r="6">',
            '<row r="5"><c r="E5" s="0"/><c r="F5" s="0"/><c r="G5" t="inlineStr"><is><t>keep this</t></is></c></row><row r="6">'))
        destination = self.root / 'self-closing.xlsx'
        insert_columns(self.source, destination, self.layout())
        cells = Workbook(destination).sheet('申报要素')['cells']
        self.assertEqual(cells['G5']['value'], '')
        self.assertEqual(cells['H5']['value'], '')
        self.assertEqual(cells['I5']['value'], 'keep this')

    def test_whole_row_selection_keeps_excel_boundary(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('sqref="G2:G4"', 'sqref="$A2:$XFD4"', 1))
        destination = self.root / 'whole-row.xlsx'
        insert_columns(self.source, destination, self.layout())
        self.assertIn('sqref="$A2:$XFD4"', Workbook(destination).parts['xl/worksheets/sheet2.xml'].decode())

    def test_scrolled_view_and_failed_audit(self):
        self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('workbookViewId="0"', 'workbookViewId="0" topLeftCell="E1"'))
        destination = self.root / 'view.xlsx'
        insert_columns(self.source, destination, self.layout())
        self.assertIn('topLeftCell="G1"', Workbook(destination).parts['xl/worksheets/sheet2.xml'].decode())
        from experiments.excel_fidelity.insert import audit
        changed = self.root / 'corrupt.xlsx'
        with ZipFile(destination) as archive, ZipFile(changed,'w') as output:
            for info in archive.infolist():
                data = archive.read(info)
                if info.filename == 'xl/worksheets/sheet2.xml':
                    data = data.replace(b'ref="H6:I6"', b'ref="H6:J6"')
                output.writestr(info,data)
        report = audit(Workbook(self.source),Workbook(changed),'申报要素',5,[1,2,3,4])
        self.assertEqual(report['status'],'failed')
        self.assertTrue(any('metadata_changed' in message for message in report['failures']))

    def test_unaffected_shared_formulas_remain_shared(self):
        self.edit_part('xl/worksheets/sheet3.xml', lambda text: text.replace('</sheetData>',
            '<row r="2"><c r="C2"><f t="shared" si="2" ref="C2:C3">A2+1</f><v>1</v></c></row>'
            '<row r="3"><c r="C3"><f t="shared" si="2"/><v>1</v></c></row></sheetData>'))
        destination = self.root / 'shared-other.xlsx'
        insert_columns(self.source, destination, self.layout())
        other = Workbook(destination).sheet('其他页')
        self.assertEqual(other['cells']['C3']['formula'], '')
        self.assertEqual(other['cells']['C3']['formula_attributes'], {'t':'shared','si':'2'})
        self.assertEqual(effective_formulas(other)['C3'],'A3+1')

    def test_floating_picture_requires_exact_image_cell_anchor(self):
        self.edit_part('xl/drawings/drawing1.xml',lambda text: text.replace('<xdr:col>5</xdr:col>', '<xdr:col>4</xdr:col>').replace('<xdr:row>2</xdr:row>','<xdr:row>1</xdr:row>'))
        mapping = map_workbook(Workbook(self.source))['image_mapping']
        self.assertEqual(len(mapping['rows'][0]['floating_images']),1)
        self.edit_part('xl/drawings/drawing1.xml', lambda text: text.replace('<xdr:col>4</xdr:col>', '<xdr:col>6</xdr:col>'))
        mapping = map_workbook(Workbook(self.source))['image_mapping']
        self.assertFalse(mapping['rows'][0]['floating_images'])
        self.assertTrue(mapping['unassigned'])

    def test_unsupported_features_fail_before_output(self):
        for part in ('xl/tables/table1.xml', 'xl/charts/chart1.xml', 'xl/calcChain.xml'):
            with self.subTest(part=part):
                make_fixture(self.source, {part:'<placeholder/>'})
                destination = self.root / (Path(part).stem + '-blocked.xlsx')
                with self.assertRaises(FidelityError):
                    insert_columns(self.source,destination,self.layout())
                self.assertFalse(destination.exists())

    def test_result_columns_not_duplicated(self):
        for label in ('AI推荐HScode', 'AI推荐hscode', 'AI推荐 HS编码', '业务员确认HScode', '置信度', '预审结论'):
            with self.subTest(label=label):
                make_fixture(self.source)
                self.edit_part('xl/worksheets/sheet2.xml', lambda text: text.replace('历史意见', label))
                with self.assertRaises(FidelityError):
                    insert_columns(self.source,self.root/'blocked.xlsx',self.layout())
                self.assertFalse((self.root/'blocked.xlsx').exists())

    def test_cli_reports_and_empty_ai_cells(self):
        report = run(self.source,self.root/'private','SYNTHETIC')
        self.assertEqual(report['audit']['status'],'passed')
        stored = json.loads((self.root/'private/SYNTHETIC-report.json').read_text(encoding='utf-8'))
        self.assertIn('pending', stored['audit']['client_excel'])
        decl = Workbook(report['output_path']).sheet('申报要素')
        self.assertEqual(decl['cells']['E2']['value'],'')
        self.assertEqual(decl['cells']['F2']['value'],'')


if __name__ == '__main__':
    unittest.main()
