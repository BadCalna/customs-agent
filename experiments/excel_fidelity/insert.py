"""Minimal OOXML insertion with explicit feature gates and a semantic audit.

This is deliberately not a general Excel editor. Unknown dependent features are
rejected before any output is published; unchanged package parts stay byte exact.
"""

from __future__ import annotations

import copy
import html
import os
import re
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

from .mapping import RESULT_ALIASES, field_matches
from .references import effective_formulas, shift_range, transform_formula
from .workbook import (CorruptPartError, FidelityError, NS, Workbook, cell_position, col_label,
                       col_number, decode_xml_text, merge_bounds, parse_int_attr, resolve_part, sha256, xml)

PREFIX = r"(?:[A-Za-z_][\w.-]*:)?"
CELL = re.compile(rf"<{PREFIX}c\b[^>]*?(?:/>|>.*?</{PREFIX}c>)", re.S)
FORMULA = re.compile(rf"<{PREFIX}f\b[^>]*?(?:/>|>.*?</{PREFIX}f>)", re.S)
ROW = re.compile(rf"(<{PREFIX}row\b[^>]*>)(.*?)(</{PREFIX}row>)", re.S)
ROW_ELEMENT = re.compile(rf"<{PREFIX}row\b[^>]*?(?:/>|>.*?</{PREFIX}row>)", re.S)


def attribute(tag: str, name: str) -> str | None:
    match = re.search(rf'\s{re.escape(name)}="([^"]*)"', tag)
    return html.unescape(match[1]) if match else None


def set_attribute(tag: str, name: str, value: str) -> str:
    pattern = rf'(\s{re.escape(name)}=")[^"]*(")'
    encoded = escape(value, {'"': '&quot;'})
    if re.search(pattern, tag):
        return re.sub(pattern, lambda m: m[1] + encoded + m[2], tag, count=1)
    return re.sub(r"/?>$", lambda m: f' {name}="{encoded}"' + m[0], tag, count=1)


def sheet_prefix(raw: str) -> str:
    match = re.search(r"<(?P<prefix>[\w.-]+:)?worksheet\b", raw)
    if not match:
        raise FidelityError("Worksheet root not found")
    return match["prefix"] or ""


def ensure_result_rows(raw: str, numbers: list[int]) -> str:
    """Materialize result cells' rows, including rows that only have a drawing."""
    wanted = set(numbers)
    if any(not 1 <= number <= 1048576 for number in wanted):
        raise FidelityError("Result row outside XLSX limit")
    prefix = sheet_prefix(raw)

    def change(match):
        body = match[2]
        entries = list(ROW_ELEMENT.finditer(body))
        present = [parse_int_attr(attribute(entry[0].split('>', 1)[0] + '>', 'r'), label='row/@r') for entry in entries]
        if len(present) != len(set(present)) or present != sorted(present):
            raise FidelityError('Duplicate or unordered worksheet rows')
        missing = iter(sorted(wanted - set(present)))
        pending = next(missing, None)
        result, cursor = [], 0
        for entry, number in zip(entries, present):
            result.append(body[cursor:entry.start()])
            while pending is not None and pending < number:
                result.append(f'<{prefix}row r="{pending}"></{prefix}row>')
                pending = next(missing, None)
            text = entry[0]
            if number in wanted and text.endswith('/>'):
                row_prefix = re.match(r'<([\w.-]+:)?row\b', text)[1] or ''
                text = text[:-2] + f'></{row_prefix}row>'
            result.append(text)
            cursor = entry.end()
        while pending is not None:
            result.append(f'<{prefix}row r="{pending}"></{prefix}row>')
            pending = next(missing, None)
        result.append(body[cursor:])
        return match[1] + ''.join(result) + match[3]

    return re.sub(rf'(<{PREFIX}sheetData\b[^>]*>)(.*?)(</{PREFIX}sheetData>)', change, raw, count=1, flags=re.S)


def extended_dimension(ref: str, last_column: int, last_row: int) -> str:
    first, last = ref.split(':') if ':' in ref else (ref, ref)
    column, row = cell_position(last)
    return f'{first}:{col_label(max(column, last_column))}{max(row, last_row)}'


