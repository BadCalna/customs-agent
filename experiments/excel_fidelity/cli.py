"""python -m experiments.excel_fidelity.cli INPUT --output-dir LOCAL_DIR."""

import argparse
import json
from pathlib import Path

from .conclusion import append_precheck_column
from .insert import insert_columns
from .mapping import map_workbook
from .workbook import FidelityError, Workbook


def run(source: Path, output_dir: Path, sample_id: str, inspect_only: bool = False,
        add_precheck: bool = False) -> dict:
    if inspect_only and add_precheck:
        raise FidelityError('Choose inspection or adding a conclusion column')
    if not sample_id or not all(c.isascii() and (c.isalnum() or c in "-_") for c in sample_id):
        raise FidelityError("Sample ID must be an ASCII file label")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / (sample_id + "-report.json")
    if report_file.exists():
        raise FidelityError("Use a new output directory; existing reports are not overwritten")
    workbook = Workbook(source)
    mapping = map_workbook(workbook)
    report = {"sample_id": sample_id, "source_path": str(source.resolve()),
              "inventory": workbook.inventory(), "mapping": mapping,
              "historical_values": "preserved; no classification or AI precheck executed"}
    if not inspect_only:
        destination = output_dir / (sample_id + ("-含预审结论.xlsx" if add_precheck else "-插列验证.xlsx"))
        writer = append_precheck_column if add_precheck else insert_columns
        report["audit"] = writer(source, destination, mapping["declaration"])
        report["output_path"] = str(destination.resolve())
    with report_file.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description="P03 local-only mapping/insertion experiment")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Local ignored directory: reports contain original business data")
    parser.add_argument("--sample-id", default="S-P03-01")
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--add-precheck", action="store_true", help="Append a missing conclusion column to an existing template copy")
    args = parser.parse_args()
    try:
        report = run(args.source, args.output_dir, args.sample_id, args.inspect_only, args.add_precheck)
    except (FidelityError, OSError, KeyError) as exc:
        parser.exit(2, f"P03 failed: {exc}\n")
    print(json.dumps({"sample_id": report["sample_id"],
                      "packing_rows": len(report["mapping"]["packing"]["records"]),
                      "declaration_rows": len(report["mapping"]["declaration"]["records"]),
                      "status": report.get("audit", {}).get("status", "inspected"),
                      "report_dir": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
