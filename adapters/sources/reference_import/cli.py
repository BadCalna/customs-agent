"""P23 导入流水线 CLI：盘点 → 导入 → 验证 → 发布 → 回滚。

命令与退出码
------------
- ``scan``：只读盘点原件（哈希、页签、表头识别、行数、数据类别声明），不写业务数据；
- ``import``：生成候选快照 + 导入报告 + 查询验证；
- ``verify``：对已存在的候选快照重跑查询验证；
- ``publish`` / ``releases`` / ``activate``：显式发布与版本回退；
- 退出码：``0`` 成功、``1`` 部分拒绝（有拒绝/冲突/缺失输入/验证未通过）、``2`` 失败。

默认输出根目录是 ``.local/reference-import``（Git 忽略）：真实数据、原件副本、
报告与发布物都不进公开仓库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from adapters.sources.reference_import.convert_catalog import (
    element_layout,
    index_layout,
)
from adapters.sources.reference_import.convert_tax import tax_layout
from adapters.sources.reference_import.model import (
    CONVERTER_VERSION,
    DatasetId,
    ExitCode,
    render_counts_line,
    utc_now,
    write_json_atomic,
)
from adapters.sources.reference_import.publish import (
    PublishError,
    PublishRequest,
    activate_release,
    dataset_paths,
    list_releases,
    publish_release,
)
from adapters.sources.reference_import.snapshot import (
    DEFAULT_OUT_ROOT,
    SnapshotFailed,
    SnapshotRequest,
    build_snapshot,
)
from adapters.sources.reference_import.sources import (
    LayoutError,
    SourceError,
    load_source_manifest,
    open_workbook,
    locate_dataset_inputs,
)


def configure_django() -> None:
    """适配器通过 Django Ninja 定义 DTO；CLI 在导入前显式配置 settings。"""

    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    try:
        django.setup()
    except RuntimeError:  # 已在测试/宿主进程中配置过 apps
        pass


def _print(message: str = "", *, file=None) -> None:
    print(message, file=file)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _scan_workbook(source) -> dict[str, Any]:
    access = open_workbook(source)
    info: dict[str, Any] = {
        "source_id": source.source_id,
        "kind": source.kind.value,
        "path": str(source.path),
        "sha256": source.sha256,
        "bytes": source.size,
        "hash_verified": source.hash_verified,
        "sheets": [],
        "layout": {},
        "error": None,
    }
    for sheet in access.sheets:
        rows = access.row_numbers(sheet)
        info["sheets"].append(
            {
                "name": sheet.name,
                "visible": sheet.visible,
                "rows_with_content": len(rows),
                "first_row": rows[0] if rows else None,
                "last_row": rows[-1] if rows else None,
            }
        )
    for label, locator in (
        ("hs_index", index_layout),
        ("element_detail", element_layout),
        ("tax_rate", tax_layout),
    ):
        try:
            layout = locator(access)
        except LayoutError as exc:
            info["layout"][label] = {"detected": False, "reason": str(exc)}
            continue
        info["layout"][label] = {
            "detected": True,
            "sheet": layout.sheet,
            "header_row": layout.header_row,
            "columns": {
                name: _column_letter(column) for name, column in layout.columns.items()
            },
            "extra_columns": sorted(layout.extras),
        }
    return info


def _column_letter(column: int) -> str:
    from adapters.files.ooxml import col_label

    return col_label(column)


def _scan_commentary(source) -> dict[str, Any]:
    info: dict[str, Any] = {
        "source_id": source.source_id,
        "kind": source.kind.value,
        "path": str(source.path),
        "sha256": source.sha256,
        "bytes": source.size,
        "hash_verified": source.hash_verified,
        "error": None,
    }
    try:
        payload = json.loads(source.path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    items = payload.get("items") if isinstance(payload, Mapping) else None
    info["top_level_keys"] = sorted(payload) if isinstance(payload, Mapping) else []
    info["declared_total"] = payload.get("total") if isinstance(payload, Mapping) else None
    info["item_count"] = len(items) if isinstance(items, list) else 0
    kinds: dict[str, int] = {}
    section_labels: dict[str, int] = {}
    tariff_no_lengths: dict[int, int] = {}
    has_sections = 0
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind") or "")
            kinds[kind] = kinds.get(kind, 0) + 1
            tariff_no = str(item.get("tariff_no") or "")
            tariff_no_lengths[len(tariff_no)] = tariff_no_lengths.get(len(tariff_no), 0) + 1
            detail = item.get("detail") or {}
            sections = detail.get("sections") if isinstance(detail, Mapping) else None
            if isinstance(sections, list) and sections:
                has_sections += 1
                for section in sections:
                    label = str(section.get("label") or "")
                    section_labels[label] = section_labels.get(label, 0) + 1
    info["kinds"] = kinds
    info["section_labels"] = section_labels
    info["tariff_no_lengths"] = {str(key): value for key, value in sorted(tariff_no_lengths.items())}
    info["items_with_sections"] = has_sections
    info["fetched_at"] = payload.get("fetched_at") if isinstance(payload, Mapping) else None
    return info


def command_scan(args: argparse.Namespace) -> ExitCode:
    manifest = load_source_manifest(Path(args.sources), kind=args.dataset_kind)
    report: dict[str, Any] = {
        "generated_at": utc_now(),
        "batch_id": manifest.batch_id,
        "dataset_kind": manifest.kind.value,
        "converter_version": CONVERTER_VERSION,
        "manifest": manifest.manifest_path.name,
        "declared_datasets": [item.as_dict() for item in manifest.declared],
        "sources": [],
        "unresolved_inputs": [item.as_dict() for item in manifest.unresolved],
        "notes": manifest.notes,
    }
    for source in manifest.sources:
        if source.kind.value.startswith("commentary_json"):
            report["sources"].append(_scan_commentary(source))
        else:
            try:
                report["sources"].append(_scan_workbook(source))
            except (SourceError, LayoutError) as exc:
                report["sources"].append(
                    {
                        "source_id": source.source_id,
                        "kind": source.kind.value,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    report["dataset_inputs"] = {}
    for dataset in DatasetId:
        available, missing = locate_dataset_inputs(manifest, dataset)
        report["dataset_inputs"][dataset.value] = {
            "available": [item.source_id for item in available],
            "missing": missing,
        }
    if args.out:
        write_json_atomic(Path(args.out), report)
        _print(f"盘点报告：{args.out}")
    _print(f"批次：{report['batch_id']}（{report['dataset_kind']}）")
    _print(f"数据类别声明：")
    for item in report["declared_datasets"]:
        reason = f" — {item['reason']}" if item.get("reason") else ""
        _print(f"  - {item['dataset']}: {item['status']}{reason}")
    _print("原件：")
    for item in report["sources"]:
        if item.get("error"):
            _print(f"  - {item['source_id']}: 无法读取（{item['error']}）")
            continue
        _print(
            f"  - {item['source_id']} {item['kind']} sha256={item['sha256'][:16]}… "
            f"bytes={item['bytes']} 哈希一致={'是' if item['hash_verified'] else '否'}"
        )
        if "sheets" in item:
            for sheet in item["sheets"]:
                _print(
                    f"      页签 {sheet['name']!r}：行 {sheet['first_row']}–{sheet['last_row']}"
                )
            for label, layout in item["layout"].items():
                if layout.get("detected"):
                    _print(
                        f"      {label}: 页签 {layout['sheet']!r} 表头行 {layout['header_row']} "
                        f"列 {layout['columns']}"
                    )
                else:
                    _print(f"      {label}: 未识别（{layout.get('reason')}）")
        if "item_count" in item:
            _print(
                f"      条目 {item['item_count']}（声明 {item['declared_total']}）；"
                f"类型 {item['kinds']}；分段 {item['section_labels']}"
            )
    for item in report["unresolved_inputs"]:
        _print(f"  - 取不到：{item['source_id']}（{item['reason']}）")
    has_error = any(item.get("error") for item in report["sources"])
    return ExitCode.PARTIAL if has_error else ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# import / verify
# ---------------------------------------------------------------------------


def command_import(args: argparse.Namespace) -> ExitCode:
    from adapters.sources.reference_import.query_check import run_query_checks

    request = SnapshotRequest(
        manifest_path=Path(args.sources),
        out_root=Path(args.out_root),
        dataset_kind=args.dataset_kind,
        acquisition_date=args.acquisition_date,
    )
    build = build_snapshot(request, verify_hook=run_query_checks)
    report_path = build.report_path
    _print(f"快照：{build.snapshot_dir}")
    _print(f"报告：{report_path}")
    _print(f"业务指纹：{build.report.get('business_fingerprint')}")
    if build.reused:
        _print("本次输入与既有快照一致：复用既有快照，未产生重复数据")
    _print("数据类别：")
    for item in build.report.get("datasets", []):
        counts = item["counts"]
        _print(
            f"  - {item['dataset']}: {item['status']} / 单位 {item.get('input_unit') or '-'} / "
            f"输入 {counts['input_total']} 导入 {counts['imported']} 重复 {counts['duplicate']} "
            f"冲突 {counts['conflict']} 待核 {counts['pending']} 拒绝 {counts['rejected']} "
            f"未导入 {counts['not_imported']} 输出 {counts['output_records']}"
        )
    totals = build.report["totals"]
    _print(f"汇总：{render_counts_line(totals)}")
    if totals.get("pending"):
        _print(
            f"注意：{totals['pending']} 条记录保留但待核（来源未声明/未匹配/需人工确认），"
            "这些记录不构成可用结论；逐条原因见报告。"
        )
    verification = build.report.get("verification") or {}
    if verification:
        counts = verification.get("counts", {})
        _print(
            f"查询验证：通过 {counts.get('pass', 0)} / 失败 {counts.get('fail', 0)} / "
            f"跳过 {counts.get('skip', 0)}"
        )
        for item in verification.get("checks", []):
            if item["status"] == "fail":
                _print(f"  ! {item['id']}: {item['detail']}")
    _print(f"结果：{build.report.get('status')}（退出码 {int(build.exit_code)}）")
    return build.exit_code


def command_verify(args: argparse.Namespace) -> ExitCode:
    from adapters.sources.reference_import.query_check import run_query_checks

    snapshot_dir = Path(args.snapshot)
    if not snapshot_dir.is_dir():
        _print(f"快照目录不存在：{snapshot_dir}", file=sys.stderr)
        return ExitCode.FAILED
    verification = run_query_checks(snapshot_dir)
    out = Path(args.out) if args.out else Path(args.out_root) / "verification" / f"{snapshot_dir.name}.json"
    write_json_atomic(out, verification)
    _print(f"验证报告：{out}")
    for item in verification["checks"]:
        _print(f"  [{item['status']}] {item['id']}: {item['detail']}")
    counts = verification["counts"]
    _print(
        f"通过 {counts['pass']} / 失败 {counts['fail']} / 跳过 {counts['skip']}"
    )
    return ExitCode.SUCCESS if verification["all_passed"] else ExitCode.PARTIAL


# ---------------------------------------------------------------------------
# publish / releases / activate
# ---------------------------------------------------------------------------


def command_publish(args: argparse.Namespace) -> ExitCode:
    try:
        result = publish_release(
            PublishRequest(
                snapshot_dir=Path(args.snapshot),
                out_root=Path(args.out_root),
                review_path=Path(args.review) if args.review else None,
                allow_content_change=args.allow_content_change,
                reason=args.reason,
                activate=not args.no_activate,
            )
        )
    except PublishError as exc:
        _print(f"发布被拒绝：{exc}", file=sys.stderr)
        return ExitCode.FAILED
    release = result.release
    _print(f"release：{result.release_id}")
    _print(f"目录：{result.release_dir}")
    _print(f"发布指纹：{release['release_fingerprint']}")
    _print(f"复核状态：{release['review_status']}")
    if result.activated:
        _print("已切换 current 指针")
        if result.activation:
            event = result.activation
            _print(f"本次激活事件：{event['event_id']}（前驱 {event['previous_release'] or '无'}）")
            _print(f"本次换版理由：{event['reason'] or '未提供（无内容换版）'}")
    if result.changed:
        _print(f"内容相对上一版有变化：{json.dumps(result.diff, ensure_ascii=False)}")
    _print("数据文件：")
    for name, info in sorted((release.get("files") or {}).items()):
        _print(f"  - {name}: {result.release_dir / info['file']}（复核 {info['review_status']}）")
    warning = (result.activation_result or {}).get("audit_warning")
    if warning:
        _print(f"审计警告：{warning}", file=sys.stderr)
        return ExitCode.PARTIAL
    return ExitCode.SUCCESS


def command_releases(args: argparse.Namespace) -> ExitCode:
    items = list_releases(Path(args.out_root))
    if not items:
        _print("还没有发布任何 release")
        return ExitCode.SUCCESS
    for item in items:
        marker = "*" if item["active"] else " "
        _print(
            f"{marker} {item['release_id']} 复核={item['review_status']} "
            f"发布={item['published_at']} 数据类别={','.join(item['datasets'])}"
        )
    return ExitCode.SUCCESS


def command_activate(args: argparse.Namespace) -> ExitCode:
    try:
        current = activate_release(Path(args.out_root), args.release_id)
    except PublishError as exc:
        _print(f"切换失败：{exc}", file=sys.stderr)
        return ExitCode.FAILED
    _print(f"当前 release：{current['release_id']}")
    _print(f"本次激活事件：{current['activation_event_id']}")
    if current.get("audit_warning"):
        _print(f"审计警告：{current['audit_warning']}", file=sys.stderr)
        return ExitCode.PARTIAL
    return ExitCode.SUCCESS


def command_paths(args: argparse.Namespace) -> ExitCode:
    try:
        paths = dataset_paths(Path(args.out_root), release_id=args.release_id)
    except PublishError as exc:
        _print(f"取路径失败：{exc}", file=sys.stderr)
        return ExitCode.FAILED
    for name, path in sorted(paths.items()):
        _print(f"{name}={path}")
    return ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import-reference-data",
        description=(
            "官方参考数据导入流水线（P23）：盘点 → 导入 → 验证 → 发布 → 回滚。"
            "不写业务数据库、不改 .env、不联网。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="只读盘点原件：哈希、页签、表头、行数与数据类别声明")
    scan.add_argument("--sources", required=True, help="来源清单 JSON")
    scan.add_argument("--dataset-kind", choices=("real", "synthetic"), default=None)
    scan.add_argument("--out", help="盘点报告输出路径（JSON）")
    scan.set_defaults(func=command_scan)

    imp = sub.add_parser("import", help="生成候选快照、导入报告与查询验证")
    imp.add_argument("--sources", required=True, help="来源清单 JSON")
    imp.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT), help="输出根目录（默认 .local/reference-import）")
    imp.add_argument("--dataset-kind", choices=("real", "synthetic"), default=None)
    imp.add_argument(
        "--acquisition-date",
        help="来源取得/核对日期（ISO），用于判断退税率记录是否仍在窗口内；默认取清单 prepared_at",
    )
    imp.set_defaults(func=command_import)

    verify = sub.add_parser("verify", help="对已存在的候选快照重跑查询验证")
    verify.add_argument("--snapshot", required=True, help="候选快照目录")
    verify.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    verify.add_argument("--out", help="验证报告输出路径")
    verify.set_defaults(func=command_verify)

    publish = sub.add_parser("publish", help="把候选快照发布为不可变 release（不自动合并、不改 .env）")
    publish.add_argument("--snapshot", required=True, help="候选快照目录")
    publish.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    publish.add_argument("--review", help="人工复核记录 JSON；不提供则发布为未复核数据")
    publish.add_argument(
        "--allow-content-change",
        action="store_true",
        help="允许用新内容替换已发布内容（默认拒绝，防止静默覆盖）",
    )
    publish.add_argument("--reason", default="", help="内容变化的理由，写入 release.json")
    publish.add_argument("--no-activate", action="store_true", help="只生成 release，不切换 current")
    publish.set_defaults(func=command_publish)

    releases = sub.add_parser("releases", help="列出已发布的 release 与当前指针")
    releases.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    releases.set_defaults(func=command_releases)

    activate = sub.add_parser("activate", help="把 current 指针切回指定 release（回滚）")
    activate.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    activate.add_argument("--release-id", required=True)
    activate.set_defaults(func=command_activate)

    paths = sub.add_parser("paths", help="打印当前 release 各数据类别的文件路径（供环境变量使用）")
    paths.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    paths.add_argument("--release-id")
    paths.set_defaults(func=command_paths)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_django()
    try:
        return int(args.func(args))
    except (SourceError, LayoutError, SnapshotFailed) as exc:
        _print(f"失败：{exc}", file=sys.stderr)
        return int(ExitCode.FAILED)
    except (ValueError, FileNotFoundError) as exc:
        _print(f"失败：{exc}", file=sys.stderr)
        return int(ExitCode.FAILED)


if __name__ == "__main__":  # pragma: no cover - 模块 CLI 入口
    raise SystemExit(main())
