"""Conservative A1-reference handling for this experiment's supported formulas."""

import re

from .workbook import FidelityError, cell_position, col_label, col_number

TOKEN = re.compile(
    r'"(?:[^"]|"")*"|'
    r"(?<![\w.$])(?:(?P<sheet>'(?:[^']|'')+'|[\w\u0080-\uffff]+)!)?"
    r"(?P<ref>\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?"
    r"|\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}|\$?[1-9][0-9]*:\$?[1-9][0-9]*)"
    r"(?![\w.(])"
)


def shift_range(value: str, start: int, count: int = 2) -> str:
    def shift(match):
        number = col_number(match[2])
        # Whole-row selections/formula ranges end at Excel's fixed boundary.
        # Their right endpoint stays XFD; an actual cell at XFD still refuses
        # overflow because its address contains no range separator.
        if number == 16384 and ":" in value and match.end() == len(value.rstrip("0123456789$")):
            return match[1] + "XFD"
        return match[1] + col_label(number + count if number >= start else number)
    return re.sub(r"(\$?)([A-Za-z]{1,3})(?=\$?[0-9]|:|$)", shift, value)


def transform_formula(formula: str, context: str, target: str, start: int, count: int = 2) -> str:
    # Strings, INDIRECT, structured refs, external refs and 3-D refs are not
    # guessed. Static A1 expressions are sufficient for the supplied templates.
    outside_strings = re.sub(r'"(?:[^"]|"")*"', '""', formula)
    if re.search(r"\b(?:INDIRECT|OFFSET)\s*\(|\[|\]|(?:'[^']*:[^']*'|\w+:\w+)!", outside_strings, re.I):
        raise FidelityError("Dynamic/external/structured/3-D formula requires a native editor")

    def replace(match):
        if match[0].startswith('"'):
            return match[0]
        sheet = match["sheet"]
        sheet_name = sheet[1:-1].replace("''", "'") if sheet and sheet.startswith("'") else sheet
        if (sheet_name or context) != target:
            return match[0]
        try:
            for label in re.findall(r"[A-Za-z]+", match["ref"]):
                if col_number(label) > 16384:
                    return match[0]
            result = shift_range(match["ref"], start, count)
        except FidelityError:
            raise
        return ((sheet + "!") if sheet else "") + result

    return TOKEN.sub(replace, formula)


def relative_formula(formula: str, origin: str, destination: str) -> str:
    old_col, old_row = cell_position(origin)
    new_col, new_row = cell_position(destination)

    def replace(match):
        if match[0].startswith('"'):
            return match[0]

        def move(endpoint):
            cell = re.fullmatch(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)", endpoint)
            if not cell:
                raise FidelityError("Shared whole-column/row formula requires native editor")
            column = col_number(cell[2]) + (0 if cell[1] else new_col - old_col)
            row = int(cell[4]) + (0 if cell[3] else new_row - old_row)
            if not 1 <= row <= 1048576:
                raise FidelityError("Shared formula row outside XLSX limit")
            return f"{cell[1]}{col_label(column)}{cell[3]}{row}"

        ref = ":".join(move(part) for part in match["ref"].split(":"))
        return ((match["sheet"] + "!") if match["sheet"] else "") + ref

    return TOKEN.sub(replace, formula)


def effective_formulas(sheet: dict) -> dict[str, str]:
    cells = sheet["cells"]
    masters = {}
    for ref, cell in cells.items():
        attributes = cell["formula_attributes"]
        if attributes.get("t") == "shared" and cell["formula"]:
            key = attributes.get("si")
            if key in masters:
                raise FidelityError("Ambiguous shared formula master")
            masters[key] = (ref, cell["formula"])
    result = {}
    for ref, cell in cells.items():
        if cell["formula"] is None:
            continue
        attributes = cell["formula_attributes"]
        if attributes.get("t") == "shared":
            master = masters.get(attributes.get("si"))
            if master is None:
                raise FidelityError("Shared formula master missing")
            result[ref] = relative_formula(master[1], master[0], ref)
        else:
            result[ref] = cell["formula"]
    return result