def check_supported(workbook: Workbook, target: dict, start: int, rows: list[int]):
    unsupported_parts = ("/tables/", "/pivot", "/charts/", "/externallinks/", "/slicer",
                         "calcchain.xml", "vba", "_xmlsignatures/", "threadedcomment", "connections.xml")
    for part in workbook.parts:
        if any(marker in part.lower() for marker in unsupported_parts):
            raise FidelityError(f"Feature requires additional dependency handling: {part}")
    supported_tags = {
        "sheetPr", "dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData",
        "sheetCalcPr", "sheetProtection", "autoFilter", "mergeCells", "conditionalFormatting",
        "dataValidations", "hyperlinks", "printOptions", "pageMargins", "pageSetup",
        "headerFooter", "rowBreaks", "drawing", "legacyDrawing", "legacyDrawingHF",
    }
    for child in target["root"]:
        if child.tag.rsplit("}", 1)[-1] not in supported_tags:
            raise FidelityError(f"Unsupported target worksheet element: {child.tag}")
    if target["root"].find("s:sheetProtection", NS) is not None:
        raise FidelityError("Protected target worksheet; do not bypass protection")
    if target["root"].find(".//s:filterColumn", NS) is not None:
        raise FidelityError("Active filter criteria require additional index handling")
    for c in target["root"].findall("s:cols/s:col", NS):
        c_min = parse_int_attr(c.get("min"), label="col/@min")
        c_max = parse_int_attr(c.get("max"), label="col/@max")
        if c_max > 16382 and not (c_max == 16384 and c_min <= 16382):
            raise FidelityError("Explicit styles in final XLSX columns cannot be preserved during insertion")
    for sheet in workbook.sheets:
        for cell in sheet["cells"].values():
            if cell["formula_attributes"].get("t") not in (None, "normal", "shared"):
                raise FidelityError("Array/data-table formula requires native editor")
        for formula in effective_formulas(sheet).values():
            transform_formula(formula, sheet["name"], target["name"], start)
        for node in sheet["root"].iter():
            if node.tag.rsplit("}", 1)[-1] in ("f", "formula", "formula1", "formula2"):
                if not node.tag.startswith("{" + NS["s"] + "}"):
                    raise FidelityError("Extended validation/conditional formula requires additional handling")
                transform_formula(node.text or "", sheet["name"], target["name"], start)
    for merge in target["root"].findall("s:mergeCells/s:mergeCell", NS):
        (c1, r1), (c2, r2), _ = merge_bounds(merge)
        if c1 < start <= c2 and any(r1 <= r <= r2 for r in rows):
            raise FidelityError("Insertion intersects a merged result cell")
    drawing_owners = {}
    for sheet in workbook.sheets:
        for rel in workbook.relationships(sheet["part"]).values():
            if rel.get("TargetMode") == "External":
                continue
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            if kind in ("drawing", "vmlDrawing", "comments"):
                target_ref = rel.get("Target")
                if not target_ref:
                    raise CorruptPartError(f"missing Target on {kind} relationship")
                part = resolve_part(sheet["part"], target_ref)
                drawing_owners.setdefault(part, []).append(sheet["name"])
    if any(len(owners) > 1 and target["name"] in owners for owners in drawing_owners.values()):
        raise FidelityError("A drawing/comment part is shared between worksheets")


def rewrite_formulas(raw: str, sheet: dict, target: str, start: int, prefix: str) -> str:
    effective = effective_formulas(sheet)
    changed_groups = set()
    for ref, formula in effective.items():
        cell = sheet["cells"][ref]
        if cell["formula_attributes"].get("t") == "shared" and (
            sheet["name"] == target or transform_formula(formula, sheet["name"], target, start) != formula
        ):
            changed_groups.add(cell["formula_attributes"].get("si"))

    def change_cell(match):
        text = match[0]
        ref = attribute(text.split(">", 1)[0] + ">", "r")
        cell = sheet["cells"].get(ref)
        if cell is None or ref not in effective:
            return text
        formula = transform_formula(effective[ref], sheet["name"], target, start)
        expand = (cell["formula_attributes"].get("t") == "shared"
                  and cell["formula_attributes"].get("si") in changed_groups)
        if cell["formula_attributes"].get("t") == "shared" and not expand:
            return text
        if not expand and formula == cell["formula"]:
            return text
        def replace_formula(original_match):
            if expand:
                return f"<{prefix}f>{escape(formula)}</{prefix}f>"
            opening = original_match[0].split(">", 1)[0].removesuffix("/") + ">"
            return opening + escape(formula) + f"</{prefix}f>"
        return FORMULA.sub(replace_formula, text, count=1)

    return CELL.sub(change_cell, raw)


