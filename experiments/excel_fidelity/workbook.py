"""Read OOXML without re-saving through a library that can drop WPS parts."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"s": S, "r": R}


class FidelityError(ValueError):
    """An unsupported or ambiguous input must not be silently rewritten."""


def xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise FidelityError("DTD/entity XML is unsupported")
    return ET.fromstring(data)


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
        self.source_hash = sha256(self.path.read_bytes())
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
        self.shared = []
        if "xl/sharedStrings.xml" in self.parts:
            self.shared = [text_content(e) for e in xml(self.parts["xl/sharedStrings.xml"])]
        workbook = xml(self.parts["xl/workbook.xml"])
        self.sheets = []
        relationships = self.relationships("xl/workbook.xml")
        for element in workbook.findall("s:sheets/s:sheet", NS):
            relation = relationships[element.attrib[f"{{{R}}}id"]]
            if relation.get("TargetMode") == "External":
                raise FidelityError("External worksheet relationship")
            part = resolve_part("xl/workbook.xml", relation["Target"])
            root = xml(self.parts[part])
            cells = {}
            for c in root.findall("s:sheetData/s:row/s:c", NS):
                ref = c.attrib["r"]
                formula = c.find("s:f", NS)
                value = c.findtext("s:v", "", NS)
                kind = c.get("t", "n")
                if kind == "s":
                    value = self.shared[int(value)] if value else ""
                elif kind == "inlineStr":
                    value = text_content(c.find("s:is", NS))
                cells[ref] = {
                    "value": value, "type": kind, "style": c.get("s"),
                    "formula": None if formula is None else formula.text or "",
                    "formula_attributes": {} if formula is None else dict(formula.attrib),
                }
            self.sheets.append({
                "name": element.attrib["name"], "part": part, "root": root,
                "cells": cells, "state": element.get("state", "visible"),
            })

    def relationships(self, part: str) -> dict[str, dict[str, str]]:
        data = self.parts.get(relationship_part(part))
        if not data:
            return {}
        return {r.attrib["Id"]: dict(r.attrib) for r in xml(data)}

    def sheet(self, name: str) -> dict:
        matches = [s for s in self.sheets if s["name"] == name]
        if len(matches) != 1:
            raise FidelityError(f"Expected exactly one worksheet named {name}")
        return matches[0]

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
                target = resolve_part(part, rel["Target"]) if rel.get("Target") else None
                definitions.setdefault(image_id, []).append({
                    "definition_part": part, "relationship_id": rid,
                    "media_part": target, "external": rel.get("TargetMode") == "External",
                })
        records = []
        for sheet in self.sheets:
            formulas = effective_formulas(sheet)
            for ref, cell in sheet["cells"].items():
                formula = formulas.get(ref, "")
                if "DISPIMG" not in formula.upper():
                    continue
                match = re.search(r'\bDISPIMG\s*\(\s*"([^"\r\n]+)"', formula, re.I)
                image_id = match[1] if match else None
                candidates = definitions.get(image_id, [])
                record = {"sheet": sheet["name"], "cell": ref, "image_id": image_id}
                if not match:
                    record["status"] = "invalid_formula"
                elif len(candidates) != 1:
                    record["status"] = "missing_definition" if not candidates else "ambiguous_definition"
                else:
                    record.update(candidates[0])
                    media = self.parts.get(record["media_part"])
                    if record["external"]:
                        record["status"] = "external_unsupported"
                    elif not media:
                        record["status"] = "missing_resource"
                    else:
                        record["media_sha256"] = sha256(media)
                        record["status"] = "resource_found"
                        try:
                            from PIL import Image
                        except ImportError:
                            record["decode_status"] = "not_run_Pillow_unavailable"
                        else:
                            try:
                                with Image.open(io.BytesIO(media)) as image:
                                    record["size"] = list(image.size)
                                    record["format"] = image.format
                                    image.verify()
                                record["decode_status"] = "passed"
                            except Exception as exc:
                                record["status"] = "corrupt_resource"
                                record["decode_status"] = type(exc).__name__
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
                if not relation["Type"].endswith("/drawing"):
                    continue
                part = resolve_part(sheet["part"], relation["Target"])
                relationships = self.relationships(part)
                for index, anchor in enumerate(xml(self.parts[part])):
                    record = {"sheet": sheet["name"], "part": part, "index": index,
                              "anchor_type": anchor.tag.rsplit("}", 1)[-1], "markers": {}, "images": []}
                    for marker in anchor:
                        local = marker.tag.rsplit("}", 1)[-1]
                        if local in ("from", "to"):
                            record["markers"][local] = {e.tag.rsplit("}", 1)[-1]: int(e.text)
                                                        for e in marker}
                    for element in anchor.iter():
                        if not element.tag.endswith("}blip"):
                            continue
                        rid = element.get(f"{{{R}}}embed", "")
                        rel = relationships.get(rid, {})
                        target = resolve_part(part, rel["Target"]) if rel.get("Target") else None
                        media = self.parts.get(target)
                        record["images"].append({"relationship_id": rid, "media_part": target,
                                                  "status": "resource_found" if media else "missing_resource",
                                                  "media_sha256": sha256(media) if media else None})
                    records.append(record)
        return records
