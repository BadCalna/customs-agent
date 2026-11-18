"""P23 合成来源批次构造器：一份来源清单 + 两个工作簿 + 一份注释 JSON。

所有内容都是**合成样本**：

- 编码用 ``99999999xx`` / ``0999999991`` 这类明显不存在的号段，名称一律带“合成样本”；
- 数值、日期、税率、匹配层级都是构造值，不代表任何真实政策；
- 来源清单里 ``dataset_kind`` 由调用方决定：``synthetic`` 用于导入/验证测试，
  ``real`` 只用于发布流程测试（发布门禁拒绝合成批次）；
- 清单里的 ``sha256`` / ``bytes`` 一律由 :meth:`Package.build` 的返回值与真实文件大小填入，
  保证“声明值与实测值一致”这个前提在夹具层面成立。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from adapters.sources.reference_import.convert_commentary import render_text_all
from adapters.sources.reference_import.model import sha256_file

from tests.p23.workbooks import Package

#: 合成批次默认标识（写进 report/snapshot 目录名，便于一眼看出不是真实批次）。
BATCH_ID = "customs-reference-synthetic-2026-11-18"
PREPARED_AT = "2026-11-18T01:00:00+08:00"
DATASET_PREFIX = "customs-reference-synthetic"

SYNTHETIC_NOTICE = (
    "合成样本：本批全部数据由 tests/p23/builders.py 构造，编码 99999999xx / 0999999991 "
    "不代表任何真实税号，不得作为业务依据。"
)

INDEX_SHEET = "HS编码索引"
ELEMENT_SHEET = "申报要素明细"
TAX_SHEET = "退税率明细"

INDEX_HEADERS = (
    "HS编码",
    "商品名称",
    "适用前缀",
    "第一计量单位",
    "监管条件代码",
    "申报要素数",
    "必填数",
    "匹配状态",
)
ELEMENT_HEADERS = ("HS编码", "商品名称", "适用前缀", "申报要素", "必填标记（1/0）")
TAX_HEADERS = (
    "HS编码",
    "商品名称",
    "出口退税率",
    "税务匹配编码",
    "匹配层级",
    "税率生效日",
    "税率失效日",
    "税率核验状态",
    "11位细分税码及税率",
)

STATUS_PROVIDED = "provided"
STATUS_NOT_PROVIDED = "not_provided"
STATUS_UNSUPPORTED = "unsupported"

MATCHED = "已匹配"
LEVEL_EXACT = "10位精确"
LEVEL_FALLBACK = "8位回退"
LEVEL_UNMATCHED = "未匹配"


def column_label(index: int) -> str:
    """1 → A；仅支持 1..26（P23 夹具最多 9 列）。"""

    if not 1 <= index <= 26:
        raise ValueError(f"夹具列号超出范围：{index}")
    return chr(ord("A") + index - 1)


def cell_ref(row: int, column: int) -> str:
    return f"{column_label(column)}{row}"


# ---------------------------------------------------------------------------
# 规格对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexRow:
    """HS 编码索引页签的一行。"""

    code: str
    name: str
    prefix: str = "合成前缀"
    unit: str = "001"
    reg_codes: str = "A"
    match_status: str = MATCHED
    element_count: int | None = None
    required_count: int | None = None
    numeric: bool = False
    """``True`` 时编码写成数值单元格（用于“精度丢失/无损数值”场景）。"""


@dataclass(frozen=True)
class ElementRow:
    """申报要素明细页签的一行。"""

    code: str
    name: str
    label: str
    flag: str = "1"


@dataclass(frozen=True)
class TaxRow:
    """退税率明细页签的一行。"""

    code: str
    name: str
    value: str
    """单元格原值：数值单元格写 ``0.13``；文本写 ``13%``。"""

    percent: bool | None = True
    """``True`` 用百分数格式、``False`` 用两位小数格式、``None`` 不写任何样式。"""

    kind: str = "n"
    """``n`` = 数值单元格；``inlineStr`` = 文本单元格。"""

    effective_from: str = "2026-01-01"
    effective_to: str = "2026-12-31"
    check_status: str = MATCHED
    level: str = LEVEL_EXACT
    tax_code: str = "9999999999001"
    detail: str = ""


@dataclass
class SyntheticBatch:
    root: Path
    manifest: Path
    workbooks: list[Path]
    commentary: Path | None

    @property
    def out_root(self) -> Path:
        """默认输出根（每个测试独立临时目录内，绝不写仓库里的 .local/）。"""

        return self.root / "out"


# ---------------------------------------------------------------------------
# 工作簿
# ---------------------------------------------------------------------------


def _write_sheet_with_header(
    package: Package,
    name: str,
    headers: Sequence[str],
    header_row: int,
):
    sheet = package.sheet(name)
    # 表头之前放标题/元数据行：证明“表头不在第 1 行”也能被按表头文字定位。
    sheet.text("A1", "合成样本：仅供 P23 测试，非真实业务数据")
    for offset, header in enumerate(headers, start=1):
        sheet.text(cell_ref(header_row, offset), header)
    return sheet


def write_catalog_workbook(
    path: str | Path,
    *,
    index_rows: Sequence[IndexRow],
    element_rows: Sequence[ElementRow],
    tax_rows: Sequence[TaxRow] = (),
    index_header_row: int = 9,
    element_header_row: int = 3,
    tax_header_row: int = 2,
) -> str:
    package = Package()
    index = _write_sheet_with_header(package, INDEX_SHEET, INDEX_HEADERS, index_header_row)
    for offset, row in enumerate(index_rows, start=index_header_row + 1):
        code_ref = cell_ref(offset, 1)
        if row.numeric:
            index.number(code_ref, row.code)
        else:
            index.text(code_ref, row.code)
        index.text(cell_ref(offset, 2), row.name)
        index.text(cell_ref(offset, 3), row.prefix)
        index.text(cell_ref(offset, 4), row.unit)
        index.text(cell_ref(offset, 5), row.reg_codes)
        if row.element_count is not None:
            index.text(cell_ref(offset, 6), str(row.element_count))
        if row.required_count is not None:
            index.text(cell_ref(offset, 7), str(row.required_count))
        if row.match_status:
            index.text(cell_ref(offset, 8), row.match_status)

    elements = _write_sheet_with_header(
        package, ELEMENT_SHEET, ELEMENT_HEADERS, element_header_row
    )
    for offset, row in enumerate(element_rows, start=element_header_row + 1):
        elements.text(cell_ref(offset, 1), row.code)
        elements.text(cell_ref(offset, 2), row.name)
        elements.text(cell_ref(offset, 3), "合成前缀")
        elements.text(cell_ref(offset, 4), row.label)
        elements.text(cell_ref(offset, 5), row.flag)

    if tax_rows:
        tax = _write_sheet_with_header(package, TAX_SHEET, TAX_HEADERS, tax_header_row)
        for offset, row in enumerate(tax_rows, start=tax_header_row + 1):
            tax.text(cell_ref(offset, 1), row.code)
            tax.text(cell_ref(offset, 2), row.name)
            if row.kind == "inlineStr":
                tax.text(cell_ref(offset, 3), row.value)
            else:
                tax.number(cell_ref(offset, 3), row.value, percent=row.percent)
            tax.text(cell_ref(offset, 4), row.tax_code)
            tax.text(cell_ref(offset, 5), row.level)
            tax.text(cell_ref(offset, 6), row.effective_from)
            tax.text(cell_ref(offset, 7), row.effective_to)
            tax.text(cell_ref(offset, 8), row.check_status)
            if row.detail:
                tax.text(cell_ref(offset, 9), row.detail)

    return package.build(path)


# ---------------------------------------------------------------------------
# 注释 JSON
# ---------------------------------------------------------------------------


def commentary_item(
    *,
    tariff_no: str,
    kind: str,
    sections: Sequence[tuple[str, str]],
    chapter: str = "",
    name: str = "",
    index: int | None = None,
) -> dict[str, Any]:
    """构造一条注释条目；``text_all`` 与 ``char_count`` 都按来源口径生成。"""

    section_payload = [{"label": label, "text": text} for label, text in sections]
    return {
        "tariff_no": tariff_no,
        "kind": kind,
        "chapter": chapter,
        "tariff_name": name or f"合成注释样本 {tariff_no}",
        "index": index,
        "status": "synthetic",
        "detail": {
            "sections": section_payload,
            "text_all": render_text_all(section_payload),
            "char_count": sum(len(item["text"]) for item in section_payload),
        },
    }


def core_commentary_items() -> list[dict[str, Any]]:
    """覆盖品目（4 位）、章注（CH1）、类注（CA1）、其他注释（ot3）四类层级键。"""

    return [
        commentary_item(
            tariff_no="9991",
            kind="品目",
            chapter="99",
            name="合成品目注释样本",
            index=1,
            sections=(
                ("品目注释", "合成样本：品目 9991 的注释正文，含“改良种用”字样用于验证关键词检索。"),
                ("子目注释", "合成样本：子目注释第二段正文。"),
            ),
        ),
        commentary_item(
            tariff_no="CH1",
            kind="章注",
            chapter="99",
            name="合成章注样本",
            index=2,
            sections=(("章注", "合成样本：第 99 章章注正文。"),),
        ),
        commentary_item(
            tariff_no="CA1",
            kind="类注",
            name="合成类注样本",
            index=3,
            sections=(("类注", "合成样本：第一类类注正文。"),),
        ),
        commentary_item(
            tariff_no="ot3",
            kind="其他注释",
            name="合成其他注释样本",
            index=4,
            sections=(("总注释", "合成样本：归类总规则注释正文。"),),
        ),
    ]


def write_commentary(
    path: str | Path, items: Sequence[Mapping[str, Any]], *, fetched_at: str = "2026-11-18T10:00:00+08:00"
) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {
            "page": "https://example.invalid/p23-synthetic-commentary",
            "api_home": "https://example.invalid/p23-synthetic-commentary/api",
        },
        "fetched_at": fetched_at,
        "total": len(items),
        "items": list(items),
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return sha256_file(target)


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


def _input_entry(
    root: Path,
    *,
    source_id: str,
    kind: str,
    path: Path,
    roles: Sequence[str],
    retrieved_at: str,
    recorded_source: Mapping[str, Any],
    notes: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "kind": kind,
        "repository_path": path.relative_to(root).as_posix(),
        "original_filename": path.name,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "roles": list(roles),
        "retrieved_at": retrieved_at,
        "recorded_source": dict(recorded_source),
        "notes": notes,
    }


def _dataset_entry(
    dataset: str, status: str, *, inputs: Sequence[str] = (), reason: str = ""
) -> dict[str, Any]:
    payload: dict[str, Any] = {"dataset": dataset, "status": status}
    if reason:
        payload["reason"] = reason
    if inputs:
        payload["inputs"] = list(inputs)
    return payload


def _defaults(prefix: str) -> dict[str, str]:
    return {
        "dataset_prefix": prefix,
        "tariff_version": "2026",
        "tariff_effective_from": "2026-11-16",
        "elements_version": "2026",
        "elements_effective_from": "2026-11-16",
        "tax_version": "2026C",
        "commentary_version": "2026-11-18T10-00",
    }


def write_manifest(
    root: Path,
    *,
    batch_id: str,
    kind: str,
    inputs: Sequence[Mapping[str, Any]],
    datasets: Sequence[Mapping[str, Any]],
    defaults: Mapping[str, str],
    prepared_at: str = PREPARED_AT,
    notes: str = SYNTHETIC_NOTICE,
    name: str = "reference-sources.json",
) -> Path:
    path = root / name
    payload = {
        "schema_version": "p23.sources.1",
        "batch_id": batch_id,
        "dataset_kind": kind,
        "prepared_at": prepared_at,
        "notes": notes,
        "defaults": dict(defaults),
        "datasets": list(datasets),
        "inputs": list(inputs),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


def build_batch(
    root: str | Path,
    *,
    index_rows: Sequence[IndexRow],
    element_rows: Sequence[ElementRow],
    tax_rows: Sequence[TaxRow] = (),
    commentary_items: Sequence[Mapping[str, Any]] | None = None,
    second_name_overrides: Mapping[str, str] | None = None,
    kind: str = "synthetic",
    batch_id: str = BATCH_ID,
    prepared_at: str = PREPARED_AT,
    dataset_prefix: str = DATASET_PREFIX,
    tax_in_both: bool = False,
    workbook_names: Sequence[str] = (
        "合成_HS编码与申报要素映射_M01.xlsx",
        "合成_HS编码与申报要素及出口退税率映射_M02.xlsx",
    ),
    index_header_row: int = 9,
    element_header_row: int = 3,
) -> SyntheticBatch:
    """写出一批合成原件与来源清单；返回各文件路径。"""

    base = Path(root)
    mappings = base / "mappings"
    mappings.mkdir(parents=True, exist_ok=True)
    overrides = dict(second_name_overrides or {})

    m01 = mappings / workbook_names[0]
    m02 = mappings / workbook_names[1]
    write_catalog_workbook(
        m01,
        index_rows=index_rows,
        element_rows=element_rows,
        tax_rows=tax_rows if tax_in_both else (),
        index_header_row=index_header_row,
        element_header_row=element_header_row,
    )
    write_catalog_workbook(
        m02,
        index_rows=[
            replace(row, name=overrides.get(row.code, row.name)) for row in index_rows
        ],
        element_rows=[
            replace(row, name=overrides.get(row.code, row.name)) for row in element_rows
        ],
        tax_rows=tax_rows,
        index_header_row=index_header_row,
        element_header_row=element_header_row,
    )

    commentary_path: Path | None = None
    if commentary_items is not None:
        commentary_path = base / "commentary" / "品目注释_全量_合成_20261118.json"
        write_commentary(commentary_path, commentary_items)

    inputs = [
        _input_entry(
            base,
            source_id="M01",
            kind="hs_element_mapping_workbook",
            path=m01,
            roles=("hs_catalog", "declaration_elements"),
            retrieved_at="2026-11-16T09:00:00Z",
            recorded_source={
                "publisher": "合成样本（非真实来源）",
                "hs_system": "合成样本 · HS 编码索引",
                "elements_system": "合成样本 · 申报要素明细",
                "hs_url": "https://example.invalid/p23-synthetic-hs",
            },
            notes="合成样本：与 M02 的索引/要素页签内容一致，用于验证重复导入不产生重复记录。",
        ),
        _input_entry(
            base,
            source_id="M02",
            kind="hs_element_tax_rate_workbook",
            path=m02,
            roles=("hs_catalog", "declaration_elements", "tax_rates"),
            retrieved_at="2026-11-16T09:00:00Z",
            recorded_source={
                "publisher": "合成样本（非真实来源）",
                "hs_system": "合成样本 · HS 编码索引",
                "elements_system": "合成样本 · 申报要素明细",
                "tax_system": "合成样本 · 出口退税率",
                "tax_url": "https://example.invalid/p23-synthetic-tax",
            },
            notes="合成样本：在 M01 基础上增加出口退税率相关列。",
        ),
    ]
    if commentary_path is not None:
        inputs.append(
            _input_entry(
                base,
                source_id="C01",
                kind="commentary_json",
                path=commentary_path,
                roles=("tariff_commentary",),
                retrieved_at="2026-11-18T00:36:05+08:00",
                recorded_source={
                    "publisher": "合成样本（非真实来源）",
                    "system": "合成样本 · 品目注释",
                    "url": "https://example.invalid/p23-synthetic-commentary",
                },
                notes="合成样本：注释 JSON，正文为构造文本。",
            )
        )

    catalog_inputs = ["M01", "M02"]
    if tax_rows:
        tax_inputs = ["M01", "M02"] if tax_in_both else ["M02"]
        tax_dataset = _dataset_entry("p17_tax_rates", STATUS_PROVIDED, inputs=tax_inputs)
    else:
        tax_dataset = _dataset_entry(
            "p17_tax_rates", STATUS_NOT_PROVIDED, reason="合成样本：本批未构造退税率列"
        )
    commentary_dataset = (
        _dataset_entry("tariff_commentary", STATUS_PROVIDED, inputs=["C01"])
        if commentary_path is not None
        else _dataset_entry(
            "tariff_commentary", STATUS_NOT_PROVIDED, reason="合成样本：本批未构造注释 JSON"
        )
    )
    datasets = [
        _dataset_entry("p14_tariff", STATUS_PROVIDED, inputs=catalog_inputs),
        _dataset_entry("p14_elements", STATUS_PROVIDED, inputs=catalog_inputs),
        tax_dataset,
        _dataset_entry(
            "regulatory_code_ledger", STATUS_PROVIDED, inputs=catalog_inputs
        ),
        _dataset_entry(
            "p17_regulatory",
            STATUS_NOT_PROVIDED,
            reason=(
                "合成样本：未提供监管条件表（只有代码串，没有方向/措施/证件/有效期），"
                "不得按代码猜含义"
            ),
        ),
        commentary_dataset,
    ]

    manifest = write_manifest(
        base,
        batch_id=batch_id,
        kind=kind,
        inputs=inputs,
        datasets=datasets,
        defaults=_defaults(dataset_prefix),
        prepared_at=prepared_at,
    )
    return SyntheticBatch(
        root=base,
        manifest=manifest,
        workbooks=[m01, m02],
        commentary=commentary_path,
    )


# ---------------------------------------------------------------------------
# 常用批次
# ---------------------------------------------------------------------------


def core_index_rows() -> list[IndexRow]:
    return [
        IndexRow(
            "0999999991",
            "合成样本甲",
            unit="001",
            reg_codes="A",
            element_count=2,
            required_count=1,
        ),
        IndexRow(
            "9999999911",
            "合成样本乙",
            unit="002",
            reg_codes="AB",
            element_count=1,
            required_count=1,
        ),
        IndexRow(
            "9999999912",
            "合成样本丙",
            unit="003",
            reg_codes="B",
            element_count=1,
            required_count=0,
        ),
    ]


def core_element_rows() -> list[ElementRow]:
    return [
        ElementRow("0999999991", "合成样本甲", "合成要素甲", "1"),
        ElementRow("0999999991", "合成样本甲", "合成要素乙", "0"),
        ElementRow("9999999911", "合成样本乙", "合成要素丙", "1"),
        ElementRow("9999999912", "合成样本丙", "合成要素丁", "0"),
    ]


def core_tax_rows() -> list[TaxRow]:
    return [
        TaxRow("0999999991", "合成样本甲", "0.13", percent=True),
        TaxRow("9999999911", "合成样本乙", "0.09", percent=True),
        TaxRow("9999999912", "合成样本丙", "0", percent=True),
    ]


def build_core_batch(root: str | Path) -> SyntheticBatch:
    """干净批次：无拒绝、无冲突、无待核，用于计数对账与查询验证。"""

    return build_batch(
        root,
        index_rows=core_index_rows(),
        element_rows=core_element_rows(),
        tax_rows=core_tax_rows(),
        commentary_items=core_commentary_items(),
    )


def build_duplicate_batch(root: str | Path) -> SyntheticBatch:
    """两个工作簿字节完全相同（不含退税率页签），用于 duplicate 口径。"""

    return build_batch(
        root,
        index_rows=core_index_rows(),
        element_rows=core_element_rows(),
        tax_rows=(),
        commentary_items=None,
        workbook_names=(
            "合成_重复来源_甲.xlsx",
            "合成_重复来源_乙.xlsx",
        ),
    )


def build_publish_batch(
    root: str | Path,
    *,
    label: str = "A",
    kind: str = "real",
    name_suffix: str = "",
) -> SyntheticBatch:
    """发布流程用批次：内容由 ``label``/``name_suffix`` 决定，可构造 A/B 两版内容。

    默认 ``kind="real"``：发布门禁只接受真实批次，而合成批次必须被拒绝（另有测试覆盖）。
    这里的内容仍是合成样本，仅用于验证发布流程本身。
    """

    name = f"合成样本甲{name_suffix}"
    index_rows = [
        IndexRow("0999999991", name, unit="001", reg_codes="A", element_count=1, required_count=1),
        IndexRow("9999999911", "合成样本乙", unit="002", reg_codes="B", element_count=1, required_count=0),
    ]
    element_rows = [
        ElementRow("0999999991", name, "合成要素甲", "1"),
        ElementRow("9999999911", "合成样本乙", "合成要素乙", "0"),
    ]
    return build_batch(
        root,
        index_rows=index_rows,
        element_rows=element_rows,
        tax_rows=(),
        commentary_items=None,
        kind=kind,
        batch_id=f"customs-reference-publish-{label}",
        dataset_prefix=f"customs-reference-publish-{label}",
        workbook_names=(
            f"合成_发布批次_{label}_M01.xlsx",
            f"合成_发布批次_{label}_M02.xlsx",
        ),
    )


# ---------------------------------------------------------------------------
# 清单改写与验证报告
# ---------------------------------------------------------------------------


def mutate_manifest(
    batch: SyntheticBatch,
    mutate: Callable[[dict[str, Any]], None],
    *,
    name: str = "reference-sources-variant.json",
) -> Path:
    """读现有清单、调用 ``mutate`` 改动后另存一份（不覆盖原清单）。"""

    payload = json.loads(batch.manifest.read_text(encoding="utf-8"))
    mutate(payload)
    path = batch.root / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def write_verification(
    out_root: str | Path,
    snapshot_id: str,
    *,
    all_passed: bool = True,
    checks: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """写一份查询验证报告；发布门禁会读 ``<out_root>/verification/<snapshot_id>.json``。"""

    verification_dir = Path(out_root) / "verification"
    verification_dir.mkdir(parents=True, exist_ok=True)
    if checks is None:
        checks = (
            [{"id": "p14.tariff.leading_zero_hit", "status": "pass", "detail": "合成样本"}]
            if all_passed
            else [{"id": "p14.tariff.leading_zero_hit", "status": "fail", "detail": "合成样本：故意失败"}]
        )
    payload = {
        "schema_version": "p23.query-check.1",
        "generated_at": "2026-11-18T02:00:00+00:00",
        "snapshot_dir": snapshot_id,
        "checks": list(checks),
        "counts": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "fail": sum(1 for item in checks if item["status"] == "fail"),
            "skip": sum(1 for item in checks if item["status"] == "skip"),
        },
        "all_passed": all_passed,
    }
    path = verification_dir / f"{snapshot_id}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def write_review(path: str | Path, **overrides: Any) -> Path:
    """写一份复核文件；默认是“合规”版本，``overrides`` 用来构造各类不合格样例。"""

    payload: dict[str, Any] = {
        "schema_version": "p23.review.1",
        "reviewed_by": "合成复核人",
        "reviewed_at": "2026-11-18T03:00:00+08:00",
        "decision": "approved",
        "scope": [],
        "confirmations": dict(overrides.pop("confirmations", {}) or {}),
        "notes": "合成样本：仅用于测试发布门禁，不代表真实复核",
    }
    payload.update(overrides)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


__all__ = [
    "BATCH_ID",
    "DATASET_PREFIX",
    "ELEMENT_HEADERS",
    "ELEMENT_SHEET",
    "ElementRow",
    "INDEX_HEADERS",
    "INDEX_SHEET",
    "IndexRow",
    "LEVEL_EXACT",
    "LEVEL_FALLBACK",
    "LEVEL_UNMATCHED",
    "MATCHED",
    "PREPARED_AT",
    "STATUS_NOT_PROVIDED",
    "STATUS_PROVIDED",
    "STATUS_UNSUPPORTED",
    "SYNTHETIC_NOTICE",
    "SyntheticBatch",
    "TAX_HEADERS",
    "TAX_SHEET",
    "TaxRow",
    "build_batch",
    "build_core_batch",
    "build_duplicate_batch",
    "build_publish_batch",
    "cell_ref",
    "column_label",
    "commentary_item",
    "core_commentary_items",
    "core_element_rows",
    "core_index_rows",
    "core_tax_rows",
    "mutate_manifest",
    "write_catalog_workbook",
    "write_commentary",
    "write_manifest",
    "write_review",
    "write_verification",
]