def rewrite_columns(raw: str, start: int, prefix: str) -> str:
    pattern = re.compile(rf"(<{PREFIX}cols\b[^>]*>)(.*?)(</{PREFIX}cols>)", re.S)
    match = pattern.search(raw)
    new_columns = (f'<{prefix}col min="{start}" max="{start}" width="18" customWidth="1"/>'
                   f'<{prefix}col min="{start + 1}" max="{start + 1}" width="50" customWidth="1"/>')
    if not match:
        return re.sub(rf"(?=<{PREFIX}sheetData\b)", f"<{prefix}cols>{new_columns}</{prefix}cols>", raw, count=1)
    columns = []
    for item in re.finditer(rf"<{PREFIX}col\b[^>]*/>", match[2]):
        text = item[0]
        first = parse_int_attr(attribute(text, "min"), label="col/@min")
        last = parse_int_attr(attribute(text, "max"), label="col/@max")
        if last < start:
            columns.append((first, text))
        elif first >= start:
            columns.append((first + 2, set_attribute(set_attribute(text, "min", str(first + 2)), "max", str(min(last + 2, 16384)))))
        else:
            columns.append((first, set_attribute(text, "max", str(start - 1))))
            columns.append((start + 2, set_attribute(set_attribute(text, "min", str(start + 2)), "max", str(min(last + 2, 16384)))))
    columns.append((start, new_columns))
    replacement = match[1] + "".join(text for _, text in sorted(columns)) + match[3]
    return raw[:match.start()] + replacement + raw[match.end():]


def rewrite_target(raw: str, target: dict, start: int, header_row: int, data_rows: list[int]) -> str:
    prefix = sheet_prefix(raw)
    selected_rows = set(data_rows) | {header_row}
    raw = ensure_result_rows(raw, list(selected_rows))
    raw = rewrite_formulas(raw, target, target["name"], start, prefix)

    def shift_cell(match):
        text = match[0]
        old = attribute(text.split(">", 1)[0] + ">", "r")
        if old:
            opening, rest = text.split(">", 1)
            text = set_attribute(opening + ">", "r", shift_range(old, start)) + rest
        return text

    raw = CELL.sub(shift_cell, raw)

    def shift_tag(match):
        text = match[0]
        local = re.match(rf"<{PREFIX}([\w.-]+)", text)[1]
        for attr in ("ref", "sqref", "activeCell", "topLeftCell"):
            value = attribute(text, attr)
            if value is not None:
                text = set_attribute(text, attr, " ".join(shift_range(v, start) for v in value.split()))
        if local == 'dimension' and attribute(text, 'ref'):
            text = set_attribute(text, 'ref', extended_dimension(attribute(text, 'ref'), start + 1, max(selected_rows)))
        if local == "row" and attribute(text, "spans"):
            first, last = map(int, attribute(text, "spans").split(":"))
            text = set_attribute(text, "spans", f"{first + (2 if first >= start else 0)}:{last + (2 if last >= start else 0)}")
        if local == "pane" and attribute(text, "state") in ("frozen", "frozenSplit"):
            value = attribute(text, "xSplit")
            if value is not None and float(value) >= start - 1:
                text = set_attribute(text, "xSplit", str(int(float(value)) + 2))
        return text

    # Coordinates on <c> were already changed; r on rows is intentionally intact.
    raw = re.sub(rf"<{PREFIX}(?:dimension|mergeCell|autoFilter|sortState|sortCondition|selection|pane|sheetView|row|f|dataValidation|conditionalFormatting|hyperlink)\b[^>]*>", shift_tag, raw)
    raw = rewrite_metadata_formulas(raw, target["name"], target["name"], start)
    raw = rewrite_columns(raw, start, prefix)

    def add_cells(match):
        row = int(attribute(match[1], "r"))
        if row not in selected_rows:
            return match[0]
        source = target["cells"].get(f"{col_label(start - 1)}{row}", {})
        style = f' s="{source["style"]}"' if source.get("style") is not None else ""
        values = ("AI推荐HScode", "预审结论") if row == header_row else ("", "")
        additions = "".join(
            f'<{prefix}c r="{col_label(start + i)}{row}"{style} t="inlineStr"><{prefix}is><{prefix}t>{value}</{prefix}t></{prefix}is></{prefix}c>'
            for i, value in enumerate(values)
        )
        cells = list(CELL.finditer(match[2]))
        position = next((c.start() for c in cells if cell_position(attribute(c[0].split(">", 1)[0] + ">", "r"))[0] > start + 1), len(match[2]))
        return match[1] + match[2][:position] + additions + match[2][position:] + match[3]

    raw = ROW.sub(add_cells, raw)
    return raw


