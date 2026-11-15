"""Traceable semantic mapping; uncertainty is data, never a guessed join."""

import re
import unicodedata
from collections import defaultdict

from .workbook import FidelityError, NS, cell_position, col_label, col_number

ALIASES = {
    "contract": ("采购合同号", "合同号", "合同编号"),
    "item": ("合同项号", "合同项次"),
    "primary_name": ("合同第一条品名",),
    "alias": ("附件清单品名",),
    "name": ("品名", "商品品名"),
    "specification": ("规格型号", "规格"),
    "manufacturer_model": ("厂家提供型号", "供应商提供型号"),
    "material": ("物料代码", "物料编码"),
    "hs": ("海关编码", "HSCODE", "HS编码"),
    "elements": ("申报要素",),
    "image": ("附图", "商品图片"),
    "refund_rate": ("退税率",),
    "merge_hint": ("合并申报",),
    "quantity": ("数量",),
    "unit": ("单位",),
}

RESULT_ALIASES = {
    "ai_hs": ("AI推荐HScode", "AI推荐HS编码", "AI推荐海关编码"),
    "confirmed_hs": ("业务员确认HScode", "业务员确认HS编码"),
    "confidence": ("置信度",),
    "precheck_conclusion": ("预审结论",),
}


def normalized(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).upper()


def field_matches(value: str, alias: str) -> bool:
    value, alias = normalized(value), normalized(alias)
    return value == alias or value.startswith(alias + "(") or (
        alias == "退税率" and value == "退税率%"
    )


def floating_product_row(obj: dict, sheet: str, image_column: str | None, header_row: int) -> int | None:
    """Only an exact product-image cell anchor can supply a candidate row."""
    if obj["sheet"] != sheet or not obj["images"] or not image_column:
        return None
    first, last = obj["markers"].get("from", {}), obj["markers"].get("to", {})
    row = first.get("row")
    if (isinstance(row, int) and header_row <= row < 1048576
            and row == last.get("row")
            and first.get("col") == last.get("col") == col_number(image_column) - 1):
        return row + 1
    return None


