"""Append a missing precheck column without moving existing template columns."""

import copy
import os
from pathlib import Path
import re
import tempfile
from zipfile import ZipFile

from .insert import (ROW, PREFIX, attribute, canonical, check_supported,
                     set_attribute, sheet_prefix)
from .mapping import RESULT_ALIASES, field_matches
from .workbook import FidelityError, NS, Workbook, cell_position, col_label, xml


def append_precheck_column(source: str | Path, destination: str | Path, layout: dict) -> dict:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise FidelityError("Destination must be a new file, distinct from source")
    original = Workbook(source)
    target = original.sheet(layout['sheet'])
    header_row = layout['header_row']
    rows = sorted(set(layout['data_rows']) | {header_row})
    if any(cell_position(ref)[1] == header_row and any(
        field_matches(cell['value'], alias) for alias in RESULT_ALIASES['precheck_conclusion'])
        for ref, cell in target['cells'].items()
    ):
        raise FidelityError('Precheck conclusion column already present')
    columns = [cell_position(ref)[0] for ref in target['cells']]
    for merge in target['root'].findall('s:mergeCells/s:mergeCell', NS):
        columns.extend(cell_position(endpoint)[0] for endpoint in merge.get('ref').split(':'))
    dimension = target['root'].find('s:dimension', NS)
    if dimension is not None:
        columns.extend(cell_position(endpoint)[0] for endpoint in dimension.get('ref').split(':'))
    for obj in original.floating_records():
        if obj['sheet'] == target['name']:
            columns.extend(marker['col'] + 1 for marker in obj['markers'].values())
    column = max(columns, default=0) + 1
    label = col_label(column)  # Refuse exhaustion of the XLSX column limit.
    # Appending cells leaves original references untouched. Retain the same
    # conservative feature gates as the insertion experiment.
    check_supported(original, target, column, rows)
    for container in target['root']:
        if container.tag.rsplit('}', 1)[-1] in ('dataValidations', 'conditionalFormatting'):
            nodes = list(container) if container.tag.endswith('}dataValidations') else [container]
            for node in nodes:
                for area in node.get('sqref', '').split():
                    endpoints = area.replace('$', '').split(':')
                    try:
                        first, last = cell_position(endpoints[0]), cell_position(endpoints[-1])
                    except FidelityError:
                        raise FidelityError('Unbounded validation/format range requires native editor') from None
                    if first[0] <= column <= last[0] and any(first[1] <= row <= last[1] for row in rows):
                        raise FidelityError('New conclusion cells intersect existing validation/format rules')
    raw = original.parts[target['part']].decode('utf-8')
    prefix = sheet_prefix(raw)
    new_dimension = f'A1:{label}{max(cell_position(ref)[1] for ref in target["cells"])}'
    if dimension is None:
        raise FidelityError('Missing dimension requires additional append handling')
    raw = re.sub(rf'<{PREFIX}dimension\b[^>]*/>',
                 lambda match: set_attribute(match[0], 'ref', new_dimension), raw, count=1)
    old_columns = target['root'].findall('s:cols/s:col', NS)
    properties = {}
    for node in old_columns:
        if int(node.get('min')) <= column <= int(node.get('max')):
            properties = {key: value for key, value in node.attrib.items() if key not in ('min', 'max', 'hidden')}
    properties.update(width='50', customWidth='1')
    new_col = f'<{prefix}col min="{column}" max="{column}"/>'
    for key, value in properties.items():
        new_col = set_attribute(new_col, key, value)
    if target['root'].find('s:cols', NS) is not None:
        def replace_columns(match):
            entries = []
            for item in re.finditer(rf'<{PREFIX}col\b[^>]*/>', match[2]):
                first, last = int(attribute(item[0], 'min')), int(attribute(item[0], 'max'))
                if not first <= column <= last:
                    entries.append((first, item[0]))
                else:
                    if first < column:
                        entries.append((first, set_attribute(item[0], 'max', str(column - 1))))
                    if last > column:
                        entries.append((column + 1, set_attribute(item[0], 'min', str(column + 1))))
            entries.append((column, new_col))
            return match[1] + ''.join(text for _, text in sorted(entries)) + match[3]
        raw = re.sub(rf'(<{PREFIX}cols\b[^>]*>)(.*?)(</{PREFIX}cols>)', replace_columns, raw, count=1, flags=re.S)
    else:
        raw = re.sub(rf'(?=<{PREFIX}sheetData\b)', f'<{prefix}cols>{new_col}</{prefix}cols>', raw, count=1)
    selected_rows = set(rows)

    def append_cell(match):
        number = int(attribute(match[1], 'r'))
        if number not in selected_rows:
            return match[0]
        opening = match[1]
        spans = attribute(opening, 'spans')
        if spans:
            opening = set_attribute(opening, 'spans', f'{spans.split(":")[0]}:{column}')
        hs = target['cells'].get(f'{layout["fields"]["hs"]}{number}', {})
        style = f' s="{hs["style"]}"' if hs.get('style') is not None else ''
        value = '预审结论' if number == header_row else ''
        addition = (f'<{prefix}c r="{label}{number}"{style} t="inlineStr">'
                    f'<{prefix}is><{prefix}t>{value}</{prefix}t></{prefix}is></{prefix}c>')
        return opening + match[2] + addition + match[3]

    raw = ROW.sub(append_cell, raw)
    parts = dict(original.parts)
    parts[target['part']] = raw.encode('utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.p03-', suffix='.xlsx', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, 'w') as archive:
            archive.comment = original.zip_comment
            for info in original.infos:
                archive.writestr(info, parts[info.filename])
        output = Workbook(temporary)
        actual = copy.deepcopy(output.sheet(target['name'])['root'])
        expected = copy.deepcopy(target['root'])
        failures = []
        added_refs = {f'{label}{number}' for number in rows}
        added_cells = {ref: cell for ref, cell in output.sheet(target['name'])['cells'].items()
                       if ref in added_refs}
        if set(added_cells) != added_refs:
            failures.append('missing_conclusion_cells')
        for ref, cell in added_cells.items():
            value = '预审结论' if cell_position(ref)[1] == header_row else ''
            hs = target['cells'].get(f'{layout["fields"]["hs"]}{cell_position(ref)[1]}', {})
            if cell['value'] != value or cell['formula'] is not None or cell['type'] != 'inlineStr' or cell['style'] != hs.get('style'):
                failures.append(f'invalid_conclusion_cell:{ref}')
        expected_rows = {row.get('r'): row for row in expected.findall('s:sheetData/s:row', NS)}
        for row in actual.findall('s:sheetData/s:row', NS):
            for child in list(row):
                if child.get('r') in added_refs:
                    row.remove(child)
            if int(row.get('r')) in selected_rows:
                previous = expected_rows[row.get('r')]
                if previous.get('spans'):
                    if row.get('spans') != f'{previous.get("spans").split(":")[0]}:{column}':
                        failures.append('invalid_row_span')
                    row.set('spans', previous.get('spans'))
        actual_dimension = actual.find('s:dimension', NS)
        if actual_dimension.get('ref') != new_dimension:
            failures.append('invalid_dimension')
        actual_dimension.set('ref', dimension.get('ref'))
        def column_properties(root):
            result = {}
            for node in root.findall('s:cols/s:col', NS):
                attributes = {key: value for key, value in node.attrib.items() if key not in ('min', 'max')}
                for index in range(int(node.get('min')), int(node.get('max')) + 1):
                    if index in result:
                        failures.append('overlapping_column_definitions')
                    result[index] = attributes
            return result
        expected_columns = column_properties(expected)
        expected_columns[column] = properties
        if column_properties(actual) != expected_columns:
            failures.append('invalid_conclusion_column')
        for root in (expected, actual):
            for node in list(root.findall('s:cols', NS)):
                root.remove(node)
        if canonical(expected) != canonical(actual):
            failures.append('original_sheet_content_or_metadata_changed')
        changed = [part for part in original.parts if original.parts[part] != output.parts.get(part)]
        if set(original.parts) != set(output.parts) or changed != [target['part']]:
            failures.append('unexpected_package_change')
        if failures:
            raise FidelityError('Append audit failed: ' + ', '.join(failures))
        os.link(temporary, destination)
        return {'status': 'passed', 'operation': 'append_precheck_column',
                'sheet': target['name'], 'column': label, 'header': '预审结论',
                'original_cells_checked': sum(len(sheet['cells']) for sheet in original.sheets),
                'media_parts_byte_identical': sum(part.startswith('xl/media/') and not part.endswith('/') for part in original.parts),
                'changed_parts': changed, 'source_sha256': original.source_hash,
                'output_sha256': output.source_hash, 'client_wps': 'pending', 'client_excel': 'pending',
                'formula_calculation': 'not_recalculated; original formulas and caches preserved'}
    finally:
        temporary.unlink(missing_ok=True)