def rewrite_metadata_formulas(raw: str, context: str, target: str, start: int) -> str:
    for tag in ("formula", "formula1", "formula2"):
        def change(match):
            formula = html.unescape(match[2])
            rewritten = transform_formula(formula, context, target, start)
            return match[0] if rewritten == formula else match[1] + escape(rewritten) + match[3]
        raw = re.sub(rf"(<{PREFIX}{tag}\b[^>]*>)(.*?)(</{PREFIX}{tag}>)", change, raw, flags=re.S)
    return raw


def rewrite_hyperlinks(raw: str, context: str, target: str, start: int) -> str:
    def change(match):
        text = match[0]
        location = attribute(text, "location")
        return set_attribute(text, "location", transform_formula(location, context, target, start)) if location else text
    return re.sub(rf"<{PREFIX}hyperlink\b[^>]*>", change, raw)


def _sheet_by_local_id(workbook: Workbook, local_id: str | None) -> str:
    if local_id is None:
        return ""
    index = parse_int_attr(local_id, label="definedName/@localSheetId")
    if not 0 <= index < len(workbook.sheets):
        raise CorruptPartError(f"definedName localSheetId out of range: {index}")
    return workbook.sheets[index]["name"]


def rewrite_names(raw: str, workbook: Workbook, target: str, start: int) -> str:
    def change(match):
        context = _sheet_by_local_id(workbook, attribute(match[1], "localSheetId"))
        formula = html.unescape(match[2])
        result = transform_formula(formula, context, target, start)
        return match[0] if result == formula else match[1] + escape(result) + match[3]
    return re.sub(rf"(<{PREFIX}definedName\b[^>]*>)(.*?)(</{PREFIX}definedName>)", change, raw, flags=re.S)


def rewrite_drawing(raw: str, start: int) -> str:
    # Only xdr marker columns, not absolute pixel positions or shape dimensions.
    return re.sub(rf"(<{PREFIX}col>)([0-9]+)(</{PREFIX}col>)",
                  lambda m: m[1] + str(int(m[2]) + (2 if int(m[2]) >= start - 1 else 0)) + m[3], raw)


def rewrite_vml(raw: str, start: int) -> str:
    raw = re.sub(rf"(<{PREFIX}Column>)([0-9]+)(</{PREFIX}Column>)",
                 lambda m: m[1] + str(int(m[2]) + (2 if int(m[2]) >= start - 1 else 0)) + m[3], raw)

    def anchor(match):
        values = [v.strip() for v in match[2].split(",")]
        if len(values) != 8 or not all(v.isdigit() for v in values):
            raise FidelityError("Unsupported VML anchor")
        for index in (0, 4):
            number = int(values[index])
            if number >= start - 1:
                values[index] = str(number + 2)
        return match[1] + ", ".join(values) + match[3]
    return re.sub(rf"(<{PREFIX}Anchor>)(.*?)(</{PREFIX}Anchor>)", anchor, raw, flags=re.S)


