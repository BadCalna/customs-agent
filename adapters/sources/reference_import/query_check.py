"""用**实际适配器**验证导入产出的快照（P23）。

这一层专门防止“文件生成了”被写成“引擎查询已通过”：

- 每条检查都用 P14/P17/P23 的真实存储类读快照文件（``JsonTariffStore`` /
  ``JsonElementStore`` / ``JsonTaxRateStore`` / ``JsonCommentaryStore``），
  不走内存夹具；
- 代表性编码从真实数据里确定性挑选（排序后取第一个符合条件者），并把
  **请求、状态、来源定位**一起记进报告，方便人工复跑；
- 覆盖命中、查无、版本待核、格式非法、来源未配置、缺条件、零税率等等；
- 未接入适配器的数据（监管条件）单独记为 ``source_unconfigured``，不写成“已通过”。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

from adapters.sources.declaration_elements import (
    DeclarationElementsSource,
    ElementQuery,
    JsonElementStore,
)
from adapters.sources.hs_lookup import HsLookupSource, HsQuery, JsonTariffStore, QueryBasis
from adapters.sources.regulatory_lookup import (
    Direction,
    JsonRegulatoryStore,
    RegulatoryQuery,
    RegulatorySource,
)
from adapters.sources.tariff_commentary import (
    JsonCommentaryStore,
    TariffCommentarySource,
)
from adapters.sources.tax_lookup import JsonTaxRateStore, TaxRateQuery, TaxRateSource

from adapters.sources.reference_import.model import utc_now

#: 验证脚本的结构版本；结果被发布门禁读取，结构变化时递增。
VERIFICATION_SCHEMA_VERSION = "p23.query-check.1"


def _check(
    identifier: str,
    status: str,
    detail: str,
    *,
    request: Mapping[str, Any] | None = None,
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": identifier,
        "status": status,
        "detail": detail,
        "request": dict(request or {}),
        "observed": dict(observed or {}),
    }
    return payload


def _skip(identifier: str, detail: str) -> dict[str, Any]:
    return _check(identifier, "skip", detail)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _bump(day: str) -> str:
    """在版本窗口起点之后取一天，保证“申报日期落在版本有效期内”。"""

    try:
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    except ValueError:
        return day


def run_query_checks(snapshot_dir: str | Path) -> dict[str, Any]:
    """对快照目录跑一轮确定性查询验证，返回可直接写盘的报告。"""

    root = Path(snapshot_dir)
    checks: list[dict[str, Any]] = []
    dataset_paths: dict[str, str] = {}
    basis_date = "2026-11-17"
    tariff_codes: list[dict[str, Any]] = []
    schemas: list[dict[str, Any]] = []
    tax_records: list[dict[str, Any]] = []
    commentary_records: list[dict[str, Any]] = []

    tariff_path = root / "tariff.json"
    if tariff_path.is_file():
        dataset_paths["p14_tariff"] = tariff_path.name
        payload = _load(tariff_path)
        tariff_codes = sorted(payload.get("codes", []), key=lambda item: item["code"])
        window = (payload.get("versions") or [{}])[0]
        basis_date = _bump(str(window.get("effective_from") or basis_date))
        tariff_version = str(window.get("version") or "")
        checks.extend(
            _check_tariff(tariff_path, tariff_codes, basis_date, tariff_version)
        )
    else:
        checks.append(_skip("p14.tariff", "快照里没有 tariff.json（税则未导入）"))

    elements_path = root / "elements.json"
    if elements_path.is_file():
        dataset_paths["p14_elements"] = elements_path.name
        payload = _load(elements_path)
        schemas = sorted(payload.get("schemas", []), key=lambda item: item["hs_code"])
        elements_window = (payload.get("versions") or [{}])[0]
        checks.extend(
            _check_elements(
                elements_path,
                schemas,
                tariff_codes,
                basis_date,
                str(elements_window.get("version") or ""),
            )
        )
    else:
        checks.append(_skip("p14.elements", "快照里没有 elements.json（要素清单未导入）"))

    tax_path = root / "tax_rates.json"
    if tax_path.is_file():
        dataset_paths["p17_tax_rates"] = tax_path.name
        payload = _load(tax_path)
        tax_records = sorted(payload.get("rates", []), key=lambda item: item["hs_code"])
        tax_window = (payload.get("versions") or [{}])[0]
        checks.extend(
            _check_tax(
                tax_path,
                tax_records,
                basis_date,
                str(tax_window.get("version") or ""),
            )
        )
    else:
        checks.append(_skip("p17.tax_rates", "快照里没有 tax_rates.json（退税率未导入）"))

    # 监管条件：本批未提供可用来源，必须显式失败/未配置，不能写成通过。
    checks.extend(_check_regulatory())

    commentary_path = root / "commentary.json"
    if commentary_path.is_file():
        dataset_paths["tariff_commentary"] = commentary_path.name
        payload = _load(commentary_path)
        commentary_records = payload.get("records", [])
        checks.extend(_check_commentary(commentary_path, commentary_records))
    else:
        checks.append(
            _skip("p23.commentary", "快照里没有 commentary.json（品目注释未导入）")
        )

    passed = [item for item in checks if item["status"] == "pass"]
    failed = [item for item in checks if item["status"] == "fail"]
    skipped = [item for item in checks if item["status"] == "skip"]
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "snapshot_dir": root.name,
        "datasets": dataset_paths,
        "basis_date": basis_date,
        "checks": checks,
        "counts": {"pass": len(passed), "fail": len(failed), "skip": len(skipped)},
        "all_passed": not failed,
        "note": (
            "全部检查都用真实 JSON 存储类读取本快照；未接入适配器的数据（监管条件）"
            "记为 source_unconfigured，不写成“查询通过”。"
        ),
    }


# ---------------------------------------------------------------------------
# P14 税则
# ---------------------------------------------------------------------------


def _check_tariff(
    path: Path,
    codes: list[dict[str, Any]],
    basis_date: str,
    tariff_version: str,
) -> list[dict[str, Any]]:
    source = HsLookupSource(JsonTariffStore(path))
    checks: list[dict[str, Any]] = []
    basis = QueryBasis(declaration_date=basis_date, tariff_version=tariff_version)

    leading_zero = next((item for item in codes if item["code"].startswith("0")), None)
    if leading_zero:
        code = leading_zero["code"]
        result = source.lookup(HsQuery(hs_code_raw=code, basis=basis))
        evidence = result.evidence[0] if result.evidence else None
        ok = (
            result.evidence_status.value == "obtained"
            and result.format.normalized == code
            and result.code is not None
            and result.code.code == code
            and result.authoritative is False
            and result.declaration_ready is False
            and evidence is not None
            and evidence.document
            and evidence.locator
        )
        checks.append(
            _check(
                "p14.tariff.leading_zero_hit",
                "pass" if ok else "fail",
                (
                    f"前导零编码 {code} 命中，格式归一保留前导零；证据定位 "
                    f"{evidence.locator if evidence else '无'}"
                    if ok
                    else f"前导零编码 {code} 命中结果不符合预期"
                ),
                request={"hs_code_raw": code, "basis": basis.model_dump()},
                observed={
                    "query_status": result.query_status.value,
                    "evidence_status": (
                        result.evidence_status.value if result.evidence_status else None
                    ),
                    "normalized": result.format.normalized,
                    "code_state": result.code_state.value,
                    "declaration_ready": result.declaration_ready,
                    "evidence": (
                        {
                            "document": evidence.document,
                            "locator": evidence.locator,
                        }
                        if evidence
                        else None
                    ),
                },
            )
        )

    hit = next(
        (item for item in codes if len(item["code"]) == 10 and not item["code"].startswith("0")),
        codes[0] if codes else None,
    )
    if hit:
        code = hit["code"]
        eight = code[:8]
        result = source.lookup(HsQuery(hs_code_raw=eight, basis=basis))
        ok = (
            result.format.state.value == "subheading_only"
            and result.evidence_status.value == "not_found"
            and result.declaration_ready is False
            and any(issue.code == "HS_SUBHEADING_ONLY" for issue in result.issues)
        )
        checks.append(
            _check(
                "p14.tariff.subheading_only",
                "pass" if ok else "fail",
                f"8 位子目 {eight} 不按 10 位命中，且给出 HS_SUBHEADING_ONLY",
                request={"hs_code_raw": eight, "basis": basis.model_dump()},
                observed={
                    "format_state": result.format.state.value,
                    "evidence_status": result.evidence_status.value,
                    "declaration_ready": result.declaration_ready,
                    "issues": [issue.code for issue in result.issues],
                },
            )
        )

    result = source.lookup(HsQuery(hs_code_raw="9999999999", basis=basis))
    ok = (
        result.evidence_status.value == "not_found"
        and result.code is None
        and result.code_state.value == "not_found"
        and result.declaration_ready is False
    )
    checks.append(
        _check(
            "p14.tariff.unknown_code",
            "pass" if ok else "fail",
            "未收录编码 9999999999 只报 not_found，不判“编码无效”",
            request={"hs_code_raw": "9999999999", "basis": basis.model_dump()},
            observed={
                "evidence_status": result.evidence_status.value,
                "code_state": result.code_state.value,
                "reason": result.reason,
            },
        )
    )

    if hit:
        result = source.lookup(HsQuery(hs_code_raw=hit["code"]))
        ok = (
            result.evidence_status.value == "version_unverified"
            and result.declaration_ready is False
            and result.authoritative is False
        )
        checks.append(
            _check(
                "p14.tariff.missing_basis",
                "pass" if ok else "fail",
                "不给申报日期/版本时报 version_unverified，不代填今天",
                request={"hs_code_raw": hit["code"], "basis": None},
                observed={
                    "evidence_status": result.evidence_status.value,
                    "reason": result.reason,
                },
            )
        )

    result = source.lookup(HsQuery(hs_code_raw="85A", basis=basis))
    ok = result.query_status.value == "invalid_input" and result.evidence_status is None
    checks.append(
        _check(
            "p14.tariff.bad_format",
            "pass" if ok else "fail",
            "非数字编码是 invalid_input，不发起查询",
            request={"hs_code_raw": "85A", "basis": basis.model_dump()},
            observed={"query_status": result.query_status.value},
        )
    )
    return checks


# ---------------------------------------------------------------------------
# P14 申报要素
# ---------------------------------------------------------------------------


def _check_elements(
    path: Path,
    schemas: list[dict[str, Any]],
    codes: list[dict[str, Any]],
    basis_date: str,
    elements_version: str,
) -> list[dict[str, Any]]:
    source = DeclarationElementsSource(JsonElementStore(path))
    checks: list[dict[str, Any]] = []
    basis = QueryBasis(declaration_date=basis_date, tariff_version=elements_version)

    # 取一个真实清单，按来源要求把必填项全部填上，看它会不会在未复核时误报“充足”。
    target = next(
        (item for item in schemas if item.get("hs_code", "").startswith("0")), None
    ) or (schemas[0] if schemas else None)
    if target:
        code = target["hs_code"]
        declared = {
            element["key"]: "示例值"
            for element in target["elements"]
            if element.get("requirement") == "required"
        }
        result = source.check(
            ElementQuery(hs_code_raw=code, basis=basis, declared=declared)
        )
        evidence = result.evidence[0] if result.evidence else None
        ok = (
            result.query_status.value == "ok"
            and result.schema_status is not None
            and result.schema_status.value == "obtained"
            and result.sufficiency.value == "unverified"
            and result.supplement_items == []
            and result.authoritative is False
            and evidence is not None
            and evidence.document
            and evidence.locator
        )
        checks.append(
            _check(
                "p14.elements.full_but_unreviewed",
                "pass" if ok else "fail",
                (
                    f"编码 {code} 的必填项全部给出，但来源未复核，结论必须是 unverified "
                    "且不生成供应商补料"
                    if ok
                    else f"编码 {code} 未复核时给出了不该给的结论"
                ),
                request={"hs_code_raw": code, "declared_required": len(declared)},
                observed={
                    "query_status": result.query_status.value,
                    "schema_status": (
                        result.schema_status.value if result.schema_status else None
                    ),
                    "sufficiency": result.sufficiency.value,
                    "supplement_items": len(result.supplement_items),
                    "evidence": (
                        {"document": evidence.document, "locator": evidence.locator}
                        if evidence
                        else None
                    ),
                },
            )
        )

    # 目录里未匹配到要素清单的编码：必须查无，而不是“无需要素”。
    unmatched = [
        item["code"]
        for item in codes
        if item["code"] not in {schema["hs_code"] for schema in schemas}
    ]
    if unmatched:
        code = sorted(unmatched)[0]
        result = source.check(ElementQuery(hs_code_raw=code, basis=basis, declared={}))
        ok = (
            result.schema_record is None
            and result.schema_status is not None
            and result.schema_status.value == "not_found"
            and result.sufficiency.value == "unverified"
            and result.supplement_items == []
        )
        checks.append(
            _check(
                "p14.elements.no_schema_not_found",
                "pass" if ok else "fail",
                f"缺清单的编码 {code} 报 not_found，不判“无需申报要素”且不生成补料",
                request={"hs_code_raw": code, "basis": basis.model_dump()},
                observed={
                    "schema_status": (
                        result.schema_status.value if result.schema_status else None
                    ),
                    "sufficiency": result.sufficiency.value,
                },
            )
        )
    return checks


# ---------------------------------------------------------------------------
# P17 退税率
# ---------------------------------------------------------------------------


def _check_tax(
    path: Path,
    records: list[dict[str, Any]],
    basis_date: str,
    tax_version: str,
) -> list[dict[str, Any]]:
    source = TaxRateSource(JsonTaxRateStore(path))
    checks: list[dict[str, Any]] = []
    basis = QueryBasis(declaration_date=basis_date, tariff_version=tax_version)

    hit = next(
        (
            item
            for item in records
            if item.get("rate_kind") == "export_rebate" and item.get("rate_state") == "in_force"
        ),
        records[0] if records else None,
    )
    if hit:
        code = hit["hs_code"]
        result = source.lookup(
            TaxRateQuery(
                hs_code_raw=code,
                basis=basis,
                original_rate_raw=f"{hit['rate_percent']:g}",
            )
        )
        evidence = result.evidence[0] if result.evidence else None
        ok = (
            result.evidence_status.value == "obtained"
            and result.record is not None
            and result.looked_up_rate is not None
            and result.looked_up_rate.rate_percent == hit["rate_percent"]
            # 来源没有条件列 → not_declared → 不能判“适用”
            and result.conditions_state.value == "not_declared"
            and result.applicability.value == "unknown"
            and result.usable_as_basis is False
            and result.expert_review_required is True
            and evidence is not None
            and evidence.document
            and evidence.locator
        )
        checks.append(
            _check(
                "p17.tax.hit_but_conditions_undeclared",
                "pass" if ok else "fail",
                (
                    f"税号 {code} 命中退税率 {hit['rate_percent']:g}%，但来源未声明条件 → "
                    "applicability=unknown、不可作依据"
                    if ok
                    else f"税号 {code} 的退税结论不符合预期"
                ),
                request={"hs_code_raw": code, "basis": basis.model_dump()},
                observed={
                    "evidence_status": result.evidence_status.value,
                    "looked_up_rate": (
                        result.looked_up_rate.model_dump()
                        if result.looked_up_rate
                        else None
                    ),
                    "conditions_state": result.conditions_state.value,
                    "applicability": result.applicability.value,
                    "usable_as_basis": result.usable_as_basis,
                    "issues": [issue.code for issue in result.issues],
                    "evidence": (
                        {"document": evidence.document, "locator": evidence.locator}
                        if evidence
                        else None
                    ),
                },
            )
        )

    zero = next((item for item in records if item.get("rate_kind") == "zero_rate"), None)
    if zero:
        code = zero["hs_code"]
        result = source.lookup(TaxRateQuery(hs_code_raw=code, basis=basis))
        ok = (
            result.looked_up_rate is not None
            and result.looked_up_rate.rate_kind.value == "zero_rate"
            and result.looked_up_rate.rate_percent == 0
        )
        checks.append(
            _check(
                "p17.tax.zero_rate_is_not_blank",
                "pass" if ok else "fail",
                f"税号 {code} 的 0 是 zero_rate（明确零税率），不是空白也不是“不予退税”",
                request={"hs_code_raw": code, "basis": basis.model_dump()},
                observed={
                    "rate_kind": (
                        result.looked_up_rate.rate_kind.value
                        if result.looked_up_rate
                        else None
                    ),
                    "rate_percent": (
                        result.looked_up_rate.rate_percent
                        if result.looked_up_rate
                        else None
                    ),
                },
            )
        )

    result = source.lookup(TaxRateQuery(hs_code_raw="9999999999", basis=basis))
    ok = (
        result.evidence_status.value == "not_found"
        and result.looked_up_rate is None
        and any(issue.code == "TAX_RATE_NOT_FOUND" for issue in result.issues)
    )
    checks.append(
        _check(
            "p17.tax.not_found",
            "pass" if ok else "fail",
            "未收录税号只报 not_found，不补 0%",
            request={"hs_code_raw": "9999999999", "basis": basis.model_dump()},
            observed={
                "evidence_status": result.evidence_status.value,
                "looked_up_rate": None if result.looked_up_rate is None else "非空",
            },
        )
    )

    if hit:
        result = source.lookup(
            TaxRateQuery(hs_code_raw=hit["hs_code"], original_rate_raw="")
        )
        ok = (
            result.rate_comparison.status.value == "not_compared"
            and any(issue.code == "TAX_ORIGINAL_RATE_BLANK" for issue in result.issues)
        )
        checks.append(
            _check(
                "p17.tax.original_blank_not_zero",
                "pass" if ok else "fail",
                "供应商原表退税率空白时只报 not_compared，不按 0% 比对",
                request={"hs_code_raw": hit["hs_code"], "original_rate_raw": ""},
                observed={
                    "rate_comparison": result.rate_comparison.status.value,
                    "original_state": result.original_rate.state.value,
                },
            )
        )

    result = source.lookup(TaxRateQuery(hs_code_raw=(hit or {}).get("hs_code", "0101210010")))
    ok = result.evidence_status.value == "version_unverified"
    checks.append(
        _check(
            "p17.tax.missing_basis",
            "pass" if ok else "fail",
            "不给版本/日期时报 version_unverified，不代填",
            request={"hs_code_raw": (hit or {}).get("hs_code", "0101210010")},
            observed={"evidence_status": result.evidence_status.value},
        )
    )
    return checks


# ---------------------------------------------------------------------------
# P17 监管条件（本批未提供）
# ---------------------------------------------------------------------------


def _check_regulatory() -> list[dict[str, Any]]:
    source = RegulatorySource(JsonRegulatoryStore("/nonexistent/regulatory.json"))
    result = source.lookup(
        RegulatoryQuery(
            hs_code_raw="8482102000",
            direction=Direction.EXPORT,
            basis=QueryBasis(declaration_date="2026-11-17"),
        )
    )
    ok = (
        result.query_status.value == "source_unreadable"
        and result.evidence_status.value == "query_failed"
        and result.outcome.value == "unknown"
        and result.measures == []
    )
    detail = (
        "本批没有监管条件表：读取失败必须报 query_failed/outcome=unknown，"
        "不能给“无监管”结论"
    )
    # 未配置环境变量时更早失败（source_unconfigured），两种都算“明确不可用”，
    # 但必须是失败态而不是通过态。
    if result.query_status.value == "source_unconfigured":
        ok = (
            result.evidence_status.value == "query_failed"
            and result.outcome.value == "unknown"
        )
        detail = "未配置监管条件来源：报 source_unconfigured/query_failed，不编造监管结论"
    return [
        _check(
            "p17.regulatory.not_available",
            "pass" if ok else "fail",
            detail,
            request={
                "hs_code_raw": "8482102000",
                "direction": "export",
                "basis": {"declaration_date": "2026-11-17"},
            },
            observed={
                "query_status": result.query_status.value,
                "evidence_status": (
                    result.evidence_status.value if result.evidence_status else None
                ),
                "outcome": result.outcome.value,
            },
        )
    ]


# ---------------------------------------------------------------------------
# P23 品目注释
# ---------------------------------------------------------------------------


def _check_commentary(
    path: Path, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source = TariffCommentarySource(JsonCommentaryStore(path))
    checks: list[dict[str, Any]] = []
    headings = sorted(
        (item for item in records if item.get("kind") == "品目"),
        key=lambda item: item["tariff_no"],
    )
    target = headings[0] if headings else None
    if target:
        result = source.lookup(target["tariff_no"])
        evidence = result.evidence[0] if result.evidence else None
        ok = (
            result.query_status.value == "ok"
            and result.evidence_status.value == "obtained"
            and result.records
            and result.records[0].tariff_no == target["tariff_no"]
            and result.engine_integration == "not_wired_p16"
            and evidence is not None
            and evidence.document
            and evidence.locator
        )
        checks.append(
            _check(
                "p23.commentary.heading_hit",
                "pass" if ok else "fail",
                (
                    f"品目 {target['tariff_no']} 取到 {len(result.records[0].sections)} 段注释；"
                    "engine_integration=not_wired_p16"
                    if ok
                    else f"品目 {target['tariff_no']} 注释结果不符合预期"
                ),
                request={"key_raw": target["tariff_no"]},
                observed={
                    "evidence_status": result.evidence_status.value,
                    "sections": [item.label for item in result.records[0].sections]
                    if result.records
                    else [],
                    "engine_integration": result.engine_integration,
                    "evidence": (
                        {"document": evidence.document, "locator": evidence.locator}
                        if evidence
                        else None
                    ),
                },
            )
        )
        # 关键词必须从快照内容里确定性挑选（不能在代码里写死某个词，
        # 否则换一批注释就会误判失败，进而错误地挡住发布）。
        target_text = target.get("text_all") or ""
        keyword = ""
        for run in __import__("re").findall(r"\S{4,}", target_text):
            keyword = run[:6]
            break
        if keyword:
            result = source.lookup({"keyword": keyword, "limit": 5})
            ok = (
                result.evidence_status.value == "obtained"
                and any(
                    item.tariff_no == target["tariff_no"] for item in result.records
                )
            )
            checks.append(
                _check(
                    "p23.commentary.keyword_search",
                    "pass" if ok else "fail",
                    "关键词子串检索命中，返回带定位的条目",
                    request={"keyword": keyword, "limit": 5},
                    observed={
                        "evidence_status": result.evidence_status.value,
                        "records": [item.tariff_no for item in result.records][:5],
                    },
                )
            )
        else:
            checks.append(
                _skip(
                    "p23.commentary.keyword_search",
                    "注释正文太短，无法从内容里确定性地取关键词",
                )
            )

    # 10 位编码不能被当成品目注释的查询键
    code = (records and next(
        (
            item["tariff_no"]
            for item in records
            if len(item.get("tariff_no", "")) == 4
        ),
        "0101",
    )) or "0101"
    result = source.lookup({"key_raw": f"{code}010010"[:10]})
    ok = (
        result.query_status.value == "invalid_input"
        and result.evidence_status is None
        and any("8/10 位" in (issue.message or "") for issue in result.issues)
    )
    checks.append(
        _check(
            "p23.commentary.rejects_declaration_code",
            "pass" if ok else "fail",
            "传 10 位申报编码 → invalid_input，说明注释层级键不能用来判断编码有效性",
            request={"key_raw": f"{code}010010"[:10]},
            observed={
                "query_status": result.query_status.value,
                "issues": [issue.code for issue in result.issues],
            },
        )
    )

    result = source.lookup("9999")
    ok = (
        result.query_status.value == "ok"
        and result.evidence_status.value == "not_found"
        and not result.records
    )
    checks.append(
        _check(
            "p23.commentary.not_found",
            "pass" if ok else "fail",
            "未收录品目报 not_found，不编造注释",
            request={"key_raw": "9999"},
            observed={"evidence_status": result.evidence_status.value},
        )
    )
    return checks


__all__ = ["VERIFICATION_SCHEMA_VERSION", "run_query_checks"]
