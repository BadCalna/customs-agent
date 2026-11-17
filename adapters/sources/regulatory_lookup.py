"""监管条件与检验检疫要求来源适配（P17）。

定位：只回答“这个编码在某个申报日期、某个进出口方向下，来源声明了哪些监管/检验
检疫要求”，不替专家判断“要不要办证”
--------------------------------------------------------------------------------
本模块是**事实来源**，不是合规裁定：

- 不接收图片、不做 OCR：监管条件不能从图片推得；夹带图片字段的请求直接按
  ``invalid_input`` 拒绝，不“忽略后照常算”。
- **不内置“代码 → 含义”的记忆表**：``A``/``B``/``M``/``N`` 等代码的名称与主管机关
  只能来自来源原文；来源只给代码不给名称时，结果保留代码并标
  ``REG_MEASURE_NAME_UNKNOWN``，**不靠模型或硬编码补意思**（那等于虚构法规）。
- **“没查到”不等于“无监管”**。仅当来源对该税号、该申报日期、该方向**明确声明**
  没有监管要求（``measures: []`` 且可定位）时才是 ``declared_none``；
  缺日期、缺方向、来源失败、版本待核、字段缺失一律 ``unknown``。
- 两用物项只输出**清单命中线索与来源原文**，``verdict`` 恒为 ``unknown``：
  是否属于管制物项必须由专家按技术参数、最终用途与用户判定，不由本模块或模型裁定。
- 不访问网络、不读数据库、不调用模型；数据经可替换的 :class:`RegulatoryStore` 注入。

三态（``outcome``）
------------------
=====================  ==========================================  ====================
取值                    含义                                        能否读成“无监管”
=====================  ==========================================  ====================
``measures_required``   来源明确列出监管/检验检疫要求                 否（是“有要求”）
``declared_none``       来源明确声明无监管要求，且已核对到日期/方向    仅当 ``usable_as_basis=true``
``not_effective``       记录已废止，或申报日期不在其有效期              否
``unknown``             未取得/字段缺失/缺基准/方向不明/来源失败        否
=====================  ==========================================  ====================

``usable_as_basis`` 要求：来源权威（官方 + 已复核）、版本已核、申报日期落在记录有效期、
记录现行有效、方向核对一致、措施可定位。**来源是合成/第三方/未复核时即使
``outcome=declared_none``，也不能读作“无监管”**，只能作线索。

进出口方向
----------
监管条件按方向不同（如 A 类针对进口、B 类针对出口）。``RegulatoryQuery.direction``
必须由调用方给出；没给就 ``unknown``，不能默认按进口或“双向都行”处理。来源记录
``direction=both`` 表示该条要求双向适用，可与任一方向匹配。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping, Protocol

from ninja import Schema
from pydantic import Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus, IssueEntry

from .hs_lookup import (
    EffectiveStatus,
    HsFormat,
    HsFormatState,
    QueryBasis,
    QueryStatus,
    SourceRef,
    VersionResolution,
    VersionWindow,
    classify_hs_format,
    resolve_version,
    _parse_date,
    _strip_invisible,
)
from .tax_lookup import (
    AccessMode,
    PolicyDatasetInfo,
    SourceAvailability,
    SourceStoreError,
    SourceStoreNotConfigured,
    StrictSchema,
    _summarize_validation_error,
    read_policy_document,
)

#: 监管条件数据文件的结构版本。
SCHEMA_VERSION = "p17.regulatory.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 监管条件文件路径的环境变量。**没有默认样本**：未配置就明确报“来源未配置”。
ENV_REGULATORY_PATH = "CUSTOMS_REGULATORY_PATH"

#: 输入里禁止出现的字段：监管条件不能从图片/OCR 推得。
_FORBIDDEN_INPUT_KEYS: dict[str, str] = {
    "image": "监管条件查询不接受图片输入；图片只能支持可见事实，不能作为监管依据",
    "image_id": "监管条件查询不接受图片输入；图片只能支持可见事实，不能作为监管依据",
    "picture": "监管条件查询不接受图片输入；图片只能支持可见事实，不能作为监管依据",
    "ocr": "监管条件查询不接受 OCR 文本作为依据；必须来自可定位的监管来源原文",
    "requires_licence": "监管要求不能由调用方直接断言；必须来自可定位的来源记录",
    "has_controls": "监管要求不能由调用方直接断言；必须来自可定位的来源记录",
}


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class RegulatorySourceKind(str, Enum):
    """监管来源类型。第三方与合成数据在任何情况下都不能成为合规依据。"""

    OFFICIAL_REGULATORY = "official_regulatory"
    OFFICIAL_DUAL_USE = "official_dual_use"
    THIRD_PARTY = "third_party"
    SYNTHETIC = "synthetic"


class Direction(str, Enum):
    """进出口方向；监管条件按方向不同，不能默认一个方向。"""

    IMPORT = "import"
    EXPORT = "export"
    BOTH = "both"
    UNKNOWN = "unknown"


class MeasureCategory(str, Enum):
    """措施类别；``unknown`` 表示来源未说明，不得按代码猜。"""

    LICENCE = "licence"
    INSPECTION_QUARANTINE = "inspection_quarantine"
    DUAL_USE = "dual_use"
    UNKNOWN = "unknown"


class RecordState(str, Enum):
    """来源声明的监管记录状态。"""

    IN_FORCE = "in_force"
    ABOLISHED = "abolished"
    UNKNOWN = "unknown"


class MeasuresState(str, Enum):
    """``measures`` 字段的声明情况；未声明不等于没有。"""

    NOT_DECLARED = "not_declared"
    DECLARED_NONE = "declared_none"
    DECLARED = "declared"


class RegulatoryOutcome(str, Enum):
    """来源对本税号监管要求的结论；只有 ``declared_none`` 可能是“无监管”。"""

    MEASURES_REQUIRED = "measures_required"
    DECLARED_NONE = "declared_none"
    NOT_EFFECTIVE = "not_effective"
    UNKNOWN = "unknown"


class DualUseVerdict(str, Enum):
    """两用物项判定；**只有 unknown**——是否属于管制物项必须由专家判定。"""

    UNKNOWN = "unknown"


class RegulatoryDatasetInfo(PolicyDatasetInfo):
    """监管条件来源的数据集说明。"""

    _allowed_kinds: ClassVar[tuple[str, ...]] = tuple(
        kind.value for kind in RegulatorySourceKind
    )


# ---------------------------------------------------------------------------
# 输入与记录
# ---------------------------------------------------------------------------


class RegulatoryQuery(StrictSchema):
    """监管条件查询输入。刻意**不含**图片、也不接受调用方直接断言“要不要办证”。"""

    hs_code_raw: str = Field(default="", description="待审 HS 编码原文；原样保留")
    direction: Direction = Field(
        default=Direction.UNKNOWN, description="进出口方向；未给就是 unknown，不默认"
    )
    basis: QueryBasis = Field(default_factory=QueryBasis)
    note: str = Field(default="", description="调用方备注，不进入证据定位")


class RegulatoryMeasure(Schema):
    """来源声明的一条监管/检验检疫措施。

    ``name`` 与 ``agency`` **只允许来自来源原文**；来源只给代码时留空并转专家，
    不能由本模块或模型补出证件名称。
    """

    code: str = Field(min_length=1, description="监管证件代码或检验检疫类别，原样保留")
    category: MeasureCategory = MeasureCategory.UNKNOWN
    name: str = Field(default="", description="来源给出的证件/要求名称；没有就留空")
    agency: str = Field(default="", description="来源给出的主管机关；没有就留空")
    document: str | None = Field(default=None, description="代码表/公告标识")
    locator: str | None = Field(default=None, description="条款/表行定位")
    url: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "RegulatoryMeasure":
        if not _strip_invisible(self.code):
            raise ValueError("监管措施代码去除不可见字符后不能为空")
        has_doc = bool(_strip_invisible(self.document))
        has_loc = bool(_strip_invisible(self.locator))
        if has_doc != has_loc:
            raise ValueError("措施要么同时给 document 与 locator，要么都不给；不能只给一半")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("措施 url 必须以 http(s):// 开头；没有就留空，不编造")
        return self

    @property
    def located(self) -> bool:
        return bool(_strip_invisible(self.document)) and bool(_strip_invisible(self.locator))

    @property
    def meaning_known(self) -> bool:
        return bool(_strip_invisible(self.name))


class DualUseItem(Schema):
    """两用物项清单命中线索；``verdict`` 恒为 ``unknown``，不自动判定。"""

    list_name: str = Field(min_length=1, description="清单名称（来源原文）")
    item_description: str = Field(default="", description="清单条目/管制描述原文")
    technical_criteria: str = Field(default="", description="来源给出的技术门槛原文")
    document: str | None = None
    locator: str | None = None
    url: str | None = None
    verdict: DualUseVerdict = DualUseVerdict.UNKNOWN
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "DualUseItem":
        if not _strip_invisible(self.list_name):
            raise ValueError("两用物项线索必须给出清单名称")
        if self.verdict is not DualUseVerdict.UNKNOWN:
            raise ValueError("两用物项是否适用只能由专家判定，本模块不得给出非 unknown 结论")
        has_doc = bool(_strip_invisible(self.document))
        has_loc = bool(_strip_invisible(self.locator))
        if has_doc != has_loc:
            raise ValueError("两用物项线索要么同时给 document 与 locator，要么都不给")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("两用物项 url 必须以 http(s):// 开头；没有就留空，不编造")
        return self

    @property
    def located(self) -> bool:
        return bool(_strip_invisible(self.document)) and bool(_strip_invisible(self.locator))


class RegulatoryRecord(Schema):
    """来源中的一条监管条件记录（某版本、某税号、某方向）。"""

    version: str = Field(min_length=1)
    hs_code: str = Field(min_length=8, max_length=12, description="8 位税目/子目或 10 位申报编码")
    direction: Direction = Field(description="该条记录适用的进出口方向")
    measures: list[RegulatoryMeasure] | None = Field(
        default=None,
        description="null=来源未声明（待核）；[]=来源明确声明无监管要求；非空=有措施",
    )
    dual_use: list[DualUseItem] = Field(
        default_factory=list, description="两用物项清单命中线索，需专家判定"
    )
    state: RecordState = RecordState.UNKNOWN
    effective_from: str | None = None
    effective_to: str | None = None
    product_name: str = ""
    source: SourceRef
    note: str = ""

    @model_validator(mode="after")
    def _check_record(self) -> "RegulatoryRecord":
        if not self.hs_code.isdigit() or len(self.hs_code) not in (8, 10):
            raise ValueError(f"监管记录的 hs_code 必须是 8 或 10 位数字：{self.hs_code!r}")
        if self.direction is Direction.UNKNOWN:
            raise ValueError("监管记录必须声明适用方向（import/export/both），不能是 unknown")
        if self.direction is Direction.BOTH and self.measures:
            raise ValueError(
                "direction=both 的记录不能携带具体监管措施：来源表里进口/出口同类要求"
                "（如 A 类进口、B 类出口）必须拆成两条方向明确的记录，否则无法逐条核对方向"
            )
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
    def measures_state(self) -> MeasuresState:
        if self.measures is None:
            return MeasuresState.NOT_DECLARED
        if not self.measures:
            return MeasuresState.DECLARED_NONE
        return MeasuresState.DECLARED


class RegulatoryResult(Schema):
    """一次监管条件查询的完整结果；字段之间的一致性由校验器强制。"""

    query: RegulatoryQuery | None = None
    query_status: QueryStatus
    evidence_status: EvidenceStatus | None = Field(
        default=None, description="D4 四态；输入不合法时为 null（未发起查询）"
    )
    format: HsFormat
    version: VersionResolution
    record: RegulatoryRecord | None = None
    direction: Direction = Direction.UNKNOWN
    direction_matched: bool = False
    outcome: RegulatoryOutcome = RegulatoryOutcome.UNKNOWN
    measures_state: MeasuresState = MeasuresState.NOT_DECLARED
    measures: list[RegulatoryMeasure] = Field(default_factory=list)
    dual_use: list[DualUseItem] = Field(default_factory=list)
    effective_status: EffectiveStatus = EffectiveStatus.UNKNOWN
    authoritative: bool = False
    usable_as_basis: bool = False
    expert_review_required: bool = True
    pending_reasons: list[str] = Field(default_factory=list, description="待核原因（人读）")
    reason: str = ""
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    dataset: RegulatoryDatasetInfo | None = None
    searched_at: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "RegulatoryResult":
        if self.outcome is RegulatoryOutcome.UNKNOWN and not self.pending_reasons:
            raise ValueError("unknown 必须写明为什么不能当成无监管")
        if self.usable_as_basis and self.outcome not in (
            RegulatoryOutcome.MEASURES_REQUIRED,
            RegulatoryOutcome.DECLARED_NONE,
        ):
            raise ValueError("usable_as_basis 只在有明确结论的结果上成立")
        if self.usable_as_basis and not self.authoritative:
            raise ValueError("来源非权威时不得 usable_as_basis，不能读成合规依据")
        if self.direction_matched and self.direction is Direction.UNKNOWN:
            raise ValueError("方向未给出时不得声明方向已核对")

        if self.query_status is QueryStatus.INVALID_INPUT:
            if self.evidence_status is not None:
                raise ValueError("invalid_input 必须先于查询，evidence_status 必须为 null")
            if self.record is not None or self.evidence:
                raise ValueError("invalid_input 不得携带来源记录或证据")
            if self.outcome is not RegulatoryOutcome.UNKNOWN:
                raise ValueError("invalid_input 的结论只能是 unknown")
            if self.measures or self.dual_use:
                raise ValueError("invalid_input 不得携带措施或两用物项线索")
            if self.version.verified:
                raise ValueError("invalid_input 未发起查询，版本不能是 verified")
            return self

        if self.query_status is not QueryStatus.OK:
            if self.evidence_status is not EvidenceStatus.QUERY_FAILED:
                raise ValueError(f"{self.query_status.value} 必须对应 query_failed")
            if self.record is not None or self.evidence:
                raise ValueError("查询失败时不得携带记录或虚构证据")
            if self.outcome is not RegulatoryOutcome.UNKNOWN:
                raise ValueError("查询失败时不得给出监管结论，更不能当成无监管")
            return self

        if self.record is None:
            expected = (
                EvidenceStatus.NOT_FOUND
                if self.version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            )
            if self.evidence_status is not expected:
                raise ValueError(f"未取到记录时 evidence_status 必须是 {expected.value}")
            if self.outcome is not RegulatoryOutcome.UNKNOWN:
                raise ValueError("未取到记录时不得给出监管结论，更不能当成无监管")
            if self.measures or self.dual_use or self.evidence:
                raise ValueError("未取到记录时不得携带措施、线索或虚构证据")
            if self.effective_status is not EffectiveStatus.UNKNOWN:
                raise ValueError("未取到记录时不得声明记录有效期状态")
            if self.measures_state is not MeasuresState.NOT_DECLARED:
                raise ValueError("未取到记录时不得声明措施声明状态")
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
        if self.direction is not self.query.direction:
            raise ValueError("direction 必须回填调用方给出的方向，不能擅自改判")
        if self.measures_state is not record.measures_state:
            raise ValueError("measures_state 必须由记录计算")
        if [item.model_dump() for item in self.measures] != [
            item.model_dump() for item in (record.measures or [])
        ]:
            raise ValueError("measures 必须与记录逐条一致，不能补造或改名称")
        if [item.model_dump() for item in self.dual_use] != [
            item.model_dump() for item in record.dual_use
        ]:
            raise ValueError("dual_use 必须与记录逐条一致，不能补造线索")
        expected_effective = resolve_effective_status(record, self.query.basis, self.version)
        if self.effective_status is not expected_effective:
            raise ValueError(
                f"effective_status 必须由记录有效期与申报日期计算，期望 {expected_effective.value}"
            )
        if self.effective_status is not EffectiveStatus.UNKNOWN and not (
            self.query.basis.declaration_date or ""
        ).strip():
            raise ValueError("记录有效期状态必须有业务申报日期才能声明")
        matched = direction_matches(record.direction, self.query.direction)
        if self.direction_matched != matched:
            raise ValueError("direction_matched 必须由记录方向与查询方向计算")
        expected_outcome = resolve_outcome(
            record, self.version, expected_effective, dataset=self.dataset, direction_ok=matched
        )
        if self.outcome is not expected_outcome:
            raise ValueError(f"outcome 必须由来源与基准计算，期望 {expected_outcome.value}")
        expected_usable = resolve_usable_as_basis(
            expected_outcome, self.dataset, record, matched
        )
        if self.usable_as_basis != expected_usable:
            raise ValueError("usable_as_basis 必须按来源权威、版本、方向与可定位性计算")
        expected_review = resolve_expert_review(
            expected_outcome, expected_usable, record
        )
        if self.expert_review_required != expected_review:
            raise ValueError("expert_review_required 必须按结论与线索计算")

        located_measures = [item for item in self.measures if item.located]
        located_dual = [item for item in self.dual_use if item.located]
        if len(self.evidence) != 1 + len(located_measures) + len(located_dual):
            raise ValueError("证据必须为 1 条记录证据 + 每条可定位措施/线索各 1 条")
        head = self.evidence[0]
        if head.status is not self.evidence_status:
            raise ValueError("记录证据状态必须与 evidence_status 一致")
        if head.evidence_id != f"reg:{record.version}:{record.hs_code}:{record.direction.value}":
            raise ValueError("记录证据 ID 必须是 reg:<版本>:<税号>:<方向>")
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
# 纯函数：有效期、方向、结论
# ---------------------------------------------------------------------------


def _record_window(
    record: RegulatoryRecord, version: VersionResolution
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
    record: RegulatoryRecord, basis: QueryBasis, version: VersionResolution
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
        return EffectiveStatus.UNKNOWN
    return EffectiveStatus.UNKNOWN


def direction_matches(record_direction: Direction, query_direction: Direction) -> bool:
    """记录方向与查询方向是否匹配；``both`` 双向适用，``unknown`` 一律不匹配。"""

    if query_direction is Direction.UNKNOWN or record_direction is Direction.UNKNOWN:
        return False
    if record_direction is Direction.BOTH:
        return query_direction in (Direction.IMPORT, Direction.EXPORT)
    return record_direction is query_direction


def resolve_outcome(
    record: RegulatoryRecord | None,
    version: VersionResolution,
    effective: EffectiveStatus,
    *,
    dataset: RegulatoryDatasetInfo | None,
    direction_ok: bool,
) -> RegulatoryOutcome:
    """来源层面的结论：只有“明确声明 + 日期/方向可核”才可能 ``declared_none``。"""

    if record is None or dataset is None or not version.verified:
        return RegulatoryOutcome.UNKNOWN
    if not direction_ok:
        return RegulatoryOutcome.UNKNOWN
    if record.state is RecordState.ABOLISHED:
        return RegulatoryOutcome.NOT_EFFECTIVE
    if effective is EffectiveStatus.NOT_YET_EFFECTIVE or effective is EffectiveStatus.EXPIRED:
        return RegulatoryOutcome.NOT_EFFECTIVE
    if effective is not EffectiveStatus.EFFECTIVE:
        return RegulatoryOutcome.UNKNOWN
    if record.state is not RecordState.IN_FORCE:
        return RegulatoryOutcome.UNKNOWN
    if record.measures_state is MeasuresState.NOT_DECLARED:
        return RegulatoryOutcome.UNKNOWN
    if record.measures_state is MeasuresState.DECLARED_NONE:
        return RegulatoryOutcome.DECLARED_NONE
    return RegulatoryOutcome.MEASURES_REQUIRED


def resolve_usable_as_basis(
    outcome: RegulatoryOutcome,
    dataset: RegulatoryDatasetInfo | None,
    record: RegulatoryRecord | None,
    direction_ok: bool,
) -> bool:
    """能否作为合规依据：来源权威 + 结论明确 + 方向已核 + 每条措施可定位且含义已知。

    只有代码没有名称时，不能“知道要办证”更不知道办什么证，只能算线索。
    """

    if dataset is None or record is None:
        return False
    if not dataset.authoritative or not direction_ok:
        return False
    if outcome not in (RegulatoryOutcome.MEASURES_REQUIRED, RegulatoryOutcome.DECLARED_NONE):
        return False
    return all(
        item.located and item.meaning_known for item in (record.measures or [])
    )


def resolve_expert_review(
    outcome: RegulatoryOutcome,
    usable: bool,
    record: RegulatoryRecord | None,
) -> bool:
    """是否需要转专家：结论不可用、两用物项命中、措施含义未知都要转。"""

    if not usable:
        return True
    if record is None:
        return True
    if record.dual_use:
        return True
    for measure in record.measures or []:
        if measure.category is MeasureCategory.UNKNOWN or not measure.meaning_known:
            return True
    return False


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegulatorySnapshot:
    dataset: RegulatoryDatasetInfo
    versions: tuple[VersionWindow, ...]
    records: tuple[RegulatoryRecord, ...]


class RegulatoryStore(Protocol):
    """可替换的监管条件存储：实现 ``load()`` 即可接本地文件、数据库或授权接口。"""

    def load(self) -> RegulatorySnapshot: ...


def default_regulatory_path() -> Path | None:
    override = os.environ.get(ENV_REGULATORY_PATH, "").strip()
    return Path(override).expanduser() if override else None


def _validate_schema_version(payload: Mapping[str, Any], *, label: str) -> None:
    version = payload.get("schema_version")
    if version is None:
        raise SourceStoreError(f"{label}文件缺少 schema_version，不能按未知结构静默导入")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SourceStoreError(
            f"{label}文件结构版本不受支持：{version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )


def _dataset_from_payload(payload: Any, *, source_name: str) -> RegulatoryDatasetInfo:
    if payload is None:
        payload = {
            "dataset_id": source_name,
            "source_type": RegulatorySourceKind.SYNTHETIC.value,
            "authority": "unspecified",
            "synthetic": None,
            "description": "数据文件未声明 dataset；按未声明来源处理",
        }
    if not isinstance(payload, Mapping):
        raise SourceStoreError(f"监管条件文件的 dataset 必须是对象：{source_name}")
    try:
        dataset = RegulatoryDatasetInfo.model_validate(dict(payload))
    except ValidationError as exc:
        raise SourceStoreError(
            f"监管条件文件的 dataset 描述不可用（{_summarize_validation_error(exc)}）"
        ) from exc
    if not dataset.dataset_id:
        dataset = dataset.model_copy(update={"dataset_id": source_name})
    return dataset


def _snapshot_from_payload(payload: Any, *, source_name: str) -> RegulatorySnapshot:
    if not isinstance(payload, Mapping):
        raise SourceStoreError(f"监管条件文件顶层必须是对象：{source_name}")
    _validate_schema_version(payload, label="监管条件")
    dataset = _dataset_from_payload(payload.get("dataset"), source_name=source_name)

    versions_raw = payload.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        raise SourceStoreError(f"监管条件文件缺少非空 versions 数组：{source_name}")
    versions: list[VersionWindow] = []
    for index, item in enumerate(versions_raw):
        try:
            versions.append(VersionWindow.model_validate(item))
        except ValidationError as exc:
            raise SourceStoreError(
                f"第 {index + 1} 个监管条件版本不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
    seen_versions = {window.version for window in versions}
    if len(seen_versions) != len(versions):
        raise SourceStoreError(f"监管条件文件存在重复的版本号：{source_name}")

    records_raw = payload.get("records")
    if not isinstance(records_raw, list):
        raise SourceStoreError(f"监管条件文件缺少 records 数组：{source_name}")
    records: list[RegulatoryRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(records_raw):
        try:
            record = RegulatoryRecord.model_validate(item)
        except ValidationError as exc:
            raise SourceStoreError(
                f"第 {index + 1} 条监管记录不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
        if record.version not in seen_versions:
            raise SourceStoreError(
                f"第 {index + 1} 条监管记录引用未声明的版本：{record.version}"
            )
        key = (record.version, record.hs_code, record.direction.value)
        if key in seen:
            raise SourceStoreError(
                f"监管记录重复：{record.version} / {record.hs_code} / {record.direction.value}"
            )
        seen.add(key)
        records.append(record)
    return RegulatorySnapshot(
        dataset=dataset, versions=tuple(versions), records=tuple(records)
    )


class JsonRegulatoryStore:
    """本地 JSON 监管条件存储。文件格式见 D6 §2.6 与回交文档。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RegulatorySnapshot:
        payload = read_policy_document(self._path, label="监管条件")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class EnvRegulatoryStore(JsonRegulatoryStore):
    """按 ``CUSTOMS_REGULATORY_PATH`` 读取；变量缺失时在 ``load()`` 明确报“未配置”。"""

    def __init__(self) -> None:
        self._path = default_regulatory_path()

    @property
    def path(self) -> Path | None:  # type: ignore[override]
        return self._path

    def load(self) -> RegulatorySnapshot:
        if self._path is None:
            raise SourceStoreNotConfigured(
                f"未配置监管条件来源：设置环境变量 {ENV_REGULATORY_PATH} 指向导入的监管条件 JSON；"
                "本包不提供默认样本，避免把演示数据当成真实监管条件"
            )
        payload = read_policy_document(self._path, label="监管条件")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryRegulatoryStore:
    """内存存储：测试或上游已持有快照时注入，不读写文件。"""

    def __init__(
        self,
        records: Iterable[RegulatoryRecord | Mapping[str, Any]],
        versions: Iterable[VersionWindow | Mapping[str, Any]],
        *,
        dataset: RegulatoryDatasetInfo | Mapping[str, Any],
    ):
        self._raw_records = tuple(records)
        self._raw_versions = tuple(versions)
        self._dataset = dataset

    def load(self) -> RegulatorySnapshot:
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
                    f"第 {index + 1} 个监管条件版本不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            versions.append(parsed)
        records: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_records):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, RegulatoryRecord)
                    else RegulatoryRecord.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise SourceStoreError(
                    f"第 {index + 1} 条监管记录不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            records.append(parsed)
        dataset = (
            self._dataset.model_dump(mode="json")
            if isinstance(self._dataset, RegulatoryDatasetInfo)
            else RegulatoryDatasetInfo.model_validate(self._dataset).model_dump(mode="json")
        )
        return _snapshot_from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": dataset,
                "versions": versions,
                "records": records,
            },
            source_name="in-memory",
        )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