def audit(original: Workbook, output: Workbook, target: str, start: int, added_rows: list[int]) -> dict:
    failures = []
    checked = 0
    if [s["name"] for s in original.sheets] != [s["name"] for s in output.sheets]:
        failures.append("sheet_order_or_names_changed")
    for old_sheet, new_sheet in zip(original.sheets, output.sheets):
        old_formulas = effective_formulas(old_sheet)
        new_formulas = effective_formulas(new_sheet)
        expected_refs = set()
        for ref, cell in old_sheet["cells"].items():
            new_ref = shift_range(ref, start) if old_sheet["name"] == target else ref
            expected_refs.add(new_ref)
            actual = new_sheet["cells"].get(new_ref)
            expected_formula = (transform_formula(old_formulas[ref], old_sheet["name"], target, start)
                                if ref in old_formulas else None)
            if actual is None or any(actual[k] != cell[k] for k in ("value", "type", "style")) or new_formulas.get(new_ref) != expected_formula:
                failures.append(f"cell_changed:{old_sheet['name']}!{ref}->{new_ref}")
            checked += 1
        extras = set(new_sheet["cells"]) - expected_refs
        allowed = ({f"{col_label(c)}{r}" for c in (start, start + 1) for r in added_rows}
                   if old_sheet["name"] == target else set())
        if extras != allowed:
            failures.append(f"unexpected_cells:{old_sheet['name']}")
        failures.extend(audit_sheet_metadata(old_sheet, new_sheet, target, start, added_rows))
    if set(original.parts) != set(output.parts):
        failures.append("package_part_set_changed")
    changed = [p for p in original.parts if original.parts[p] != output.parts.get(p)]
    media = [p for p in original.parts if p.startswith("xl/media/") and not p.endswith("/")]
    if any(original.parts[p] != output.parts.get(p) for p in media):
        failures.append("media_bytes_changed")
    before_images, after_images = original.image_records(), output.image_records()
    normalized_images = []
    for image in before_images:
        image = dict(image)
        if image["sheet"] == target:
            image["cell"] = shift_range(image["cell"], start)
        normalized_images.append(image)
    if normalized_images != after_images:
        failures.append("cell_image_identity_or_row_changed")
    expected_floating = copy.deepcopy(original.floating_records())
    for obj in expected_floating:
        if obj["sheet"] == target:
            for marker in obj["markers"].values():
                if marker["col"] >= start - 1:
                    marker["col"] += 2
    if expected_floating != output.floating_records():
        failures.append("floating_object_mapping_changed")
    expected_workbook = copy.deepcopy(xml(original.parts["xl/workbook.xml"]))
    for name in expected_workbook.findall("s:definedNames/s:definedName", NS):
        context = _sheet_by_local_id(original, name.get("localSheetId"))
        name.text = transform_formula(name.text or "", context, target, start)
    if canonical(expected_workbook) != canonical(xml(output.parts["xl/workbook.xml"])):
        failures.append("workbook_metadata_changed")
    for rel in original.relationships(original.sheet(target)["part"]).values():
        if rel.get("TargetMode") == "External":
            continue
        kind = rel.get("Type", "").rsplit("/", 1)[-1]
        if kind not in ("drawing", "comments", "vmlDrawing"):
            continue
        target_ref = rel.get("Target")
        if not target_ref:
            raise CorruptPartError(f"missing Target on {kind} relationship")
        part = resolve_part(original.sheet(target)["part"], target_ref)
        if part not in original.parts:
            raise CorruptPartError(f"missing related part: {part}")
        expected = copy.deepcopy(xml(original.parts[part]))
        if kind == "drawing":
            for anchor in expected:
                for marker in anchor:
                    if marker.tag.rsplit("}", 1)[-1] not in ("from", "to"):
                        continue
                    for coordinate in marker:
                        if coordinate.tag.endswith("}col"):
                            col_val = parse_int_attr(coordinate.text, label="drawing col")
                            if col_val >= start - 1:
                                coordinate.text = str(col_val + 2)
        elif kind == "comments":
            for comment in expected.findall("s:commentList/s:comment", NS):
                comment_ref = comment.get("ref")
                if not comment_ref:
                    raise CorruptPartError("comment element is missing ref attribute")
                comment.set("ref", shift_range(comment_ref, start))
        else:
            for node in expected.iter():
                if node.tag.endswith("}Column"):
                    col_val = parse_int_attr(node.text, label="VML Column")
                    if col_val >= start - 1:
                        node.text = str(col_val + 2)
                elif node.tag.endswith("}Anchor"):
                    if not node.text:
                        raise CorruptPartError("VML Anchor element is empty")
                    values = [parse_int_attr(x, label="VML Anchor") for x in node.text.split(",")]
                    if len(values) != 8:
                        raise FidelityError("Unsupported VML anchor")
                    for index in (0, 4):
                        if values[index] >= start - 1:
                            values[index] += 2
                    node.text = ", ".join(map(str, values))
        if canonical(expected) != canonical(xml(output.parts[part])):
            failures.append(f"object_metadata_changed:{part}")
    return {"status": "passed" if not failures else "failed", "failures": failures,
            "original_cells_checked": checked, "media_parts_byte_identical": len(media),
            "cell_image_references_checked": len(before_images), "changed_parts": changed,
            "unchanged_parts": len(original.parts) - len(changed),
            "formula_calculation": "not_recalculated; cached values preserved",
            "client_wps": "pending", "client_excel": "pending"}


