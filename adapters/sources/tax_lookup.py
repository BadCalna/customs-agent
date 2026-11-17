"""出口退税率来源适配（P17）。

定位：只回答“这个编码在某期退税政策里对应什么退税率/处理方式、适用条件与日期是什么”，
不替引擎下结论
-----------------------------------------------------------------------------
本模块是**事实来源**，不是退税判定，也不是审批：

- **不接收图片**、不做 OCR：退税率不能从图片推得，也不能由关税税率换算
  （“关税税率不等于出口退税率”是一次性红线）。输入里携带关税税率/图片字段的请求
  直接按 ``invalid_input`` 拒绝并说明原因，不“忽略后照常算”。
- **三块分开输出**，绝不合并成一个“税率结论”：
  ``original_rate``（供应商原表退税率，原样保留）、``looked_up_rate``（来源查询值）、
  ``rate_comparison``（**机械比对**，不是 AI 判断、不代表审批）。
- 缺申报日期 / 缺适用条件 / 来源失败 / 版本待核，一律保持待核，
  **绝不写成“零税率”“不予退税”“无退税”**。
- 不虚构文号、条款、税率；不内置“退税代码→含义”的记忆表——处理方式与条件只来自来源原文。
- 不访问网络、不读数据库、不调用模型；数据经可替换的 :class:`TaxRateStore` 注入
  （默认读环境变量指向的本地 JSON）。与 P14 的海关税则**分库管理**：
  退税率来自税务/财政体系，不能与海关税则混库（数据源清单第 7 节第 3 条）。

“退税率”“零税率”“不予退税”是三种不同的事实
------------------------------------------
``rate_kind`` 是**来源声明的处理方式**，``rate_percent`` 只在数值型处理方式下存在：

==================  ==================  ==============  ==========================
来源声明             ``rate_kind``       ``rate_percent``  含义
==================  ==================  ==============  ==========================
具体退税率           ``export_rebate``   13.0            退税率 13%
明确零税率           ``zero_rate``       0.0             零税率（不是“没查到”）
免税                 ``exempt``          ``null``        免税；不是 0% 退税率
不予退税             ``not_refundable``  ``null``        来源明确不予退税；不是 0%
不适用出口退税       ``not_applicable``  ``null``        不适用；不是 0%
来源未说明           ``unknown``         ``null``        待核；不得补成任何数值
==================  ==================  ==============  ==========================

因此“查不到记录”只会得到 ``record=None`` + ``looked_up_rate=None`` + ``not_found``，
不会得到 0%；原表退税率空白也只会是 ``original_rate.state=blank``，不会变成 0%。

四态与版本
---------
D4 的 ``obtained / not_found / query_failed / version_unverified`` 与结果一一对应，
且版本基准沿用 P14 的 :class:`QueryBasis`（**系统不填拟申报日期**）：

=================  ==================================  ================================
情形                ``evidence_status``                 说明
=================  ==================================  ================================
输入不合法          ``None``                            没有发起查询
来源未配置/读不了    ``query_failed``                    技术失败，可重试
取到记录 + 版本已核  ``obtained``                        可作为该版本的事实
取到记录 + 版本待核  ``version_unverified``              数据取到了，但版本不确认
未取到 + 版本已核    ``not_found``                       是“本期未收录”，不是“不予退税”
未取到 + 版本待核    ``version_unverified``              不能宣称不予退税/零税率
=================  ==================================  ================================

适用性（``applicability``）与证据
--------------------------------
``applies`` 只在“来源权威 + 已复核 + 版本已核 + 申报日期落在记录有效期 + 来源明确声明
无条件限制 + 处理方式与状态明确 + 适用退税代码核对一致”时出现；有未机读化的条件文本
就是 ``conditional``，缺日期/缺条件声明/来源非权威一律 ``unknown``，日期越界或政策废止
是 ``not_effective``。**任何非 ``applies`` 的结果都必须带至少一条 ``pending_reasons``**，
由 P18 转专家，不能读成“无风险”。

条件要素与 P14 同一口径：来源只给人类可读文本，本模块**不自动判断条件是否成立**；
要自动化必须先由关务专家把条件写成机读规则并另行评审。
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping, Protocol

from ninja import Schema
from pydantic import ConfigDict, Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus, IssueEntry

from .hs_lookup import (
    Authority,
    EffectiveStatus,
    HsFormat,
    HsFormatState,
    QueryBasis,
    QueryStatus,
    ReviewStatus,
    SourceRef,
    TariffStoreError,
    VersionResolution,
    VersionWindow,
    classify_hs_format,
    read_json_document,
    resolve_version,
    _parse_date,
    _strip_invisible,
)

#: 退税率数据文件的结构版本；结构变化时递增，旧格式明确报错而不是猜。
SCHEMA_VERSION = "p17.taxrate.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 退税率文件路径的环境变量。**没有默认样本**：未配置就明确报“来源未配置”，
#: 不能让演示数据在无人察觉时充当真实退税率文库。
ENV_TAX_RATE_PATH = "CUSTOMS_TAX_RATE_PATH"

_PERCENT = re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,2})?)\s*[%％]?$")

#: 原表/来源里常见的“非百分数”整值写法（全等匹配）。
_NON_PERCENT_EXACT = ("无", "—", "-", "/", "空", "")

#: 只做包含匹配的多字词；短符号不能包含匹配，否则 "13-17" 会被误判成“无”。
_NON_PERCENT_WORDS = (
    "免税",
    "零税率",
    "不退税",
    "不予退税",
    "免征",
    "不征",
    "不适用",
)

#: 输入里禁止出现的字段：关税税率不能推导出口退税率，图片不能当税率来源。
_FORBIDDEN_INPUT_KEYS: dict[str, tuple[str, str]] = {
    "tariff_rate": (
        "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED",
        "输入包含关税税率；关税税率不等于出口退税率，不能据此推导退税率",
    ),
    "customs_duty_rate": (
        "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED",
        "输入包含关税税率；关税税率不等于出口退税率，不能据此推导退税率",
    ),
    "import_tariff_rate": (
        "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED",
        "输入包含关税税率；关税税率不等于出口退税率，不能据此推导退税率",
    ),
    "export_tariff_rate": (
        "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED",
        "输入包含关税税率；关税税率不等于出口退税率，不能据此推导退税率",
    ),
    "duty_rate": (
        "TAX_RATE_DERIVED_FROM_TARIFF_REFUSED",
        "输入包含关税税率；关税税率不等于出口退税率，不能据此推导退税率",
    ),
    "tax_rate_from_image": (
        "TAX_RATE_FROM_IMAGE_REFUSED",
        "输入声明从图片取退税率；退税率不能从图片推得，必须来自可定位的政策来源",
    ),
    "image": (
        "TAX_RATE_FROM_IMAGE_REFUSED",
        "退税率查询不接受图片输入；图片只能支持可见事实，不能作为税率依据",
    ),
    "image_id": (
        "TAX_RATE_FROM_IMAGE_REFUSED",
        "退税率查询不接受图片输入；图片只能支持可见事实，不能作为税率依据",
    ),
    "ocr": (
        "TAX_RATE_FROM_IMAGE_REFUSED",
        "退税率查询不接受 OCR 文本作为依据；必须来自可定位的政策来源原文",
    ),
    "picture": (
        "TAX_RATE_FROM_IMAGE_REFUSED",
        "退税率查询不接受图片输入；图片只能支持可见事实，不能作为税率依据",
    ),
}



class StrictSchema(Schema):
    """拒绝未知字段的契约基类：拼错字段名或塞入任务外字段时报错，不静默忽略。"""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class RebateSourceKind(str, Enum):
    """退税/财政政策来源类型。第三方与合成数据在任何情况下都不能成为合规依据。"""

    OFFICIAL_TAX = "official_tax"
    OFFICIAL_FINANCE = "official_finance"
    THIRD_PARTY = "third_party"
    SYNTHETIC = "synthetic"


class RateKind(str, Enum):
    """来源声明的出口退税处理方式；决定 ``rate_percent`` 是否存在。"""

    EXPORT_REBATE = "export_rebate"
    ZERO_RATE = "zero_rate"
    EXEMPT = "exempt"
    NOT_REFUNDABLE = "not_refundable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


#: 有数值的 ``rate_kind``；其余取值一律 ``rate_percent=None``，不得补 0。
NUMERIC_RATE_KINDS = (RateKind.EXPORT_REBATE, RateKind.ZERO_RATE)


class RateState(str, Enum):
    """来源声明的政策状态；``not_found`` 不在此列——“查不到”由证据四态表达。"""

    IN_FORCE = "in_force"
    ABOLISHED = "abolished"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class ConditionsState(str, Enum):
    """适用条件的声明情况；“来源没写条件”不等于“无条件适用”。"""

    NOT_DECLARED = "not_declared"
    DECLARED_NONE = "declared_none"
    DECLARED_UNVERIFIED = "declared_unverified"


class OriginalRateState(str, Enum):
    """供应商原表退税率的解析状态；空白与 0 是两件事。"""

    BLANK = "blank"
    NUMERIC = "numeric"
    NON_NUMERIC = "non_numeric"
    UNPARSEABLE = "unparseable"


class ComparisonStatus(str, Enum):
    """原值与来源值的**机械**比对结果；不是 AI 判断，也不代表审批。"""

    NOT_COMPARED = "not_compared"
    MATCHED = "matched"
    CONFLICT = "conflict"
    UNVERIFIED = "unverified"


class TaxApplicability(str, Enum):
    """退税率能否作为该申报日期的业务依据。"""

    APPLIES = "applies"
    CONDITIONAL = "conditional"
    NOT_EFFECTIVE = "not_effective"
    UNKNOWN = "unknown"


class AccessMode(str, Enum):
    MANUAL_IMPORT = "manual_import"
    PUBLIC_PAGE = "public_page"
    OPEN_API = "open_api"
    INTERNAL_API = "internal_api"


class SourceAvailability(str, Enum):
    NOT_CONFIGURED = "not_configured"
    REQUIRES_AUTHORIZATION = "requires_authorization"
    UNREACHABLE = "unreachable"
    AVAILABLE = "available"


# ---------------------------------------------------------------------------
# 来源元数据（regulatory_lookup 复用）
# ---------------------------------------------------------------------------


class PolicyDatasetInfo(StrictSchema):
    """政策来源的数据集说明；``authoritative`` 是给下游的诚实标记。

    ``source_type`` 的具体取值由子类用 ``_allowed_kinds`` 限定：
    退税用 :class:`RebateSourceKind`，监管用
    :class:`adapters.sources.regulatory_lookup.RegulatorySourceKind`。
    ``official_*`` 前缀统一视为官方体系，其余按第三方/合成处理。
    """

    dataset_id: str = Field(default="", description="数据批次标识")
    source_type: str = Field(description="official_* / third_party / synthetic")
    authority: Authority = Field(
        default=Authority.UNSPECIFIED,
        description="authoritative 才可用于合规结论；未声明按 unspecified 处理",
    )
    synthetic: bool | None = Field(
        default=None, description="true=合成样本；false=调用方声明真实数据；null=未声明"
    )
    description: str = ""
    document: str = Field(default="", description="数据集级文件/公告标识")
    url: str | None = None
    retrieved_at: str | None = None
    review_status: ReviewStatus = Field(
        default=ReviewStatus.UNREVIEWED,
        description="官方文件导入后也要经关务人员复核，才能支撑业务结论",
    )
    reviewed_by: str = Field(default="", description="复核人标识；不进入模型输入")
    reviewed_at: str | None = None
    notes: str = ""

    _allowed_kinds: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _kind_allowed(self) -> "PolicyDatasetInfo":
        if self._allowed_kinds and self.source_type not in self._allowed_kinds:
            raise ValueError(
                f"source_type={self.source_type!r} 不属于本来源类型；"
                f"允许 {self._allowed_kinds}"
            )
        return self

    @model_validator(mode="after")
    def _authority_consistency(self) -> "PolicyDatasetInfo":
        official = self.source_type.startswith("official_")
        if self.source_type == "synthetic":
            if self.synthetic is False:
                raise ValueError("source_type=synthetic 时 synthetic 不能为 false")
            if self.authority is Authority.AUTHORITATIVE:
                raise ValueError("合成数据不能声明 authoritative")
        elif self.synthetic is True:
            raise ValueError("synthetic=true 只能与 source_type=synthetic 一起使用")
        if official and self.authority in (Authority.SYNTHETIC, Authority.UNVERIFIED_THIRD_PARTY):
            raise ValueError("官方来源不能声明 synthetic / unverified_third_party")
        if self.source_type == "third_party" and self.authority not in (
            Authority.UNVERIFIED_THIRD_PARTY,
            Authority.UNSPECIFIED,
        ):
            raise ValueError("第三方来源只能声明 unverified_third_party 或 unspecified")
        if self.review_status is ReviewStatus.REVIEWED:
            if not _strip_invisible(self.reviewed_by) or not self.reviewed_at:
                raise ValueError("reviewed 数据必须记录 reviewed_by 与 reviewed_at")
            if _parse_date(self.reviewed_at) is None:
                raise ValueError("reviewed_at 必须是 ISO 日期/时间")
            if _parse_date(self.retrieved_at) is None:
                raise ValueError("已复核数据的 retrieved_at 必须是 ISO 日期/时间")
        elif self.retrieved_at and _parse_date(self.retrieved_at) is None:
            raise ValueError("retrieved_at 必须是 ISO 日期/时间")
        return self

    @property
    def official(self) -> bool:
        return self.source_type.startswith("official_")

    @property
    def authoritative(self) -> bool:
        return (
            self.authority is Authority.AUTHORITATIVE
            and self.official
            and self.synthetic is not True
            and self.review_status is ReviewStatus.REVIEWED
        )


class TaxRateDatasetInfo(PolicyDatasetInfo):
    """退税率/退税政策来源的数据集说明。"""

    _allowed_kinds: ClassVar[tuple[str, ...]] = tuple(kind.value for kind in RebateSourceKind)


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


class TaxRateQuery(StrictSchema):
    """退税查询输入。刻意**不含**关税税率、图片、品名描述：

    - 关税税率不能换算退税率（要换算就不会有这条查询）；
    - 图片只支持可见事实，不能作为税率依据；
    - 本适配器不“从品名猜税号”，只按码查询。
    """

    hs_code_raw: str = Field(default="", description="待审 HS 编码原文；原样保留")
    rebate_code: str = Field(
        default="", description="适用退税代码（调用方给出的政策口径），没有就留空"
    )
    basis: QueryBasis = Field(default_factory=QueryBasis)
    original_rate_raw: str = Field(
        default="", description="供应商原表退税率原文；空白就是空白，不替换成 0%"
    )
    note: str = Field(default="", description="调用方备注，不进入证据定位")


class RebateCondition(Schema):
    """来源声明的一条适用条件；没有可定位文档就不能当证据。"""

    text: str = Field(min_length=1, description="条件原文；不改写、不概括成推断")
    document: str | None = Field(default=None, description="条件所在公告/文件标识")
    locator: str | None = Field(default=None, description="条款/页码定位")
    url: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "RebateCondition":
        if not _strip_invisible(self.text):
            raise ValueError("条件原文去除不可见字符后不能为空")
        has_doc = bool(_strip_invisible(self.document))
        has_loc = bool(_strip_invisible(self.locator))
        if has_doc != has_loc:
            raise ValueError("条件要么同时给 document 与 locator，要么都不给；不能只给一半")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("条件 url 必须以 http(s):// 开头；没有就留空，不编造")
        return self

    @property
    def located(self) -> bool:
        return bool(_strip_invisible(self.document)) and bool(_strip_invisible(self.locator))


class TaxRateRecord(Schema):
    """来源中的一条退税率记录（某政策版本、某税号）。"""

    version: str = Field(min_length=1)
    hs_code: str = Field(min_length=8, max_length=12, description="8 位税目/子目或 10 位申报编码")
    rebate_code: str = Field(default="", description="来源标注的适用退税代码")
    product_name: str = ""
    rate_kind: RateKind
    rate_raw: str = Field(default="", description="来源原文，必须保留")
    rate_percent: float | None = Field(
        default=None, description="归一后的百分数；非数值处理方式必须为空"
    )
    rate_state: RateState = RateState.UNKNOWN
    effective_from: str | None = None
    effective_to: str | None = None
    conditions: list[RebateCondition] | None = Field(
        default=None,
        description="null=来源未声明条件（待核）；[]=来源明确声明无附加条件；非空=有未机读化条件",
    )
    source: SourceRef
    note: str = ""

    @model_validator(mode="after")
    def _check_record(self) -> "TaxRateRecord":
        if not self.hs_code.isdigit() or len(self.hs_code) not in (8, 10):
            raise ValueError(f"退税记录的 hs_code 必须是 8 或 10 位数字：{self.hs_code!r}")
        if self.rate_kind in NUMERIC_RATE_KINDS:
            if self.rate_percent is None:
                raise ValueError(
                    f"rate_kind={self.rate_kind.value} 必须给出 rate_percent"
                )
            if not (0 <= self.rate_percent <= 100):
                raise ValueError(f"退税率必须在 0–100 之间：{self.rate_percent!r}")
            if self.rate_kind is RateKind.ZERO_RATE and abs(self.rate_percent) > 1e-9:
                raise ValueError("rate_kind=zero_rate 的 rate_percent 必须是 0")
            if self.rate_kind is RateKind.EXPORT_REBATE and abs(self.rate_percent) < 1e-9:
                raise ValueError(
                    "0% 属于零税率，必须用 rate_kind=zero_rate 表达；"
                    "否则会把“未取得/不予退税”和“零税率”混为一谈"
                )
        elif self.rate_percent is not None:
            raise ValueError(
                f"rate_kind={self.rate_kind.value} 不是数值型，rate_percent 必须为空（不得补 0）"
            )
        if self.rate_kind is not RateKind.UNKNOWN and not _strip_invisible(self.rate_raw):
            raise ValueError("来源声明了处理方式时 rate_raw 必须保留原文，否则无法回查")
        start = _parse_date(self.effective_from) if self.effective_from else None
        end = _parse_date(self.effective_to) if self.effective_to else None
        if self.effective_from and start is None:
            raise ValueError(f"记录 {self.hs_code} 的 effective_from 不是有效日期")
        if self.effective_to and end is None:
            raise ValueError(f"记录 {self.hs_code} 的 effective_to 不是有效日期")
        if start and end and end < start:
            raise ValueError(f"记录 {self.hs_code} 的结束日期早于开始日期")
        return self

    @property
    def conditions_state(self) -> ConditionsState:
        if self.conditions is None:
            return ConditionsState.NOT_DECLARED
        if not self.conditions:
            return ConditionsState.DECLARED_NONE
        return ConditionsState.DECLARED_UNVERIFIED


# ---------------------------------------------------------------------------
# 三块分开的输出
# ---------------------------------------------------------------------------


class OriginalRate(Schema):
    """供应商原表退税率：**只解析不修正**，空白不等于 0%。"""

    raw: str = ""
    state: OriginalRateState
    percent: float | None = Field(default=None, description="仅在 state=numeric 时存在")
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "OriginalRate":
        if (self.state is OriginalRateState.NUMERIC) != (self.percent is not None):
            raise ValueError("original_rate.percent 只在 numeric 状态存在")
        if self.state is OriginalRateState.BLANK and self.raw.strip():
            raise ValueError("blank 状态的原值必须是空白")
        return self


class LookedUpRate(Schema):
    """来源查询值：只描述来源声明了什么，不判断是否适用于本票业务。"""

    rate_kind: RateKind
    rate_percent: float | None = None
    rate_raw: str = ""
    rate_state: RateState = RateState.UNKNOWN
    rebate_code: str = ""

    @model_validator(mode="after")
    def _check(self) -> "LookedUpRate":
        if (self.rate_kind in NUMERIC_RATE_KINDS) != (self.rate_percent is not None):
            raise ValueError(
                f"rate_kind={self.rate_kind.value} 与 rate_percent 必须一致；"
                "非数值处理方式不得补 0"
            )
        return self


class RateComparison(Schema):
    """原值与查询值的机械比对；**不是** AI 判断，也不代表专家是否认可。"""

    status: ComparisonStatus
    original_percent: float | None = None
    looked_up_percent: float | None = None
    reason: str = ""
    produced_by: str = Field(
        default="deterministic_adapter",
        description="固定为 deterministic_adapter；AI 判断由 P18 另行记录，不在此字段",
    )

    @model_validator(mode="after")
    def _check(self) -> "RateComparison":
        if self.produced_by != "deterministic_adapter":
            raise ValueError("机械比对只能由适配器固定规则产生，不能标记为模型判断")
        both = self.original_percent is not None and self.looked_up_percent is not None
        if self.status in (ComparisonStatus.MATCHED, ComparisonStatus.CONFLICT) and not both:
            raise ValueError("matched/conflict 必须同时有原值与来源值")
        if self.status is ComparisonStatus.NOT_COMPARED and both:
            raise ValueError("两端都有数值时必须给出 matched 或 conflict，不能留 not_compared")
        return self


class TaxRateResult(Schema):
    """一次退税查询的完整结果；字段之间的一致性由校验器强制。"""

    query: TaxRateQuery | None = None
    query_status: QueryStatus
    evidence_status: EvidenceStatus | None = Field(
        default=None, description="D4 四态；输入不合法时为 null（未发起查询）"
    )
    format: HsFormat
    version: VersionResolution
    record: TaxRateRecord | None = None
    rate_state: RateState = RateState.UNKNOWN
    conditions_state: ConditionsState = ConditionsState.NOT_DECLARED
    conditions: list[RebateCondition] = Field(default_factory=list)
    effective_status: EffectiveStatus = EffectiveStatus.UNKNOWN
    applicability: TaxApplicability = TaxApplicability.UNKNOWN
    original_rate: OriginalRate
    looked_up_rate: LookedUpRate | None = None
    rate_comparison: RateComparison
    authoritative: bool = False
    usable_as_basis: bool = False
    expert_review_required: bool = True
    pending_reasons: list[str] = Field(default_factory=list, description="待核原因（人读）")
    reason: str = ""
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    dataset: TaxRateDatasetInfo | None = None
    searched_at: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "TaxRateResult":
        if self.usable_as_basis != (self.applicability is TaxApplicability.APPLIES):
            raise ValueError("usable_as_basis 必须与 applicability=applies 一致")
        if self.applicability is not TaxApplicability.APPLIES and not self.pending_reasons:
            raise ValueError("非 applies 的结果必须写明至少一条待核原因")
        if self.authoritative and self.dataset is None:
            raise ValueError("authoritative 必须由 dataset 计算，不能没有 dataset 就声明")

        if self.query_status is QueryStatus.INVALID_INPUT:
            if self.evidence_status is not None:
                raise ValueError("invalid_input 必须先于查询，evidence_status 必须为 null")
            if self.record is not None or self.looked_up_rate is not None:
                raise ValueError("invalid_input 不得携带来源记录或查询值")
            if self.evidence:
                raise ValueError("invalid_input 不得生成证据")
            if self.applicability is not TaxApplicability.UNKNOWN:
                raise ValueError("invalid_input 的适用性只能是 unknown")
            if self.rate_comparison.status is not ComparisonStatus.NOT_COMPARED:
                raise ValueError("invalid_input 不得声称已比对")
            if self.version.verified:
                raise ValueError("invalid_input 未发起查询，版本不能是 verified")
            if self.rate_state is not RateState.UNKNOWN:
                raise ValueError("invalid_input 不得声明政策状态")
            return self

        if self.query_status is not QueryStatus.OK:
            if self.evidence_status is not EvidenceStatus.QUERY_FAILED:
                raise ValueError(f"{self.query_status.value} 必须对应 query_failed")
            if self.record is not None or self.looked_up_rate is not None or self.evidence:
                raise ValueError("查询失败时不得携带记录、查询值或虚构证据")
            if self.applicability is not TaxApplicability.UNKNOWN:
                raise ValueError("查询失败时适用性只能是 unknown，不得读成无退税")
            if self.rate_comparison.status is not ComparisonStatus.NOT_COMPARED:
                raise ValueError("查询失败时不得声称已比对")
            return self

        if self.record is None:
            expected = (
                EvidenceStatus.NOT_FOUND
                if self.version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            )
            if self.evidence_status is not expected:
                raise ValueError(f"未取到记录时 evidence_status 必须是 {expected.value}")
            if self.looked_up_rate is not None or self.evidence:
                raise ValueError("未取到记录时不得携带查询值或虚构证据")
            if self.applicability is not TaxApplicability.UNKNOWN:
                raise ValueError("未取到记录时不得判断适用性，更不能当成零税率/不予退税")
            if self.rate_state is not RateState.UNKNOWN:
                raise ValueError("未取到记录时不得声明政策状态")
            if self.rate_comparison.status is not ComparisonStatus.NOT_COMPARED:
                raise ValueError("未取到来源值时不得声称已比对")
            if self.effective_status is not EffectiveStatus.UNKNOWN:
                raise ValueError("未取到记录时不得声明记录有效期状态")
            return self

        # --- 取到记录 ---
        record = self.record
        expected = (
            EvidenceStatus.OBTAINED if self.version.verified else EvidenceStatus.VERSION_UNVERIFIED
        )
        if self.evidence_status is not expected:
            raise ValueError(f"取到记录时 evidence_status 必须是 {expected.value}")
        if self.query is None or self.dataset is None:
            raise ValueError("取到记录时必须携带 query 与 dataset，不能省略来源元数据")
        if self.authoritative != self.dataset.authoritative:
            raise ValueError("authoritative 必须由 dataset 的来源/复核状态计算")
        if self.rate_state is not record.rate_state:
            raise ValueError("rate_state 必须与记录一致")
        expected_conditions = record.conditions_state
        if self.conditions_state is not expected_conditions:
            raise ValueError(
                f"conditions_state 必须由记录计算，期望 {expected_conditions.value}"
            )
        if [item.model_dump() for item in self.conditions] != [
            item.model_dump() for item in (record.conditions or [])
        ]:
            raise ValueError("conditions 必须与记录逐条一致，不能改写或补造条件")
        if self.looked_up_rate is None:
            raise ValueError("取到记录时必须给出 looked_up_rate（来源查询值）")
        if (
            self.looked_up_rate.rate_kind is not record.rate_kind
            or self.looked_up_rate.rate_percent != record.rate_percent
            or self.looked_up_rate.rate_raw != record.rate_raw
            or self.looked_up_rate.rate_state is not record.rate_state
            or self.looked_up_rate.rebate_code != record.rebate_code
        ):
            raise ValueError("looked_up_rate 必须逐字段镜像来源记录，不能人工改写")
        expected_effective = resolve_effective_status(record, self.query.basis, self.version)
        if self.effective_status is not expected_effective:
            raise ValueError(
                f"effective_status 必须由记录有效期与申报日期计算，期望 {expected_effective.value}"
            )
        if self.effective_status is not EffectiveStatus.UNKNOWN and not (
            self.query.basis.declaration_date or ""
        ).strip():
            raise ValueError("记录有效期状态必须有业务申报日期才能声明")
        expected_app = resolve_applicability(
            record,
            self.version,
            expected_effective,
            self.dataset,
            rebate_code_verified=rebate_code_matches(self.query, record),
            fmt=self.format,
        )
        if self.applicability is not expected_app:
            raise ValueError(f"applicability 必须由来源与基准计算，期望 {expected_app.value}")
        expected_comparison = compare_rates(self.original_rate, self.looked_up_rate)
        if (
            self.rate_comparison.status is not expected_comparison.status
            or self.rate_comparison.original_percent != expected_comparison.original_percent
            or self.rate_comparison.looked_up_percent != expected_comparison.looked_up_percent
        ):
            raise ValueError("rate_comparison 必须由机械比对规则产生，不能人工指定")
        if self.expert_review_required != resolve_expert_review(
            expected_app, expected_comparison
        ):
            raise ValueError("expert_review_required 必须按适用性与比对状态计算")

        located = [item for item in self.conditions if item.located]
        if len(self.evidence) != 1 + len(located):
            raise ValueError("证据必须为 1 条记录证据 + 每条可定位条件各 1 条")
        head = self.evidence[0]
        if head.status is not self.evidence_status:
            raise ValueError("记录证据状态必须与 evidence_status 一致")
        if head.evidence_id != f"tax:{record.version}:{record.hs_code}":
            raise ValueError("记录证据 ID 必须是 tax:<版本>:<税号>")
        for entry in self.evidence:
            if not _strip_invisible(entry.document) or not _strip_invisible(entry.locator):
                raise ValueError("证据必须带非空 document 与 locator，否则无法回查")
            for field in ("retrieved_at", "effective_from", "effective_to"):
                value = getattr(entry, field)
                if value and _parse_date(value) is None:
                    raise ValueError(f"证据的 {field} 必须是 ISO 日期/时间")
            if self.authoritative and not entry.retrieved_at:
                raise ValueError("权威来源的证据必须记录 retrieved_at（取得时间）")
        return self

    def to_evidence_entries(self) -> list[EvidenceEntry]:
        """返回证据条目；查无/失败不生成虚构条款，返回空列表。"""

        return list(self.evidence)


# ---------------------------------------------------------------------------
# 纯函数：解析、比对、有效期、适用性
# ---------------------------------------------------------------------------


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def parse_original_rate(raw: str | None) -> OriginalRate:
    """解析供应商原表退税率：只分类与取值，不补 0、不换算。"""

    original = "" if raw is None else str(raw)
    text = unicodedata.normalize("NFKC", original).strip()
    if not text:
        return OriginalRate(
            raw=original,
            state=OriginalRateState.BLANK,
            note="原表退税率空白：空白不是 0%，也不是“不予退税”；不自动补齐",
        )
    match = _PERCENT.match(text)
    if match:
        value = _decimal(match.group("num"))
        if value is None or value < 0 or value > 100:
            return OriginalRate(
                raw=original,
                state=OriginalRateState.UNPARSEABLE,
                note=f"原值 {text!r} 超出 0–100 的百分数范围；不截断、不猜测",
            )
        return OriginalRate(
            raw=original,
            state=OriginalRateState.NUMERIC,
            percent=float(value),
            note="原表百分数原样解析；是否与政策一致由比对结果说明，不代表审批",
        )
    collapsed = _strip_invisible(text)
    if collapsed in _NON_PERCENT_EXACT or any(
        word in collapsed for word in _NON_PERCENT_WORDS
    ):
        return OriginalRate(
            raw=original,
            state=OriginalRateState.NON_NUMERIC,
            note=f"原值 {text!r} 不是百分数；不能按 0% 处理，需人工比对政策原文",
        )
    return OriginalRate(
        raw=original,
        state=OriginalRateState.UNPARSEABLE,
        note=f"原值 {text!r} 无法确定为唯一百分数；不猜缺省值，转人工核对",
    )


def compare_rates(original: OriginalRate, looked_up: LookedUpRate | None) -> RateComparison:
    """机械比对原值与来源值；任一端不可比就明确说为什么，不默认“一致”。"""

    if looked_up is None:
        return RateComparison(
            status=ComparisonStatus.NOT_COMPARED,
            original_percent=original.percent,
            reason="未取得来源退税率，无法比对；不等于原值正确或错误",
        )
    if original.state is OriginalRateState.BLANK:
        return RateComparison(
            status=ComparisonStatus.NOT_COMPARED,
            looked_up_percent=looked_up.rate_percent,
            reason="原表退税率空白：不能按 0% 比对，也不能判为缺项以外的一致",
        )
    if original.state in (OriginalRateState.NON_NUMERIC, OriginalRateState.UNPARSEABLE):
        return RateComparison(
            status=ComparisonStatus.UNVERIFIED,
            original_percent=original.percent,
            looked_up_percent=looked_up.rate_percent,
            reason="原值无法确定为唯一百分数（例如免税/区间/多个值），转人工核对原文",
        )
    if looked_up.rate_percent is None:
        return RateComparison(
            status=ComparisonStatus.NOT_COMPARED,
            original_percent=original.percent,
            reason=(
                f"来源处理方式为 {looked_up.rate_kind.value}，不是百分数；"
                "不能按 0% 比对，需人工核对政策原文"
            ),
        )
    left = _decimal(str(original.percent))
    right = _decimal(str(looked_up.rate_percent))
    if left is not None and right is not None and left == right:
        return RateComparison(
            status=ComparisonStatus.MATCHED,
            original_percent=original.percent,
            looked_up_percent=looked_up.rate_percent,
            reason="原值与来源退税率数值一致（机械比对，不代表已审批）",
        )
    return RateComparison(
        status=ComparisonStatus.CONFLICT,
        original_percent=original.percent,
        looked_up_percent=looked_up.rate_percent,
        reason="原值与来源退税率数值不一致；需专家按政策原文判定，不由本模块裁定谁对",
    )


def _record_window(
    record: TaxRateRecord, version: VersionResolution
) -> tuple[date | None, date | None]:
    """记录自身有效期优先；记录完全没给日期时才退回版本窗口，不混用两套边界。"""

    if record.effective_from or record.effective_to:
        return _parse_date(record.effective_from), _parse_date(record.effective_to)
    if version.window is not None:
        return _parse_date(version.window.effective_from), _parse_date(
            version.window.effective_to
        )
    return None, None


def resolve_effective_status(
    record: TaxRateRecord, basis: QueryBasis, version: VersionResolution
) -> EffectiveStatus:
    """按业务申报日期判断记录是否在有效期内；没有日期就不冒充“已核对”。"""

    query_date = _parse_date(basis.declaration_date)
    if query_date is None:
        return EffectiveStatus.UNKNOWN
    start, end = _record_window(record, version)
    if start is not None and end is not None and start <= query_date <= end:
        return EffectiveStatus.EFFECTIVE
    if start is not None and query_date < start:
        return EffectiveStatus.NOT_YET_EFFECTIVE
    if end is not None and query_date > end:
        return EffectiveStatus.EXPIRED
    if start is None and end is None:
        # 记录与版本都没有给出任何有效期依据：不能兜底成“有效”。
        return EffectiveStatus.UNKNOWN
    # 只给了一端：另一端未知，不能判“整个区间有效”。
    return EffectiveStatus.UNKNOWN


def _norm_rebate_code(value: str | None) -> str:
    return _strip_invisible(unicodedata.normalize("NFKC", "" if value is None else str(value))).upper()


def rebate_code_matches(query: TaxRateQuery, record: TaxRateRecord) -> bool:
    """调用方给出的适用退税代码是否与来源记录一致；没给就按“不核对”处理。"""

    wanted = _norm_rebate_code(query.rebate_code)
    if not wanted:
        return True
    return _norm_rebate_code(record.rebate_code) == wanted


def resolve_applicability(
    record: TaxRateRecord,
    version: VersionResolution,
    effective: EffectiveStatus,
    dataset: TaxRateDatasetInfo | None,
    *,
    rebate_code_verified: bool,
    fmt: HsFormat,
) -> TaxApplicability:
    """判定退税率能否作为该申报日期的依据；任何不确定都不给 ``applies``。"""

    if dataset is None or not version.verified:
        return TaxApplicability.UNKNOWN
    if record.rate_kind is RateKind.UNKNOWN or record.rate_state is RateState.UNKNOWN:
        return TaxApplicability.UNKNOWN
    if record.rate_state in (RateState.ABOLISHED, RateState.SUPERSEDED):
        return TaxApplicability.NOT_EFFECTIVE
    if effective is EffectiveStatus.NOT_YET_EFFECTIVE or effective is EffectiveStatus.EXPIRED:
        return TaxApplicability.NOT_EFFECTIVE
    if effective is not EffectiveStatus.EFFECTIVE:
        return TaxApplicability.UNKNOWN
    if fmt.state is not HsFormatState.VALID:
        # 8 位子目只能说明该层级：文库若按 10 位维护，退税率可能不同。
        return TaxApplicability.UNKNOWN
    if record.conditions_state is ConditionsState.DECLARED_UNVERIFIED:
        return TaxApplicability.CONDITIONAL
    if record.conditions_state is not ConditionsState.DECLARED_NONE:
        return TaxApplicability.UNKNOWN
    if not rebate_code_verified:
        return TaxApplicability.UNKNOWN
    if not dataset.authoritative:
        return TaxApplicability.UNKNOWN
    return TaxApplicability.APPLIES


def resolve_expert_review(
    applicability: TaxApplicability, comparison: RateComparison
) -> bool:
    """是否需要转专家：只有“已核可依 **且** 数值一致”才不需要。

    ``not_compared``（原值空白、来源是非数值处理方式如“不予退税”）与 ``unverified``
    （原值无法确定为唯一百分数）都意味着“原表与政策的关系没被核清”，必须由专家看政策原文；
    早先只有 conflict/unverified 会转专家，导致“原表 13%、来源不予退税”被标成无需专家。
    """

    if applicability is not TaxApplicability.APPLIES:
        return True
    return comparison.status is not ComparisonStatus.MATCHED


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class SourceStoreError(RuntimeError):
    """政策来源数据不可读/结构不可用；调用方以 query_failed 表达，不能伪装成查无。"""


class SourceStoreNotConfigured(SourceStoreError):
    """没有配置任何政策来源。与“文件坏了”分开，提示先接入来源。"""


@dataclass(frozen=True)
class TaxRateSnapshot:
    dataset: TaxRateDatasetInfo
    versions: tuple[VersionWindow, ...]
    records: tuple[TaxRateRecord, ...]


class TaxRateStore(Protocol):
    """可替换的退税率存储：实现 ``load()`` 即可接本地文件、数据库或授权接口。"""

    def load(self) -> TaxRateSnapshot: ...


def default_tax_rate_path() -> Path | None:
    override = os.environ.get(ENV_TAX_RATE_PATH, "").strip()
    return Path(override).expanduser() if override else None


def read_policy_document(path: Path, *, label: str) -> Any:
    """读取 JSON 文件；把 P14 的存储异常统一成本层异常，错误信息不带绝对路径。"""

    try:
        return read_json_document(path, label=label)
    except TariffStoreError as exc:
        raise SourceStoreError(str(exc)) from exc


def _validate_schema_version(payload: Mapping[str, Any], *, label: str) -> None:
    version = payload.get("schema_version")
    if version is None:
        raise SourceStoreError(f"{label}文件缺少 schema_version，不能按未知结构静默导入")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SourceStoreError(
            f"{label}文件结构版本不受支持：{version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )


def _summarize_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError) and exc.errors():
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "input"
        return f"{location}: {first.get('msg', '输入不合法')}"
    return type(exc).__name__


def _dataset_from_payload(
    payload: Any, *, source_name: str
) -> TaxRateDatasetInfo:
    if payload is None:
        payload = {
            "dataset_id": source_name,
            "source_type": RebateSourceKind.SYNTHETIC.value,
            "authority": Authority.UNSPECIFIED.value,
            "synthetic": None,
            "description": "数据文件未声明 dataset；按未声明来源处理",
        }
    if not isinstance(payload, Mapping):
        raise SourceStoreError(f"退税率文件的 dataset 必须是对象：{source_name}")
    try:
        dataset = TaxRateDatasetInfo.model_validate(dict(payload))
    except ValidationError as exc:
        raise SourceStoreError(
            f"退税率文件的 dataset 描述不可用（{_summarize_validation_error(exc)}）"
        ) from exc
    if not dataset.dataset_id:
        dataset = dataset.model_copy(update={"dataset_id": source_name})
    return dataset


def _snapshot_from_payload(payload: Any, *, source_name: str) -> TaxRateSnapshot:
    if not isinstance(payload, Mapping):
        raise SourceStoreError(f"退税率文件顶层必须是对象：{source_name}")
    _validate_schema_version(payload, label="退税率")
    dataset = _dataset_from_payload(payload.get("dataset"), source_name=source_name)

    versions_raw = payload.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        raise SourceStoreError(f"退税率文件缺少非空 versions 数组：{source_name}")
    versions: list[VersionWindow] = []
    for index, item in enumerate(versions_raw):
        try:
            versions.append(VersionWindow.model_validate(item))
        except ValidationError as exc:
            raise SourceStoreError(
                f"第 {index + 1} 个退税政策版本不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
    seen_versions = {window.version for window in versions}
    if len(seen_versions) != len(versions):
        raise SourceStoreError(f"退税率文件存在重复的版本号：{source_name}")

    records_raw = payload.get("rates")
    if not isinstance(records_raw, list):
        raise SourceStoreError(f"退税率文件缺少 rates 数组：{source_name}")
    records: list[TaxRateRecord] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(records_raw):
        try:
            record = TaxRateRecord.model_validate(item)
        except ValidationError as exc:
            raise SourceStoreError(
                f"第 {index + 1} 条退税率记录不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
        if record.version not in seen_versions:
            raise SourceStoreError(
                f"第 {index + 1} 条退税率记录引用未声明的版本：{record.version}"
            )
        key = (record.version, record.hs_code)
        if key in seen:
            raise SourceStoreError(f"退税率记录重复：{record.version} / {record.hs_code}")
        seen.add(key)
        records.append(record)
    return TaxRateSnapshot(dataset=dataset, versions=tuple(versions), records=tuple(records))


class JsonTaxRateStore:
    """本地 JSON 退税率存储。文件格式见 D6 与回交文档。

    每次 ``load()`` 重新读文件，手工导入新版本后无需重启；文件缺失/损坏/记录
    不合契约都抛 :class:`SourceStoreError`，由查询入口转成 ``query_failed``。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TaxRateSnapshot:
        payload = read_policy_document(self._path, label="退税率")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class EnvTaxRateStore(JsonTaxRateStore):
    """按 ``CUSTOMS_TAX_RATE_PATH`` 读取；变量缺失时在 ``load()`` 明确报“未配置”。"""

    def __init__(self) -> None:
        self._path = default_tax_rate_path()

    @property
    def path(self) -> Path | None:  # type: ignore[override]
        return self._path

    def load(self) -> TaxRateSnapshot:
        if self._path is None:
            raise SourceStoreNotConfigured(
                f"未配置退税率来源：设置环境变量 {ENV_TAX_RATE_PATH} 指向导入的退税率 JSON；"
                "本包不提供默认样本，避免把演示数据当成真实退税率文库"
            )
        payload = read_policy_document(self._path, label="退税率")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryTaxRateStore:
    """内存存储：测试或上游已持有快照时注入，不读写文件。"""

    def __init__(
        self,
        records: Iterable[TaxRateRecord | Mapping[str, Any]],
        versions: Iterable[VersionWindow | Mapping[str, Any]],
        *,
        dataset: TaxRateDatasetInfo | Mapping[str, Any],
    ):
        self._raw_records = tuple(records)
        self._raw_versions = tuple(versions)
        self._dataset = dataset

    def load(self) -> TaxRateSnapshot:
        versions: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_versions):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, VersionWindow)
                    else VersionWindow.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise SourceStoreError(
                    f"第 {index + 1} 个退税政策版本不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            versions.append(parsed)
        records: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_records):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, TaxRateRecord)
                    else TaxRateRecord.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise SourceStoreError(
                    f"第 {index + 1} 条退税率记录不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            records.append(parsed)
        dataset = (
            self._dataset.model_dump(mode="json")
            if isinstance(self._dataset, TaxRateDatasetInfo)
            else TaxRateDatasetInfo.model_validate(self._dataset).model_dump(mode="json")
        )
        # 复用与 JSON 存储相同的结构校验：重复版本/税号、未声明版本引用、坏日期一律拒绝。
        return _snapshot_from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": dataset,
                "versions": versions,
                "rates": records,
            },
            source_name="in-memory",
        )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


