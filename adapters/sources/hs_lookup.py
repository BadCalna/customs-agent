"""HS 编码来源适配（P14）。

定位：只回答“这个编码在某个税则版本里是什么、状态如何”，不替引擎归类
--------------------------------------------------------------------------
本模块是事实来源，不是归类引擎：

- 不接收品名/规格去“猜码”，也不产生 ``suggested_hs``；按码查询、按版本核对；
- 不把“来源里没有”写成“编码无效”，不把“查询失败”写成“查无此码”；
- 不填拟申报日期：查询基准只能由调用方给出，缺基准就保持 ``version_unverified``；
- 不访问网络、不读数据库、不调用模型；数据通过可替换的
  :class:`TariffStore` 注入（默认读环境变量指向的本地 JSON）。

四态与版本
----------
D4 的 ``obtained / not_found / query_failed / version_unverified`` 与查询结果
的对应关系由 :class:`HsLookupResult` 的校验器强制：

===============  ==========================================  =====================
情形              evidence_status                             说明
===============  ==========================================  =====================
输入不合法        ``None``                                    没有发起查询
来源未配置/读不了  ``query_failed``                            技术失败，可重试
查到编码 + 版本已核 ``obtained``                               可作为该版本的事实
查到编码 + 版本待核 ``version_unverified``                     数据取到了，但版本不确认
未查到 + 版本已核   ``not_found``                               是“本版本未收录”，不是“无效”
未查到 + 版本待核   ``version_unverified``                     不能宣称该码不存在
===============  ==========================================  =====================

格式、存在性、有效性、版本是四个独立字段（A05）：格式异常/8 位子目/长度不对
分别记录；编码是否存在由来源回答；是否作废只看来源声明的 ``state``，不按
“查不到”推断，也不按本机日期推断。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from ninja import Schema
from pydantic import Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus, IssueEntry

#: 税则数据文件的结构版本；结构变化时递增，旧格式明确报错而不是猜。
SCHEMA_VERSION = "p14.tariff.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 数据文件路径的环境变量。**没有默认样本**：未配置就明确报“来源未配置”，
#: 不能让演示数据在无人察觉时充当真实税则。
ENV_TARIFF_PATH = "CUSTOMS_TARIFF_PATH"

#: 税则与申报要素数据共用同一套版本窗口；要素路径变量在 declaration_elements 中。

_HSPACE = re.compile(r"[\s.\-‐‑–—_]+")
#: 不可见/排版字符（含零宽空格、BOM、NBSP、各类空白），用于“去空白后非空”校验。
_INVISIBLE = re.compile(r"[\u0000-\u0020\u00a0\u1680\u2000-\u200f\u2028\u2029\u202f\u205f\u3000\ufeff]+")


def _strip_invisible(value: str | None) -> str:
    return _INVISIBLE.sub("", "" if value is None else value)


class SourceKind(str, Enum):
    """来源类型。第三方与合成数据在任何情况下都不能成为合规依据。"""

    OFFICIAL_TARIFF = "official_tariff"
    OFFICIAL_DECLARATION = "official_declaration"
    THIRD_PARTY = "third_party"
    SYNTHETIC = "synthetic"


class Authority(str, Enum):
    """来源权威级别；与 ``SourceKind`` 分开，避免“官方类型”自动等于“已核实”。"""

    AUTHORITATIVE = "authoritative"
    UNVERIFIED_THIRD_PARTY = "unverified_third_party"
    SYNTHETIC = "synthetic"
    UNSPECIFIED = "unspecified"


class ReviewStatus(str, Enum):
    """数据是否经过关务人员复核；官方来源导入后也仍须复核。"""

    UNREVIEWED = "unreviewed"
    REVIEWED = "reviewed"


class QueryStatus(str, Enum):
    """一次查询的技术状态；与证据四态分开，避免“没查”被写成“没查到”。"""

    OK = "ok"
    INVALID_INPUT = "invalid_input"
    SOURCE_UNCONFIGURED = "source_unconfigured"
    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_ERROR = "source_error"


class VersionStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class HsFormatState(str, Enum):
    """编码格式的分类；长度 8 是“税目/子目”，不是“错码”。"""

    VALID = "valid"
    SUBHEADING_ONLY = "subheading_only"
    EMPTY = "empty"
    NONDIGIT = "nondigit"
    WRONG_LENGTH = "wrong_length"


class CodeState(str, Enum):
    """来源声明的编码状态；``not_found`` 只表示本版本未收录。"""

    ACTIVE = "active"
    EXPIRED = "expired"
    MERGED = "merged"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class EffectiveStatus(str, Enum):
    """编码记录在“申报日期”这一天的适用性，与来源声明的状态分开。"""

    EFFECTIVE = "effective"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    EXPIRED = "expired"
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
# 基础 DTO
# ---------------------------------------------------------------------------


class SourceRef(Schema):
    """来源引用：必须能定位到具体文件/公告与条款位置，才允许进入证据。"""

    system: str = Field(default="", description="来源系统或发布渠道")
    document: str = Field(min_length=1, description="文件/公告/数据批次标识，原样保留")
    locator: str = Field(min_length=1, description="条款、页码、税则号列或数据行定位")
    url: str | None = Field(default=None, description="可打开的来源地址；没有就留空，不编造")
    publisher: str = Field(default="", description="发布方")
    retrieved_at: str | None = Field(default=None, description="取得数据的时间")

    @model_validator(mode="after")
    def _require_locatable(self) -> "SourceRef":
        if not _strip_invisible(self.document):
            raise ValueError("document 去除不可见字符后不能为空，否则无法定位来源")
        if not _strip_invisible(self.locator):
            raise ValueError("locator 去除不可见字符后不能为空，否则无法定位来源")
        return self


class DatasetInfo(Schema):
    """数据集说明。``synthetic``/``authority`` 是给下游的诚实标记。"""

    dataset_id: str = Field(default="", description="数据批次标识")
    source_type: SourceKind = Field(description="official_tariff / official_declaration / third_party / synthetic")
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

    @model_validator(mode="after")
    def _authority_consistency(self) -> "DatasetInfo":
        if self.source_type is SourceKind.SYNTHETIC:
            if self.synthetic is False:
                raise ValueError("source_type=synthetic 时 synthetic 不能为 false")
            if self.authority is Authority.AUTHORITATIVE:
                raise ValueError("合成数据不能声明 authoritative")
        elif self.synthetic is True:
            raise ValueError("synthetic=true 只能与 source_type=synthetic 一起使用")
        if self.source_type in (SourceKind.OFFICIAL_TARIFF, SourceKind.OFFICIAL_DECLARATION):
            if self.authority in (Authority.SYNTHETIC, Authority.UNVERIFIED_THIRD_PARTY):
                raise ValueError("官方来源不能声明 synthetic / unverified_third_party")
        if self.source_type is SourceKind.THIRD_PARTY:
            if self.authority not in (Authority.UNVERIFIED_THIRD_PARTY, Authority.UNSPECIFIED):
                raise ValueError("第三方来源只能声明 unverified_third_party 或 unspecified")
        if self.review_status is ReviewStatus.REVIEWED:
            if not _strip_invisible(self.reviewed_by) or not self.reviewed_at:
                raise ValueError("reviewed 数据必须记录 reviewed_by 与 reviewed_at")
            if _parse_date(self.reviewed_at) is None:
                raise ValueError("reviewed_at 必须是 ISO 日期/时间")
            if _parse_date(self.retrieved_at) is None:
                raise ValueError("已复核数据的 retrieved_at 必须是 ISO 日期/时间")
        return self

    @property
    def authoritative(self) -> bool:
        return (
            self.authority is Authority.AUTHORITATIVE
            and self.source_type
            in (SourceKind.OFFICIAL_TARIFF, SourceKind.OFFICIAL_DECLARATION)
            and self.synthetic is not True
            and self.review_status is ReviewStatus.REVIEWED
        )


class QueryBasis(Schema):
    """查询基准：申报日期或税则版本，只能由调用方给出。

    **系统不填拟申报日期**（D1/R12、D3 第 5 节）。两个字段都为空时，结果保持
    ``version_unverified``，由业务员/专家补基准后再查；不得用查询当天顶替。
    """

    declaration_date: str | None = Field(
        default=None, description="业务申报日期原文（YYYY-MM-DD 或带时区 ISO）；系统不生成"
    )
    tariff_version: str | None = Field(default=None, description="税则版本，如 2026")
    note: str = Field(default="", description="基准来源说明，例如“取自申报要素表拟申报日期列”")

    @property
    def empty(self) -> bool:
        return not (self.declaration_date or "").strip() and not (self.tariff_version or "").strip()

    def describe(self) -> str:
        parts: list[str] = []
        if (self.declaration_date or "").strip():
            parts.append(f"申报日期 {self.declaration_date.strip()}")
        if (self.tariff_version or "").strip():
            parts.append(f"税则版本 {self.tariff_version.strip()}")
        if (self.note or "").strip():
            parts.append(self.note.strip())
        return "；".join(parts) if parts else "未提供查询基准"


class VersionWindow(Schema):
    """一个税则版本的有效期；``effective_to`` 为空表示来源未给出结束日期。"""

    version: str = Field(min_length=1)
    effective_from: str = Field(min_length=1)
    effective_to: str | None = None
    label: str = ""
    source: SourceRef | None = None

    @model_validator(mode="after")
    def _check_order(self) -> "VersionWindow":
        start = _parse_date(self.effective_from)
        end = _parse_date(self.effective_to) if self.effective_to else None
        if start is None:
            raise ValueError(f"版本 {self.version} 的 effective_from 不是有效日期")
        if self.effective_to and end is None:
            raise ValueError(f"版本 {self.version} 的 effective_to 不是有效日期")
        if end and end < start:
            raise ValueError(f"版本 {self.version} 的结束日期早于开始日期")
        return self


class VersionResolution(Schema):
    """查询基准 → 税则版本的解析结果；不唯一/缺基准一律 unverified。"""

    status: VersionStatus
    version: str | None = None
    basis_note: str = ""
    reason: str = ""
    window: VersionWindow | None = None

    @property
    def verified(self) -> bool:
        return self.status is VersionStatus.VERIFIED

    @model_validator(mode="after")
    def _check_verified(self) -> "VersionResolution":
        if self.status is VersionStatus.VERIFIED:
            if not (self.version or "").strip():
                raise ValueError("verified 必须携带确定的税则版本")
            if self.window is None:
                raise ValueError("verified 必须携带匹配的版本有效期窗口")
        return self


class HsFormat(Schema):
    """编码格式分类；保留原文，规范化只用于查询。"""

    state: HsFormatState
    raw: str = ""
    normalized: str = ""
    length: int = 0
    reason: str = ""

    @property
    def queryable(self) -> bool:
        return self.state in (HsFormatState.VALID, HsFormatState.SUBHEADING_ONLY)

    @property
    def declaration_ready(self) -> bool:
        return self.state is HsFormatState.VALID


class TariffCode(Schema):
    """来源中的一个税则号列记录。"""

    version: str = Field(min_length=1)
    code: str = Field(min_length=8, max_length=10, description="8 位税目/子目或 10 位申报编码")
    description: str = ""
    chapter: str = ""
    heading: str = ""
    subheading: str = ""
    state: CodeState = CodeState.UNKNOWN
    merged_to: str | None = Field(default=None, description="state=merged 时指向的新码")
    effective_from: str | None = None
    effective_to: str | None = None
    source: SourceRef
    note: str = ""

    @model_validator(mode="after")
    def _check_record(self) -> "TariffCode":
        if not self.code.isdigit() or len(self.code) not in (8, 10):
            raise ValueError(f"税则记录的 code 必须是 8 或 10 位数字：{self.code!r}")
        if self.state is CodeState.MERGED and not (self.merged_to or "").strip():
            raise ValueError(f"编码 {self.code} 标记 merged 但未给出 merged_to")
        start = _parse_date(self.effective_from) if self.effective_from else None
        end = _parse_date(self.effective_to) if self.effective_to else None
        if self.effective_from and start is None:
            raise ValueError(f"编码 {self.code} 的 effective_from 不是有效日期")
        if self.effective_to and end is None:
            raise ValueError(f"编码 {self.code} 的 effective_to 不是有效日期")
        if start and end and end < start:
            raise ValueError(f"编码 {self.code} 的结束日期早于开始日期")
        return self


class HsQuery(Schema):
    """按码查询的输入。刻意不含品名/规格：本适配器不负责“从描述猜码”。"""

    hs_code_raw: str = ""
    basis: QueryBasis = Field(default_factory=QueryBasis)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


class HsLookupResult(Schema):
    """一次 HS 查询的完整结果；字段之间的一致性由校验器强制。"""

    query: HsQuery | None = None
    query_status: QueryStatus
    evidence_status: EvidenceStatus | None = Field(
        default=None, description="D4 四态；输入不合法时为 null（未发起查询）"
    )
    format: HsFormat
    version: VersionResolution
    code: TariffCode | None = None
    code_state: CodeState = CodeState.UNKNOWN
    effective_status: EffectiveStatus = Field(
        default=EffectiveStatus.UNKNOWN,
        description="按申报日期核对编码记录有效期；未提供日期时为 unknown",
    )
    declaration_ready: bool = Field(
        default=False,
        description="仅当 10 位格式、版本已核、来源权威且编码为 active 时才为 true",
    )
    authoritative: bool = Field(default=False, description="来源是否可作为合规依据")
    usable_as_basis: bool = Field(default=False, description="本次结果能否作为申报依据")
    reason: str = ""
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    dataset: DatasetInfo | None = None
    searched_at: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "HsLookupResult":
        if self.usable_as_basis != self.declaration_ready:
            raise ValueError("usable_as_basis 与 declaration_ready 必须一致")
        if self.declaration_ready and (
            self.query_status is not QueryStatus.OK or self.code is None
        ):
            raise ValueError("declaration_ready 必须来自一次成功查询且携带税则记录")

        if self.query_status is QueryStatus.INVALID_INPUT:
            if self.evidence_status is not None:
                raise ValueError("invalid_input 必须先于查询，evidence_status 必须为 null")
            if self.code is not None or self.code_state is not CodeState.UNKNOWN:
                raise ValueError("invalid_input 不得携带税则记录或存在性结论")
            if self.evidence:
                raise ValueError("invalid_input 不得携带证据")
            return self
        if self.query_status is not QueryStatus.OK:
            if self.evidence_status is not EvidenceStatus.QUERY_FAILED:
                raise ValueError(f"{self.query_status.value} 必须对应 query_failed")
            if self.code is not None:
                raise ValueError("查询失败时不得携带税则记录")
            if self.evidence:
                raise ValueError("查询失败时不得生成虚构证据")
            return self

        if self.code is None:
            if self.effective_status is not EffectiveStatus.UNKNOWN:
                raise ValueError("没有税则记录时不得声明编码记录有效期状态")
            expected = (
                EvidenceStatus.NOT_FOUND
                if self.version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            )
            if self.evidence_status is not expected:
                raise ValueError(
                    f"未查到编码时 evidence_status 必须是 {expected.value}"
                )
            expected_code_state = (
                CodeState.NOT_FOUND if self.version.verified else CodeState.UNKNOWN
            )
            if self.code_state is not expected_code_state:
                raise ValueError(
                    f"未查到编码时 code_state 必须是 {expected_code_state.value}；"
                    "版本待核不能声称该码不存在"
                )
            if self.evidence:
                raise ValueError("未取到税则记录时不得生成虚构证据")
            return self

        expected = (
            EvidenceStatus.OBTAINED
            if self.version.verified
            else EvidenceStatus.VERSION_UNVERIFIED
        )
        if self.evidence_status is not expected:
            raise ValueError(
                f"查到编码时 evidence_status 必须是 {expected.value}"
            )
        if self.code_state is CodeState.NOT_FOUND:
            raise ValueError("携带税则记录时 code_state 不能是 not_found")
        if self.code_state is not self.code.state:
            raise ValueError("code_state 必须与 code.state 一致")
        if self.dataset is not None and self.authoritative != self.dataset.authoritative:
            raise ValueError("authoritative 必须由 dataset 的来源/复核状态计算，不能手工覆盖")
        if self.dataset is None:
            raise ValueError("查到税则记录时必须携带 dataset，不能省略来源元数据")
        if self.query is None:
            raise ValueError("查到税则记录时必须携带 query，不能省略查询基准")
        expected_effective = _effective_status(self.code, self.query.basis, self.version)
        if self.effective_status is not expected_effective:
            raise ValueError(
                f"effective_status 必须由记录有效期与申报日期计算，期望 {expected_effective.value}"
            )
        if self.effective_status is not EffectiveStatus.UNKNOWN:
            if not (self.query.basis.declaration_date or "").strip():
                raise ValueError("编码记录有效期状态必须有业务申报日期才能声明")
        if len(self.evidence) != 1 or self.evidence[0].status is not self.evidence_status:
            raise ValueError("查到税则记录时必须且只能携带一条同状态的可定位证据")
        evidence = self.evidence[0]
        if not _strip_invisible(evidence.document) or not _strip_invisible(evidence.locator):
            raise ValueError("证据必须带非空 document 与 locator，否则无法回查")
        for field in ("retrieved_at", "effective_from", "effective_to"):
            value = getattr(evidence, field)
            if value and _parse_date(value) is None:
                raise ValueError(f"证据的 {field} 必须是 ISO 日期/时间")
        if self.authoritative and not evidence.retrieved_at:
            raise ValueError("权威来源的证据必须记录 retrieved_at（取得时间）")
        if self.declaration_ready:
            if self.format.state is not HsFormatState.VALID:
                raise ValueError("declaration_ready 要求 10 位完整编码")
            if not self.version.verified:
                raise ValueError("declaration_ready 要求版本已核")
            if self.effective_status is not EffectiveStatus.EFFECTIVE:
                raise ValueError("declaration_ready 要求编码记录在申报日期有效")
            if not self.authoritative or not self.usable_as_basis:
                raise ValueError("declaration_ready 要求来源权威且可作为依据")
            if self.code.state is not CodeState.ACTIVE:
                raise ValueError("declaration_ready 要求来源声明编码为 active")
        return self

    def to_evidence_entries(self) -> list[EvidenceEntry]:
        """返回证据条目；查无/失败不生成虚构条款，返回空列表。"""

        return list(self.evidence)


# ---------------------------------------------------------------------------
# 查询基准与版本
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    """只接受 ISO 日期/时间；无法解析就保持待核，绝不截断后猜测。"""

    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00")).date()
    except ValueError:
        return None


def _validity_window(record: TariffCode, version: VersionResolution) -> tuple[date | None, date | None]:
    """编码自身的有效期优先；记录完全没给日期时才退回版本窗口，不混用两套边界。"""

    if record.effective_from or record.effective_to:
        return _parse_date(record.effective_from), _parse_date(record.effective_to)
    if version.window is not None:
        return _parse_date(version.window.effective_from), _parse_date(version.window.effective_to)
    return None, None


def _effective_status(
    record: TariffCode, basis: QueryBasis, version: VersionResolution
) -> EffectiveStatus:
    """按业务申报日期判断编码记录是否适用；没有日期就不冒充“已核对”。"""

    query_date = _parse_date(basis.declaration_date)
    if query_date is None:
        return EffectiveStatus.UNKNOWN
    start, end = _validity_window(record, version)
    if start is not None and query_date < start:
        return EffectiveStatus.NOT_YET_EFFECTIVE
    if end is not None and query_date > end:
        return EffectiveStatus.EXPIRED
    if record.effective_from or record.effective_to:
        # 记录自身只给了一端日期：另一端未知，不能判“整个区间有效”。
        if start is None or end is None:
            return EffectiveStatus.UNKNOWN
    return EffectiveStatus.EFFECTIVE


def resolve_version(
    basis: QueryBasis | None,
    versions: Sequence[VersionWindow],
    *,
    dataset: DatasetInfo | None = None,
) -> VersionResolution:
    """把查询基准解析为唯一税则版本；任何不确定都返回 ``unverified``。

    规则（顺序即优先级）：

    1. 两个基准都为空 → 未提供基准（**不用今天代替**）；
    2. 给了版本但数据集未收录 → 未核实（不能拿别的版本顶替）；
    3. 同时给日期和版本 → 日期必须落在该版本有效期内；
    4. 只给日期 → 必须有且仅有一个版本窗口覆盖该日期；
    5. 日期无法解析 → 未核实，原因写清楚。

    数据集本身是合成/第三方时，版本可以“解析成功”，但是否权威由
    ``DatasetInfo.authoritative`` 单独表达，不在这里混为一谈。
    """

    basis = basis or QueryBasis()
    basis_note = basis.describe()
    windows = list(versions)
    wanted_version = (basis.tariff_version or "").strip()
    wanted_date = _parse_date(basis.declaration_date)

    if basis.empty:
        return VersionResolution(
            status=VersionStatus.UNVERIFIED,
            basis_note=basis_note,
            reason="未提供查询基准（申报日期或税则版本）；系统不代填拟申报日期",
        )

    if not windows:
        return VersionResolution(
            status=VersionStatus.UNVERIFIED,
            version=wanted_version or None,
            basis_note=basis_note,
            reason="来源未声明任何税则版本有效期，无法核实适用版本",
        )

    if (basis.declaration_date or "").strip() and wanted_date is None:
        return VersionResolution(
            status=VersionStatus.UNVERIFIED,
            version=wanted_version or None,
            basis_note=basis_note,
            reason=f"申报日期无法解析（{basis.declaration_date!r}），不能用查询当天代替",
        )

    if wanted_version:
        window = next((item for item in windows if item.version == wanted_version), None)
        if window is None:
            return VersionResolution(
                status=VersionStatus.UNVERIFIED,
                version=wanted_version,
                basis_note=basis_note,
                reason=f"来源未收录税则版本 {wanted_version}；不能用其它版本顶替",
            )
        start = _parse_date(window.effective_from)
        end = _parse_date(window.effective_to) if window.effective_to else None
        if wanted_date is not None:
            if start and wanted_date < start:
                return VersionResolution(
                    status=VersionStatus.UNVERIFIED,
                    version=wanted_version,
                    basis_note=basis_note,
                    reason=(
                        f"申报日期 {wanted_date.isoformat()} 早于版本 {wanted_version} 的"
                        f"生效日期 {window.effective_from}"
                    ),
                    window=window,
                )
            if end and wanted_date > end:
                return VersionResolution(
                    status=VersionStatus.UNVERIFIED,
                    version=wanted_version,
                    basis_note=basis_note,
                    reason=(
                        f"申报日期 {wanted_date.isoformat()} 晚于版本 {wanted_version} 的"
                        f"失效日期 {window.effective_to}"
                    ),
                    window=window,
                )
        return VersionResolution(
            status=VersionStatus.VERIFIED,
            version=wanted_version,
            basis_note=basis_note,
            reason=f"按调用方给出的税则版本 {wanted_version} 查询"
            + ("，且申报日期落在有效期内" if wanted_date else "（未提供申报日期，仅按版本核对）"),
            window=window,
        )

    # 只给了日期
    assert wanted_date is not None
    matching = []
    for window in windows:
        start = _parse_date(window.effective_from)
        end = _parse_date(window.effective_to) if window.effective_to else None
        if start is None:
            continue
        if start <= wanted_date and (end is None or wanted_date <= end):
            matching.append(window)
    if len(matching) == 1:
        window = matching[0]
        return VersionResolution(
            status=VersionStatus.VERIFIED,
            version=window.version,
            basis_note=basis_note,
            reason=f"申报日期 {wanted_date.isoformat()} 落在税则版本 {window.version} 有效期内",
            window=window,
        )
    if not matching:
        return VersionResolution(
            status=VersionStatus.UNVERIFIED,
            basis_note=basis_note,
            reason=(
                f"申报日期 {wanted_date.isoformat()} 不在来源已收录的任何税则版本有效期内；"
                "不推断适用版本"
            ),
        )
    return VersionResolution(
        status=VersionStatus.UNVERIFIED,
        basis_note=basis_note,
        reason=(
            "多个税则版本的有效期同时覆盖该申报日期（"
            + "、".join(window.version for window in matching)
            + "），无法唯一确定适用版本"
        ),
    )


# ---------------------------------------------------------------------------
# 编码格式
# ---------------------------------------------------------------------------


def classify_hs_format(raw: str | None) -> HsFormat:
    """把编码原文分类；``raw`` 原样保留，只对查询副本做 NFKC 与分隔符清理。"""

    original = "" if raw is None else str(raw)
    text = unicodedata.normalize("NFKC", original).strip()
    if not text:
        return HsFormat(
            state=HsFormatState.EMPTY,
            raw=original,
            reason="编码为空；原值保持空白，不用建议值回填",
        )
    compact = _HSPACE.sub("", text)
    if not compact.isdigit():
        return HsFormat(
            state=HsFormatState.NONDIGIT,
            raw=original,
            normalized="",
            reason="编码含非数字字符；保留原文，不猜测或替换字符",
        )
    if len(compact) == 10:
        return HsFormat(
            state=HsFormatState.VALID,
            raw=original,
            normalized=compact,
            length=10,
            reason="10 位申报编码",
        )
    if len(compact) == 8:
        return HsFormat(
            state=HsFormatState.SUBHEADING_ONLY,
            raw=original,
            normalized=compact,
            length=8,
            reason="8 位税目/子目，不是 10 位申报编码；可查税则层级，但无法确认附加编号",
        )
    return HsFormat(
        state=HsFormatState.WRONG_LENGTH,
        raw=original,
        normalized=compact,
        length=len(compact),
        reason=f"编码长度为 {len(compact)} 位，既不是 8 位税目/子目也不是 10 位申报编码",
    )


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class TariffStoreError(RuntimeError):
    """税则存储不可读/格式不可用；调用方以 query_failed 表达，不能伪装成查无。"""


class TariffStoreNotConfigured(TariffStoreError):
    """没有配置任何税则来源。与“文件坏了”分开，提示先接入来源。"""


@dataclass(frozen=True)
class TariffSnapshot:
    dataset: DatasetInfo
    versions: tuple[VersionWindow, ...]
    codes: tuple[TariffCode, ...]


class TariffStore(Protocol):
    """可替换的税则存储：实现 ``load()`` 即可接本地文件、数据库或授权接口。"""

    def load(self) -> TariffSnapshot: ...


def default_tariff_path() -> Path | None:
    override = os.environ.get(ENV_TARIFF_PATH, "").strip()
    return Path(override).expanduser() if override else None


def read_json_document(path: Path, *, label: str) -> Any:
    """读取 JSON 文件，错误信息带字段名但不泄露服务器绝对路径。"""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TariffStoreError(f"{label}文件不可读：{path.name}（{type(exc).__name__}）") from exc
    except UnicodeError as exc:
        raise TariffStoreError(
            f"{label}文件不是 UTF-8 编码：{path.name}（{type(exc).__name__}）"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TariffStoreError(
            f"{label}文件不是合法 JSON：{path.name}（第 {exc.lineno} 行）"
        ) from exc


def _validate_schema_version(payload: Mapping[str, Any], *, label: str) -> None:
    version = payload.get("schema_version")
    if version is None:
        raise TariffStoreError(f"{label}文件缺少 schema_version，不能按未知结构静默导入")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TariffStoreError(
            f"{label}文件结构版本不受支持：{version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )


def _summarize_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError) and exc.errors():
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "input"
        return f"{location}: {first.get('msg', '输入不合法')}"
    return type(exc).__name__


def _dataset_from_payload(payload: Any, *, source_name: str) -> DatasetInfo:
    if payload is None:
        payload = {
            "dataset_id": source_name,
            "source_type": SourceKind.SYNTHETIC,
            "authority": Authority.UNSPECIFIED,
            "synthetic": None,
            "description": "数据文件未声明 dataset；按未声明来源处理",
        }
    if not isinstance(payload, Mapping):
        raise TariffStoreError(f"税则文件的 dataset 必须是对象：{source_name}")
    try:
        dataset = DatasetInfo.model_validate(dict(payload))
    except ValidationError as exc:
        raise TariffStoreError(
            f"税则文件的 dataset 描述不可用（{_summarize_validation_error(exc)}）"
        ) from exc
    if not dataset.dataset_id:
        dataset = dataset.model_copy(update={"dataset_id": source_name})
    return dataset


def _snapshot_from_payload(payload: Any, *, source_name: str) -> TariffSnapshot:
    if not isinstance(payload, Mapping):
        raise TariffStoreError(f"税则文件顶层必须是对象：{source_name}")
    _validate_schema_version(payload, label="税则")
    dataset = _dataset_from_payload(payload.get("dataset"), source_name=source_name)

    versions_raw = payload.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        raise TariffStoreError(f"税则文件缺少非空 versions 数组：{source_name}")
    versions: list[VersionWindow] = []
    for index, item in enumerate(versions_raw):
        try:
            versions.append(VersionWindow.model_validate(item))
        except ValidationError as exc:
            raise TariffStoreError(
                f"第 {index + 1} 个税则版本不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
    seen_versions = {window.version for window in versions}
    if len(seen_versions) != len(versions):
        raise TariffStoreError(f"税则文件存在重复的版本号：{source_name}")

    codes_raw = payload.get("codes")
    if not isinstance(codes_raw, list):
        raise TariffStoreError(f"税则文件缺少 codes 数组：{source_name}")
    codes: list[TariffCode] = []
    seen_codes: set[tuple[str, str]] = set()
    for index, item in enumerate(codes_raw):
        try:
            record = TariffCode.model_validate(item)
        except ValidationError as exc:
            raise TariffStoreError(
                f"第 {index + 1} 条税则记录不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
        if record.version not in seen_versions:
            raise TariffStoreError(
                f"第 {index + 1} 条税则记录引用未声明的版本：{record.version}"
            )
        key = (record.version, record.code)
        if key in seen_codes:
            raise TariffStoreError(f"税则记录重复：{record.version} / {record.code}")
        seen_codes.add(key)
        codes.append(record)
    return TariffSnapshot(dataset=dataset, versions=tuple(versions), codes=tuple(codes))


class JsonTariffStore:
    """本地 JSON 税则存储。文件格式见 D6 与交接文档。

    每次 ``load()`` 重新读文件，手工导入新版本后无需重启；文件缺失/损坏/记录
    不合契约都抛 :class:`TariffStoreError`，由查询入口转成 ``query_failed``。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TariffSnapshot:
        payload = read_json_document(self._path, label="税则")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class EnvTariffStore(JsonTariffStore):
    """按 ``CUSTOMS_TARIFF_PATH`` 读取；变量缺失时在 ``load()`` 明确报“未配置”。

    构造本身不抛异常，这样 ``HsLookupSource()`` 能在未配置时仍返回结构化
    ``query_failed``，不会让调用方拿到 500。
    """

    def __init__(self) -> None:
        self._path = default_tariff_path()

    @property
    def path(self) -> Path | None:
        return self._path

    def load(self) -> TariffSnapshot:
        if self._path is None:
            raise TariffStoreNotConfigured(
                f"未配置税则来源：设置环境变量 {ENV_TARIFF_PATH} 指向导入的税则 JSON；"
                "本包不提供默认样本，避免把演示数据当成真实税则"
            )
        payload = read_json_document(self._path, label="税则")
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryTariffStore:
    """内存存储：测试或上游已持有快照时注入，不读写文件。"""

    def __init__(
        self,
        codes: Iterable[TariffCode | Mapping[str, Any]],
        versions: Iterable[VersionWindow | Mapping[str, Any]],
        *,
        dataset: DatasetInfo,
    ):
        self._raw_codes = tuple(codes)
        self._raw_versions = tuple(versions)
        self._dataset = dataset

    def load(self) -> TariffSnapshot:
        versions: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_versions):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, VersionWindow)
                    else VersionWindow.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise TariffStoreError(
                    f"第 {index + 1} 个税则版本不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            versions.append(parsed)
        codes: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_codes):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, TariffCode)
                    else TariffCode.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise TariffStoreError(
                    f"第 {index + 1} 条税则记录不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            codes.append(parsed)
        # 复用与 JSON 存储相同的结构校验：重复版本/编码、未声明版本引用、坏日期一律拒绝。
        return _snapshot_from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": self._dataset.model_dump(mode="json"),
                "versions": versions,
                "codes": codes,
            },
            source_name="in-memory",
        )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