def canonical(element):
    """Tree comparison ignores only attribute order, not content/format settings."""
    return (element.tag, tuple(sorted(element.attrib.items())), element.text or "",
            tuple(canonical(child) for child in element))


def audit_sheet_metadata(before, after, target, start, added_rows=()):
    failures = []
    expected = copy.deepcopy(before["root"])
    actual = copy.deepcopy(after["root"])
    shifted = before["name"] == target
    if shifted:
        data = expected.find('s:sheetData', NS)
        if data is not None:
            from xml.etree.ElementTree import Element
            present = {parse_int_attr(row.get('r'), label='row/@r') for row in data}
            for number in sorted(set(added_rows) - present):
                position = next((index for index, row in enumerate(data)
                                 if parse_int_attr(row.get('r'), label='row/@r') > number), len(data))
                data.insert(position, Element('{' + NS['s'] + '}row', {'r': str(number)}))
    for root in (expected, actual):
        data = root.find("s:sheetData", NS)
        if data is not None:
            # Rows carry heights/hidden/outline settings independently of cells.
            for row in data:
                for child in list(row):
                    row.remove(child)
    for node in expected.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if shifted:
            for key in ("ref", "sqref", "activeCell", "topLeftCell"):
                if key in node.attrib:
                    node.set(key, " ".join(shift_range(v, start) for v in node.get(key).split()))
            if local == 'dimension' and node.get('ref') and added_rows:
                node.set('ref', extended_dimension(node.get('ref'), start + 1, max(added_rows)))
            if local == "row" and node.get("spans"):
                first, last = map(int, node.get("spans").split(":"))
                node.set("spans", f"{first + (2 if first >= start else 0)}:{last + (2 if last >= start else 0)}")
            if local == "pane" and node.get("state") in ("frozen", "frozenSplit") and float(node.get("xSplit", 0)) >= start - 1:
                node.set("xSplit", str(int(float(node.get("xSplit"))) + 2))
        if local in ("formula", "formula1", "formula2"):
            node.text = transform_formula(node.text or "", before["name"], target, start)
        if local == "hyperlink" and node.get("location"):
            node.set("location", transform_formula(node.get("location"), before["name"], target, start))
    if shifted:
        def columns(root):
            result = {}
            for node in root.findall("s:cols/s:col", NS):
                properties = {k:v for k,v in node.attrib.items() if k not in ("min", "max")}
                c_min = parse_int_attr(node.get("min"), label="col/@min")
                c_max = parse_int_attr(node.get("max"), label="col/@max")
                for number in range(c_min, c_max + 1):
                    result[number] = properties
            return result
        expected_columns, actual_columns = columns(expected), columns(actual)
        expected_columns = {c + (2 if c >= start else 0): value for c,value in expected_columns.items()
                            if c + (2 if c >= start else 0) <= 16384}
        for c,width in ((start, "18"), (start + 1, "50")):
            expected_columns[c] = {"width": width, "customWidth": "1"}
        if expected_columns != actual_columns:
            failures.append(f"column_properties_changed:{before['name']}")
        for root in (expected, actual):
            for node in list(root.findall("s:cols", NS)):
                root.remove(node)
    if canonical(expected) != canonical(actual):
        failures.append(f"sheet_metadata_changed:{before['name']}")
    return failures