class RegulatorySource:
    """按税号 + 方向查询监管条件。

    典型用法::

        source = RegulatorySource()                    # 读 CUSTOMS_REGULATORY_PATH
        result = source.lookup(RegulatoryQuery(
            hs_code_raw="8544422900",
            direction=Direction.EXPORT,
            basis=QueryBasis(declaration_date="2026-05-20"),
        ))
        if result.usable_as_basis and result.outcome is RegulatoryOutcome.DECLARED_NONE:
            ...                                        # 才可读作“来源明确无监管要求”

    未配置来源时 ``query_status=source_unconfigured``、``evidence_status=query_failed``，
    ``outcome`` 保持 ``unknown``，不会回退到合成数据，也不会抛 500。
    """

    def __init__(self, store: RegulatoryStore | None = None, *, clock: Any = None):
        self._store = store or EnvRegulatoryStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "RegulatorySource":
        return cls(JsonRegulatoryStore(path), **kwargs)

    def lookup(
        self, query: RegulatoryQuery | Mapping[str, Any] | None = None
    ) -> RegulatoryResult:
        """执行一次查询；任何输入/来源问题都返回结构化结果，不向上抛异常。"""

        prepared, problem, issue_code = self._prepare(query)
        raw_code = _raw_field(query, "hs_code_raw")
        if prepared is None:
            return self._invalid(problem, raw=raw_code, code=issue_code)

        fmt = classify_hs_format(prepared.hs_code_raw)
        now = _as_aware(self._clock()).isoformat(timespec="seconds")

        if fmt.state in (
            HsFormatState.EMPTY,
            HsFormatState.NONDIGIT,
            HsFormatState.WRONG_LENGTH,
        ):
            return self._invalid(
                f"编码格式不可查询：{fmt.reason}；未发起查询，不能据此判定无监管要求",
                raw=prepared.hs_code_raw,
                fmt=fmt,
                basis=prepared.basis,
                direction=prepared.direction,
                searched_at=now,
                code="REG_FORMAT_INVALID",
            )

        if prepared.direction is Direction.UNKNOWN:
            # 方向是必需输入：监管条件按方向不同（A 类进口、B 类出口），
            # 不默认方向，也不允许“没方向但结论是无监管”。
            return self._invalid(
                "未给出进出口方向；监管条件按方向不同，系统不默认方向，"
                "也不能据此判定无监管要求",
                raw=prepared.hs_code_raw,
                fmt=fmt,
                basis=prepared.basis,
                direction=prepared.direction,
                searched_at=now,
                code="REG_DIRECTION_UNKNOWN",
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
                f"监管条件查询发生未预期异常（{type(exc).__name__}）；"
                "这不等于无监管要求，需修复存储后重试",
                prepared,
                fmt,
                now,
            )

        version = resolve_version(prepared.basis, snapshot.versions, dataset=snapshot.dataset)
        record = _match_record(snapshot, version, fmt, prepared.direction)
        if record is None:
            return self._not_found(prepared, fmt, version, snapshot, now)
        return self._found(prepared, fmt, version, record, snapshot, now)

    # --- 构造结果 ---

    @staticmethod
    def _prepare(
        query: RegulatoryQuery | Mapping[str, Any] | None,
    ) -> tuple[RegulatoryQuery | None, str, str]:
        if isinstance(query, RegulatoryQuery):
            raw: RegulatoryQuery | Mapping[str, Any] = query
        elif isinstance(query, Mapping):
            payload = dict(query)
            for key, message in _FORBIDDEN_INPUT_KEYS.items():
                if key in payload:
                    return None, f"{message}；未发起查询", "REG_QUERY_REFUSED"
            try:
                raw = RegulatoryQuery.model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                return (
                    None,
                    f"查询输入不符合契约（{_summarize_validation_error(exc)}）；未发起查询",
                    "REG_QUERY_INVALID",
                )
        elif query is None:
            return None, "查询为空；未发起查询", "REG_QUERY_INVALID"
        else:
            return (
                None,
                f"查询输入类型不受支持（{type(query).__name__}）；未发起查询",
                "REG_QUERY_INVALID",
            )

        try:
            prepared = RegulatoryQuery(
                hs_code_raw="" if raw.hs_code_raw is None else str(raw.hs_code_raw),
                direction=(
                    raw.direction
                    if isinstance(raw.direction, Direction)
                    else Direction(str(raw.direction))
                ),
                basis=(
                    raw.basis
                    if isinstance(raw.basis, QueryBasis)
                    else QueryBasis.model_validate(raw.basis)
                ),
                note="" if raw.note is None else str(raw.note),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return (
                None,
                f"查询输入无法解析（{_summarize_validation_error(exc)}）；未发起查询",
                "REG_QUERY_INVALID",
            )
        return prepared, "", ""

    def _invalid(
        self,
        reason: str,
        *,
        raw: str,
        code: str = "REG_QUERY_INVALID",
        fmt: HsFormat | None = None,
        basis: QueryBasis | None = None,
        direction: Direction = Direction.UNKNOWN,
        searched_at: str | None = None,
    ) -> RegulatoryResult:
        fmt = fmt or classify_hs_format(raw)
        basis = basis or QueryBasis()
        return RegulatoryResult(
            query=RegulatoryQuery(hs_code_raw=raw, direction=direction, basis=basis),
            query_status=QueryStatus.INVALID_INPUT,
            evidence_status=None,
            format=fmt,
            version=resolve_version(basis, ()),
            direction=direction,
            reason=reason,
            pending_reasons=[reason],
            issues=[
                IssueEntry(
                    code=code,
                    message=reason,
                    field="supplier_hs_raw",
                    missing="可查询的 10 位编码、进出口方向与申报基准",
                    suggested_role="supplier",
                )
            ],
            searched_at=searched_at,
        )

    def _failed(
        self,
        status: QueryStatus,
        reason: str,
        query: RegulatoryQuery,
        fmt: HsFormat,
        now: str,
    ) -> RegulatoryResult:
        message = f"监管条件查询失败：{reason}；这不等于无监管要求"
        return RegulatoryResult(
            query=query,
            query_status=status,
            evidence_status=EvidenceStatus.QUERY_FAILED,
            format=fmt,
            version=resolve_version(query.basis, ()),
            direction=query.direction,
            reason=message,
            pending_reasons=[message],
            issues=[
                IssueEntry(
                    code="REG_QUERY_FAILED",
                    message=f"监管来源不可用：{reason}",
                    field="supplier_hs_raw",
                    missing="可用的监管条件来源",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    def _not_found(
        self,
        query: RegulatoryQuery,
        fmt: HsFormat,
        version: VersionResolution,
        snapshot: RegulatorySnapshot,
        now: str,
    ) -> RegulatoryResult:
        issues: list[IssueEntry] = []
        pending: list[str] = []
        if version.verified:
            reason = (
                f"监管条件版本 {version.version} 未收录编码 {fmt.normalized}"
                f"（方向 {query.direction.value}）；这是查无记录，不等于无监管要求"
            )
            pending.append("来源未收录该税号/方向：需补来源或转专家，不能当成无监管")
            issues.append(
                IssueEntry(
                    code="REG_NOT_FOUND",
                    message=reason,
                    field="supplier_hs_raw",
                    missing=f"{version.version} 期监管记录",
                    suggested_role="expert",
                )
            )
            other = sorted(
                {
                    f"{item.version}/{item.direction.value}"
                    for item in snapshot.records
                    if item.hs_code == fmt.normalized
                }
            )
            if other:
                issues.append(
                    IssueEntry(
                        code="REG_OTHER_VERSION_OR_DIRECTION",
                        message=(
                            f"同一编码在其它版本/方向有记录（{'、'.join(other)}），"
                            "不能据此判定本期本方向无监管"
                        ),
                        field="direction",
                        suggested_role="expert",
                    )
                )
        else:
            reason = (
                f"未能核实编码 {fmt.normalized or fmt.raw} 的监管条件：{version.reason}；"
                "不能宣称无监管要求"
            )
            pending.append("缺少可确认的查询基准，监管条件未核对")
            issues.append(
                IssueEntry(
                    code="REG_VERSION_UNVERIFIED",
                    message=reason,
                    field="query_basis",
                    missing="业务申报日期或监管条件版本",
                    suggested_role="expert",
                )
            )
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="REG_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目；监管条件通常按 10 位维护，可能不同",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        return RegulatoryResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=(
                EvidenceStatus.NOT_FOUND if version.verified else EvidenceStatus.VERSION_UNVERIFIED
            ),
            format=fmt,
            version=version,
            direction=query.direction,
            reason=reason,
            pending_reasons=pending,
            issues=issues,
            dataset=snapshot.dataset,
            searched_at=now,
        )

    def _found(
        self,
        query: RegulatoryQuery,
        fmt: HsFormat,
        version: VersionResolution,
        record: RegulatoryRecord,
        snapshot: RegulatorySnapshot,
        now: str,
    ) -> RegulatoryResult:
        dataset = snapshot.dataset
        effective = resolve_effective_status(record, query.basis, version)
        matched = direction_matches(record.direction, query.direction)
        outcome = resolve_outcome(
            record, version, effective, dataset=dataset, direction_ok=matched
        )
        usable = resolve_usable_as_basis(outcome, dataset, record, matched)
        issues, pending = self._found_issues(
            query, record, version, dataset, effective, matched, fmt, outcome
        )
        evidence = self._evidence(query, fmt, version, record, dataset, effective)
        measures = list(record.measures or [])
        dual_use = list(record.dual_use)

        reason = (
            f"在监管条件版本 {record.version} 查到 {record.hs_code}"
            f"（方向 {record.direction.value}）："
            + (
                "来源明确声明无监管要求"
                if outcome is RegulatoryOutcome.DECLARED_NONE
                else f"{len(measures)} 项监管/检验检疫要求"
                if outcome is RegulatoryOutcome.MEASURES_REQUIRED
                else "不能给出监管结论"
            )
            + f"；申报日适用性 {effective.value}；方向核对 {'一致' if matched else '不一致'}"
        )
        if dual_use:
            reason += f"；另有 {len(dual_use)} 条两用物项线索需专家判定"
        if not dataset.authoritative:
            reason += "；来源非权威，不能作为合规依据，更不能读作“无监管”"

        return RegulatoryResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=(
                EvidenceStatus.OBTAINED if version.verified else EvidenceStatus.VERSION_UNVERIFIED
            ),
            format=fmt,
            version=version,
            record=record,
            direction=query.direction,
            direction_matched=matched,
            outcome=outcome,
            measures_state=record.measures_state,
            measures=measures,
            dual_use=dual_use,
            effective_status=effective,
            authoritative=dataset.authoritative,
            usable_as_basis=usable,
            expert_review_required=resolve_expert_review(outcome, usable, record),
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
        query: RegulatoryQuery,
        record: RegulatoryRecord,
        version: VersionResolution,
        dataset: RegulatoryDatasetInfo,
        effective: EffectiveStatus,
        direction_ok: bool,
        fmt: HsFormat,
        outcome: RegulatoryOutcome,
    ) -> tuple[list[IssueEntry], list[str]]:
        issues: list[IssueEntry] = []
        pending: list[str] = []

        if not version.verified:
            issues.append(
                IssueEntry(
                    code="REG_VERSION_UNVERIFIED",
                    message=f"监管记录已取到，但适用版本未核实：{version.reason}",
                    field="query_basis",
                    missing="业务申报日期或监管条件版本",
                    suggested_role="expert",
                )
            )
            pending.append(f"监管条件版本未核实：{version.reason}")
        if not dataset.authoritative:
            issues.append(
                IssueEntry(
                    code="REG_AUTHORITY_NOT_OFFICIAL",
                    message=(
                        f"来源权威级别为 {dataset.authority.value}、类型为 {dataset.source_type}；"
                        "只能作线索，不能作为合规依据"
                    ),
                    field="evidence",
                    missing="官方监管来源（含复核记录）",
                    suggested_role="operator",
                )
            )
            pending.append("来源非权威：不能读作“无监管”或“有监管”的结论")

        if not direction_ok:
            if query.direction is Direction.UNKNOWN:
                issues.append(
                    IssueEntry(
                        code="REG_DIRECTION_UNKNOWN",
                        message="未给出进出口方向；监管条件按方向不同，不能默认按某一方向核对",
                        field="direction",
                        missing="进口/出口方向",
                        suggested_role="supplier",
                    )
                )
                pending.append("缺进出口方向，未核对")
            else:
                issues.append(
                    IssueEntry(
                        code="REG_DIRECTION_MISMATCH",
                        message=(
                            f"来源记录方向为 {record.direction.value}，与查询方向 "
                            f"{query.direction.value} 不一致；不能据此方向判定无监管"
                        ),
                        field="direction",
                        suggested_role="expert",
                    )
                )
                pending.append("记录方向与查询方向不一致")
        if record.measures_state is MeasuresState.NOT_DECLARED:
            issues.append(
                IssueEntry(
                    code="REG_MEASURES_NOT_DECLARED",
                    message="来源未声明该税号的监管要求字段；不得推定“无监管”",
                    field="evidence",
                    missing="来源中的监管要求字段",
                    suggested_role="expert",
                )
            )
            pending.append("来源未声明监管要求")

        for index, measure in enumerate(record.measures or [], start=1):
            if not measure.meaning_known:
                issues.append(
                    IssueEntry(
                        code="REG_MEASURE_NAME_UNKNOWN",
                        message=(
                            f"第 {index} 项措施只有代码 {measure.code!r}，来源未给出证件名称；"
                            "不得由本模块或模型补出含义"
                        ),
                        field="evidence",
                        missing="监管证件代码表（含名称与主管机关）",
                        suggested_role="expert",
                    )
                )
                pending.append(f"第 {index} 项措施代码 {measure.code} 含义未取得")
            if measure.category is MeasureCategory.UNKNOWN:
                pending.append(f"第 {index} 项措施类别未说明")
            if not measure.located:
                issues.append(
                    IssueEntry(
                        code="REG_MEASURE_UNLOCATED",
                        message=f"第 {index} 项措施没有文档与条款定位，不能作为可回查证据",
                        field="evidence",
                        missing="措施所在代码表/公告条款",
                        suggested_role="operator",
                    )
                )
                pending.append(f"第 {index} 项措施无法定位")

        for index, item in enumerate(record.dual_use, start=1):
            if not item.located:
                issues.append(
                    IssueEntry(
                        code="REG_DUAL_USE_UNLOCATED",
                        message=f"第 {index} 条两用物项线索没有文档与条款定位，不能作为可回查证据",
                        field="evidence",
                        missing="清单条目定位",
                        suggested_role="operator",
                    )
                )
            pending.append(
                f"两用物项线索 {index}（{item.list_name}）需专家按技术参数、最终用途与用户判定"
            )
            issues.append(
                IssueEntry(
                    code="REG_DUAL_USE_UNVERIFIED",
                    message=(
                        f"命中两用物项清单线索（{item.list_name}）；是否属于管制物项"
                        "必须由专家判定，不自动认定，也不靠改名规避"
                    ),
                    field="supplier_hs_raw",
                    missing="技术参数与最终用途/用户确认",
                    suggested_role="expert",
                )
            )

        if record.state is RecordState.ABOLISHED:
            issues.append(
                IssueEntry(
                    code="REG_RECORD_ABOLISHED",
                    message="来源声明该监管记录已废止；不能作为当期申报依据",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )
            pending.append("来源声明记录已废止")
        elif record.state is RecordState.UNKNOWN:
            issues.append(
                IssueEntry(
                    code="REG_RECORD_STATE_UNKNOWN",
                    message="来源未声明该监管记录是否现行有效；不能当成已核实的当期要求",
                    field="evidence",
                    suggested_role="expert",
                )
            )
            pending.append("来源未声明记录状态")

        if effective is EffectiveStatus.NOT_YET_EFFECTIVE:
            issues.append(
                IssueEntry(
                    code="REG_RECORD_NOT_EFFECTIVE",
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
                    code="REG_RECORD_NOT_EFFECTIVE",
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
                        code="REG_EFFECTIVE_DATE_UNKNOWN",
                        message="未提供业务申报日期，无法核对监管条件在该日是否适用；系统不代填申报日期",
                        field="query_basis",
                        missing="业务申报日期",
                        suggested_role="expert",
                    )
                )
                pending.append("缺业务申报日期，未核对申报日适用性")
            else:
                issues.append(
                    IssueEntry(
                        code="REG_RECORD_EFFECTIVE_UNVERIFIED",
                        message="记录只提供部分生效/失效日期，无法确认申报日期是否在有效期内",
                        field="query_basis",
                        missing="完整的记录有效期",
                        suggested_role="expert",
                    )
                )
                pending.append("记录有效期不完整")

        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="REG_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目；监管条件通常按 10 位维护，可能不同",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )

        if outcome is RegulatoryOutcome.DECLARED_NONE and dataset.authoritative:
            pending.append("来源明确声明无监管要求；仍需专家确认该条政策适用于本票业务")
        return issues, pending

    # --- 证据 ---

    def _evidence(
        self,
        query: RegulatoryQuery,
        fmt: HsFormat,
        version: VersionResolution,
        record: RegulatoryRecord,
        dataset: RegulatoryDatasetInfo,
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
        summary = (
            "来源明确声明无监管要求"
            if record.measures_state is MeasuresState.DECLARED_NONE
            else f"监管措施代码 {', '.join(item.code for item in record.measures or [])}"
            if record.measures
            else "来源未声明监管要求字段"
        )
        head = EvidenceEntry(
            evidence_id=f"reg:{record.version}:{record.hs_code}:{record.direction.value}",
            source_type=dataset.source_type,
            title=(
                f"监管条件 {record.version}：{record.hs_code}"
                f"（{record.direction.value}）{record.product_name}"
            ).strip(),
            status=status,
            url=source.url,
            document=source.document,
            locator=source.locator,
            excerpt=summary,
            retrieved_at=source.retrieved_at,
            effective_from=evidence_from,
            effective_to=evidence_to,
            scope=(
                f"监管条件版本 {record.version}；方向 {record.direction.value}；"
                f"记录状态 {record.state.value}；申报日适用性 {effective.value}"
            ),
            query={
                "hs_code": fmt.normalized,
                "direction": record.direction.value,
                "basis": query.basis.model_dump(),
            },
            note=(
                f"来源类型 {dataset.source_type}；权威级别 {dataset.authority.value}；"
                f"{summary}；措施声明状态 {record.measures_state.value}"
                + ("；仅作线索，不能作为合规依据" if not dataset.authoritative else "")
            ),
        )
        entries = [head]
        for index, measure in enumerate(record.measures or [], start=1):
            if not measure.located:
                continue
            entries.append(
                EvidenceEntry(
                    evidence_id=(
                        f"reg:{record.version}:{record.hs_code}:"
                        f"{record.direction.value}:measure{index}"
                    ),
                    source_type=dataset.source_type,
                    title=f"监管措施 {measure.code}（{record.hs_code} 第 {index} 项）",
                    status=status,
                    url=measure.url,
                    document=measure.document,
                    locator=measure.locator,
                    excerpt=(measure.name or f"代码 {measure.code}（来源未给名称）"),
                    retrieved_at=source.retrieved_at,
                    effective_from=evidence_from,
                    effective_to=evidence_to,
                    scope=(
                        f"措施类别 {measure.category.value}；"
                        f"主管机关 {measure.agency or '来源未给出'}"
                    ),
                    query={"hs_code": fmt.normalized, "measure_code": measure.code},
                    note=measure.note or "措施含义与要求以来源原文为准，本模块不改写",
                )
            )
        for index, item in enumerate(record.dual_use, start=1):
            if not item.located:
                continue
            entries.append(
                EvidenceEntry(
                    evidence_id=(
                        f"reg:{record.version}:{record.hs_code}:"
                        f"{record.direction.value}:dual{index}"
                    ),
                    source_type=dataset.source_type,
                    title=f"两用物项线索 {index}：{item.list_name}",
                    status=status,
                    url=item.url,
                    document=item.document,
                    locator=item.locator,
                    excerpt=item.item_description or item.technical_criteria or None,
                    retrieved_at=source.retrieved_at,
                    effective_from=evidence_from,
                    effective_to=evidence_to,
                    scope="两用物项清单线索（是否适用需专家判定）",
                    query={"hs_code": fmt.normalized, "list_name": item.list_name},
                    note=item.note or "清单线索原样保留，是否属于管制物项不由本模块判定",
                )
            )
        return entries


def _match_record(
    snapshot: RegulatorySnapshot,
    version: VersionResolution,
    fmt: HsFormat,
    direction: Direction,
) -> RegulatoryRecord | None:
    """按版本 + 税号 + 方向取记录；方向不明或版本不唯一时不猜。"""

    if fmt.state not in (HsFormatState.VALID, HsFormatState.SUBHEADING_ONLY):
        return None
    if direction is Direction.UNKNOWN:
        return None
    candidates = [
        item
        for item in snapshot.records
        if item.hs_code == fmt.normalized and direction_matches(item.direction, direction)
    ]
    if version.version is not None:
        candidates = [item for item in candidates if item.version == version.version]
    elif len(snapshot.versions) != 1:
        return None
    else:
        only = snapshot.versions[0].version
        candidates = [item for item in candidates if item.version == only]
    if not candidates:
        return None
    # 精确方向优先于 both：同一版本可能既有 both 也有细分记录。
    exact = [item for item in candidates if item.direction is direction]
    if exact:
        return exact[0]
    return candidates[0]


def _raw_field(query: Any, field: str) -> str:
    if isinstance(query, RegulatoryQuery):
        return getattr(query, field) or ""
    if isinstance(query, Mapping):
        value = query.get(field)
        return "" if value is None else str(value)
    return ""


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 来源能力清单（D4 §7 / D6 §2.6）
# ---------------------------------------------------------------------------


class RegulatorySourceCapability(Schema):
    """一个候选监管来源的可用能力与限制；供 D4/D6 与 P18 判断“能不能查”。"""

    source_id: str
    name: str
    content: str = Field(description="measures / dual_use")
    source_type: RegulatorySourceKind
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
REGULATORY_SOURCE_CAPABILITIES: tuple[RegulatorySourceCapability, ...] = (
    RegulatorySourceCapability(
        source_id="single-window-params",
        name="中国国际贸易单一窗口参数查询（监管条件/检验检疫类别）",
        content="measures",
        source_type=RegulatorySourceKind.OFFICIAL_REGULATORY,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.REQUIRES_AUTHORIZATION,
        auth_required=True,
        fields=["税号", "监管条件代码", "检验检疫类别", "商品名称"],
        limitations=[
            "页面为 JavaScript 单页应用，未登录只返回加载提示；无公开数据接口",
            "需企业账号与授权；自动化访问前需确认单一窗口使用条款",
            "代码含义（证件名称/主管机关）需另取官方代码表；只拿到代码不能下结论",
        ],
        url="https://www.singlewindow.cn/",
        checked_at="2026-11-17",
        check_note="门户 https 可达（HTTP 200，0.5s）；P14 已记录正文只有“需要启用 JavaScript”，本机未取得参数数据。",
    ),
    RegulatorySourceCapability(
        source_id="customs-regulatory-portal",
        name="海关总署监管条件/检验检疫要求公告",
        content="measures",
        source_type=RegulatorySourceKind.OFFICIAL_REGULATORY,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.UNREACHABLE,
        auth_required=False,
        fields=["公告标题", "公告号", "监管条件调整", "检验检疫要求"],
        limitations=[
            "官方无开放 API；只能页面查询或授权导出",
            "本机访问 www.customs.gov.cn 不通（连接失败/TLS 异常），与 P14 结论一致",
        ],
        url="https://www.customs.gov.cn/",
        checked_at="2026-11-17",
        check_note="curl 12s 超时，http_code=000；需 Jay 提供可用的导出文件或内网接口。",
    ),
    RegulatorySourceCapability(
        source_id="mofcom-export-control",
        name="商务部中国出口管制信息网（两用物项清单）",
        content="dual_use",
        source_type=RegulatorySourceKind.OFFICIAL_DUAL_USE,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.AVAILABLE,
        auth_required=False,
        fields=["清单名称", "管制物项描述", "技术门槛/管制参数", "公告号与生效日期"],
        limitations=[
            "清单文本可公开查阅，但“是否属于管制物项”必须由专家按技术参数、最终用途与用户判定",
            "清单条目与 HS 编码不是一一对应，不能按码自动命中即认定需办证",
            "本包未抓取入库；只人工确认页面可访问且含两用物项清单栏目",
        ],
        url="http://exportcontrol.mofcom.gov.cn/",
        checked_at="2026-11-17",
        check_note="页面可达（HTTP 200，0.45s），正文含“两用物项”“清单”；未取具体清单条目原文。",
    ),
    RegulatorySourceCapability(
        source_id="third-party-regulatory",
        name="第三方监管条件查询站",
        content="measures",
        source_type=RegulatorySourceKind.THIRD_PARTY,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.AVAILABLE,
        auth_required=False,
        fields=["税号", "监管条件代码（可能滞后）"],
        limitations=[
            "整理自公开数据，可能滞后、缺项或错误；只能作初筛",
            "不得作为合规结论，也不得写成 authoritative 证据",
        ],
        url=None,
        checked_at="2026-11-17",
        check_note="本包未访问第三方站点；不提供具体站点以免被当成依据。",
    ),
)


def describe_sources(*, content: str | None = None) -> list[RegulatorySourceCapability]:
    """返回监管来源能力清单；可按 ``measures``/``dual_use`` 过滤。"""

    items = list(REGULATORY_SOURCE_CAPABILITIES)
    if content:
        items = [item for item in items if item.content == content]
    return items


def configured_regulatory_path() -> Path | None:
    """当前环境是否配置了监管条件文件；供 D6 自检与页面提示，不读取文件内容。"""

    return default_regulatory_path()
