"""Static Excel 2016 picture fallback with narrow OOXML changes and an audit.

WPS originals remain intact. Direct DISPIMG calls become standard drawings in a
separate copy. This does not evaluate conditional/dynamic image formulas.
"""
from __future__ import annotations

import copy
import io
import math
import os
from pathlib import Path
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZipFile

from .insert import CELL, FORMULA, PREFIX, attribute, canonical, sheet_prefix
from .references import effective_formulas
from .workbook import (CorruptPartError, FidelityError, NS, P, R, S, Workbook, cell_position,
                       decode_xml_text, merge_bounds, parse_int_attr, relationship_part, resolve_part, sha256, xml)

XDR = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
DRAWING_TYPE = 'application/vnd.openxmlformats-officedocument.drawing+xml'
IMAGE_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
               'gif': 'image/gif', 'bmp': 'image/bmp', 'tif': 'image/tiff', 'tiff': 'image/tiff'}


def _attr(value):
    return escape(str(value), {'"': '&quot;'})


def _append(raw, root_name, fragment):
    closing = re.compile(rf'</{PREFIX}{root_name}\s*>\s*$')
    match = closing.search(raw)
    if match:
        return raw[:match.start()] + fragment + raw[match.start():]
    empty = re.search(rf'<({PREFIX}{root_name})\b[^>]*?/>\s*$', raw)
    if not empty:
        raise FidelityError(f'Cannot append to {root_name}')
    return raw[:empty.start()] + empty[0].rstrip()[:-2] + '>' + fragment + f'</{empty[1]}>'


def _drawing_node_position(root):
    after = {'legacyDrawing', 'legacyDrawingHF', 'drawingHF', 'picture', 'oleObjects',
             'controls', 'webPublishItems', 'tableParts', 'extLst'}
    return next((index for index, node in enumerate(root) if node.tag.rsplit('}', 1)[-1] in after), len(root))


def _add_sheet_drawing(raw, root, rid):
    fragment = f'<{sheet_prefix(raw)}drawing xmlns:r="{R}" r:id="{_attr(rid)}"/>'
    position = _drawing_node_position(root)
    if position < len(root):
        name = root[position].tag.rsplit('}', 1)[-1]
        match = re.search(rf'<{PREFIX}{name}\b', raw)
    else:
        match = re.search(rf'</{PREFIX}worksheet\s*>', raw)
    if not match:
        raise FidelityError('Cannot place worksheet drawing in schema order')
    return raw[:match.start()] + fragment + raw[match.start():]


def _unused(prefix, used):
    number = 1
    while f'{prefix}{number}' in used:
        number += 1
    return f'{prefix}{number}'