def detect_layout(workbook, role: str) -> dict:
    label = "装箱明细" if role == "packing" else "申报要素"
    sheets = [s for s in workbook.sheets if label in s["name"]]
    if len(sheets) != 1:
        raise FidelityError(f"Expected one {label} worksheet; found {len(sheets)}")
    sheet = sheets[0]
    aliases_by_field = ALIASES | (RESULT_ALIASES if role == "declaration" else {})
    rows = defaultdict(dict)
    cells_by_row = defaultdict(list)
    for ref, cell in sheet["cells"].items():
        column, row = cell_position(ref)
        cells_by_row[row].append(cell)
        if row <= 25:
            for field, aliases in aliases_by_field.items():
                if any(field_matches(cell["value"], alias) for alias in aliases):
                    rows[row].setdefault(field, []).append(column)
    required = {"contract", "item"} | ({"hs", "elements"} if role == "declaration" else set())
    candidates = [(r, f) for r, f in rows.items() if required <= f.keys()]
    if len(candidates) != 1:
        raise FidelityError(f"Ambiguous/missing {label} header row")
    row, matches = candidates[0]
    if any(len(columns) != 1 for columns in matches.values()):
        raise FidelityError(f"Duplicate semantic header in {label}")
    fields = {f: col_label(columns[0]) for f, columns in matches.items()}
    warnings = []
    if role == "packing" and "primary_name" not in fields and "name" in fields:
        fields["primary_name"] = fields["name"]
        warnings.append("legacy_name_header: 品名用于本样本主品名，未声称存在合同第一条品名列")
    optional = (("primary_name", "alias", "specification", "manufacturer_model", "quantity", "unit")
                if role == "packing" else ("image", *RESULT_ALIASES))
    warnings.extend(f"missing_column:{field}" for field in optional if field not in fields)
    merges = []
    for merge in sheet["root"].findall("s:mergeCells/s:mergeCell", NS):
        start, end = merge.attrib["ref"].split(":")
        merges.append((cell_position(start), cell_position(end), start))

    def value_at(column, row_number):
        ref = f"{column}{row_number}"
        cell = sheet["cells"].get(ref, {})
        if cell.get("value"):
            return cell["value"], ref
        col = cell_position(ref)[0]
        for (c1, r1), (c2, r2), anchor in merges:
            if c1 == c2 == col and r1 <= row_number <= r2:
                return sheet["cells"].get(anchor, {}).get("value", ""), anchor
        return cell.get("value", ""), ref

    floating_by_row = defaultdict(list)
    if role == "declaration":
        for obj in workbook.floating_records():
            image_row = floating_product_row(obj, sheet["name"], fields.get("image"), row)
            if image_row is not None:
                floating_by_row[image_row].append(obj)
    candidate_rows = sorted(number for number in cells_by_row.keys() | floating_by_row.keys() if number > row)
    records = []
    non_item_rows = []
    for number in candidate_rows:
        facts = {field: value_at(column, number) for field, column in fields.items()}
        contract = facts.get("contract", ("", ""))[0].strip()
        name = facts.get("primary_name" if role == "packing" else "name", ("", ""))[0].strip()
        item = facts.get("item", ("", ""))[0].strip()
        raw_values = [cell for cell in cells_by_row[number] if cell["value"] or cell["formula"]]
        evidence_fields = ("hs", "elements", "image", "specification", "manufacturer_model",
                           "material", "quantity", "unit", "refund_rate", *RESULT_ALIASES)
        strong_fields = ("hs", "elements", "image", "specification", "manufacturer_model", "material")

        def has_fact(field):
            value, ref = facts.get(field, ("", ""))
            if field == "refund_rate" and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%?", value.strip()):
                return bool(sheet["cells"].get(ref, {}).get("formula"))
            return bool(value.strip() or sheet["cells"].get(ref, {}).get("formula"))

        evidence = bool(floating_by_row[number]) or any(has_fact(field) for field in evidence_fields)

        def preceding_total(cell):
            match = re.fullmatch(
                r'(?:SUM|COUNT|COUNTA|AVERAGE|MIN|MAX)\s*\(\s*\$?([A-Z]{1,3})\$?([0-9]+)'
                r'\s*:\s*\$?\1\$?([0-9]+)\s*\)', cell['formula'] or '', re.I)
            return bool(match and row < int(match[2]) <= int(match[3]) < number)

        if (not contract and not item and not name and not floating_by_row[number]
                and not any(has_fact(field) for field in strong_fields)
                and len(raw_values) > 1 and all(preceding_total(cell) for cell in raw_values)):
            non_item_rows.append({'row': number, 'reason': 'summary_formula_only; retained, not treated as an item'})
            continue
        elements_ref = facts.get("elements", ("", ""))[1]
        aggregate_elements = re.match(r"(?:SUM|SUBTOTAL|AGGREGATE|COUNT|COUNTA|AVERAGE|MIN|MAX)\s*\(",
                                      sheet["cells"].get(elements_ref, {}).get("formula") or "", re.I)
        if not contract and not item and re.fullmatch(r"(?:合计|总计|小计)[:：]?", name) and not any(
            has_fact(field) for field in strong_fields if field != "elements"
        ) and (not has_fact("elements") or aggregate_elements) and not floating_by_row[number]:
            non_item_rows.append({"row": number, "reason": "summary_row; retained, not treated as an item"})
            continue
        if not item and not name and not evidence:
            populated = bool(contract) or bool(raw_values)
            if populated:
                non_item_rows.append({"row": number, "reason": "contract_only_or_footer; retained, not treated as an item"})
            continue
        missing = [f"missing_{field}" for field, value in
                   (("contract", contract), ("item", item), ("name", name)) if not value]
        records.append({
            "row": number, "contract": contract, "item_raw": item,
            "status": "requires_review" if missing else "identified",
            "issues": missing,
            "floating_image_sources": [{"part": obj["part"], "index": obj["index"],
                                         "cell": f'{fields["image"]}{number}'}
                                        for obj in floating_by_row[number]],
            "facts": {field: {"value": value, "cell": ref} for field, (value, ref) in facts.items()},
        })
    return {"sheet": sheet["name"], "part": sheet["part"], "header_row": row,
            "fields": fields, "warnings": warnings, "records": records,
            "non_item_rows": non_item_rows,
            "data_rows": [r["row"] for r in records]}


def parse_items(value: str) -> list[str]:
    value = unicodedata.normalize("NFKC", value).strip()
    if not value:
        raise FidelityError("missing_item")
    result = []
    for segment in re.split(r"[、,;；\n]+", value):
        segment = segment.strip()
        if re.fullmatch(r"[0-9]+", segment):
            members = [segment]
        else:
            match = re.fullmatch(r"([0-9]+)\s*[-~～—–]\s*([0-9]+)", segment)
            if not match:
                raise FidelityError("unparsed_item_list")
            first, last = int(match[1]), int(match[2])
            if last < first or last - first > 1000:
                raise FidelityError("ambiguous_or_excessive_item_range")
            width = len(match[1]) if match[1].startswith("0") else 0
            members = [str(i).zfill(width) for i in range(first, last + 1)]
        if any(m in result for m in members):
            raise FidelityError("duplicate_item_in_list")
        result.extend(members)
    return result