class TaxRateSource:
    """按税号查询出口退税率/处理方式。

    典型用法::

        source = TaxRateSource()                       # 读 CUSTOMS_TAX_RATE_PATH
        result = source.lookup(TaxRateQuery(
            hs_code_raw="8544422900",
            basis=QueryBasis(declaration_date="2026-05-20"),
            original_rate_raw="13%",
        ))
        if result.usable_as_basis:
            engine.attach(result.to_evidence_entries())

    未配置来源时 ``query_status=source_unconfigured``、``evidence_status=query_failed``，
    不会回退到合成数据，也不会抛 500。
    """

    def __init__(self, store: TaxRateStore | None = None, *, clock: Any = None):
        self._store = store or EnvTaxRateStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "TaxRateSource":
        return cls(JsonTaxRateStore(path), **kwargs)

    def lookup(self, query: TaxRateQuery | Mapping[str, Any] | None = None) -> TaxRateResult:
        """执行一次查询；任何输入/来源问题都返回结构化结果，不向上抛异常。"""

        prepared, problem, issue_code = self._prepare(query)
        raw_code = _raw_field(query, "hs_code_raw")
        raw_original = _raw_field(query, "original_rate_raw")
        if prepared is None:
            return self._invalid(problem, raw=raw_code, original=raw_original, code=issue_code)

        fmt = classify_hs_format(prepared.hs_code_raw)
        now = _as_aware(self._clock()).isoformat(timespec="seconds")

        if fmt.state in (
            HsFormatState.EMPTY,
            HsFormatState.NONDIGIT,
            HsFormatState.WRONG_LENGTH,
        ):
            return self._invalid(
                f"编码格式不可查询：{fmt.reason}；未发起查询，不能据此判定不予退税或零税率",
                raw=prepared.hs_code_raw,
                original=prepared.original_rate_raw,
                fmt=fmt,
                basis=prepared.basis,
                searched_at=now,
                code="TAX_FORMAT_INVALID",
            )

        try:
            snapshot = self._store.load()
        except SourceStoreNotConfigured as exc:
            return self._failed(QueryStatus.SOURCE_UNCONFIGURED, str(exc), prepared, fmt, now)
        except SourceStoreError as exc:
            return self._failed(QueryStatus.SOURCE_UNREADABLE, str(exc), prepared, fmt, now)
        except Exception as exc:  # 存储实现自身缺陷不能变成 500
            return self._failed(
                QueryStatus.SOURCE_ERROR,
                f"退税率查询发生未预期异常（{type(exc).__name__}）；"
                "这不等于查无退税，需修复存储后重试",
                prepared,
                fmt,
                now,
            )

        version = resolve_version(prepared.basis, snapshot.versions, dataset=snapshot.dataset)
        record = _match_record(snapshot, version, fmt)
        try:
            if record is None:
                return self._not_found(prepared, fmt, version, snapshot, now)
            return self._found(prepared, fmt, version, record, snapshot, now)
        except ValidationError as exc:
            # 结果自检是最后一道防线：适配器内部缺陷不能变成 500，也不能返回一个“看起来通过”的结果。
            return self._failed(
                QueryStatus.SOURCE_ERROR,
                "结果自检失败（适配器内部缺陷："
                + _summarize_validation_error(exc)
                + "）；本次结论不成立，按缺陷上报，不能据此判定退税率",
                prepared,
                fmt,
                now,
            )

    # --- 构造结果 ---

    @staticmethod
    def _prepare(
        query: TaxRateQuery | Mapping[str, Any] | None,
    ) -> tuple[TaxRateQuery | None, str, str]:
        if isinstance(query, TaxRateQuery):
            raw: TaxRateQuery | Mapping[str, Any] = query
            payload: Mapping[str, Any] = query.model_dump()
        elif isinstance(query, Mapping):
            payload = dict(query)
            for key, (code, message) in _FORBIDDEN_INPUT_KEYS.items():
                if key in payload:
                    return None, f"{message}；未发起查询", code
            try:
                raw = TaxRateQuery.model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                return (
                    None,
                    f"查询输入不符合契约（{_summarize_validation_error(exc)}）；未发起查询",
                    "TAX_QUERY_INVALID",
                )
        elif query is None:
            return None, "查询为空；未发起查询", "TAX_QUERY_INVALID"
        else:
            return (
                None,
                f"查询输入类型不受支持（{type(query).__name__}）；未发起查询",
                "TAX_QUERY_INVALID",
            )

        try:
            prepared = TaxRateQuery(
                hs_code_raw="" if raw.hs_code_raw is None else str(raw.hs_code_raw),
                rebate_code="" if raw.rebate_code is None else str(raw.rebate_code),
                basis=(
                    raw.basis
                    if isinstance(raw.basis, QueryBasis)
                    else QueryBasis.model_validate(raw.basis)
                ),
                original_rate_raw=(
                    "" if raw.original_rate_raw is None else str(raw.original_rate_raw)
                ),
                note="" if raw.note is None else str(raw.note),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return (
                None,
                f"查询输入无法解析（{_summarize_validation_error(exc)}）；未发起查询",
                "TAX_QUERY_INVALID",
            )
        return prepared, "", ""

    def _invalid(
        self,
        reason: str,
        *,
        raw: str,
        original: str = "",
        code: str = "TAX_QUERY_INVALID",
        fmt: HsFormat | None = None,
        basis: QueryBasis | None = None,
        searched_at: str | None = None,
    ) -> TaxRateResult:
        fmt = fmt or classify_hs_format(raw)
        basis = basis or QueryBasis()
        return TaxRateResult(
            query=TaxRateQuery(hs_code_raw=raw, basis=basis, original_rate_raw=original),
            query_status=QueryStatus.INVALID_INPUT,
            evidence_status=None,
            format=fmt,
            version=resolve_version(basis, ()),
            record=None,
            original_rate=parse_original_rate(original),
            rate_comparison=compare_rates(parse_original_rate(original), None),
            reason=reason,
            pending_reasons=[reason],
            issues=[
                IssueEntry(
                    code=code,
                    message=reason,
                    field="supplier_hs_raw",
                    missing="可查询的 10 位编码与申报基准",
                    suggested_role="supplier",
                )
            ],
            searched_at=searched_at,
        )

    def _failed(
        self,
        status: QueryStatus,
        reason: str,
        query: TaxRateQuery,
        fmt: HsFormat,
        now: str,
    ) -> TaxRateResult:
        message = f"退税率查询失败：{reason}；这不等于不予退税，也不是零税率"
        return TaxRateResult(
            query=query,
            query_status=status,
            evidence_status=EvidenceStatus.QUERY_FAILED,
            format=fmt,
            version=resolve_version(query.basis, ()),
            record=None,
            original_rate=parse_original_rate(query.original_rate_raw),
            rate_comparison=compare_rates(parse_original_rate(query.original_rate_raw), None),
            reason=message,
            pending_reasons=[message],
            issues=[
                IssueEntry(
                    code="TAX_QUERY_FAILED",
                    message=f"退税率来源不可用：{reason}",
                    field="supplier_tax_rate_raw",
                    missing="可用的退税政策来源",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    def _not_found(
        self,
        query: TaxRateQuery,
        fmt: HsFormat,
        version: VersionResolution,
        snapshot: TaxRateSnapshot,
        now: str,
    ) -> TaxRateResult:
        original = parse_original_rate(query.original_rate_raw)
        issues: list[IssueEntry] = []
        pending: list[str] = []
        if version.verified:
            reason = (
                f"退税政策版本 {version.version} 未收录编码 {fmt.normalized}；"
                "这是查无记录，不等于不予退税、零税率或无监管"
            )
            pending.append("来源未收录该税号：需补政策来源或转专家确认，不能按 0% 处理")
            issues.append(
                IssueEntry(
                    code="TAX_RATE_NOT_FOUND",
                    message=reason,
                    field="supplier_hs_raw",
                    missing=f"{version.version} 期退税率记录",
                    suggested_role="expert",
                )
            )
            other = sorted(
                {
                    item.version
                    for item in snapshot.records
                    if item.hs_code == fmt.normalized and item.version != version.version
                }
            )
            if other:
                issues.append(
                    IssueEntry(
                        code="TAX_RATE_OTHER_VERSION",
                        message=(
                            f"同一编码在其它政策版本有收录（{'、'.join(other)}），"
                            "不能据此判定本期退税率或不予退税"
                        ),
                        field="query_basis",
                        suggested_role="expert",
                    )
                )
        else:
            reason = (
                f"未能核实编码 {fmt.normalized or fmt.raw} 的退税率：{version.reason}；"
                "不能宣称不予退税或零税率"
            )
            pending.append("缺少可确认的查询基准，退税率未核对")
            issues.append(
                IssueEntry(
                    code="TAX_VERSION_UNVERIFIED",
                    message=reason,
                    field="query_basis",
                    missing="业务申报日期或退税政策版本",
                    suggested_role="expert",
                )
            )
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="TAX_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目；退税文库若按 10 位维护，退税率可能不同",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        if original.state is OriginalRateState.BLANK:
            issues.append(
                IssueEntry(
                    code="TAX_ORIGINAL_RATE_BLANK",
                    message="原表退税率空白；空白不是 0%，也不是“不予退税”",
                    field="supplier_tax_rate_raw",
                    missing="供应商原表退税率",
                    suggested_role="supplier",
                )
            )
        elif original.state in (
            OriginalRateState.NON_NUMERIC,
            OriginalRateState.UNPARSEABLE,
        ):
            issues.append(
                IssueEntry(
                    code="TAX_ORIGINAL_RATE_UNPARSEABLE",
                    message=original.note,
                    field="supplier_tax_rate_raw",
                    suggested_role="expert",
                )
            )
        return TaxRateResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=(
                EvidenceStatus.NOT_FOUND if version.verified else EvidenceStatus.VERSION_UNVERIFIED
            ),
            format=fmt,
            version=version,
            record=None,
            original_rate=original,
            rate_comparison=compare_rates(original, None),
            reason=reason,
            pending_reasons=pending,
            issues=issues,
            dataset=snapshot.dataset,
            searched_at=now,
        )

    def _found(
        self,
        query: TaxRateQuery,
        fmt: HsFormat,
        version: VersionResolution,
        record: TaxRateRecord,
        snapshot: TaxRateSnapshot,
        now: str,
    ) -> TaxRateResult:
        dataset = snapshot.dataset
        effective = resolve_effective_status(record, query.basis, version)
        conditions_state = record.conditions_state
        code_ok = rebate_code_matches(query, record)
        applicability = resolve_applicability(
            record,
            version,
            effective,
            dataset,
            rebate_code_verified=code_ok,
            fmt=fmt,
        )
        original = parse_original_rate(query.original_rate_raw)
        looked_up = LookedUpRate(
            rate_kind=record.rate_kind,
            rate_percent=record.rate_percent,
            rate_raw=record.rate_raw,
            rate_state=record.rate_state,
            rebate_code=record.rebate_code,
        )
        comparison = compare_rates(original, looked_up)
        issues, pending = self._found_issues(
            query, record, version, dataset, effective, code_ok, original, comparison, fmt
        )
        evidence = self._evidence(query, fmt, version, record, dataset, effective)

        if applicability is not TaxApplicability.APPLIES and not pending:
            # 防线：任何“不可用”的结论都必须写清为什么，否则下游会把空原因读成无风险。
            pending.append(
                f"未形成可用结论（{applicability.value}）"
                + ("：" + "、".join(item.code for item in issues) if issues else "：原因未记录")
                + "；需人工核对后再用"
            )
        rate_text = (
            f"{record.rate_percent:g}%"
            if record.rate_percent is not None
            else record.rate_kind.value
        )
        reason = (
            f"在退税政策版本 {record.version} 查到 {record.hs_code} 的退税率/处理方式："
            f"{rate_text}（原文 {record.rate_raw or '未给出'}）；"
            f"状态 {record.rate_state.value}；申报日适用性 {effective.value}；"
            f"适用性判定 {applicability.value}"
        )
        if not version.verified:
            reason += f"；但版本未核实（{version.reason}）"
        if not dataset.authoritative:
            reason += "；来源非权威，不能作为申报依据"

        return TaxRateResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=(
                EvidenceStatus.OBTAINED if version.verified else EvidenceStatus.VERSION_UNVERIFIED
            ),
            format=fmt,
            version=version,
            record=record,
            rate_state=record.rate_state,
            conditions_state=conditions_state,
            conditions=list(record.conditions or []),
            effective_status=effective,
            applicability=applicability,
            original_rate=original,
            looked_up_rate=looked_up,
            rate_comparison=comparison,
            authoritative=dataset.authoritative,
            usable_as_basis=applicability is TaxApplicability.APPLIES,
            expert_review_required=resolve_expert_review(applicability, comparison),
            pending_reasons=pending,
            reason=reason,
            issues=issues,
            evidence=evidence,
            dataset=dataset,
            searched_at=now,
        )

    # --- 问题与待核原因 ---

    def _found_issues(
        self,
        query: TaxRateQuery,
        record: TaxRateRecord,
        version: VersionResolution,
        dataset: TaxRateDatasetInfo,
        effective: EffectiveStatus,
        code_ok: bool,
        original: OriginalRate,
        comparison: RateComparison,
        fmt: HsFormat,
    ) -> tuple[list[IssueEntry], list[str]]:
        issues: list[IssueEntry] = []
        pending: list[str] = []

        if not version.verified:
            issues.append(
                IssueEntry(
                    code="TAX_VERSION_UNVERIFIED",
                    message=f"退税率已取到，但适用版本未核实：{version.reason}",
                    field="query_basis",
                    missing="业务申报日期或退税政策版本",
                    suggested_role="expert",
                )
            )
            pending.append(f"政策版本未核实：{version.reason}")
        if not dataset.authoritative:
            issues.append(
                IssueEntry(
                    code="TAX_AUTHORITY_NOT_OFFICIAL",
                    message=(
                        f"来源权威级别为 {dataset.authority.value}、类型为 {dataset.source_type}；"
                        "只能作线索，不能作为申报依据"
                    ),
                    field="evidence",
                    missing="官方退税政策来源（含复核记录）",
                    suggested_role="operator",
                )
            )
            pending.append("来源非权威：只能作线索，不能作为申报依据")

        if record.rate_kind is RateKind.UNKNOWN:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_KIND_UNKNOWN",
                    message="来源未说明该税号是具体退税率、零税率、免税还是不予退税；不得补成 0%",
                    field="supplier_tax_rate_raw",
                    suggested_role="expert",
                )
            )
            pending.append("来源未说明处理方式")
        if record.rate_state is RateState.UNKNOWN:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_STATE_UNKNOWN",
                    message="来源未声明该条退税政策是否现行有效；不能当成已核实的当期政策",
                    field="evidence",
                    suggested_role="expert",
                )
            )
            pending.append("来源未声明政策状态")
        elif record.rate_state is RateState.ABOLISHED:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_ABOLISHED",
                    message="来源声明该退税政策已废止；不能作为当期申报依据",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )
            pending.append("来源声明政策已废止")
        elif record.rate_state is RateState.SUPERSEDED:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_SUPERSEDED",
                    message="来源声明该退税政策已被替代；需按现行政策核对",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )
            pending.append("来源声明政策已被替代")

        if record.conditions_state is ConditionsState.DECLARED_UNVERIFIED:
            issues.append(
                IssueEntry(
                    code="TAX_CONDITION_UNVERIFIED",
                    message=(
                        "来源给出适用条件文本但未机读化，本模块不自动判断条件是否成立："
                        + "；".join(item.text for item in record.conditions or [])
                    ),
                    field="supplier_hs_raw",
                    missing="由关务专家确认的条件判定",
                    suggested_role="expert",
                )
            )
            pending.append("存在未机读化的适用条件，退税率是否适用本票业务需专家确认")
        elif record.conditions_state is ConditionsState.NOT_DECLARED:
            issues.append(
                IssueEntry(
                    code="TAX_CONDITION_NOT_DECLARED",
                    message="来源未声明该条退税率的适用条件；不得推定“无条件适用”",
                    field="evidence",
                    missing="来源中的适用条件说明",
                    suggested_role="expert",
                )
            )
            pending.append("来源未声明适用条件")

        for index, condition in enumerate(record.conditions or [], start=1):
            if condition.located:
                continue
            issues.append(
                IssueEntry(
                    code="TAX_CONDITION_UNLOCATED",
                    message=f"第 {index} 条适用条件没有文档与条款定位，不能作为可回查证据",
                    field="evidence",
                    missing="条件所在公告号与条款",
                    suggested_role="operator",
                )
            )
            pending.append(f"第 {index} 条适用条件无法定位")

        if effective is EffectiveStatus.NOT_YET_EFFECTIVE:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_NOT_EFFECTIVE",
                    message=(
                        f"记录自 {record.effective_from} 起生效，晚于申报日期 "
                        f"{query.basis.declaration_date}；不能作为该日依据"
                    ),
                    field="query_basis",
                    suggested_role="expert",
                )
            )
            pending.append("记录生效日期晚于申报日期")
        elif effective is EffectiveStatus.EXPIRED:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_NOT_EFFECTIVE",
                    message=(
                        f"记录有效期至 {record.effective_to}，早于申报日期 "
                        f"{query.basis.declaration_date}；不能作为该日依据"
                    ),
                    field="query_basis",
                    suggested_role="expert",
                )
            )
            pending.append("记录失效日期早于申报日期")
        elif effective is EffectiveStatus.UNKNOWN:
            if not (query.basis.declaration_date or "").strip():
                issues.append(
                    IssueEntry(
                        code="TAX_EFFECTIVE_DATE_UNKNOWN",
                        message="未提供业务申报日期，无法核对退税率在该日是否适用；系统不代填申报日期",
                        field="query_basis",
                        missing="业务申报日期",
                        suggested_role="expert",
                    )
                )
                pending.append("缺业务申报日期，未核对申报日适用性")
            else:
                issues.append(
                    IssueEntry(
                        code="TAX_RATE_EFFECTIVE_UNVERIFIED",
                        message="记录只提供部分生效/失效日期，无法确认申报日期是否在有效期内",
                        field="query_basis",
                        missing="完整的记录有效期",
                        suggested_role="expert",
                    )
                )
                pending.append("记录有效期不完整")

        if not code_ok:
            issues.append(
                IssueEntry(
                    code="TAX_REBATE_CODE_MISMATCH",
                    message=(
                        f"调用方给出的适用退税代码 {query.rebate_code!r} 与来源记录 "
                        f"{record.rebate_code!r} 不一致或来源未标注；不能确认该退税率适用"
                    ),
                    field="rebate_code",
                    missing="可核对的适用退税代码",
                    suggested_role="expert",
                )
            )
            pending.append("适用退税代码无法核对")

        if original.state is OriginalRateState.BLANK:
            issues.append(
                IssueEntry(
                    code="TAX_ORIGINAL_RATE_BLANK",
                    message="原表退税率空白；空白不是 0%，也不是“不予退税”",
                    field="supplier_tax_rate_raw",
                    missing="供应商原表退税率",
                    suggested_role="supplier",
                )
            )
        elif original.state in (
            OriginalRateState.NON_NUMERIC,
            OriginalRateState.UNPARSEABLE,
        ):
            issues.append(
                IssueEntry(
                    code="TAX_ORIGINAL_RATE_UNPARSEABLE",
                    message=original.note,
                    field="supplier_tax_rate_raw",
                    suggested_role="expert",
                )
            )
        if comparison.status is ComparisonStatus.CONFLICT:
            issues.append(
                IssueEntry(
                    code="TAX_RATE_CONFLICT",
                    message=(
                        f"原表 {original.percent}% 与来源 {record.rate_percent}% 不一致；"
                        "需专家按政策原文判定，不由适配器裁定"
                    ),
                    field="supplier_tax_rate_raw",
                    suggested_role="expert",
                )
            )
            pending.append("原表退税率与来源值不一致")
        elif comparison.status is ComparisonStatus.UNVERIFIED:
            pending.append("原表退税率无法确认为唯一百分数")
        elif comparison.status is ComparisonStatus.NOT_COMPARED:
            pending.append(f"未完成数值比对：{comparison.reason}")

        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="TAX_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目；退税文库若按 10 位维护，退税率可能不同",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
            pending.append("只有 8 位税目/子目，未确认 10 位申报编码，退税率不能直接当依据")
        return issues, pending

    # --- 证据 ---

    def _evidence(
        self,
        query: TaxRateQuery,
        fmt: HsFormat,
        version: VersionResolution,
        record: TaxRateRecord,
        dataset: TaxRateDatasetInfo,
        effective: EffectiveStatus,
    ) -> list[EvidenceEntry]:
        source = record.source
        if not source.retrieved_at and dataset.retrieved_at:
            source = source.model_copy(update={"retrieved_at": dataset.retrieved_at})
        status = (
            EvidenceStatus.OBTAINED if version.verified else EvidenceStatus.VERSION_UNVERIFIED
        )
        record_has_dates = bool(record.effective_from or record.effective_to)
        window = version.window
        evidence_from = (
            record.effective_from if record_has_dates else (window.effective_from if window else None)
        )
        evidence_to = (
            record.effective_to if record_has_dates else (window.effective_to if window else None)
        )
        rate_text = (
            f"{record.rate_percent:g}%"
            if record.rate_percent is not None
            else record.rate_kind.value
        )
        head = EvidenceEntry(
            evidence_id=f"tax:{record.version}:{record.hs_code}",
            source_type=dataset.source_type,
            title=f"出口退税 {record.version}：{record.hs_code} {record.product_name}".strip(),
            status=status,
            url=source.url,
            document=source.document,
            locator=source.locator,
            excerpt=record.rate_raw or None,
            retrieved_at=source.retrieved_at,
            effective_from=evidence_from,
            effective_to=evidence_to,
            scope=(
                f"退税政策版本 {record.version}；处理方式 {record.rate_kind.value}；"
                f"政策状态 {record.rate_state.value}；申报日适用性 {effective.value}"
            ),
            query={
                "hs_code": fmt.normalized,
                "rebate_code": query.rebate_code,
                "basis": query.basis.model_dump(),
            },
            note=(
                f"来源类型 {dataset.source_type}；权威级别 {dataset.authority.value}；"
                f"退税率/处理方式 {rate_text}；"
                f"适用条件 {record.conditions_state.value}"
                + ("；仅作线索，不能作为申报依据" if not dataset.authoritative else "")
            ),
        )
        entries = [head]
        for index, condition in enumerate(record.conditions or [], start=1):
            if not condition.located:
                continue
            entries.append(
                EvidenceEntry(
                    evidence_id=f"tax:{record.version}:{record.hs_code}:cond{index}",
                    source_type=dataset.source_type,
                    title=f"退税适用条件（{record.hs_code} 第 {index} 条）",
                    status=status,
                    url=condition.url,
                    document=condition.document,
                    locator=condition.locator,
                    excerpt=condition.text,
                    retrieved_at=source.retrieved_at,
                    effective_from=evidence_from,
                    effective_to=evidence_to,
                    scope="退税适用条件（未机读化，需专家判定）",
                    query={"hs_code": fmt.normalized, "condition_index": index},
                    note=condition.note or "条件文本原样保留，本模块不自动判断条件是否成立",
                )
            )
        return entries


