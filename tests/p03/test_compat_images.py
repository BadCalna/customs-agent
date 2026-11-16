"""Excel 2016 fallback: real embedded drawings, not unsupported image formulas."""
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from experiments.excel_fidelity.compat_images import export_excel2016_images, audit_compat_images
from experiments.excel_fidelity.mapping import detect_layout
from experiments.excel_fidelity.cli import run
from experiments.excel_fidelity.workbook import FidelityError, Workbook
from tests.p03.fixtures import A, make_fixture


class ImageCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.xlsx'
        self.destination = self.root / 'compatible.xlsx'
        make_fixture(self.source)

    def edit(self, part, function):
        with ZipFile(self.source) as archive:
            parts = {info.filename: archive.read(info) for info in archive.infolist()}
        parts[part] = function(parts[part].decode()).encode()
        make_fixture(self.source, parts)

    def convert(self):
        return export_excel2016_images(self.source, self.destination,
                                       detect_layout(Workbook(self.source), 'declaration'))

    def test_shared_images_become_row_matched_drawings_and_keep_other_content(self):
        original = Workbook(self.source)
        report = self.convert()
        result = Workbook(self.destination)
        self.assertEqual(report['converted_images'], 3)
        self.assertEqual(result.image_records(), [])
        self.assertEqual(len(result.floating_records()), len(original.floating_records()) + 3)
        self.assertEqual(result.floating_records()[:1], original.floating_records())
        for before in original.sheets:
            after = result.sheet(before['name'])
            for ref, cell in before['cells'].items():
                if before['name'] == '申报要素' and ref in ('E2', 'E3', 'E4'):
                    self.assertEqual(after['cells'][ref]['formula'], None)
                    self.assertEqual(after['cells'][ref]['value'], '')
                    self.assertEqual(after['cells'][ref]['style'], cell['style'])
                else:
                    self.assertEqual(after['cells'][ref], cell)
        for record in result.floating_records()[1:]:
            self.assertEqual(record['markers']['from']['col'], 4)
            self.assertEqual(record['markers']['from']['row'], record['markers']['to']['row'])
            self.assertEqual(record['markers']['from']['col'], record['markers']['to']['col'])
            self.assertEqual(record['images'][0]['decode_status'], 'passed')
        self.assertEqual({r['cell'] for r in report['images']}, {'E2', 'E3', 'E4'})
        self.assertTrue(all(r['original_cell']['formula_attributes']['t'] == 'shared' for r in report['images']))
        self.assertEqual(Workbook(self.source).source_hash, original.source_hash)
        self.assertEqual(result.parts['xl/media/test.png'], original.parts['xl/media/test.png'])
        self.assertEqual(audit_compat_images(original, result, report)['status'], 'passed')

    def test_creates_a_standard_drawing_when_sheet_had_none(self):
        self.edit('xl/worksheets/sheet2.xml', lambda s: s.replace('<drawing r:id="rIdDraw"/>', ''))
        self.edit('xl/worksheets/_rels/sheet2.xml.rels', lambda s:
                  re.sub(r'<Relationship\b[^>]*Id="rIdDraw"[^>]*/>', '', s))
        self.convert()
        result = Workbook(self.destination)
        self.assertEqual(len(result.floating_records()), 3)
        self.assertIn(b'application/vnd.openxmlformats-officedocument.drawing+xml', result.parts['[Content_Types].xml'])
        self.assertTrue(all(r['markers']['from']['row'] in (1, 2, 3) for r in result.floating_records()))

    def test_non_direct_or_missing_or_corrupt_images_refuse_publication(self):
        for kind in ('conditional', 'missing', 'corrupt', 'mode'):
            with self.subTest(kind=kind):
                make_fixture(self.source)
                if kind in ('conditional', 'missing', 'mode'):
                    old = '_xlfn.DISPIMG("ID_SYNTHETIC",1)'
                    new = {'conditional': 'IF(FALSE,' + old + ',"")',
                           'missing': '_xlfn.DISPIMG("MISSING",1)',
                           'mode': '_xlfn.DISPIMG("ID_SYNTHETIC",2)'}[kind]
                    self.edit('xl/worksheets/sheet2.xml', lambda s: s.replace(old, new))
                else:
                    make_fixture(self.source, {'xl/media/test.png': b'not an image'})
                with self.assertRaises(FidelityError):
                    self.convert()
                self.assertFalse(self.destination.exists())

    def test_rotation_is_baked_into_a_new_image_while_original_media_remains(self):
        from PIL import Image
        media = io.BytesIO()
        source_image = Image.new('RGB', (12, 24), 'green')
        source_image.putpixel((0, 23), (255, 0, 0))
        source_image.save(media, format='PNG')
        make_fixture(self.source, {'xl/media/test.png': media.getvalue()})
        self.edit('xl/cellimages.xml', lambda s: s.replace('</xdr:pic>',
                  f'<xdr:spPr><a:xfrm xmlns:a="{A}" rot="5400000"/></xdr:spPr></xdr:pic>'))
        report = self.convert()
        result = Workbook(self.destination)
        self.assertEqual(result.parts['xl/media/test.png'], media.getvalue())
        for record in report['images']:
            self.assertNotEqual(record['display_media_part'], record['media_part'])
            with Image.open(io.BytesIO(result.parts[record['display_media_part']])) as image:
                self.assertEqual(image.size, (24, 12))
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))

    def test_unsupported_rotation_hidden_or_merged_positions_refuse(self):
        for kind in ('rotation', 'row', 'column', 'merge'):
            with self.subTest(kind=kind):
                make_fixture(self.source)
                if kind == 'rotation':
                    self.edit('xl/cellimages.xml', lambda s: s.replace('</xdr:pic>',
                              f'<xdr:spPr><a:xfrm xmlns:a="{A}" rot="10"/></xdr:spPr></xdr:pic>'))
                else:
                    self.edit('xl/worksheets/sheet2.xml', lambda s: {
                        'row': s.replace('<row r="2"', '<row hidden="1" r="2"'),
                        'column': s.replace('min="1" max="8"', 'hidden="1" min="1" max="8"'),
                        'merge': s.replace('ref="F6:G6"', 'ref="E2:E3"'),
                    }[kind])
                with self.assertRaises(FidelityError):
                    self.convert()
                self.assertFalse(self.destination.exists())

    def test_existing_destination_source_and_repeat_conversion_are_protected(self):
        original = self.source.read_bytes()
        with self.assertRaises(FidelityError):
            export_excel2016_images(self.source, self.source, detect_layout(Workbook(self.source), 'declaration'))
        self.convert()
        saved = self.destination.read_bytes()
        with self.assertRaises(FidelityError):
            self.convert()
        with self.assertRaises(FidelityError):
            export_excel2016_images(self.destination, self.root / 'again.xlsx',
                                   detect_layout(Workbook(self.destination), 'declaration'))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.destination.read_bytes(), saved)

    def test_independent_audit_catches_data_media_anchor_and_other_sheet_changes(self):
        original = Workbook(self.source)
        report = self.convert()
        result = Workbook(self.destination)
        for kind in ('data', 'media', 'anchor', 'other_sheet'):
            with self.subTest(kind=kind):
                parts = dict(result.parts)
                if kind == 'media':
                    parts['xl/media/test.png'] = b'broken'
                elif kind == 'anchor':
                    part = report['images'][0]['drawing_part']
                    raw = parts[part].decode()
                    # Only generated anchors have this explicit namespace declaration.
                    start = raw.index('<xdr:twoCellAnchor xmlns:xdr=')
                    parts[part] = (raw[:start] + raw[start:].replace('<xdr:col>4</xdr:col>', '<xdr:col>3</xdr:col>', 1)).encode()
                else:
                    part = 'xl/worksheets/sheet2.xml' if kind == 'data' else 'xl/worksheets/sheet3.xml'
                    parts[part] = parts[part].replace(b'TEST_HS', b'CHANGED') if kind == 'data' else parts[part] + b' '
                path = self.root / (kind + '.xlsx')
                with ZipFile(path, 'w') as archive:
                    for name, data in parts.items():
                        archive.writestr(name, data)
                self.assertEqual(audit_compat_images(original, Workbook(path), report)['status'], 'failed')

    def test_cli_compatible_copy_is_explicit_and_modes_are_exclusive(self):
        report = run(self.source, self.root / 'output', 'SYNTHETIC', excel2016_images=True)
        self.assertTrue(Path(report['output_path']).name.endswith('-Excel2016图片兼容版.xlsx'))
        self.assertEqual(report['audit']['converted_images'], 3)
        self.assertEqual(report['audit']['client_excel2016'], 'pending_user_validation')
        with self.assertRaises(FidelityError):
            run(self.source, self.root / 'mixed', 'SYNTHETIC', add_precheck=True, excel2016_images=True)

    def test_failed_audit_and_concurrent_source_change_do_not_publish(self):
        with patch('experiments.excel_fidelity.compat_images.audit_compat_images',
                   return_value={'status': 'failed', 'failures': ['injected_corruption']}):
            with self.assertRaisesRegex(FidelityError, 'audit failed'):
                self.convert()
        self.assertFalse(self.destination.exists())
        real_audit = audit_compat_images
        def change_source(original, output, report):
            result = real_audit(original, output, report)
            with self.source.open('ab') as stream:
                stream.write(b'concurrent change')
            return result
        with patch('experiments.excel_fidelity.compat_images.audit_compat_images', side_effect=change_source):
            with self.assertRaisesRegex(FidelityError, 'Source changed'):
                self.convert()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.p03-images-*')), [])


if __name__ == '__main__':
    unittest.main()