def insert_columns(source: str | Path, destination: str | Path, layout: dict) -> dict:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise FidelityError("Destination must be a new file, distinct from source")
    original = Workbook(source)
    target = original.sheet(layout["sheet"])
    header_row = layout["header_row"]
    rows = layout["data_rows"]
    start = col_number(layout["fields"]["hs"]) + 1
    for ref, cell in target["cells"].items():
        if cell_position(ref)[1] == header_row and any(field_matches(cell["value"], alias)
               for aliases in RESULT_ALIASES.values() for alias in aliases):
            raise FidelityError("Result columns already present; resolve reuse explicitly")
    check_supported(original, target, start, [header_row] + rows)
    parts = copy.copy(original.parts)
    allowed = {"xl/workbook.xml", *(s["part"] for s in original.sheets)}
    for sheet in original.sheets:
        raw = decode_xml_text(parts[sheet["part"]], sheet["part"])
        if sheet["name"] == target["name"]:
            raw = rewrite_target(raw, sheet, start, header_row, rows)
        else:
            raw = rewrite_formulas(raw, sheet, target["name"], start, sheet_prefix(raw))
            raw = rewrite_metadata_formulas(raw, sheet["name"], target["name"], start)
        raw = rewrite_hyperlinks(raw, sheet["name"], target["name"], start)
        parts[sheet["part"]] = raw.encode("utf-8")
    parts["xl/workbook.xml"] = rewrite_names(decode_xml_text(parts["xl/workbook.xml"], "xl/workbook.xml"), original, target["name"], start).encode("utf-8")
    for rel in original.relationships(target["part"]).values():
        if rel.get("TargetMode") == "External":
            continue
        kind = rel.get("Type", "").rsplit("/", 1)[-1]
        if kind not in ("drawing", "comments", "vmlDrawing"):
            continue
        target_ref = rel.get("Target")
        if not target_ref:
            raise CorruptPartError(f"missing Target on {kind} relationship")
        part = resolve_part(target["part"], target_ref)
        if part not in parts:
            raise CorruptPartError(f"missing related part: {part}")
        allowed.add(part)
        raw = decode_xml_text(parts[part], part)
        if kind == "drawing":
            raw = rewrite_drawing(raw, start)
        elif kind == "vmlDrawing":
            raw = rewrite_vml(raw, start)
        else:
            def _shift_comment(m):
                ref = attribute(m[0], "ref")
                if not ref:
                    raise CorruptPartError("comment element is missing ref attribute")
                return set_attribute(m[0], "ref", shift_range(ref, start))
            raw = re.sub(rf"<{PREFIX}comment\b[^>]*>", _shift_comment, raw)
        parts[part] = raw.encode("utf-8")
    for part, data in parts.items():
        if data != original.parts[part] and part not in allowed:
            raise FidelityError(f"Unexpected part change: {part}")
        if part.endswith((".xml", ".rels")):
            xml(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(suffix=".xlsx", prefix=".p03-", dir=destination.parent)
    os.close(fd)
    try:
        with ZipFile(temporary, "w") as archive:
            archive.comment = original.zip_comment
            for info in original.infos:
                archive.writestr(copy.copy(info), parts[info.filename])
        result = audit(original, Workbook(temporary), target["name"], start, [header_row] + rows)
        after_hash = sha256(source.read_bytes())
        result["source_sha256_before"] = original.source_hash
        result["source_sha256_after"] = after_hash
        if after_hash != original.source_hash or result["status"] != "passed":
            raise FidelityError(f"Output audit failed: {result['failures']}")
        # Same-directory hard link publishes the audited complete file atomically
        # and refuses an existing destination (including races).
        os.link(temporary, destination)
        result["output_sha256"] = sha256(destination.read_bytes())
        result["insertion"] = {"sheet": target["name"], "after": layout["fields"]["hs"],
                               "new_columns": [col_label(start), col_label(start + 1)], "data_rows": len(rows)}
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)