def _match_record(
    snapshot: TaxRateSnapshot, version: VersionResolution, fmt: HsFormat
) -> TaxRateRecord | None:
    """按版本 + 税号取记录；版本不唯一时不猜。"""

    if fmt.state not in (HsFormatState.VALID, HsFormatState.SUBHEADING_ONLY):
        return None
    if version.version is not None:
        return next(
            (
                item
                for item in snapshot.records
                if item.version == version.version and item.hs_code == fmt.normalized
            ),
            None,
        )
    if len(snapshot.versions) == 1:
        only = snapshot.versions[0].version
        return next(
            (
                item
                for item in snapshot.records
                if item.version == only and item.hs_code == fmt.normalized
            ),
            None,
        )
    return None


def _raw_field(query: Any, field: str) -> str:
    if isinstance(query, TaxRateQuery):
        return getattr(query, field) or ""
    if isinstance(query, Mapping):
        value = query.get(field)
        return "" if value is None else str(value)
    return ""


# ---------------------------------------------------------------------------
# 来源能力清单（D4 §7 / D6 §2.6）
# ---------------------------------------------------------------------------


class RebateSourceCapability(Schema):
    """一个候选退税来源的可用能力与限制；供 D4/D6 与 P18 判断“能不能查”。"""

    source_id: str
    name: str
    content: str = Field(description="tax_rate / legal_basis")
    source_type: RebateSourceKind
    access_mode: AccessMode
    availability: SourceAvailability
    auth_required: bool = False
    fields: list[str] = Field(default_factory=list, description="可取得的字段")
    limitations: list[str] = Field(default_factory=list)
    url: str | None = None
    checked_at: str | None = None
    check_note: str = ""