def map_workbook(workbook) -> dict:
    packing = detect_layout(workbook, "packing")
    declaration = detect_layout(workbook, "declaration")
    index = defaultdict(list)
    issues = []
    for record in packing["records"]:
        try:
            if not record["contract"] or re.search(r"[\r\n、]", record["contract"]):
                raise FidelityError("missing_or_multiple_contracts")
            members = parse_items(record["item_raw"])
            if len(members) != 1:
                raise FidelityError("packing_item_not_atomic")
            index[(normalized(record["contract"]), members[0])].append(record)
        except FidelityError as exc:
            issues.append({"type": str(exc), "sheet": packing["sheet"], "row": record["row"]})
    associations = []
    used = set()
    owners = defaultdict(list)
    for record in declaration["records"]:
        link = {"declaration_row": record["row"], "members": [], "issues": list(record["issues"])}
        try:
            if not record["contract"] or re.search(r"[\r\n、]", record["contract"]):
                raise FidelityError("missing_or_multiple_contracts")
            members = parse_items(record["item_raw"])
        except FidelityError as exc:
            link["issues"].append(str(exc))
            link["status"] = "requires_review"
            associations.append(link)
            continue
        for member in members:
            key = (normalized(record["contract"]), member)
            owners[key].append(record["row"])
            candidates = index.get(key, [])
            if not candidates:
                link["issues"].append(f"missing_packing_member:{member}")
            signatures = {tuple(p["facts"].get(f, {}).get("value", "") for f in
                                ("primary_name", "alias", "specification", "manufacturer_model", "material"))
                          for p in candidates}
            if len(signatures) > 1:
                link["issues"].append(f"same_key_different_facts:{member}")
            for candidate in candidates:
                link["members"].append({"item": member, "packing_row": candidate["row"]})
                if candidate["issues"]:
                    link["issues"].append(f"packing_row_requires_review:{candidate['row']}")
                used.add(candidate["row"])
        link["status"] = "requires_review" if link["issues"] else "linked_by_contract_item"
        associations.append(link)
    for key, rows in owners.items():
        if len(rows) > 1:
            issues.append({"type": "member_claimed_by_multiple_declarations", "declaration_rows": rows,
                           "contract": key[0], "item": key[1]})
            for link in associations:
                if link["declaration_row"] in rows:
                    link["status"] = "requires_review"
                    link["issues"].append(f"member_claimed_by_multiple_declarations:{key[1]}")
                    link["conflicting_declaration_rows"] = sorted(set(
                        link.get("conflicting_declaration_rows", []) +
                        [number for number in rows if number != link["declaration_row"]]))
    issues.extend({"type": "unlinked_packing_row", "row": r["row"]}
                  for r in packing["records"] if r["row"] not in used)
    return {"packing": packing, "declaration": declaration,
            "associations": associations, "issues": issues,
            "image_mapping": map_images(workbook, declaration),
            "meaning": "位置及候选成员映射，不代表已完成P04商品语义稽核或HS归类"}


def map_images(workbook, declaration):
    rows = {row: {"row": row, "cell_images": [], "floating_images": []}
            for row in declaration["data_rows"]}
    image_column = declaration["fields"].get("image")
    image_column_number = col_number(image_column) if image_column else None
    unassigned = []
    for image in workbook.image_records():
        if image["sheet"] != declaration["sheet"]:
            continue
        col, row = cell_position(image["cell"])
        if row in rows and col == image_column_number:
            rows[row]["cell_images"].append(image)
        else:
            unassigned.append({"kind": "cell_image", "record": image,
                               "reason": "outside identified product/image cells"})
    for obj in workbook.floating_records():
        if obj["sheet"] != declaration["sheet"] or not obj["images"]:
            continue
        image_row = floating_product_row(obj, declaration["sheet"], image_column, declaration["header_row"])
        if image_row in rows:
            rows[image_row]["floating_images"].append(obj)
        else:
            unassigned.append({"kind": "floating_image", "record": obj,
                               "reason": "requires manual anchor/ownership confirmation"})
    product_issues = {record["row"]: record["issues"] for record in declaration["records"]}
    for record in rows.values():
        needs_review = (bool(product_issues[record["row"]]) or
                        any(i["status"] != "resource_found" or i.get("decode_status") != "passed"
                            for i in record["cell_images"]) or
                        any(i["status"] != "resource_found" or i.get("decode_status") != "passed"
                            for obj in record["floating_images"] for i in obj["images"]))
        record["status"] = ("requires_review" if needs_review else
                            "cell_image_reference" if record["cell_images"] else
                            "single_cell_floating_anchor" if record["floating_images"] else
                            "no_mapped_image; check unassigned objects separately")
    return {"rows": list(rows.values()), "unassigned": unassigned,
            "note": "位置/资源映射，不代表已经OCR或核实图中商品身份；缺资源见单项状态"}
