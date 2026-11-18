"""Read OOXML without re-saving through a library that can drop WPS parts."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile, ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"s": S, "r": R}


class FidelityError(ValueError):
    """An unsupported or ambiguous input must not be silently rewritten."""


class CorruptPartError(FidelityError):
    """A workbook part is not well-formed XML (input problem, not an export bug)."""

    code = "source_workbook_invalid"


def xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise FidelityError("DTD/entity XML is unsupported")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        # 损坏的部件属于输入文件问题：转成结构化校验错误，让入口返回 422 而不是 500。
        raise CorruptPartError(f"workbook part is not well-formed XML: {exc}") from exc
    except LookupError as exc:
        # XML 声明里出现未知/不可用编码时，ElementTree 抛的是 LookupError（不是 ParseError）。
        raise CorruptPartError(f"workbook part declares an unsupported encoding: {exc}") from exc


def decode_xml_text(data: bytes, part: str) -> str:
    """Decode an OOXML XML part as UTF-8, raising CorruptPartError on invalid bytes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptPartError(f"part {part} is not valid UTF-8: {exc}") from exc


def parse_int_attr(value: str | None, *, label: str) -> int:
    """Parse a required integer attribute/text value from an OOXML element."""
    if value is None:
        raise CorruptPartError(f"{label} is missing integer value")
    try:
        return int(value.strip())
    except ValueError as exc:
        raise CorruptPartError(f"{label} is not a valid integer: {value!r}") from exc


def merge_bounds(merge) -> tuple[tuple[int, int], tuple[int, int], str]:
    """Return ((start_col, start_row), (end_col, end_row), start_ref) for a mergeCell."""
    ref = merge.get("ref")
    if not ref:
        raise CorruptPartError("mergeCell element is missing ref attribute")
    parts = ref.split(":")
    if len(parts) != 2:
        raise CorruptPartError(f"invalid mergeCell ref: {ref!r}")
    return cell_position(parts[0]), cell_position(parts[1]), parts[0]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def col_number(label: str) -> int:
    value = 0
    for char in label.upper():
        if char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            raise FidelityError(f"Invalid column: {label}")
        value = value * 26 + ord(char) - 64
    return value


def col_label(number: int) -> str:
    if not 1 <= number <= 16384:
        raise FidelityError(f"Column outside XLSX limit: {number}")
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def cell_position(ref: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]*)", ref)
    if not match:
        raise FidelityError(f"Invalid cell reference: {ref}")
    return col_number(match[1]), int(match[2])


