"""出口退税率转换（P23 → P17 ``p17.taxrate.1``）。

来源是用户导出的「HS 编码与申报要素及出口退税率映射」工作簿里的退税率列：
出口退税率、税务匹配编码、匹配层级、税率生效日、税率失效日、11 位细分税码及税率、
税率核验状态。

三条硬规则
----------
1. **空白不是 0%**：退税率单元格为空 → 不生成记录（来源未提供），绝不写
   ``rate_percent=0``；只有单元格明确是 0 才写 ``zero_rate``。
2. **百分比必须由数字格式支撑**：单元格存的是 0.13 这类小数，只有该单元格
   自己的数字格式确实是百分数格式时才乘 100；格式读不出或不是百分数就拒绝该行，
   不按“看起来像百分比”换算。
3. **核验状态决定能不能当依据**：来源标注“11 位细分税码税率不同，需核验”时，
   金额照样保留，但 ``rate_state`` 记 ``unknown``——P17 因此只会给出待核结论，
   不会把 10 位税率当成已核清。

适用条件
--------
来源没有适用条件列，所以**不写** ``conditions`` 字段（= 来源未声明），
也不写 ``[]``（那是“来源明确声明无条件”）。这让 P17 的 ``applicability``
在复核前恒为 ``unknown``，而不是被误判成“无条件适用”。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from adapters.sources.reference_import.convert_catalog import (
    Versioning,
    data_rows,
    dataset_descriptor,
    read_hs_code,
    source_ref,
    system_name,
)
from adapters.sources.reference_import.model import (
    DatasetId,
    DatasetKind,
    DatasetOutcome,
    DatasetStatus,
    RecordIssue,
    RecordOutcome,
    RecordStatus,
    SourceFile,
    clean_source_text,
    fingerprint,
    parse_iso_date,
    visible_text,
)
from adapters.sources.reference_import.sources import (
    HeaderLayout,
    HeaderSpec,
    LayoutError,
    SourceManifest,
    WorkbookAccess,
    open_workbook,
)

TAX_SPEC = HeaderSpec(
    label="退税率列",
    required=(
        "HS编码",
        "商品名称",
        "出口退税率",
        "税务匹配编码",
        "匹配层级",
        "税率生效日",
        "税率失效日",
        "税率核验状态",
    ),
    optional=("11位细分税码及税率", "适用前缀"),
)

#: 来源对“这条退税率能不能直接用”的声明。
CHECK_MATCHED = "已匹配"
CHECK_SUBDIVIDED = "11位细分税码税率不同，需核验"
CHECK_NO_RECORD = "税务文库无有效记录，需核验"

#: 匹配层级取值；不在表内的一律记待核，不猜。
LEVEL_EXACT = "10位精确"
LEVEL_FALLBACK = "8位回退"
LEVEL_UNMATCHED = "未匹配"


@dataclass
class TaxBuild:
    payload: dict[str, Any] | None = None
    outcome: DatasetOutcome | None = None
    outcomes: list[RecordOutcome] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def tax_layout(access: WorkbookAccess) -> HeaderLayout:
    try:
        return access.locate_header(TAX_SPEC)
    except LayoutError as exc:
        raise LayoutError(exc.code, f"{access.source.source_id}: {exc}") from exc


def _digits(text: str) -> str | None:
    """只接受“十进制整数或小数”的原始数字串；其余返回 ``None``。"""

    value = visible_text(unicodedata.normalize("NFKC", text or ""))
    if not value:
        return None
    if value.startswith("-"):
        return None
    parts = value.split(".")
    if len(parts) > 2 or not parts[0].isdigit():
        return None
    if len(parts) == 2 and (not parts[1].isdigit() or not parts[1]):
        return None
    return value


def rate_from_cell(
    *,
    raw_text: str,
    cell_kind: str,
    percent_format: bool | None,
    format_code: str | None,
) -> tuple[str, str, float | None, RecordIssue | None]:
    """把退税率单元格读成 ``(rate_kind, rate_raw, rate_percent, issue)``。

    - 空白 → ``("", "", None, None)``：由调用方按“来源未提供”处理，不补 0；
    - 数值 + 百分数格式 → 乘 100 得到百分数；
    - 数值但格式不是百分数/读不出 → 报错，不换算；
    - 文本 → 只接受 ``13%`` ``13`` 这类明确写法，且必须是数值型税率。
    """

    text = visible_text(unicodedata.normalize("NFKC", raw_text or ""))
    if not text:
        return "", "", None, None
    if cell_kind == "n":
        digits = _digits(text)
        if digits is None:
            return (
                "",
                text,
                None,
                RecordIssue(
                    "TAX_RATE_CELL_NOT_NUMERIC",
                    f"退税率单元格不是可解析的十进制数：{text!r}",
                    "出口退税率",
                ),
            )
        if percent_format is not True:
            shown = format_code if format_code is not None else "读不出"
            return (
                "",
                text,
                None,
                RecordIssue(
                    "TAX_RATE_FORMAT_UNVERIFIED",
                    f"退税率单元格数值 {text!r} 的数字格式不是百分数（{shown}），"
                    "不能按“看起来像百分比”换算成百分数",
                    "出口退税率",
                ),
            )
        percent = round(float(digits) * 100, 6)
    else:
        stripped = text[:-1].strip() if text.endswith("%") else text
        digits = _digits(stripped)
        if digits is None:
            return (
                "",
                text,
                None,
                RecordIssue(
                    "TAX_RATE_CELL_NOT_NUMERIC",
                    f"退税率单元格是文本 {text!r}，既不是百分数也不是数值；"
                    "“不予退税/免税”等处理方式本批未提供",
                    "出口退税率",
                ),
            )
        percent = round(float(digits), 6)
    if percent < 0 or percent > 100:
        return (
            "",
            text,
            None,
            RecordIssue(
                "TAX_RATE_OUT_OF_RANGE",
                f"退税率 {percent:g}% 不在 0–100 之间",
                "出口退税率",
            ),
        )
    kind = "zero_rate" if abs(percent) < 1e-9 else "export_rebate"
    rate_raw = f"{percent:.2f}%"
    return kind, rate_raw, percent, None


def build_tax_rates(
    manifest: SourceManifest,
    *,
    versioning: Versioning,
    sources: Iterable[SourceFile],
    acquisition_date: str | None,
) -> TaxBuild:
    """把退税率列转换成 P17 退税率快照。

    ``acquisition_date`` 是来源取得/核对日期（本机时区），用于判断记录的
    ``rate_state``：记录窗口覆盖该日期 → ``in_force``；已过期但来源未声明作废
    → ``unknown``（不替来源宣布废止）。
    """

    build = TaxBuild()
    outcome = DatasetOutcome(
        dataset=DatasetId.P17_TAX_RATES,
        status=DatasetStatus.PROVIDED,
        input_unit="退税率行",
    )
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    documents: list[str] = []
    urls: list[str] = []
    retrieved: list[str] = []
    levels: dict[str, int] = {}
    no_record_codes: list[str] = []
    from_dates: list[str] = []
    to_dates: list[str] = []
    numeric_cells: list[str] = []
    acquisitions = parse_iso_date(acquisition_date) if acquisition_date else None

    for source in sources:
        access = open_workbook(source)
        layout = tax_layout(access)
        documents.append(source.original_filename or source.path.name)
        url = system_name(source, "tax_url", "url")
        if url:
            urls.append(url)
        if source.retrieved_at:
            retrieved.append(source.retrieved_at)
        sheet = access.sheet(layout.sheet)
        columns = layout.columns
        system = system_name(source, "tax_system") or (
            "国家税务总局出口退税率文库（用户导出整理）"
        )
        source_url = system_name(source, "tax_url", "url")
        for row_number in data_rows(access, sheet, start_row=layout.header_row):
            outcome.input_total += 1
            code_cell = access.cell(sheet, row_number, columns["HS编码"])
            read = read_hs_code(
                "" if code_cell is None else code_cell.value,
                "" if code_cell is None else code_cell.kind,
            )
            locator = access.locator(sheet, row_number, columns["HS编码"])
            if not read.ok:
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P17_TAX_RATES,
                        status=RecordStatus.REJECTED,
                        business_key=read.raw_text or f"row-{row_number}",
                        locator=locator,
                        reason=read.issue.message if read.issue else "编码不可用",
                        issues=[read.issue] if read.issue else [],
                    )
                )
                continue
            if read.recovered_from_numeric:
                numeric_cells.append(locator)
            code = read.code
            rate_column = columns["出口退税率"]
            rate_cell = access.cell(sheet, row_number, rate_column)
            kind, rate_raw, percent, issue = rate_from_cell(
                raw_text="" if rate_cell is None else rate_cell.value,
                cell_kind="" if rate_cell is None else rate_cell.kind,
                percent_format=(
                    None if rate_cell is None else access.is_percent_cell(sheet, row_number, rate_column)
                ),
                format_code=access.number_format(sheet, row_number, rate_column),
            )
            check_status = visible_text(
                access.text(sheet, row_number, columns.get("税率核验状态"))
            )
            level = visible_text(access.text(sheet, row_number, columns.get("匹配层级")))
            tax_code = visible_text(access.text(sheet, row_number, columns.get("税务匹配编码")))
            detail = visible_text(
                access.text(sheet, row_number, columns.get("11位细分税码及税率"))
            )
            from_raw = visible_text(access.text(sheet, row_number, columns.get("税率生效日")))
            to_raw = visible_text(access.text(sheet, row_number, columns.get("税率失效日")))
            levels[level or "未声明"] = levels.get(level or "未声明", 0) + 1

            problems: list[RecordIssue] = []
            if issue is not None:
                problems.append(issue)
            if not rate_raw:
                # 空白：来源未提供该编码的退税率，不生成记录、不补 0。
                outcome.count(RecordStatus.PENDING)
                no_record_codes.append(code)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P17_TAX_RATES,
                        status=RecordStatus.PENDING,
                        business_key=code,
                        locator=access.locator(sheet, row_number, rate_column),
                        reason="退税率单元格为空：来源未提供，不生成 0% 记录（空白不是零税率）",
                        issues=[
                            RecordIssue(
                                "TAX_RATE_NOT_PROVIDED",
                                "来源未填写退税率",
                                "出口退税率",
                                severity="pending",
                            )
                        ],
                    )
                )
                continue
            if check_status == CHECK_NO_RECORD:
                outcome.count(RecordStatus.PENDING)
                no_record_codes.append(code)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P17_TAX_RATES,
                        status=RecordStatus.PENDING,
                        business_key=code,
                        locator=access.locator(sheet, row_number, rate_column),
                        reason="来源标注“税务文库无有效记录，需核验”：不生成退税率记录",
                        issues=[
                            RecordIssue(
                                "TAX_RATE_NO_LIBRARY_RECORD",
                                "税务文库无有效记录",
                                "出口退税率",
                                severity="pending",
                            )
                        ],
                    )
                )
                continue
            effective_from = parse_iso_date(from_raw)
            effective_to = parse_iso_date(to_raw)
            if from_raw and effective_from is None:
                problems.append(
                    RecordIssue(
                        "TAX_EFFECTIVE_FROM_INVALID",
                        f"税率生效日不是 ISO 日期：{from_raw!r}",
                        "税率生效日",
                    )
                )
            if to_raw and effective_to is None:
                problems.append(
                    RecordIssue(
                        "TAX_EFFECTIVE_TO_INVALID",
                        f"税率失效日不是 ISO 日期：{to_raw!r}",
                        "税率失效日",
                    )
                )
            if effective_from and effective_to and effective_to < effective_from:
                problems.append(
                    RecordIssue(
                        "TAX_EFFECTIVE_RANGE_REVERSED",
                        f"税率失效日 {to_raw} 早于生效日 {from_raw}",
                        "税率失效日",
                    )
                )
            if level == LEVEL_UNMATCHED:
                outcome.count(RecordStatus.PENDING)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P17_TAX_RATES,
                        status=RecordStatus.PENDING,
                        business_key=code,
                        locator=access.locator(sheet, row_number, columns["匹配层级"]),
                        reason="来源标注税务匹配层级为“未匹配”：不生成退税率记录",
                        issues=[
                            RecordIssue(
                                "TAX_LEVEL_UNMATCHED",
                                "税务匹配层级未匹配",
                                "匹配层级",
                                severity="pending",
                            )
                        ],
                    )
                )
                continue
            if problems:
                outcome.count(RecordStatus.REJECTED)
                build.outcomes.append(
                    RecordOutcome(
                        dataset=DatasetId.P17_TAX_RATES,
                        status=RecordStatus.REJECTED,
                        business_key=code,
                        locator=access.locator(sheet, row_number, rate_column),
                        reason="；".join(item.message for item in problems),
                        issues=problems,
                    )
                )
                continue

            # 状态：来源标注需核验 → unknown；窗口覆盖取得日期 → in_force；
            # 已过期但来源未宣布作废 → unknown（不替来源判定废止）。
            rate_state = "in_force"
            state_reasons: list[str] = []
            if check_status == CHECK_SUBDIVIDED:
                rate_state = "unknown"
                state_reasons.append(
                    "来源标注“11 位细分税码税率不同，需核验”：10 位税率不能直接当依据"
                )
            elif check_status not in (CHECK_MATCHED, ""):
                rate_state = "unknown"
                state_reasons.append(f"来源核验状态未识别：{check_status!r}，保持待核")
            elif not check_status and detail:
                # 没写核验状态但给了 11 位细分：不能当成“已核验的 10 位税率”。
                rate_state = "unknown"
                state_reasons.append(
                    "来源未填写税率核验状态，但存在 11 位细分税码及税率，保持待核"
                )
            if level not in (LEVEL_EXACT, LEVEL_FALLBACK, ""):
                rate_state = "unknown"
                state_reasons.append(f"来源匹配层级未识别：{level!r}，保持待核")
            if acquisitions is not None:
                if effective_from and acquisitions < effective_from:
                    rate_state = "unknown"
                    state_reasons.append(
                        f"取得日期 {acquisition_date} 早于税率生效日 {from_raw}，尚未生效"
                    )
                elif effective_to and acquisitions > effective_to:
                    rate_state = "unknown"
                    state_reasons.append(
                        f"取得日期 {acquisition_date} 晚于税率失效日 {to_raw}；"
                        "来源未声明作废，保持待核"
                    )

            notes = [
                f"来源匹配层级 {level or '未声明'}；税务匹配编码 {tax_code or '未声明'}",
                "税务文库按 8 位码命中时，本记录是按来源逐行映射到 10 位税号，"
                "不代表文库存在该 10 位条目"
                if level == LEVEL_FALLBACK
                else "税务文库按 10 位码精确命中",
            ]
            if rate_cell is not None and rate_cell.kind == "n":
                notes.append(
                    f"单元格原值 {rate_cell.value}（数字格式 {access.number_format(sheet, row_number, rate_column)}）"
                )
            if detail:
                notes.append(f"11 位细分税码及税率原文：{detail}")
            notes.extend(state_reasons)
            record = {
                "version": versioning.tax_version,
                "hs_code": code,
                "rebate_code": "",
                "product_name": clean_source_text(
                    access.text(sheet, row_number, columns.get("商品名称"))
                ),
                "rate_kind": kind,
                "rate_raw": rate_raw,
                "rate_percent": percent,
                "rate_state": rate_state,
                "effective_from": from_raw or None,
                "effective_to": to_raw or None,
                "source": source_ref(
                    source,
                    sheet=sheet.name,
                    locator=access.locator(sheet, row_number, rate_column),
                    system=system,
                    url=source_url,
                ),
                "note": "；".join(notes),
            }
            business = {
                key_name: record[key_name]
                for key_name in ("rate_kind", "rate_raw", "rate_percent", "rate_state",
                                 "effective_from", "effective_to", "product_name")
            }
            business_fp = fingerprint(business)
            key = (versioning.tax_version, code)
            existing = seen.get(key)
            if existing is None:
                seen[key] = {
                    "fingerprint": business_fp,
                    "payload": record,
                    "locator": locator,
                }
                outcome.count(RecordStatus.IMPORTED)
                if from_raw and parse_iso_date(from_raw) is not None:
                    from_dates.append(from_raw)
                if to_raw and parse_iso_date(to_raw) is not None:
                    to_dates.append(to_raw)
            else:
                # 同键只能记一个状态：内容一致=重复，内容不同=冲突；
                # 记两个会让“输入数 = 各状态合计”的对账失败。
                if existing["fingerprint"] == business_fp:
                    outcome.count(RecordStatus.DUPLICATE)
                else:
                    outcome.count(RecordStatus.CONFLICT)
                    build.outcomes.append(
                        RecordOutcome(
                            dataset=DatasetId.P17_TAX_RATES,
                            status=RecordStatus.CONFLICT,
                            business_key=code,
                            locator=locator,
                            reason=(
                                "同一税号在两个来源里退税率不同；已保留先出现的一条，"
                                "需按政策原文确认，不能由程序挑一个"
                            ),
                            issues=[
                                RecordIssue(
                                    "TAX_RATE_CONFLICT",
                                    "同一税号同版本出现不同退税率",
                                    "出口退税率",
                                    severity="pending",
                                )
                            ],
                        )
                    )

    build.payload = _payload(
        versioning=versioning,
        kind=manifest.kind,
        seen=seen,
        documents=documents,
        urls=urls,
        retrieved=retrieved,
        from_dates=from_dates,
        to_dates=to_dates,
        levels=levels,
    )
    if not seen:
        outcome.status = DatasetStatus.INPUT_MISSING
        outcome.reason = "没有可解析的退税率列"
    else:
        outcome.notes.append(
            "匹配层级分布：" + "、".join(f"{key}={value}" for key, value in sorted(levels.items()))
        )
        if no_record_codes:
            outcome.notes.append(
                f"未生成记录的税号 {len(no_record_codes)} 个："
                + "、".join(no_record_codes[:10])
                + ("…" if len(no_record_codes) > 10 else "")
            )
    build.outcome = outcome
    build.assumptions = _assumptions(versioning, numeric_cells)
    return build


def _payload(
    *,
    versioning: Versioning,
    kind: DatasetKind,
    seen: Mapping[tuple[str, str], Mapping[str, Any]],
    documents: list[str],
    urls: list[str],
    retrieved: list[str],
    from_dates: list[str],
    to_dates: list[str],
    levels: Mapping[str, int],
) -> dict[str, Any] | None:
    if not seen:
        return None
    # 版本窗口：起点取最小的记录生效日，终点取最大的记录失效日；分别取，不混用。
    parsed_from = sorted({value for value in from_dates if parse_iso_date(value) is not None})
    parsed_to = sorted({value for value in to_dates if parse_iso_date(value) is not None})
    effective_from = parsed_from[0] if parsed_from else versioning.tariff_effective_from
    effective_to = parsed_to[-1] if parsed_to else None
    dataset = dataset_descriptor(
        dataset=DatasetId.P17_TAX_RATES,
        description=(
            "用户导出的出口退税率映射（国家税务总局出口退税率文库 2026C 版），"
            "含 10 位税号、退税率、有效期与核验状态；不含适用条件、征退税率差与关税税率。"
        ),
        document="、".join(sorted(set(documents))),
        url=urls[0] if urls else None,
        retrieved_at=retrieved[0] if retrieved else None,
        notes=(
            "由 P23 导入流水线生成；未复核。来源没有适用条件列，因此 conditions 字段未写"
            "（= 来源未声明条件），P17 不会给出“无条件适用”结论。"
            f"版本窗口 [{effective_from}, {effective_to or '未声明'}] 由本批记录的"
            "生效/失效日期极值推导，不是公告生效日。"
        ),
        kind=kind,
    )
    dataset["dataset_id"] = versioning.dataset_id(DatasetId.P17_TAX_RATES)
    return {
        "schema_version": "p17.taxrate.1",
        "dataset": dataset,
        "versions": [
            {
                "version": versioning.tax_version,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "label": (
                    "国家税务总局出口退税率文库 "
                    f"{versioning.tax_version} 版（版本窗口按记录生效/失效日期极值推导，"
                    "不是公告生效日；匹配层级分布见导入报告）"
                ),
            }
        ],
        "rates": [entry["payload"] for entry in seen.values()],
    }


def _assumptions(versioning: Versioning, numeric_cells: list[str]) -> list[str]:
    assumptions = [
        f"tax_version：{versioning.tax_version} 取自来源声明的“国家税务总局出口退税率文库 2026C 版”",
        "tax_rate_percent_from_cell_format：退税率单元格是小数（如 0.13），"
        "只有该单元格数字格式为百分数时才乘 100；格式不符/读不出则拒绝该行",
        "tax_rate_granularity_8digit：匹配层级为“8位回退”的行按来源逐行映射到该 10 位税号，"
        "并在 note/证据里标注“文库按 8 位码命中”，不视为文库存在 10 位条目",
        "tax_conditions_not_declared：来源无适用条件列，conditions 字段不写（非 []），"
        "因此复核后仍需专家补政策条件才能得到“适用”结论",
        "tax_state_no_abolish_inference：来源未声明作废；过期记录记 unknown，不替来源宣布废止",
    ]
    if numeric_cells:
        assumptions.append(
            f"numeric_cells：{len(numeric_cells)} 个税号单元格存成了数值但长度无损，已按原文采用"
        )
    return assumptions


__all__ = [
    "CHECK_MATCHED",
    "CHECK_NO_RECORD",
    "CHECK_SUBDIVIDED",
    "LEVEL_EXACT",
    "LEVEL_FALLBACK",
    "LEVEL_UNMATCHED",
    "TAX_SPEC",
    "TaxBuild",
    "build_tax_rates",
    "rate_from_cell",
    "tax_layout",
]