class HsLookupSource:
    """按码查询税则记录。

    典型用法::

        source = HsLookupSource()                    # 读 CUSTOMS_TARIFF_PATH
        result = source.lookup(HsQuery(
            hs_code_raw="8544422900",
            basis=QueryBasis(declaration_date="2026-05-20"),
        ))
        if result.declaration_ready:
            engine.attach(result.to_evidence_entries())

    未配置来源时 ``query_status=source_unconfigured``、``evidence_status=query_failed``，
    不会回退到合成数据，也不会抛 500。
    """

    def __init__(self, store: TariffStore | None = None, *, clock: Any = None):
        self._store = store or EnvTariffStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "HsLookupSource":
        return cls(JsonTariffStore(path), **kwargs)

    def lookup(self, query: HsQuery | Mapping[str, Any] | None = None) -> HsLookupResult:
        """执行一次查询；任何输入/来源问题都返回结构化结果，不向上抛异常。"""

        prepared, problem = self._prepare(query)
        if prepared is None:
            return self._invalid(problem, raw=_raw_code(query))

        fmt = classify_hs_format(prepared.hs_code_raw)
        now = _as_aware(self._clock()).isoformat(timespec="seconds")

        if fmt.state in (HsFormatState.EMPTY, HsFormatState.NONDIGIT, HsFormatState.WRONG_LENGTH):
            return self._invalid(
                f"编码格式不可查询：{fmt.reason}；未发起查询，不能据此判定编码无效",
                raw=prepared.hs_code_raw,
                fmt=fmt,
                basis=prepared.basis,
                searched_at=now,
            )

        try:
            snapshot = self._store.load()
        except TariffStoreNotConfigured as exc:
            return self._failed(QueryStatus.SOURCE_UNCONFIGURED, str(exc), prepared, fmt, now)
        except TariffStoreError as exc:
            return self._failed(QueryStatus.SOURCE_UNREADABLE, str(exc), prepared, fmt, now)
        except Exception as exc:  # 存储实现自身缺陷不能变成 500
            return self._failed(
                QueryStatus.SOURCE_ERROR,
                f"税则查询发生未预期异常（{type(exc).__name__}）；这不等于查无此码，需修复存储后重试",
                prepared,
                fmt,
                now,
            )

        version = resolve_version(prepared.basis, snapshot.versions, dataset=snapshot.dataset)
        record = None
        if version.version is not None:
            record = next(
                (
                    item
                    for item in snapshot.codes
                    if item.version == version.version and item.code == fmt.normalized
                ),
                None,
            )
        elif version.status is VersionStatus.UNVERIFIED:
            # 版本不唯一时不猜：只在来源只有一个版本时才允许“版本待核但仍取样”。
            if len(snapshot.versions) == 1:
                candidate = snapshot.versions[0]
                record = next(
                    (
                        item
                        for item in snapshot.codes
                        if item.version == candidate.version
                        and item.code == fmt.normalized
                    ),
                    None,
                )

        if record is None:
            return self._not_found(prepared, fmt, version, snapshot, now)

        return self._found(prepared, fmt, version, record, snapshot, now)

    # --- 构造结果 ---

    @staticmethod
    def _prepare(
        query: HsQuery | Mapping[str, Any] | None,
    ) -> tuple[HsQuery | None, str]:
        if isinstance(query, HsQuery):
            raw = query
        elif isinstance(query, Mapping):
            try:
                raw = HsQuery.model_validate(dict(query))
            except (ValidationError, TypeError, ValueError) as exc:
                return None, f"查询输入不符合契约（{_summarize_validation_error(exc)}）；未发起查询"
        elif query is None:
            return None, "查询为空；未发起查询"
        else:
            return None, f"查询输入类型不受支持（{type(query).__name__}）；未发起查询"
        try:
            prepared = HsQuery(
                hs_code_raw="" if raw.hs_code_raw is None else str(raw.hs_code_raw),
                basis=raw.basis if isinstance(raw.basis, QueryBasis) else QueryBasis.model_validate(raw.basis),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return None, f"查询输入无法解析（{_summarize_validation_error(exc)}）；未发起查询"
        return prepared, ""

    def _invalid(
        self,
        reason: str,
        *,
        raw: str,
        fmt: HsFormat | None = None,
        basis: QueryBasis | None = None,
        searched_at: str | None = None,
    ) -> HsLookupResult:
        fmt = fmt or classify_hs_format(raw)
        basis = basis or QueryBasis()
        return HsLookupResult(
            query=HsQuery(hs_code_raw=raw, basis=basis),
            query_status=QueryStatus.INVALID_INPUT,
            evidence_status=None,
            format=fmt,
            version=resolve_version(basis, ()),
            code=None,
            code_state=CodeState.UNKNOWN,
            reason=reason,
            issues=[
                IssueEntry(
                    code="HS_FORMAT_INVALID",
                    message=reason,
                    field="supplier_hs_raw",
                    missing="10 位数字编码",
                    suggested_role="supplier",
                )
            ],
            searched_at=searched_at,
        )

    def _failed(
        self,
        status: QueryStatus,
        reason: str,
        query: HsQuery,
        fmt: HsFormat,
        now: str,
    ) -> HsLookupResult:
        return HsLookupResult(
            query=query,
            query_status=status,
            evidence_status=EvidenceStatus.QUERY_FAILED,
            format=fmt,
            version=resolve_version(query.basis, ()),
            code=None,
            code_state=CodeState.UNKNOWN,
            reason=f"税则查询失败：{reason}；这不等于查无此码，也不是编码无效",
            issues=[
                IssueEntry(
                    code="HS_QUERY_FAILED",
                    message=f"税则来源不可用：{reason}",
                    field="supplier_hs_raw",
                    missing="可用的税则来源",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    def _not_found(
        self,
        query: HsQuery,
        fmt: HsFormat,
        version: VersionResolution,
        snapshot: TariffSnapshot,
        now: str,
    ) -> HsLookupResult:
        issues: list[IssueEntry] = []
        if version.verified:
            reason = (
                f"税则版本 {version.version} 未收录编码 {fmt.normalized}；"
                "这是查无此码，不等于编码无效或已作废"
            )
            issues.append(
                IssueEntry(
                    code="HS_CODE_NOT_FOUND",
                    message=reason,
                    field="supplier_hs_raw",
                    missing=f"{version.version} 版税则记录",
                    suggested_role="expert",
                )
            )
            other = sorted(
                {
                    item.version
                    for item in snapshot.codes
                    if item.code == fmt.normalized and item.version != version.version
                }
            )
            if other:
                issues.append(
                    IssueEntry(
                        code="HS_CODE_OTHER_VERSION",
                        message=(
                            f"同一编码在其它税则版本有收录（{'、'.join(other)}），"
                            "不能据此判定本版本有效或无效"
                        ),
                        field="supplier_hs_raw",
                        suggested_role="expert",
                    )
                )
        else:
            reason = (
                f"未能核实编码 {fmt.normalized or fmt.raw} 在适用税则版本中的状态："
                f"{version.reason}；不能宣称该码不存在"
            )
            issues.append(
                IssueEntry(
                    code="HS_VERSION_UNVERIFIED",
                    message=reason,
                    field="query_basis",
                    missing="申报日期或税则版本",
                    suggested_role="expert",
                )
            )
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="HS_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目，无法确认 10 位附加编号；税则层级信息仅供参考",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        return HsLookupResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=(
                EvidenceStatus.NOT_FOUND if version.verified else EvidenceStatus.VERSION_UNVERIFIED
            ),
            format=fmt,
            version=version,
            code=None,
            code_state=CodeState.NOT_FOUND if version.verified else CodeState.UNKNOWN,
            reason=reason,
            issues=issues,
            dataset=snapshot.dataset,
            searched_at=now,
        )

    def _found(
        self,
        query: HsQuery,
        fmt: HsFormat,
        version: VersionResolution,
        record: TariffCode,
        snapshot: TariffSnapshot,
        now: str,
    ) -> HsLookupResult:
        authoritative = snapshot.dataset.authoritative
        effective = _effective_status(record, query.basis, version)
        usable = (
            authoritative
            and version.verified
            and fmt.state is HsFormatState.VALID
            and record.state is CodeState.ACTIVE
            and effective is EffectiveStatus.EFFECTIVE
        )
        issues: list[IssueEntry] = []
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            issues.append(
                IssueEntry(
                    code="HS_SUBHEADING_ONLY",
                    message="只提供 8 位税目/子目，无法确认 10 位附加编号；税则层级信息仅供参考",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        if not version.verified:
            issues.append(
                IssueEntry(
                    code="HS_VERSION_UNVERIFIED",
                    message=f"编码已取到，但适用版本未核实：{version.reason}",
                    field="query_basis",
                    missing="申报日期或税则版本",
                    suggested_role="expert",
                )
            )
        if not authoritative:
            issues.append(
                IssueEntry(
                    code="HS_AUTHORITY_NOT_OFFICIAL",
                    message=(
                        f"来源权威级别为 {snapshot.dataset.authority.value}、类型为 "
                        f"{snapshot.dataset.source_type.value}；只能作线索，不能作为申报依据"
                    ),
                    field="evidence",
                    missing="官方税则来源",
                    suggested_role="operator",
                )
            )
        if record.state is CodeState.EXPIRED:
            issues.append(
                IssueEntry(
                    code="HS_CODE_EXPIRED",
                    message="来源声明该编码已失效；原文保留，需按现行版本改用有效编码",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )
        elif record.state is CodeState.MERGED:
            issues.append(
                IssueEntry(
                    code="HS_CODE_MERGED",
                    message=f"来源声明该编码已并入 {record.merged_to}；不能原样继续申报",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )
        elif record.state is CodeState.UNKNOWN:
            issues.append(
                IssueEntry(
                    code="HS_CODE_STATE_UNKNOWN",
                    message="来源未声明该编码的有效状态；不能当成已核实有效",
                    field="supplier_hs_raw",
                    suggested_role="expert",
                )
            )

        start, end = _validity_window(record, version)
        query_date = _parse_date(query.basis.declaration_date)
        if effective is EffectiveStatus.NOT_YET_EFFECTIVE:
            issues.append(
                IssueEntry(
                    code="HS_RECORD_NOT_EFFECTIVE",
                    message=(
                        f"编码记录自 {start.isoformat()} 起生效，晚于申报日期 "
                        f"{query_date.isoformat() if query_date else query.basis.declaration_date}；"
                        "不能作为该日申报依据"
                    ),
                    field="query_basis",
                    missing="与申报日期匹配的有效编码",
                    suggested_role="expert",
                )
            )
        elif effective is EffectiveStatus.EXPIRED:
            issues.append(
                IssueEntry(
                    code="HS_RECORD_NOT_EFFECTIVE",
                    message=(
                        f"编码记录有效期至 {end.isoformat()}，早于申报日期 "
                        f"{query_date.isoformat() if query_date else query.basis.declaration_date}；"
                        "不能作为该日申报依据"
                    ),
                    field="query_basis",
                    missing="与申报日期匹配的有效编码",
                    suggested_role="expert",
                )
            )
        elif effective is EffectiveStatus.UNKNOWN and version.verified and fmt.state is HsFormatState.VALID:
            if query_date is None:
                issues.append(
                    IssueEntry(
                        code="HS_EFFECTIVE_DATE_UNKNOWN",
                        message="未提供申报日期，无法核对编码记录有效期；不标记为可申报",
                        field="query_basis",
                        missing="业务申报日期",
                        suggested_role="expert",
                    )
                )
            else:
                issues.append(
                    IssueEntry(
                        code="HS_RECORD_EFFECTIVE_UNVERIFIED",
                        message="编码记录只提供部分生效/失效日期，无法确认申报日期是否在整个有效期内；不标记为可申报",
                        field="query_basis",
                        missing="完整的编码记录有效期",
                        suggested_role="expert",
                    )
                )

        source = record.source
        if not source.retrieved_at and snapshot.dataset.retrieved_at:
            source = source.model_copy(update={"retrieved_at": snapshot.dataset.retrieved_at})
        record_has_dates = bool(record.effective_from or record.effective_to)
        evidence_from = (
            record.effective_from if record_has_dates
            else (version.window.effective_from if version.window else None)
        )
        evidence_to = (
            record.effective_to if record_has_dates
            else (version.window.effective_to if version.window else None)
        )
        evidence = EvidenceEntry(
            evidence_id=f"hs:{record.version}:{record.code}",
            source_type=snapshot.dataset.source_type.value,
            title=f"税则 {record.version}：{record.code} {record.description}".strip(),
            status=(
                EvidenceStatus.OBTAINED
                if version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            ),
            url=source.url,
            document=source.document,
            locator=source.locator,
            excerpt=record.description or None,
            retrieved_at=source.retrieved_at,
            effective_from=evidence_from,
            effective_to=evidence_to,
            scope=(
                f"税则版本 {record.version}；编码状态 {record.state.value}；"
                f"申报日期适用性 {effective.value}"
            ),
            query={
                "hs_code": fmt.normalized,
                "tariff_version": record.version,
                "basis": query.basis.model_dump(),
            },
            note=(
                f"来源类型 {snapshot.dataset.source_type.value}；权威级别 "
                f"{snapshot.dataset.authority.value}；"
                + ("可作为该版本的税则事实" if usable else "仅作线索，不能作为申报依据")
                + ("；编码已失效" if record.state is CodeState.EXPIRED else "")
                + (f"；已并入 {record.merged_to}" if record.state is CodeState.MERGED else "")
                + (f"；申报日期不在记录有效期（{effective.value}）" if effective in (
                    EffectiveStatus.NOT_YET_EFFECTIVE,
                    EffectiveStatus.EXPIRED,
                ) else "")
            ),
        )
        reason = (
            f"在税则版本 {record.version} 查到 {record.code}：{record.description or '无描述'}；"
            f"状态 {record.state.value}；申报日期适用性 {effective.value}"
        )
        if not version.verified:
            reason += f"；但版本未核实（{version.reason}）"
        if not authoritative:
            reason += "；来源非官方，不能作为申报依据"
        if effective is EffectiveStatus.NOT_YET_EFFECTIVE:
            reason += f"；记录自 {start.isoformat() if start else '未声明'} 起生效，晚于申报日期"
        elif effective is EffectiveStatus.EXPIRED:
            reason += f"；记录有效期至 {end.isoformat() if end else '未声明'}，早于申报日期"

        return HsLookupResult(
            query=query,
            query_status=QueryStatus.OK,
            evidence_status=evidence.status,
            format=fmt,
            version=version,
            code=record,
            code_state=record.state,
            effective_status=effective,
            declaration_ready=usable,
            authoritative=authoritative,
            usable_as_basis=usable,
            reason=reason,
            issues=issues,
            evidence=[evidence],
            dataset=snapshot.dataset,
            searched_at=now,
        )


def _raw_code(query: Any) -> str:
    if isinstance(query, HsQuery):
        return query.hs_code_raw
    if isinstance(query, Mapping):
        value = query.get("hs_code_raw")
        return "" if value is None else str(value)
    return ""


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 来源能力清单（D4 §6 / D6）
# ---------------------------------------------------------------------------


class SourceCapability(Schema):
    """一个候选来源的可用能力与限制；供 D4/D6 和 P16 判断“能不能查”。"""

    source_id: str
    name: str
    content: str = Field(description="tariff / declaration_elements / both")
    source_type: SourceKind
    access_mode: AccessMode
    availability: SourceAvailability
    auth_required: bool = False
    fields: list[str] = Field(default_factory=list, description="可取得的字段")
    limitations: list[str] = Field(default_factory=list)
    url: str | None = None
    checked_at: str | None = None
    check_note: str = ""


#: 2026-11-16 在本机做的可达性/字段试验结论。结论只描述“本机能否自动取数”，
#: 不代表官方数据不存在；任何“可用”都必须由实际导入的数据文件证明。
SOURCE_CAPABILITIES: tuple[SourceCapability, ...] = (
    SourceCapability(
        source_id="customs-tariff-portal",
        name="海关总署「互联网+海关」税则查询",
        content="tariff",
        source_type=SourceKind.OFFICIAL_TARIFF,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.UNREACHABLE,
        auth_required=False,
        fields=["10 位税号", "商品名称", "章节/品目层级", "税率", "监管条件"],
        limitations=[
            "官方无开放 API；只能页面查询或授权导出",
            "本机访问 www.customs.gov.cn 返回 504/超时，TLS 证书链被本机代理替换（curl 报 self signed certificate）",
            "未取得可批量导入的当期税则文件；不得用第三方页面冒充官方数据",
        ],
        url="https://www.customs.gov.cn/",
        checked_at="2026-11-16",
        check_note="curl -k 亦为 504；门户首页 12 秒无响应。需 Jay 提供浏览器可用的导出文件或内网接口。",
    ),
    SourceCapability(
        source_id="customs-declaration-catalog",
        name="《海关进出口商品涉税规范申报目录》年度版",
        content="declaration_elements",
        source_type=SourceKind.OFFICIAL_DECLARATION,
        access_mode=AccessMode.MANUAL_IMPORT,
        availability=SourceAvailability.REQUIRES_AUTHORIZATION,
        auth_required=False,
        fields=["税号", "申报要素清单及顺序", "要素类型（归类/价格/审单/原产地）", "要素说明与示例"],
        limitations=[
            "官方不提供批量下载/开放接口，需按年度版本结构化整理",
            "必填/条件必填/选填的口径需关务专家确认，不能从页面文字自行推断",
            "本包未取得 2026 年版目录原件；合成样本只验证接口，不代表真实要素清单",
        ],
        url="https://www.customs.gov.cn/",
        checked_at="2026-11-16",
        check_note="据公开转载，2026 年版自 2026-01-01 实施（海关总署公告 2025 年第 260 号）；本机未取得目录文件，公告原文未核。",
    ),
    SourceCapability(
        source_id="single-window-params",
        name="中国国际贸易单一窗口参数查询",
        content="both",
        source_type=SourceKind.OFFICIAL_TARIFF,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.REQUIRES_AUTHORIZATION,
        auth_required=True,
        fields=["税则参数", "监管条件", "申报要素参数"],
        limitations=[
            "页面为 JavaScript 单页应用，未登录无法取数；GET 仅返回加载提示",
            "需企业账号与授权；自动化访问前需确认单一窗口使用条款",
            "HEAD 请求返回 403，不能作为可用性证明",
        ],
        url="https://www.singlewindow.cn/",
        checked_at="2026-11-16",
        check_note="GET 200 但正文只有“需要启用 JavaScript”；无公开数据接口。",
    ),
    SourceCapability(
        source_id="third-party-hs",
        name="第三方 HS 编码查询站（hsbianma / i5a6 / hsguilei 等）",
        content="both",
        source_type=SourceKind.THIRD_PARTY,
        access_mode=AccessMode.PUBLIC_PAGE,
        availability=SourceAvailability.AVAILABLE,
        auth_required=False,
        fields=["编码", "商品名称", "申报要素清单（文本）", "税率/监管条件（可能滞后）"],
        limitations=[
            "整理自公开数据，可能滞后、缺项或错误；只能作初筛与字段形状参考",
            "不得作为合规结论，也不得写成 authoritative 证据",
            "抓取前需确认对方条款；本包未抓取入库，只人工核对了字段形状",
        ],
        url="https://www.i5a6.com/hscode/detail/8419399020",
        checked_at="2026-11-16",
        check_note="页面可打开，申报要素形如“1:品牌类型;2:出口享惠情况;…”，用于校验解析器字段形状。",
    ),
)


def describe_sources(*, content: str | None = None) -> list[SourceCapability]:
    """返回来源能力清单；可按 ``tariff``/``declaration_elements`` 过滤。"""

    items = list(SOURCE_CAPABILITIES)
    if content:
        items = [item for item in items if item.content in (content, "both")]
    return items


def configured_tariff_path() -> Path | None:
    """当前环境是否配置了税则文件；供 D6 自检和页面提示，不读取文件内容。"""

    return default_tariff_path()