def _geometry(sheet, cell, size):
    column, row_number = cell_position(cell)
    for merge in sheet['root'].findall('s:mergeCells/s:mergeCell', NS):
        first, last, _ = merge_bounds(merge)
        if first[0] <= column <= last[0] and first[1] <= row_number <= last[1]:
            raise FidelityError(f'Merged image position requires manual review: {cell}')
    fmt = sheet['root'].find('s:sheetFormatPr', NS)
    defaults = fmt.attrib if fmt is not None else {}
    columns = [c.attrib for c in sheet['root'].findall('s:cols/s:col', NS)
               if parse_int_attr(c.get('min'), label='col/@min') <= column <= parse_int_attr(c.get('max'), label='col/@max')]
    if len(columns) > 1:
        raise FidelityError('Overlapping column dimensions')
    column_info = columns[0] if columns else {}
    row = next((r for r in sheet['root'].findall('s:sheetData/s:row', NS)
                if parse_int_attr(r.get('r'), label='row/@r') == row_number), None)
    row_info = row.attrib if row is not None else {}
    if (column_info.get('hidden') in ('1', 'true') or row_info.get('hidden') in ('1', 'true')
            or defaults.get('zeroHeight') in ('1', 'true') and not row_info.get('ht')):
        raise FidelityError(f'Hidden image position requires manual review: {cell}')
    width = float(column_info.get('width', defaults.get('defaultColWidth', '8.43')))
    height = float(row_info.get('ht', defaults.get('defaultRowHeight', '15')))
    scaled_width, scaled_height = width * 7, height * 12700
    if not all(math.isfinite(v) and v > 0 for v in (width, height, scaled_width, scaled_height)):
        raise FidelityError('Invalid image cell dimensions')
    # Width uses an approximate 7px maximum digit width; generous inner padding
    # leaves room for font metrics. Row point heights convert exactly to EMU.
    cell_width, cell_height = math.floor(scaled_width) * 9525, round(scaled_height)
    padding = 2 * 9525
    if min(cell_width, cell_height) <= padding * 2:
        raise FidelityError('Image cell too small for a visible fallback')
    factor = min((cell_width - padding * 2) / size[0], (cell_height - padding * 2) / size[1])
    cx, cy = max(1, math.floor(size[0] * factor)), max(1, math.floor(size[1] * factor))
    return {'column': column - 1, 'row': row_number - 1,
            'x': (cell_width - cx) // 2, 'y': (cell_height - cy) // 2, 'cx': cx, 'cy': cy}


def _display_image(book, record):
    pictures = [pic for pic in xml(book.parts[record['definition_part']]).iter()
                if pic.tag.rsplit('}', 1)[-1] == 'pic'
                and any(n.tag.endswith('}cNvPr') and n.get('name') == record['image_id'] for n in pic.iter())]
    if len(pictures) != 1:
        raise FidelityError('Image definition is ambiguous')
    pic = pictures[0]
    for node in pic.iter():
        name = node.tag.rsplit('}', 1)[-1]
        if (name == 'srcRect' and any(int(v) for v in node.attrib.values())
                or name in ('tile', 'custGeom') or name in ('effectLst', 'effectDag') and len(node)
                or name == 'prstGeom' and node.get('prst') != 'rect'):
            raise FidelityError('Unsupported picture crop/shape/effects')
        if name == 'blip' and any(c.tag.rsplit('}', 1)[-1] != 'extLst' for c in node):
            raise FidelityError('Unsupported image color/alpha effects')
    transforms = [node for node in pic.iter() if node.tag == f'{{{A}}}xfrm']
    if len(transforms) > 1:
        raise FidelityError('Ambiguous picture transform')
    transform = transforms[0].attrib if transforms else {}
    rotation = int(transform.get('rot', '0')) % 21600000
    if rotation % 5400000:
        raise FidelityError('Non-orthogonal image rotation requires manual review')
    for key in ('flipH', 'flipV'):
        if transform.get(key, '0') not in ('0', '1', 'false', 'true'):
            raise FidelityError('Invalid picture flip')
    flip_h, flip_v = (transform.get(key) in ('1', 'true') for key in ('flipH', 'flipV'))
    data = book.parts[record['media_part']]
    if not rotation and not flip_h and not flip_v:
        return data, record['size'], False
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if flip_h:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if flip_v:
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if rotation:
            image = image.transpose({5400000: Image.Transpose.ROTATE_270,
                                     10800000: Image.Transpose.ROTATE_180,
                                     16200000: Image.Transpose.ROTATE_90}[rotation])
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue(), list(image.size), True


def _anchor(record):
    g = record['geometry']
    def marker(name, x, y):
        return (f'<xdr:{name}><xdr:col>{g["column"]}</xdr:col><xdr:colOff>{x}</xdr:colOff>'
                f'<xdr:row>{g["row"]}</xdr:row><xdr:rowOff>{y}</xdr:rowOff></xdr:{name}>')
    return (f'<xdr:twoCellAnchor xmlns:xdr="{XDR}" xmlns:a="{A}" xmlns:r="{R}" editAs="twoCell">'
            + marker('from', g['x'], g['y']) + marker('to', g['x'] + g['cx'], g['y'] + g['cy'])
            + f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="{record["picture_id"]}" name="P03-{_attr(record["cell"])}" '
              f'descr="Static image from {_attr(record["cell"])}; original formula retained in local report"/>'
              '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
              f'<xdr:blipFill><a:blip r:embed="{record["relationship_id"]}"/>'
              '<a:stretch><a:fillRect/></a:stretch></xdr:blipFill>'
              f'<xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{g["cx"]}" cy="{g["cy"]}"/></a:xfrm>'
              '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>'
              '<xdr:clientData/></xdr:twoCellAnchor>')


def audit_compat_images(original, output, report):
    """Check source cells/metadata, unchanged parts, existing drawings and new anchors."""
    failures = []
    images = report['images']
    changed = set(report['changed_parts'])
    if set(output.parts) != set(original.parts) | set(report['added_parts']):
        failures.append('unexpected_package_parts')
    for part, data in original.parts.items():
        if part not in changed and output.parts.get(part) != data:
            failures.append('unexpected_part_change:' + part)
    for sheet in original.sheets:
        expected = copy.deepcopy(sheet['root'])
        converted = {r['cell'] for r in images if r['sheet'] == sheet['name']}
        for cell in expected.findall('s:sheetData/s:row/s:c', NS):
            if cell.get('r') in converted:
                for child in list(cell):
                    if child.tag in (f'{{{S}}}f', f'{{{S}}}v'):
                        cell.remove(child)
                cell.attrib.pop('t', None)
        for link in report['new_drawing_links']:
            if link['sheet'] == sheet['name']:
                expected.insert(_drawing_node_position(expected), ET.Element(f'{{{S}}}drawing', {f'{{{R}}}id': link['relationship_id']}))
        if canonical(expected) != canonical(output.sheet(sheet['name'])['root']):
            failures.append('cell_or_sheet_metadata_changed:' + sheet['name'])
    for part in {r['drawing_part'] for r in images}:
        old = xml(original.parts[part]) if part in original.parts else ET.Element(f'{{{XDR}}}wsDr')
        actual = xml(output.parts[part])
        selected = [r for r in images if r['drawing_part'] == part]
        expected_children = list(old) + [xml(_anchor(r).encode()) for r in selected]
        if actual.tag != old.tag or actual.attrib != old.attrib or len(actual) != len(expected_children) or any(
                canonical(a) != canonical(b) for a, b in zip(actual, expected_children)):
            failures.append('drawing_or_anchor_changed:' + part)
        ids = [n.get('id') for n in actual.iter() if n.tag.endswith('}cNvPr')]
        if len(ids) != len(set(ids)):
            failures.append('duplicate_picture_ids:' + part)
    for part, additions in report['added_relationships'].items():
        expected = xml(original.parts[part]) if part in original.parts else ET.Element(f'{{{P}}}Relationships')
        for attrs in additions:
            expected.append(ET.Element(f'{{{P}}}Relationship', attrs))
        if canonical(expected) != canonical(xml(output.parts[part])):
            failures.append('relationship_changed:' + part)
    content = xml(original.parts['[Content_Types].xml'])
    for entry in report['added_content_types']:
        content.append(ET.Element(f'{{{CT}}}' + entry['kind'], entry['attributes']))
    if canonical(content) != canonical(xml(output.parts['[Content_Types].xml'])):
        failures.append('content_types_changed')
    for record in images:
        relation = output.relationships(record['drawing_part']).get(record['relationship_id'], {})
        part = resolve_part(record['drawing_part'], relation.get('Target', ''))
        if (part != record['display_media_part'] or relation.get('Type') != R + '/image'
                or relation.get('TargetMode') == 'External'
                or sha256(output.parts.get(part, b'')) != record['display_media_sha256']
                or output.media_check(part).get('decode_status') != 'passed'):
            failures.append('image_resource_mismatch:' + record['cell'])
    if output.image_records():
        failures.append('unconverted_image_formulas')
    return {'status': 'failed' if failures else 'passed', 'failures': failures,
            'original_cells_checked': sum(len(s['cells']) for s in original.sheets),
            'original_media_parts_byte_identical': sum(p.startswith('xl/media/') for p in original.parts),
            'client_excel2016': 'pending_user_validation', 'client_wps': 'pending_user_validation',
            'formula_calculation': 'not_recalculated; non-image formulas and caches preserved'}


def export_excel2016_images(source, destination, layout):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise FidelityError('Destination must be a new file, distinct from source')
    book = Workbook(source)
    if any(any(token in p.lower() for token in ('calcchain.xml', 'vba', '_xmlsignatures/')) for p in book.parts):
        raise FidelityError('Calculation chains, macros or signatures require native handling')
    records = book.image_records()
    if not records:
        raise FidelityError('No direct DISPIMG images to convert')
    target = book.sheet(layout['sheet'])
    formulas = effective_formulas(target)
    for record in records:
        if (record['sheet'] != target['name'] or not layout['fields'].get('image')
                or cell_position(record['cell'])[0] != cell_position(layout['fields']['image'] + '1')[0]
                or record['cell'] not in target['cells'] or cell_position(record['cell'])[1] not in layout['data_rows']
                or record.get('formula_status') != 'direct_image_call' or record.get('decode_status') != 'passed'
                or record.get('status') != 'resource_found'
                or not re.fullmatch(r'(?:_xlfn\.)?DISPIMG\s*\(\s*"[^"\r\n]+"\s*,\s*1\s*\)', formulas.get(record['cell'], '').strip().removeprefix('='), re.I)):
            raise FidelityError('Only decoded direct DISPIMG mode 1 calls in product image cells can convert')
    parts = dict(book.parts)
    report = {'operation': 'static_excel2016_images', 'source_sha256': book.source_hash,
              'images': [], 'new_drawing_links': [], 'added_relationships': {}, 'added_content_types': []}

    def add_relationship(part, attrs):
        raw_bytes = parts.get(part, f'<Relationships xmlns="{P}"/>'.encode())
        raw = decode_xml_text(raw_bytes, part)
        fragment = '<Relationship xmlns="' + P + '" ' + ' '.join(f'{k}="{_attr(v)}"' for k, v in attrs.items()) + '/>'
        parts[part] = _append(raw, 'Relationships', fragment).encode()
        report['added_relationships'].setdefault(part, []).append(attrs)

    def ensure_content(kind, key, value, content_type):
        if '[Content_Types].xml' not in parts:
            raise CorruptPartError('missing [Content_Types].xml')
        raw_ct = decode_xml_text(parts['[Content_Types].xml'], '[Content_Types].xml')
        root = xml(parts['[Content_Types].xml'])
        existing = [e for e in root if e.tag == f'{{{CT}}}{kind}' and e.get(key, '').lower() == value.lower()]
        if existing:
            if len(existing) != 1 or existing[0].get('ContentType') != content_type:
                raise FidelityError('Conflicting drawing/image content type')
            return
        attrs = {key: value, 'ContentType': content_type}
        fragment = f'<{kind} xmlns="{CT}" ' + ' '.join(f'{k}="{_attr(v)}"' for k, v in attrs.items()) + '/>'
        parts['[Content_Types].xml'] = _append(raw_ct, 'Types', fragment).encode()
        report['added_content_types'].append({'kind': kind, 'attributes': attrs})

    nodes = target['root'].findall('s:drawing', NS)
    if len(nodes) > 1:
        raise FidelityError('Multiple worksheet drawings require manual review')
    if nodes:
        relation = book.relationships(target['part']).get(nodes[0].get(f'{{{R}}}id'), {})
        if relation.get('Type') != R + '/drawing' or relation.get('TargetMode') == 'External':
            raise FidelityError('Unsupported worksheet drawing relationship')
        target_ref = relation.get('Target')
        if not target_ref:
            raise CorruptPartError('Worksheet drawing relationship is missing Target')
        drawing = resolve_part(target['part'], target_ref)
        if drawing not in parts:
            raise CorruptPartError(f'Missing worksheet drawing part: {drawing}')
        drawing_count = 0
        for s in book.sheets:
            for r in book.relationships(s['part']).values():
                if r.get('Type') == R + '/drawing' and r.get('TargetMode') != 'External':
                    if not r.get('Target'):
                        raise CorruptPartError(f'Drawing relationship in {s["part"]} is missing Target')
                    if resolve_part(s['part'], r['Target']) == drawing:
                        drawing_count += 1
        if drawing_count != 1:
            raise FidelityError('Shared worksheet drawing requires manual review')
    else:
        drawing = _unused('xl/drawings/drawingP03Compatibility', {p.removesuffix('.xml') for p in parts}) + '.xml'
        parts[drawing] = f'<xdr:wsDr xmlns:xdr="{XDR}"/>'.encode()
        rid = _unused('rIdP03Drawing', set(book.relationships(target['part'])))
        add_relationship(relationship_part(target['part']), {'Id': rid, 'Type': R + '/drawing',
                         'Target': posixpath.relpath(drawing, posixpath.dirname(target['part']))})
        parts[target['part']] = _add_sheet_drawing(decode_xml_text(parts[target['part']], target['part']), target['root'], rid).encode()
        report['new_drawing_links'].append({'sheet': target['name'], 'relationship_id': rid})
    if xml(parts[drawing]).tag != f'{{{XDR}}}wsDr':
        raise FidelityError('Unsupported drawing namespace')
    ensure_content('Override', 'PartName', '/' + drawing, DRAWING_TYPE)
    drawing_rels = relationship_part(drawing)
    used_ids = {e.get('Id') for e in xml(parts.get(drawing_rels, f'<Relationships xmlns="{P}"/>'.encode()))}
    picture_id = max((parse_int_attr(n.get('id'), label='cNvPr/@id') for n in xml(parts[drawing]).iter() if n.tag.endswith('}cNvPr')), default=0)
    for record in records:
        media, size, transformed = _display_image(book, record)
        display_part = record['media_part']
        if transformed:
            display_part = _unused('xl/media/p03Compatibility', {p.removesuffix('.png') for p in parts}) + '.png'
            parts[display_part] = media
        extension = display_part.rsplit('.', 1)[-1].lower()
        if extension not in IMAGE_TYPES:
            raise FidelityError('Image format requires manual compatibility review')
        ensure_content('Default', 'Extension', extension, IMAGE_TYPES[extension])
        rid = _unused('rIdP03Image', used_ids)
        used_ids.add(rid)
        picture_id += 1
        entry = {**record, 'display_media_part': display_part, 'display_media_sha256': sha256(media),
                 'transformed_to_png': transformed, 'drawing_part': drawing, 'relationship_id': rid,
                 'picture_id': picture_id, 'geometry': _geometry(target, record['cell'], size),
                 'original_cell': target['cells'][record['cell']],
                 'original_effective_formula': formulas[record['cell']]}
        add_relationship(drawing_rels, {'Id': rid, 'Type': R + '/image',
                         'Target': posixpath.relpath(display_part, posixpath.dirname(drawing))})
        parts[drawing] = _append(decode_xml_text(parts[drawing], drawing), 'wsDr', _anchor(entry)).encode()
        report['images'].append(entry)
    converted = {record['cell'] for record in records}
    def clear_formula(match):
        raw = match[0]
        if attribute(raw.split('>', 1)[0] + '>', 'r') not in converted:
            return raw
        raw = FORMULA.sub('', raw)
        raw = re.sub(rf'<{PREFIX}v\b[^>]*?(?:/>|>.*?</{PREFIX}v>)', '', raw, flags=re.S)
        return re.sub(r'\st="[^"]*"', '', raw, count=1)
    parts[target['part']] = CELL.sub(clear_formula, decode_xml_text(parts[target['part']], target['part'])).encode()
    report['changed_parts'] = [p for p in book.parts if parts[p] != book.parts[p]]
    report['added_parts'] = sorted(set(parts) - set(book.parts))
    report['converted_images'] = len(records)
    report['static_snapshot'] = True
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.p03-images-', suffix='.xlsx', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, 'w') as archive:
            archive.comment = book.zip_comment
            for info in book.infos:
                archive.writestr(info, parts[info.filename])
            for part in report['added_parts']:
                archive.writestr(part, parts[part])
        output = Workbook(temporary)
        report.update(audit_compat_images(book, output, report))
        if report['status'] != 'passed':
            raise FidelityError('Image compatibility audit failed: ' + ', '.join(report['failures']))
        if sha256(source.read_bytes()) != book.source_hash:
            raise FidelityError('Source changed during conversion; output not published')
        report['output_sha256'] = output.source_hash
        os.link(temporary, destination)
        return report
    finally:
        temporary.unlink(missing_ok=True)
