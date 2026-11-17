"""候选快照构建：稳定指纹、原子替换、可逐条对账的导入报告（P23）。

快照是**候选**（candidate），与正在使用的发布快照分开：

- 输出目录默认 ``.local/reference-import``（Git 忽略），不写业务数据库、不改 ``.env``；
- 目录名由业务内容指纹决定：输入相同 + 转换器版本相同 + 配置相同 → 同一快照，
  重复运行不会产生第二份“看起来不同”的数据；
- 先写临时目录再 ``os.replace`` 到最终位置：中途失败不会损坏已有快照；
- ``generated_at`` 只写进报告与快照索引，**不参与**业务指纹计算。

报告要求：总数、成功数、重复数、冲突数、待核数、拒绝数、未导入数与逐条原因必须
能互相对上（``input_total = imported + duplicate + conflict + pending + rejected
+ not_imported``，冲突记录另有 ``kept first`` 的一条 import 记录，见报告说明）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from adapters.sources.reference_import.convert_catalog import (
    CatalogBuild,
    Versioning,
    build_catalog,
)
from adapters.sources.reference_import.convert_commentary import (
    CommentaryBuild,
    build_commentary,
)
from adapters.sources.reference_import.convert_tax import TaxBuild, build_tax_rates
from adapters.sources.reference_import.model import (
    CONVERTER_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    DatasetId,
    DatasetKind,
    DatasetOutcome,
    DatasetStatus,
    ExitCode,
    RecordOutcome,
    RecordStatus,
    SourceFile,
    exit_code_for,
    fingerprint,
    local_now,
    read_json,
    render_counts_line,
    sha256_file,
    summarize_counts,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)
from adapters.sources.reference_import.sources import (
    SourceManifest,
    load_source_manifest,
    locate_dataset_inputs,
    verify_sources_unchanged,
)

#: 报告结构版本。报告是证据，结构变化时递增。
REPORT_SCHEMA_VERSION = "p23.import-report.1"

#: 默认输出根目录（Git 忽略）。
DEFAULT_OUT_ROOT = Path(".local/reference-import")


class SnapshotFailed(RuntimeError):
    """构建失败；此时不应留下半份快照，已有旧快照必须保持可用。"""


@dataclass(frozen=True)
class SnapshotRequest:
    manifest_path: Path
    out_root: Path = DEFAULT_OUT_ROOT
    dataset_kind: DatasetKind | None = None
    acquisition_date: str | None = None
    """来源取得/核对日期（本机时区），用于判断退税率记录是否仍在窗口内。"""


@dataclass
class SnapshotBuild:
    """构建结果：既是本次运行的报告，也是后续发布/验证的输入。"""

    snapshot_id: str
    snapshot_dir: Path
    report: dict[str, Any]
    reused: bool
    exit_code: ExitCode
    report_path: Path
    report_markdown_path: Path
    manifest: SourceManifest
    datasets: dict[DatasetId, DatasetOutcome] = field(default_factory=dict)
    outcomes: list[RecordOutcome] = field(default_factory=list)

    @property
    def payload_paths(self) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for dataset in self.datasets:
            candidate = self.snapshot_dir / dataset.filename
            if candidate.is_file():
                paths[dataset.value] = candidate
        return paths


def _declared_status(manifest: SourceManifest, dataset: DatasetId) -> tuple[DatasetStatus, str]:
    declared = manifest.dataset(dataset)
    if declared is None:
        return DatasetStatus.NOT_PROVIDED, "来源清单未声明该数据类别"
    return declared.status, declared.reason


def _dataset_placeholder(
    manifest: SourceManifest, dataset: DatasetId
) -> DatasetOutcome:
    status, reason = _declared_status(manifest, dataset)
    return DatasetOutcome(dataset=dataset, status=status, reason=reason)


def _business_fingerprint(datasets: Mapping[DatasetId, DatasetOutcome], payloads: Mapping[DatasetId, Any]) -> str:
    """业务指纹：只覆盖数据内容与结构，不含生成时间、路径与复核状态。"""

    material: dict[str, Any] = {}
    for dataset, payload in payloads.items():
        if payload is None:
            continue
        material[dataset.value] = payload
    return fingerprint(
        {
            "converter_version": CONVERTER_VERSION,
            "datasets": sorted(material),
            "payloads": material,
        }
    )


def _dataset_fingerprint(payload: Any) -> str:
    return fingerprint(payload) if payload is not None else ""


def input_fingerprint(manifest: SourceManifest, versioning: Any) -> str:
    """输入指纹：原件哈希 + 转换器版本 + 版本配置。

    转换是确定性的，所以输入指纹相同就意味着业务输出相同——重复运行可以在
    解析几万行之前直接复用既有快照，不必每次重算一遍。
    """

    return fingerprint(
        {
            "converter_version": CONVERTER_VERSION,
            "dataset_kind": DatasetKind(manifest.kind).value,
            "defaults": dict(sorted(manifest.defaults.items())),
            "declared": [item.as_dict() for item in manifest.declared],
            "sources": sorted(
                (item.source_id, item.sha256, item.kind.value) for item in manifest.sources
            ),
            "unresolved": [item.source_id for item in manifest.unresolved],
            "dataset_ids": sorted(
                versioning.dataset_id(dataset) for dataset in DatasetId
            ),
        }
    )


def _find_reusable_snapshot(out_root: Path, wanted: str) -> Path | None:
    candidates_root = out_root / "candidates"
    if not candidates_root.is_dir():
        return None
    for entry in sorted(candidates_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            payload = read_json(manifest_path)
        except (OSError, ValueError):
            continue
        if payload.get("input_fingerprint") == wanted:
            return entry
    return None


def _outcomes_from_report(report: Mapping[str, Any]) -> list[DatasetOutcome]:
    outcomes: list[DatasetOutcome] = []
    for item in report.get("datasets", []) or []:
        counts = item.get("counts") or {}
        outcomes.append(
            DatasetOutcome(
                dataset=DatasetId(item["dataset"]),
                status=DatasetStatus(item["status"]),
                rejected=int(counts.get("rejected", 0)),
                conflict=int(counts.get("conflict", 0)),
                not_imported=int(counts.get("not_imported", 0)),
            )
        )
    return outcomes


def _reuse_snapshot(
    *,
    snapshot_dir: Path,
    out_root: Path,
    manifest: SourceManifest,
    verify_hook: Callable[[Path], Mapping[str, Any]] | None,
) -> "SnapshotBuild":
    """复用既有候选快照：不重算数据，只重跑查询验证并记一条运行报告。"""

    report = read_json(snapshot_dir / "report.json")
    report["reused"] = True
    report["last_seen_at"] = local_now()
    snapshot_id = str(report.get("snapshot_id") or snapshot_dir.name)
    if verify_hook is not None:
        verification = dict(_run_verification(verify_hook, snapshot_dir))
        report["verification"] = verification
        write_json_atomic(out_root / "verification" / f"{snapshot_id}.json", verification)
        if not verification.get("all_passed", False):
            report["status"] = "partial"
            if int(report.get("exit_code", 0)) == 0:
                report["exit_code"] = int(ExitCode.PARTIAL)
    outcomes = _outcomes_from_report(report)
    exit_code = exit_code_for(outcomes)
    verification = report.get("verification") or {}
    if verification and not verification.get("all_passed", True) and int(exit_code) == int(
        ExitCode.SUCCESS
    ):
        # 复用既有快照时也不能把“这次验证没过”写成成功。
        exit_code = ExitCode.PARTIAL
    report["exit_code"] = int(exit_code)
    report["status"] = {
        int(ExitCode.SUCCESS): "success",
        int(ExitCode.PARTIAL): "partial",
        int(ExitCode.FAILED): "failed",
    }[int(exit_code)]
    # 业务数据不重写；但“验证结论”是每次运行的结果，复用路径也要把它刷成最新，
    # 否则快照里的报告会一直显示上一次的失败结论。
    write_json_atomic(snapshot_dir / "report.json", report)
    _write_report_markdown(snapshot_dir / "report.md", report)
    write_json_atomic(
        out_root / "runs" / f"{snapshot_id}-{utc_now().replace(':', '')}.json", report
    )
    return SnapshotBuild(
        snapshot_id=snapshot_id,
        snapshot_dir=snapshot_dir,
        report=report,
        reused=True,
        exit_code=exit_code,
        report_path=snapshot_dir / "report.json",
        report_markdown_path=snapshot_dir / "report.md",
        manifest=manifest,
        datasets={
            DatasetId(item["dataset"]): DatasetOutcome(
                dataset=DatasetId(item["dataset"]),
                status=DatasetStatus(item["status"]),
                reason=str(item.get("reason") or ""),
                input_unit=str(item.get("input_unit") or ""),
                input_total=int((item.get("counts") or {}).get("input_total", 0)),
                imported=int((item.get("counts") or {}).get("imported", 0)),
                duplicate=int((item.get("counts") or {}).get("duplicate", 0)),
                conflict=int((item.get("counts") or {}).get("conflict", 0)),
                pending=int((item.get("counts") or {}).get("pending", 0)),
                rejected=int((item.get("counts") or {}).get("rejected", 0)),
                not_imported=int((item.get("counts") or {}).get("not_imported", 0)),
                output_records=int((item.get("counts") or {}).get("output_records", 0)),
                fingerprint=str(item.get("fingerprint") or ""),
            )
            for item in report.get("datasets", []) or []
        },
        outcomes=[],
    )


def _write_report_markdown(path: Path, report: Mapping[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# 官方参考数据导入报告 · {report['batch_id']}")
    lines.append("")
    lines.append(f"- 快照：`{report['snapshot_id']}`")
    lines.append(f"- 批次性质：{report['dataset_kind']}")
    lines.append(f"- 转换器版本：{report['converter_version']}")
    lines.append(f"- 生成时间：{report['generated_at']}（不参与业务指纹）")
    lines.append(f"- 结果：**{report['status']}**（退出码 {report['exit_code']}）")
    lines.append(f"- 汇总：`{render_counts_line(report['totals'])}`")
    if report.get("reused"):
        lines.append("- 说明：本次输入与转换器版本与既有快照一致，复用既有快照，未产生第二份数据。")
    lines.append("")
    lines.append("## 数据类别")
    lines.append("")
    lines.append("| 数据类别 | 状态 | 输入 | 导入 | 重复 | 冲突 | 待核 | 拒绝 | 未导入 | 输出记录 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in report["datasets"]:
        counts = item["counts"]
        lines.append(
            "| {dataset} | {status} | {input_total} | {imported} | {duplicate} | {conflict} | "
            "{pending} | {rejected} | {not_imported} | {output_records} |".format(
                dataset=item["dataset"], status=item["status"], **counts
            )
        )
    lines.append("")
    lines.append("## 原件核对")
    lines.append("")
    lines.append("| 来源 | 类型 | 文件 | SHA-256（前 16 位） | 字节 | 哈希一致 |")
    lines.append("|---|---|---|---|---|---|")
    for item in report["inputs"]:
        name = item.get("repository_path") or item.get("local_path_hint") or ""
        lines.append(
            "| {source_id} | {kind} | {name} | `{sha}` | {bytes} | {ok} |".format(
                source_id=item["source_id"],
                kind=item["kind"],
                name=name,
                sha=(item["sha256"] or "")[:16],
                bytes=item["bytes"],
                ok="是" if item["hash_verified"] else "**否**",
            )
        )
    if report.get("unresolved_inputs"):
        lines.append("")
        lines.append("### 取不到的原件")
        lines.append("")
        for item in report["unresolved_inputs"]:
            lines.append(
                f"- `{item['source_id']}`（{item['kind']}）：{item['reason']}"
                + (f"；本地提示路径 `{item['local_path_hint']}`" if item.get("local_path_hint") else "")
            )
    if report.get("assumptions"):
        lines.append("")
        lines.append("## 待复核假设")
        lines.append("")
        for item in report["assumptions"]:
            lines.append(f"- {item}")
    if report.get("notes"):
        lines.append("")
        lines.append("## 备注")
        lines.append("")
        for item in report["notes"]:
            lines.append(f"- {item}")
    problems = [item for item in report.get("records", []) if item["status"] != "imported"]
    if problems:
        lines.append("")
        lines.append("## 逐条原因（非 imported）")
        lines.append("")
        lines.append("| 数据类别 | 状态 | 业务键 | 定位 | 原因 |")
        lines.append("|---|---|---|---|---|")
        for item in problems[:200]:
            reason = (item.get("reason") or "").replace("|", "\\|")
            lines.append(
                f"| {item['dataset']} | {item['status']} | {item['business_key']} | "
                f"{item['locator']} | {reason} |"
            )
        if len(problems) > 200:
            lines.append(f"| … | … | … | … | 其余 {len(problems) - 200} 条见 report.json |")
    verification = report.get("verification")
    if verification:
        lines.append("")
        lines.append("## 查询验证")
        lines.append("")
        lines.append(f"- 结论：**{'通过' if verification.get('all_passed') else '未通过'}**")
        for check in verification.get("checks", []):
            lines.append(
                f"- `{check['id']}`：{check['status']}"
                + (f" — {check['detail']}" if check.get("detail") else "")
            )
    lines.append("")
    write_text_atomic(path, "\n".join(lines))


def _atomic_publish_dir(temp_dir: Path, final_dir: Path) -> None:
    """把临时目录搬到最终位置；目标已存在时不覆盖。"""

    if final_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
        return
    try:
        os.replace(temp_dir, final_dir)
    except OSError:
        # 目标在检查与替换之间被创建：保留既有快照，丢掉本次临时目录。
        shutil.rmtree(temp_dir, ignore_errors=True)
        if not final_dir.exists():  # pragma: no cover - 极端文件系统错误
            raise


def build_snapshot(
    request: SnapshotRequest,
    *,
    verify_hook: Callable[[Path], Mapping[str, Any]] | None = None,
) -> SnapshotBuild:
    """执行一次完整导入：清单 → 转换 → 校验 → 候选快照 → 报告。

    ``verify_hook`` 用于在快照写好后跑适配器查询验证；它只在快照目录已经可用后调用，
    因此验证失败不会破坏数据文件，只会让报告里的验证结论变成未通过。
    """

    manifest = load_source_manifest(
        request.manifest_path, kind=request.dataset_kind
    )
    sources = list(manifest.sources)
    if not manifest.sources and not manifest.unresolved:
        raise SnapshotFailed("来源清单里没有任何原件")

    # 导入前核对哈希：清单声明的哈希必须逐字相符。
    bad_hashes = [item.source_id for item in sources if not item.verified]
    if bad_hashes:
        raise SnapshotFailed(
            "原件与来源清单声明的 SHA-256/字节数不一致：" + "、".join(bad_hashes)
        )

    versioning = Versioning.from_manifest(manifest)
    acquisition = request.acquisition_date or manifest.prepared_at or utc_now()

    # 快速通道：输入指纹相同说明业务输出必然相同，直接复用既有快照（不再解析几万行）。
    out_root_path = Path(request.out_root)
    wanted_input = input_fingerprint(manifest, versioning)
    reusable = _find_reusable_snapshot(out_root_path, wanted_input)
    if reusable is not None:
        return _reuse_snapshot(
            snapshot_dir=reusable,
            out_root=out_root_path,
            manifest=manifest,
            verify_hook=verify_hook,
        )
    datasets: dict[DatasetId, DatasetOutcome] = {}
    payloads: dict[DatasetId, Any] = {}
    outcomes: list[RecordOutcome] = []
    assumptions: list[str] = []
    notes: list[str] = [manifest.notes] if manifest.notes else []

    def _declared(dataset: DatasetId) -> bool:
        declared = manifest.dataset(dataset)
        return declared is not None and declared.status is DatasetStatus.PROVIDED

    # ---- P14 目录与要素
    catalog_sources, catalog_missing = locate_dataset_inputs(
        manifest, DatasetId.P14_TARIFF
    )
    element_sources, element_missing = locate_dataset_inputs(
        manifest, DatasetId.P14_ELEMENTS
    )
    catalog_union: list[SourceFile] = []
    for item in (*catalog_sources, *element_sources):
        if item.source_id not in {existing.source_id for existing in catalog_union}:
            catalog_union.append(item)
    catalog: CatalogBuild | None = None
    if _declared(DatasetId.P14_TARIFF) or _declared(DatasetId.P14_ELEMENTS):
        catalog = build_catalog(
            manifest, versioning=versioning, sources=catalog_union
        )
        outcomes.extend(catalog.outcomes)
        assumptions.extend(catalog.assumptions)
        for dataset, payload, outcome in (
            (DatasetId.P14_TARIFF, catalog.tariff_payload, catalog.tariff_outcome),
            (DatasetId.P14_ELEMENTS, catalog.elements_payload, catalog.elements_outcome),
        ):
            if not _declared(dataset):
                status, reason = _declared_status(manifest, dataset)
                datasets[dataset] = DatasetOutcome(dataset=dataset, status=status, reason=reason)
                continue
            assert outcome is not None
            if payload is None:
                outcome.status = DatasetStatus.INPUT_MISSING
                outcome.reason = outcome.reason or "没有可解析的原件"
                datasets[dataset] = outcome
                continue
            payloads[dataset] = payload
            datasets[dataset] = outcome
        ledger = catalog.ledger_outcome
        if ledger is None:
            pass
        elif not _declared(DatasetId.REGULATORY_CODE_LEDGER):
            status, reason = _declared_status(manifest, DatasetId.REGULATORY_CODE_LEDGER)
            datasets[DatasetId.REGULATORY_CODE_LEDGER] = DatasetOutcome(
                dataset=DatasetId.REGULATORY_CODE_LEDGER, status=status, reason=reason
            )
        elif catalog.regulatory_ledger_payload is not None:
            payloads[DatasetId.REGULATORY_CODE_LEDGER] = catalog.regulatory_ledger_payload
            datasets[DatasetId.REGULATORY_CODE_LEDGER] = ledger
        else:
            datasets[DatasetId.REGULATORY_CODE_LEDGER] = ledger
    if catalog_missing and _declared(DatasetId.P14_TARIFF):
        notes.extend(f"税则目录缺失原件：{item}" for item in catalog_missing)
    if element_missing and _declared(DatasetId.P14_ELEMENTS):
        notes.extend(f"申报要素缺失原件：{item}" for item in element_missing)

    # ---- P17 退税率
    tax_sources, tax_missing = locate_dataset_inputs(manifest, DatasetId.P17_TAX_RATES)
    if _declared(DatasetId.P17_TAX_RATES):
        if tax_sources:
            tax: TaxBuild = build_tax_rates(
                manifest,
                versioning=versioning,
                sources=tax_sources,
                acquisition_date=acquisition,
            )
            outcomes.extend(tax.outcomes)
            assumptions.extend(tax.assumptions)
            if tax.payload is not None and tax.outcome is not None:
                payloads[DatasetId.P17_TAX_RATES] = tax.payload
                datasets[DatasetId.P17_TAX_RATES] = tax.outcome
            else:
                outcome = tax.outcome or DatasetOutcome(
                    dataset=DatasetId.P17_TAX_RATES, status=DatasetStatus.INPUT_MISSING
                )
                outcome.status = DatasetStatus.INPUT_MISSING
                datasets[DatasetId.P17_TAX_RATES] = outcome
        else:
            datasets[DatasetId.P17_TAX_RATES] = DatasetOutcome(
                dataset=DatasetId.P17_TAX_RATES,
                status=DatasetStatus.INPUT_MISSING,
                reason="清单声明已提供退税率，但本机取不到原件",
            )
            notes.extend(f"退税率缺失原件：{item}" for item in tax_missing)
    else:
        status, reason = _declared_status(manifest, DatasetId.P17_TAX_RATES)
        datasets[DatasetId.P17_TAX_RATES] = DatasetOutcome(
            dataset=DatasetId.P17_TAX_RATES, status=status, reason=reason
        )

    # ---- P17 监管条件：未提供就是未提供，不生成空 measures
    status, reason = _declared_status(manifest, DatasetId.P17_REGULATORY)
    datasets[DatasetId.P17_REGULATORY] = DatasetOutcome(
        dataset=DatasetId.P17_REGULATORY, status=status, reason=reason
    )

    # ---- 品目注释（独立快照）
    commentary_sources, commentary_missing = locate_dataset_inputs(
        manifest, DatasetId.TARIFF_COMMENTARY
    )
    if _declared(DatasetId.TARIFF_COMMENTARY):
        if commentary_sources:
            commentary: CommentaryBuild = build_commentary(
                manifest, versioning=versioning, sources=commentary_sources
            )
            outcomes.extend(commentary.outcomes)
            assumptions.extend(commentary.assumptions)
            if commentary.payload is not None and commentary.outcome is not None:
                payloads[DatasetId.TARIFF_COMMENTARY] = commentary.payload
                datasets[DatasetId.TARIFF_COMMENTARY] = commentary.outcome
            else:
                outcome = commentary.outcome or DatasetOutcome(
                    dataset=DatasetId.TARIFF_COMMENTARY, status=DatasetStatus.INPUT_MISSING
                )
                datasets[DatasetId.TARIFF_COMMENTARY] = outcome
        else:
            datasets[DatasetId.TARIFF_COMMENTARY] = DatasetOutcome(
                dataset=DatasetId.TARIFF_COMMENTARY,
                status=DatasetStatus.INPUT_MISSING,
                reason="清单声明已提供品目注释，但本机取不到原件（原件未随仓库分发）",
            )
            notes.extend(f"品目注释缺失原件：{item}" for item in commentary_missing)
    else:
        status, reason = _declared_status(manifest, DatasetId.TARIFF_COMMENTARY)
        datasets[DatasetId.TARIFF_COMMENTARY] = DatasetOutcome(
            dataset=DatasetId.TARIFF_COMMENTARY, status=status, reason=reason
        )

    # ---- 业务指纹与快照 ID
    overall_fingerprint = _business_fingerprint(datasets, payloads)
    snapshot_id = f"{manifest.batch_id}-{overall_fingerprint[:12]}"
    snapshot_dir = Path(request.out_root) / "candidates" / snapshot_id
    reused = (snapshot_dir / "manifest.json").is_file()
    business_inputs = [item for item in sources]

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "batch_id": manifest.batch_id,
        "snapshot_id": snapshot_id,
        "dataset_kind": DatasetKind(manifest.kind).value,
        "converter_version": CONVERTER_VERSION,
        "generated_at": local_now(),
        "business_fingerprint": overall_fingerprint,
        "inputs": [item.as_dict() for item in business_inputs],
        "unresolved_inputs": [item.as_dict() for item in manifest.unresolved],
        "datasets": [],
        "totals": {},
        "assumptions": assumptions,
        "notes": notes,
        "records": [],
        "reused": reused,
        "status": "",
        "exit_code": 0,
    }

    if reused:
        return _reuse_snapshot(
            snapshot_dir=snapshot_dir,
            out_root=out_root_path,
            manifest=manifest,
            verify_hook=verify_hook,
        )

    # ---- 组装报告中的数据集统计（先算，便于逐条原因对账）
    ordered_datasets = sorted(datasets.values(), key=lambda item: list(DatasetId).index(item.dataset))
    for outcome in ordered_datasets:
        if outcome.dataset in payloads:
            outcome.output_records = _output_record_count(outcome.dataset, payloads[outcome.dataset])
            outcome.fingerprint = _dataset_fingerprint(payloads[outcome.dataset])
        elif outcome.fingerprint == "":
            outcome.fingerprint = ""
    report["datasets"] = [item.as_dict() for item in ordered_datasets]
    report["totals"] = summarize_counts(ordered_datasets)
    report["records"] = [item.as_dict() for item in outcomes]
    exit_code = exit_code_for(ordered_datasets)
    report["exit_code"] = int(exit_code)
    report["status"] = {
        int(ExitCode.SUCCESS): "success",
        int(ExitCode.PARTIAL): "partial",
        int(ExitCode.FAILED): "failed",
    }[int(exit_code)]
    _check_count_reconciliation(ordered_datasets, outcomes)

    # ---- 写临时目录 → 原子替换
    out_root = Path(request.out_root)
    temp_dir = out_root / "candidates" / f".tmp-{snapshot_id}-{os.getpid()}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for dataset, payload in payloads.items():
            write_json_atomic(temp_dir / dataset.filename, payload)
        write_json_atomic(temp_dir / "sources.json", {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "batch_id": manifest.batch_id,
            "dataset_kind": DatasetKind(manifest.kind).value,
            "manifest_path": manifest.manifest_path.name,
            "inputs": report["inputs"],
            "unresolved_inputs": report["unresolved_inputs"],
        })
        outputs = {}
        for dataset in payloads:
            path = temp_dir / dataset.filename
            outputs[dataset.value] = {
                "file": dataset.filename,
                "schema_version": payloads[dataset].get("schema_version"),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "records": _output_record_count(dataset, payloads[dataset]),
                "fingerprint": _dataset_fingerprint(payloads[dataset]),
            }
        write_json_atomic(temp_dir / "manifest.json", {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "batch_id": manifest.batch_id,
            "snapshot_id": snapshot_id,
            "dataset_kind": DatasetKind(manifest.kind).value,
            "converter_version": CONVERTER_VERSION,
            "business_fingerprint": overall_fingerprint,
            "input_fingerprint": wanted_input,
            "generated_at": report["generated_at"],
            "datasets": report["datasets"],
            "outputs": outputs,
            "assumptions": assumptions,
        })
        # 最后一次核对原件：转过程只读，原件必须逐字节不变。
        changed = verify_sources_unchanged(business_inputs)
        if changed:
            raise SnapshotFailed("导入后原件发生变化：" + "；".join(changed))
        write_json_atomic(temp_dir / "report.json", report)
        _write_report_markdown(temp_dir / "report.md", report)
        _atomic_publish_dir(temp_dir, snapshot_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    verification: Mapping[str, Any] | None = None
    if verify_hook is not None:
        verification = _run_verification(verify_hook, snapshot_dir)
        report["verification"] = dict(verification)
        write_json_atomic(snapshot_dir / "report.json", report)
        _write_report_markdown(snapshot_dir / "report.md", report)
        write_json_atomic(out_root / "verification" / f"{snapshot_id}.json", dict(verification))
        if not verification.get("all_passed", False):
            report["status"] = "partial"
            if int(exit_code) == 0:
                exit_code = ExitCode.PARTIAL
                report["exit_code"] = int(exit_code)
    write_json_atomic(
        out_root / "runs" / f"{snapshot_id}-{utc_now().replace(':', '')}.json", report
    )
    return SnapshotBuild(
        snapshot_id=snapshot_id,
        snapshot_dir=snapshot_dir,
        report=report,
        reused=False,
        exit_code=exit_code,
        report_path=snapshot_dir / "report.json",
        report_markdown_path=snapshot_dir / "report.md",
        manifest=manifest,
        datasets=datasets,
        outcomes=outcomes,
    )


def _run_verification(
    verify_hook: Callable[[Path], Mapping[str, Any]], snapshot_dir: Path
) -> Mapping[str, Any]:
    """跑查询验证；钩子本身抛异常也要变成“验证失败”的结论，不能静默当通过。"""

    try:
        return dict(verify_hook(snapshot_dir))
    except Exception as exc:  # 适配器/文件问题都不能让“未验证”变成“已验证”
        return {
            "schema_version": "p23.query-check.1",
            "generated_at": utc_now(),
            "snapshot_dir": snapshot_dir.name,
            "checks": [
                {
                    "id": "verification.crashed",
                    "status": "fail",
                    "detail": f"查询验证执行失败（{type(exc).__name__}: {exc}）",
                    "request": {},
                    "observed": {},
                }
            ],
            "counts": {"pass": 0, "fail": 1, "skip": 0},
            "all_passed": False,
            "note": "验证过程异常；不能用“文件生成成功”代替查询验证",
        }


def _output_record_count(dataset: DatasetId, payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    for key in ("codes", "schemas", "rates", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _check_count_reconciliation(
    datasets: Iterable[DatasetOutcome], outcomes: Iterable[RecordOutcome]
) -> None:
    """报告内部对账：合计必须等于各状态计数，逐条明细状态不能出现未知值。"""

    known = {item.value for item in RecordStatus}
    for item in outcomes:
        if item.status.value not in known:  # pragma: no cover - 枚举已覆盖
            raise SnapshotFailed(f"未知记录状态：{item.status}")
    for outcome in datasets:
        counted = (
            outcome.imported
            + outcome.duplicate
            + outcome.conflict
            + outcome.pending
            + outcome.rejected
            + outcome.not_imported
        )
        if counted != outcome.input_total:
            raise SnapshotFailed(
                f"{outcome.dataset.value} 计数对不上：input_total={outcome.input_total}，"
                f"各状态合计={counted}"
            )


__all__ = [
    "DEFAULT_OUT_ROOT",
    "REPORT_SCHEMA_VERSION",
    "SnapshotBuild",
    "SnapshotFailed",
    "SnapshotRequest",
    "build_snapshot",
]