def relationship_part(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def resolve_part(base: str, target: str) -> str:
    result = posixpath.normpath(
        target.lstrip("/") if target.startswith("/")
        else posixpath.join(posixpath.dirname(base), target)
    )
    if result.startswith("../") or "\\" in result:
        raise FidelityError("Relationship escapes the OOXML package")
    return result


def text_content(element: ET.Element | None) -> str:
    if element is None:
        return ""
    # Rich strings may include phonetic runs, which are not additional cell text.
    return "".join((child.text or "") if child.tag == f"{{{S}}}t"
                   else child.findtext(f"{{{S}}}t", "")
                   for child in element if child.tag in (f"{{{S}}}t", f"{{{S}}}r"))


class Workbook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._media_checks = {}
        self.source_hash = sha256(self.path.read_bytes())
        try:
            with ZipFile(self.path) as archive:
                infos = archive.infolist()
                if len({i.filename for i in infos}) != len(infos):
                    raise FidelityError("Duplicate ZIP entries")
                if sum(i.file_size for i in infos) > 300 * 1024 * 1024:
                    raise FidelityError("Expanded workbook exceeds 300 MB experiment limit")
                if archive.testzip() is not None:
                    raise FidelityError("ZIP CRC validation failed")
                self.infos = infos
                self.parts = {i.filename: archive.read(i) for i in infos}
                self.zip_comment = archive.comment
        except (BadZipFile, NotImplementedError, RuntimeError) as exc:
            raise CorruptPartError(f"invalid workbook archive: {exc}") from exc
        if "xl/workbook.xml" not in self.parts:
            raise CorruptPartError("missing xl/workbook.xml")
        self.shared = []
        if "xl/sharedStrings.xml" in self.parts:
            self.shared = [text_content(e) for e in xml(self.parts["xl/sharedStrings.xml"])]
        workbook = xml(self.parts["xl/workbook.xml"])
        self.sheets = []
        relationships = self.relationships("xl/workbook.xml")
        for element in workbook.findall("s:sheets/s:sheet", NS):
            rid = element.get(f"{{{R}}}id")
            if not rid or rid not in relationships:
                raise CorruptPartError(f"missing worksheet relationship: {rid!r}")
            relation = relationships[rid]
            if relation.get("TargetMode") == "External":
                raise FidelityError("External worksheet relationship")
            target_ref = relation.get("Target")
            if not target_ref:
                raise CorruptPartError(f"worksheet relationship {rid!r} is missing Target")
            part = resolve_part("xl/workbook.xml", target_ref)
            if part not in self.parts:
                raise CorruptPartError(f"missing worksheet part: {part}")
            root = xml(self.parts[part])
            cells = {}
            for c in root.findall("s:sheetData/s:row/s:c", NS):
                ref = c.get("r")
                if not ref:
                    raise CorruptPartError(f"cell missing r attribute in {part}")
                formula = c.find("s:f", NS)
                value = c.findtext("s:v", "", NS)
                kind = c.get("t", "n")
                if kind == "s":
                    if value:
                        try:
                            index = int(value)
                        except ValueError as exc:
                            raise CorruptPartError(f"invalid shared string index in {part} {ref}: {value!r}") from exc
                        if not 0 <= index < len(self.shared):
                            raise CorruptPartError(f"shared string index out of range in {part} {ref}: {index}")
                        value = self.shared[index]
                    else:
                        value = ""
                elif kind == "inlineStr":
                    value = text_content(c.find("s:is", NS))
                cells[ref] = {
                    "value": value, "type": kind, "style": c.get("s"),
                    "formula": None if formula is None else formula.text or "",
                    "formula_attributes": {} if formula is None else dict(formula.attrib),
                }
            name_attr = element.get("name")
            if name_attr is None:
                raise CorruptPartError("worksheet element is missing name attribute")
            self.sheets.append({
                "name": name_attr, "part": part, "root": root,
                "cells": cells, "state": element.get("state", "visible"),
            })

    def relationships(self, part: str) -> dict[str, dict[str, str]]:
        data = self.parts.get(relationship_part(part))
        if not data:
            return {}
        result = {}
        for r in xml(data):
            rid = r.get("Id")
            if not rid:
                raise CorruptPartError(f"relationship entry in {relationship_part(part)} is missing Id")
            result[rid] = dict(r.attrib)
        return result

    def sheet(self, name: str) -> dict:
        matches = [s for s in self.sheets if s["name"] == name]
        if len(matches) != 1:
            raise FidelityError(f"Expected exactly one worksheet named {name}")
        return matches[0]

    def media_check(self, part: str | None, external: bool = False) -> dict:
        if external:
            return {"status": "external_unsupported"}
        if not part or not self.parts.get(part):
            return {"status": "missing_resource"}
        if part in self._media_checks:
            return dict(self._media_checks[part])
        media = self.parts[part]
        result = {"status": "resource_found", "media_sha256": sha256(media)}
        try:
            from PIL import Image
        except ImportError:
            result["decode_status"] = "not_run_Pillow_unavailable"
        else:
            try:
                with Image.open(io.BytesIO(media)) as image:
                    result["size"] = list(image.size)
                    result["format"] = image.format
                    image.verify()
                # verify() may only inspect a container; force pixel decoding.
                with Image.open(io.BytesIO(media)) as image:
                    image.load()
                result["decode_status"] = "passed"
            except Exception as exc:
                result["status"] = "corrupt_resource"
                result["decode_status"] = type(exc).__name__
        self._media_checks[part] = result
        return dict(result)

    def image_records(self) -> list[dict]:
        from .references import effective_formulas
        definitions: dict[str, list[dict]] = {}
        for part, data in self.parts.items():
            if not re.search(r"(?:^|/)cellimages[^/]*\.xml$", part, re.I):
                continue
            rels = self.relationships(part)
            for picture in xml(data).iter():
                if picture.tag.rsplit("}", 1)[-1] != "pic":
                    continue
                props = next((e for e in picture.iter() if e.tag.endswith("}cNvPr")), None)
                blip = next((e for e in picture.iter() if e.tag.endswith("}blip")), None)
                if props is None or blip is None:
                    continue
                image_id = props.get("name", "")
                rid = blip.get(f"{{{R}}}embed", "")
                rel = rels.get(rid, {})
                external = rel.get("TargetMode") == "External"
                target = None if external else (resolve_part(part, rel["Target"]) if rel.get("Target") else None)
                definitions.setdefault(image_id, []).append({
                    "definition_part": part, "relationship_id": rid,
                    "media_part": target, "external": external,
                })
        def resolve_image(image_id):
            candidates = definitions.get(image_id, [])
            result = {"image_id": image_id}
            if len(candidates) != 1:
                result["status"] = "missing_definition" if not candidates else "ambiguous_definition"
            else:
                result.update(candidates[0])
                result.update(self.media_check(result["media_part"], result["external"]))
            return result

        records = []
        call = r'(?:_xlfn\.)?DISPIMG\s*\(\s*"([^"\r\n]+)"\s*,\s*[0-9]+\s*\)'
        for sheet in self.sheets:
            formulas = effective_formulas(sheet)
            for ref, cell in sheet["cells"].items():
                formula = formulas.get(ref, "")
                if "DISPIMG" not in formula.upper():
                    continue
                match = re.fullmatch(call, formula.strip().removeprefix("="), re.I)
                record = {"sheet": sheet["name"], "cell": ref}
                if match:
                    record.update(resolve_image(match[1]))
                    record["formula_status"] = "direct_image_call"
                else:
                    record.update({"image_id": None, "status": "requires_review",
                                   "formula_status": "expression_display_not_evaluated",
                                   "decode_status": "display_not_evaluated",
                                   "reason": "conditional_or_dynamic_image; verify in target client",
                                   "candidates": [resolve_image(m[1]) for m in re.finditer(call, formula, re.I)]})
                records.append(record)
        return records

    def inventory(self) -> dict:
        return {
            "sha256": self.source_hash,
            "parts": len(self.parts),
            "sheets": [{
                "name": s["name"], "part": s["part"], "state": s["state"],
                "dimension": (s["root"].find("s:dimension", NS).get("ref")
                              if s["root"].find("s:dimension", NS) is not None else None),
                "populated_cells": sum(bool(c["value"] or c["formula"]) for c in s["cells"].values()),
                "formulas": sum(c["formula"] is not None for c in s["cells"].values()),
                "merges": [e.get("ref") for e in s["root"].findall("s:mergeCells/s:mergeCell", NS)],
                "headers_preview": [{"cell": ref, "value": c["value"]}
                                    for ref, c in s["cells"].items()
                                    if cell_position(ref)[1] <= 5 and c["value"]],
            } for s in self.sheets],
            "images": self.image_records(),
            "floating_objects": self.floating_records(),
            "feature_parts": [p for p in self.parts if any(
                marker in p.lower() for marker in ("drawing", "comment", "vml", "calcchain", "table", "external", "pivot")
            )],
        }

    def floating_records(self) -> list[dict]:
        records = []
        for sheet in self.sheets:
            for relation in self.relationships(sheet["part"]).values():
                if not relation.get("Type", "").endswith("/drawing"):
                    continue
                if relation.get("TargetMode") == "External":
                    continue
                target_ref = relation.get("Target")
                if not target_ref:
                    raise CorruptPartError(f"drawing relationship in {sheet['part']} is missing Target")
                part = resolve_part(sheet["part"], target_ref)
                if part not in self.parts:
                    raise CorruptPartError(f"missing drawing part: {part}")
                relationships = self.relationships(part)
                for index, anchor in enumerate(xml(self.parts[part])):
                    record = {"sheet": sheet["name"], "part": part, "index": index,
                              "anchor_type": anchor.tag.rsplit("}", 1)[-1], "markers": {}, "images": []}
                    for marker in anchor:
                        local = marker.tag.rsplit("}", 1)[-1]
                        if local in ("from", "to"):
                            coords = {}
                            for e in marker:
                                raw_coord = (e.text or "").strip()
                                try:
                                    coords[e.tag.rsplit("}", 1)[-1]] = int(raw_coord)
                                except ValueError as exc:
                                    raise CorruptPartError(f"invalid drawing coordinate in {part}: {e.text!r}") from exc
                            record["markers"][local] = coords
                    for element in anchor.iter():
                        if not element.tag.endswith("}blip"):
                            continue
                        rid = element.get(f"{{{R}}}embed", "")
                        rel = relationships.get(rid, {})
                        external = rel.get("TargetMode") == "External"
                        target = None if external else (resolve_part(part, rel["Target"]) if rel.get("Target") else None)
                        record["images"].append({"relationship_id": rid, "media_part": target,
                                                  **self.media_check(target, external)})
                    records.append(record)
        return records
