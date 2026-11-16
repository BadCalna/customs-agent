"""python -m experiments.excel_fidelity.cli INPUT --output-dir LOCAL_DIR."""

import argparse
import json
from pathlib import Path

from .conclusion import append_precheck_column
from .compat_images import export_excel2016_images
from .insert import insert_columns
from .mapping import map_workbook
from .workbook import FidelityError, Workbook


def run(source: Path, output_dir: Path, sample_id: str, inspect_only: bool = False,
        add_precheck: bool = False, excel2016_images: bool = False) -> dict:
    if sum((bool(inspect_only), bool(add_precheck), bool(excel2016_images))) > 1:
        raise FidelityError('Choose only one of inspection, conclusion column or Excel 2016 image conversion')
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
        suffix = "-Excel2016图片兼容版.xlsx" if excel2016_images else "-含预审结论.xlsx" if add_precheck else "-插列验证.xlsx"
        destination = output_dir / (sample_id + suffix)
        writer = export_excel2016_images if excel2016_images else append_precheck_column if add_precheck else insert_columns
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
    parser.add_argument("--excel2016-images", action="store_true", help="Create a static standard-picture copy for Excel 2016; preserve the original WPS file")
    args = parser.parse_args()
    try:
        report = run(args.source, args.output_dir, args.sample_id, args.inspect_only, args.add_precheck, args.excel2016_images)
    except (FidelityError, OSError, KeyError) as exc:
        parser.exit(2, f"P03 failed: {exc}\n")
    print(json.dumps({"sample_id": report["sample_id"],
                      "packing_rows": len(report["mapping"]["packing"]["records"]),
                      "declaration_rows": len(report["mapping"]["declaration"]["records"]),
                      "status": report.get("audit", {}).get("status", "inspected"),
                      "report_dir": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
