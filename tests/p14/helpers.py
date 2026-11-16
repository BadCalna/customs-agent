"""P14 测试夹具。

权威元数据只用于验证“当上游确实提供已复核官方数据时”的判定逻辑，不是真实海关数据，
不得复制到运行环境作为业务依据。端到端样例统一读 fixtures 下的合成 JSON。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from adapters.sources.declaration_elements import InMemoryElementStore
from adapters.sources.hs_lookup import (
    Authority,
    DatasetInfo,
    InMemoryTariffStore,
    ReviewStatus,
    SourceKind,
    SourceRef,
)

FIXED_CLOCK = lambda: datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)

VERSIONS = [
    {"version": "2025", "effective_from": "2025-01-01", "effective_to": "2025-12-31"},
    {"version": "2026", "effective_from": "2026-01-01", "effective_to": "2026-12-31"},
]

SOURCE = {
    "system": "p14-unit-fixture",
    "document": "P14-UNIT-FIXTURE",
    "locator": "测试夹具第 1 条",
    "publisher": "单元测试",
    "retrieved_at": "2026-11-16T00:00:00+08:00",
}


def dataset(*, kind: SourceKind, authority: Authority, reviewed: bool = False) -> DatasetInfo:
    return DatasetInfo(
        dataset_id="p14-unit-fixture",
        source_type=kind,
        authority=authority,
        synthetic=(True if kind is SourceKind.SYNTHETIC else False),
        description="P14 单元测试夹具，不是真实税则或规范申报目录",
        document="P14-UNIT-FIXTURE",
        retrieved_at="2026-11-16T00:00:00+08:00",
        review_status=ReviewStatus.REVIEWED if reviewed else ReviewStatus.UNREVIEWED,
        reviewed_by="fixture-reviewer" if reviewed else "",
        reviewed_at="2026-11-16T00:00:00+08:00" if reviewed else None,
    )


def official_tariff_dataset(*, reviewed: bool = True) -> DatasetInfo:
    return dataset(kind=SourceKind.OFFICIAL_TARIFF, authority=Authority.AUTHORITATIVE, reviewed=reviewed)


def official_elements_dataset(*, reviewed: bool = True) -> DatasetInfo:
    return dataset(
        kind=SourceKind.OFFICIAL_DECLARATION,
        authority=Authority.AUTHORITATIVE,
        reviewed=reviewed,
    )


def synthetic_dataset(kind: SourceKind = SourceKind.SYNTHETIC) -> DatasetInfo:
    if kind is SourceKind.SYNTHETIC:
        return dataset(kind=kind, authority=Authority.SYNTHETIC)
    return dataset(kind=kind, authority=Authority.UNVERIFIED_THIRD_PARTY)


def tariff_code(code: str = "8544422900", **overrides: Any) -> dict[str, Any]:
    record = {
        "version": "2026",
        "code": code,
        "description": "P14 合成商品描述",
        "chapter": "85",
        "heading": "8544",
        "subheading": "854442",
        "state": "active",
        "source": dict(SOURCE),
    }
    record.update(overrides)
    return record


def tariff_store(*, codes=None, versions=None, info=None) -> InMemoryTariffStore:
    return InMemoryTariffStore(
        codes=codes if codes is not None else [tariff_code()],
        versions=versions if versions is not None else VERSIONS,
        dataset=info or official_tariff_dataset(),
    )


def element_spec(key: str, label: str, order: int, **overrides: Any) -> dict[str, Any]:
    spec = {
        "key": key,
        "label": label,
        "order": order,
        "role": "classification",
        "requirement": "required",
        "value_type": "text",
        "example": f"{label}示例",
    }
    spec.update(overrides)
    return spec


def default_specs() -> list[dict[str, Any]]:
    return [
        element_spec(
            "brand_type",
            "品牌类型",
            1,
            value_type="enum",
            enum_values=["无品牌", "境外品牌（其它）"],
        ),
        element_spec("usage", "用途", 2, aliases=["使用用途"]),
        element_spec("model", "型号", 3),
        element_spec(
            "brand",
            "品牌（中文或外文名称）",
            4,
            requirement="conditional",
            condition="品牌类型不是无品牌时",
            aliases=["品牌"],
            role="price",
        ),
        element_spec("gtin", "GTIN", 5, requirement="optional", role="customs"),
    ]


def element_schema(*, specs=None, code="8544422900", version="2026", **overrides: Any) -> dict[str, Any]:
    record = {
        "hs_code": code,
        "description": "P14 合成商品描述",
        "version": version,
        "elements": specs if specs is not None else default_specs(),
        "source": dict(SOURCE),
    }
    record.update(overrides)
    return record


def element_store(*, schemas=None, versions=None, info=None) -> InMemoryElementStore:
    return InMemoryElementStore(
        schemas=schemas if schemas is not None else [element_schema()],
        versions=versions if versions is not None else VERSIONS,
        dataset=info or official_elements_dataset(),
    )


def complete_declared() -> dict[str, str]:
    return {
        "brand_type": "无品牌",
        "usage": "工业设备连接",
        "model": "X-100",
        "brand": "无",
    }


def numbered_declared() -> str:
    return (
        "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;"
        "4:品牌（中文或外文名称）:无;5:GTIN:"
    )


def positional_declared() -> str:
    return "无品牌|工业设备连接|X-100|无|"


def source_ref() -> SourceRef:
    return SourceRef.model_validate(SOURCE)