#: 2026-11-17 在本机做的可达性/字段试验结论。只描述“本机能否自动取数”，
#: 不代表官方数据不存在；任何“可用”都必须由实际导入并复核的数据文件证明。
REBATE_SOURCE_CAPABILITIES: tuple[RebateSourceCapability, ...] = (
    RebateSourceCapability(
        source_id="tax-rebate-library",
        name="税务总局出口退税率文库",
        content="tax_rate",
        source_type=RebateSourceKind.OFFICIAL_TAX,
        access_mode=AccessMode.MANUAL_IMPORT,
        availability=SourceAvailability.REQUIRES_AUTHORIZATION,
        auth_required=True,
        fields=["税号/商品代码", "出口退税率", "征税率", "商品名称", "文库版本"],
        limitations=[
            "无公开批量下载接口：文库通过电子税务局/离线申报工具按企业身份取得",
            "与海关税则分库管理：税号口径与退税文库口径可能不一致，不能互相顶替",
            "退税率随政策调整，必须带文库版本与取得时间，不能只用“最新”",
        ],
        url="https://www.chinatax.gov.cn/",
        checked_at="2026-11-17",
        check_note=(
            "总局门户 https 可达（HTTP 200，0.7s），但无公开退税率文库文件/接口；"
            "本机未取得任何文库文件。"
        ),
    ),
    RebateSourceCapability(
        source_id="tax-policy-announcements",
        name="财政部/税务总局出口退税政策公告",
        content="legal_basis",
        source_type=RebateSourceKind.OFFICIAL_FINANCE,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.AVAILABLE,
        auth_required=False,
        fields=["公告标题", "公告号", "适用商品范围", "生效/失效日期", "退税率调整条款"],
        limitations=[
            "公告只给政策口径与适用范围，不含全量税号退税率表；需人工结构化",
            "适用条件常以文字表述（商品范围、企业类型、贸易方式），未机读化前一律待核",
            "不得据公告标题反推具体税号退税率",
        ],
        url="https://www.chinatax.gov.cn/",
        checked_at="2026-11-17",
        check_note="门户可达；本包未抓取入库，只人工核对了公告栏目结构，未取具体公告原文。",
    ),
    RebateSourceCapability(
        source_id="third-party-tax-rate",
        name="第三方退税率查询站",
        content="tax_rate",
        source_type=RebateSourceKind.THIRD_PARTY,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.AVAILABLE,
        auth_required=False,
        fields=["税号", "商品名称", "退税率（可能滞后）"],
        limitations=[
            "整理自公开数据，可能滞后、缺项或错误；只能作初筛与字段形状参考",
            "不得作为合规结论，也不得写成 authoritative 证据",
            "抓取前需确认对方条款；本包未抓取入库",
        ],
        url=None,
        checked_at="2026-11-17",
        check_note="本包未访问第三方站点，只按数据源清单第 27 行标注其定位；不提供具体站点以免被当成依据。",
    ),
)


def describe_sources(*, content: str | None = None) -> list[RebateSourceCapability]:
    """返回退税来源能力清单；可按 ``tax_rate``/``legal_basis`` 过滤。"""

    items = list(REBATE_SOURCE_CAPABILITIES)
    if content:
        items = [item for item in items if item.content == content]
    return items


def configured_tax_rate_path() -> Path | None:
    """当前环境是否配置了退税率文件；供 D6 自检与页面提示，不读取文件内容。"""

    return default_tax_rate_path()
