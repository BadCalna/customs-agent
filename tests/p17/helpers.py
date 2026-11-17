"""P17 测试夹具。

权威元数据只用于验证“当上游确实提供已复核官方数据时”的判定逻辑，不是真实退税/监管
数据；端到端样例统一读 ``fixtures/`` 下的合成 JSON。任何输出都不得复制成业务依据。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapters.sources.hs_lookup import Authority, ReviewStatus
from adapters.sources.regulatory_lookup import (
    Direction,
    InMemoryRegulatoryStore,
    MeasureCategory,
    RecordState,
    RegulatoryDatasetInfo,
    RegulatorySourceKind,
)
from adapters.sources.tax_lookup import (
    InMemoryTaxRateStore,
    RateKind,
    RateState,
    RebateSourceKind,
    TaxRateDatasetInfo,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TAX_FIXTURE = FIXTURES / "tax_rates_synthetic.json"
REGULATORY_FIXTURE = FIXTURES / "regulatory_synthetic.json"

FIXED_CLOCK = lambda: datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)

#: 版本窗口：2025/2026 各一段，用于“日期 → 版本”解析。
VERSIONS = [
    {"version": "2025", "effective_from": "2025-01-01", "effective_to": "2025-12-31"},
    {"version": "2026", "effective_from": "2026-01-01", "effective_to": "2026-12-31"},
]

SOURCE = {
    "system": "p17-unit-fixture",
    "document": "P17-UNIT-FIXTURE",
    "locator": "测试夹具第 1 条",
    "publisher": "单元测试",
    "retrieved_at": "2026-11-17T00:00:00+08:00",
}

#: 用于“字段缺省”与“字段为空”区分；JSON 里少写一个键与写空数组不是一回事。
OMIT = object()


def _meta(
    *,
    source_type: str,
    authority: Authority,
    reviewed: bool,
    dataset_id: str,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "source_type": source_type,
        "authority": authority.value if isinstance(authority, Authority) else str(authority),
        "synthetic": True if source_type == "synthetic" else False,
        "description": "P17 单元测试夹具，不是真实退税/监管数据",
        "document": "P17-UNIT-FIXTURE",
        "retrieved_at": "2026-11-17T00:00:00+08:00",
        "review_status": (ReviewStatus.REVIEWED if reviewed else ReviewStatus.UNREVIEWED).value,
        "reviewed_by": "fixture-reviewer" if reviewed else "",
        "reviewed_at": "2026-11-17T00:00:00+08:00" if reviewed else None,
    }


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


def tax_dataset(
    *,
    kind: RebateSourceKind = RebateSourceKind.SYNTHETIC,
    authority: Authority = Authority.SYNTHETIC,
    reviewed: bool = False,
) -> TaxRateDatasetInfo:
    return TaxRateDatasetInfo.model_validate(
        _meta(
            source_type=kind.value,
            authority=authority,
            reviewed=reviewed,
            dataset_id="p17-tax-unit-fixture",
        )
    )


def official_tax_dataset(*, reviewed: bool = True) -> TaxRateDatasetInfo:
    return tax_dataset(
        kind=RebateSourceKind.OFFICIAL_TAX,
        authority=Authority.AUTHORITATIVE,
        reviewed=reviewed,
    )


def regulatory_dataset(
    *,
    kind: RegulatorySourceKind = RegulatorySourceKind.SYNTHETIC,
    authority: Authority = Authority.SYNTHETIC,
    reviewed: bool = False,
) -> RegulatoryDatasetInfo:
    return RegulatoryDatasetInfo.model_validate(
        _meta(
            source_type=kind.value,
            authority=authority,
            reviewed=reviewed,
            dataset_id="p17-regulatory-unit-fixture",
        )
    )


def official_regulatory_dataset(*, reviewed: bool = True) -> RegulatoryDatasetInfo:
    return regulatory_dataset(
        kind=RegulatorySourceKind.OFFICIAL_REGULATORY,
        authority=Authority.AUTHORITATIVE,
        reviewed=reviewed,
    )


# ---------------------------------------------------------------------------
# 记录构造
# ---------------------------------------------------------------------------


def rate_record(**overrides: Any) -> dict[str, Any]:
    """构造一条退税率记录；``conditions=OMIT`` 表示“来源未写该字段”。"""

    record: dict[str, Any] = {
        "version": "2026",
        "hs_code": "8482102000",
        "product_name": "单元测试商品",
        "rate_kind": RateKind.EXPORT_REBATE.value,
        "rate_raw": "13%",
        "rate_percent": 13,
        "rate_state": RateState.IN_FORCE.value,
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "conditions": [],
        "source": dict(SOURCE),
    }
    conditions = overrides.pop("conditions", [])
    record.update(overrides)
    if conditions is not OMIT:
        record["conditions"] = conditions
    else:
        record.pop("conditions", None)
    return record


def condition(
    text: str = "合成条件原文",
    *,
    document: str | None = "P17-UNIT-NOTICE",
    locator: str | None = "第一条",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text}
    if document is not None:
        payload["document"] = document
    if locator is not None:
        payload["locator"] = locator
    return payload


def regulatory_record(**overrides: Any) -> dict[str, Any]:
    """构造一条监管记录；``measures=OMIT`` 表示“来源未写该字段”。"""

    record: dict[str, Any] = {
        "version": "2026",
        "hs_code": "8482102000",
        "direction": Direction.IMPORT.value,
        "product_name": "单元测试商品",
        "state": RecordState.IN_FORCE.value,
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "measures": [measure()],
        "source": dict(SOURCE),
    }
    measures = overrides.pop("measures", [measure()])
    record.update(overrides)
    if measures is not OMIT:
        record["measures"] = measures
    else:
        record.pop("measures", None)
    return record


def measure(
    code: str = "A",
    *,
    category: MeasureCategory = MeasureCategory.LICENCE,
    name: str = "入境货物通关单（合成）",
    agency: str = "海关（合成）",
    document: str | None = "P17-UNIT-REGTABLE",
    locator: str | None = "代码表第 A 行",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "category": category.value, "name": name, "agency": agency}
    if document is not None:
        payload["document"] = document
    if locator is not None:
        payload["locator"] = locator
    return payload


def dual_use_item(
    list_name: str = "两用物项出口管制清单（合成）",
    *,
    document: str | None = "P17-UNIT-DUALUSE",
    locator: str | None = "清单第 1 条",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "list_name": list_name,
        "item_description": "合成条目描述",
        "technical_criteria": "合成技术门槛",
    }
    if document is not None:
        payload["document"] = document
    if locator is not None:
        payload["locator"] = locator
    return payload


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def tax_store(
    records: list[dict[str, Any]] | None = None,
    *,
    versions: list[dict[str, Any]] | None = None,
    dataset: TaxRateDatasetInfo | None = None,
) -> InMemoryTaxRateStore:
    return InMemoryTaxRateStore(
        records if records is not None else [rate_record()],
        versions if versions is not None else VERSIONS,
        dataset=dataset or official_tax_dataset(),
    )


def regulatory_store(
    records: list[dict[str, Any]] | None = None,
    *,
    versions: list[dict[str, Any]] | None = None,
    dataset: RegulatoryDatasetInfo | None = None,
) -> InMemoryRegulatoryStore:
    return InMemoryRegulatoryStore(
        records if records is not None else [regulatory_record()],
        versions if versions is not None else VERSIONS,
        dataset=dataset or official_regulatory_dataset(),
    )
