"""申报要素来源适配与“事实是否充足”判定（P14）。

定位：回答“这个编码按哪版目录要哪些要素、现有事实够不够”，不替业务下结论
----------------------------------------------------------------------------
本模块只做三件事：

1. 按 HS 编码 + 查询基准取该版本的申报要素清单（来源可替换，默认读环境变量
   指向的本地 JSON）；
2. 把供应商的要素原文/结构化值解析成“要素 → 值”，解析不确定就标不确定；
3. 逐项核对必填/条件必填/选填，输出 ``sufficiency``、待补项与 D4 证据。

它**不**做：推荐编码、生成要素值、猜缺失值、把“来源没收录”当成“无需申报要素”、
把“条件要素不适用”当成“缺项”。

事实充足性的判定顺序
--------------------
::

    输入不合法/编码不完整        → not_checked（未发起核对）
    来源未配置/读不了/查不到清单  → unverified（不能判充分，也不能判不足）
    版本未核实（缺基准/日期越界）  → unverified
    来源非权威（合成/第三方）      → unverified（逻辑可算，但不得作为业务结论）
    要素原文解析不完整/有歧义      → unverified（先人工核对原文）
    有必填项缺失/为空/枚举或数值非法 → insufficient（可生成补料问题）
    只有条件要素/口径未知项待确认   → unverified（转专家，不向供应商乱要）
    其余                          → sufficient

``sufficiency=sufficient`` 只在“来源权威 + 版本已核 + 清单已取得 + 解析确定 +
无缺失/无待确认”时出现；其余一律 ``unverified``，避免下游把“没查清”读成“够了”。

解析口径
--------
支持三种常见写法，均保留原文、只做保守归一：

- 位置式：``4|1|深沟球轴承|减少摩擦|SKF|6224/C3|无|无``（``|`` 分隔，按要素顺序）；
- 编号式：``1:品牌类型:无品牌;2:出口享惠情况:不享受优惠关税;...``；
- 键值式：``品牌类型:无品牌;用途:传动``。

无法唯一对应时（数量不符、编号有缺口、名称歧义、同义名指向多个要素）不猜，
标 ``partial``/``unparsed``，由人工核对原文。
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from ninja import Schema
from pydantic import Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus, IssueEntry

from .hs_lookup import (
    Authority,
    DatasetInfo,
    HsFormat,
    HsFormatState,
    QueryBasis,
    QueryStatus,
    SourceKind,
    SourceRef,
    TariffStoreError,
    VersionResolution,
    VersionStatus,
    VersionWindow,
    classify_hs_format,
    read_json_document,
    resolve_version,
    _parse_date,
    _strip_invisible,
)

#: 要素数据文件的结构版本。
SCHEMA_VERSION = "p14.elements.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 数据文件路径的环境变量。与税则一样**没有默认样本**。
ENV_ELEMENTS_PATH = "CUSTOMS_DECLARATION_ELEMENTS_PATH"

#: 输入上限：脏数据/超长原文不应拖垮核对。
MAX_RAW_LENGTH = 10_000
MAX_ELEMENTS = 200

_NUMBERED = re.compile(r"^\s*(\d+)\s*[:：]\s*(.*)$")
_WHITESPACE = re.compile(r"[\s\u3000]+")
_NAME_PUNCT = re.compile(r"[:：,，、。;；/|]")
_PAREN = re.compile(r"[（(][^）)]*[）)]")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_PERCENT = re.compile(r"^(\d+(\.\d+)?)\s*%?$")


class ElementRole(str, Enum):
    """要素用途分类，与规范申报目录的归类/价格/审单/原产地要素对应。"""

    CLASSIFICATION = "classification"
    PRICE = "price"
    CUSTOMS = "customs"
    ORIGIN = "origin"
    OTHER = "other"


class Requirement(str, Enum):
    """要素是否必填。来源没写清就用 ``unknown``，不默认成必填或选填。"""

    REQUIRED = "required"
    CONDITIONAL = "conditional"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"


class ValueType(str, Enum):
    TEXT = "text"
    ENUM = "enum"
    NUMBER = "number"
    PERCENTAGE = "percentage"
    CODE = "code"
    UNKNOWN = "unknown"


class CheckState(str, Enum):
    SATISFIED = "satisfied"
    MISSING = "missing"
    EMPTY = "empty"
    INVALID_ENUM = "invalid_enum"
    INVALID_NUMBER = "invalid_number"
    UNVERIFIABLE = "unverifiable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ParseMode(str, Enum):
    NONE = "none"
    MAPPING = "mapping"
    KEYED = "keyed"
    NUMBERED = "numbered"
    POSITIONAL = "positional"
    MIXED = "mixed"


class ParseStatus(str, Enum):
    EMPTY = "empty"
    PARSED = "parsed"
    PARTIAL = "partial"
    TEMPLATE_ONLY = "template_only"
    UNPARSED = "unparsed"


class Sufficiency(str, Enum):
    """事实是否足以支撑一次申报要素核对。"""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNVERIFIED = "unverified"
    NOT_CHECKED = "not_checked"


# ---------------------------------------------------------------------------
# 清单与输入
# ---------------------------------------------------------------------------


class ElementSpec(Schema):
    """一个申报要素的定义；``required`` 等口径以来源声明为准，不由模型推断。"""

    key: str = Field(min_length=1, description="稳定键，用作字段定位")
    label: str = Field(min_length=1, description="来源中的要素名称，原样保留")
    order: int = Field(ge=1, description="在该税号要素清单中的顺序，1 起始")
    role: ElementRole = ElementRole.OTHER
    requirement: Requirement = Requirement.UNKNOWN
    condition: str = Field(default="", description="条件要素的适用条件；为空表示来源未说明")
    value_type: ValueType = ValueType.TEXT
    enum_values: list[str] = Field(default_factory=list)
    example: str = ""
    aliases: list[str] = Field(default_factory=list, description="供应商标法/历史写法；只做精确同义，不做模糊猜")
    note: str = ""

    @model_validator(mode="after")
    def _check_spec(self) -> "ElementSpec":
        if self.value_type is ValueType.ENUM and not self.enum_values:
            raise ValueError(f"要素 {self.key} 声明为 enum 但未给出 enum_values")
        if self.requirement is Requirement.CONDITIONAL and not self.condition.strip():
            raise ValueError(f"要素 {self.key} 声明为条件必填但未说明条件")
        return self


class ElementSchema(Schema):
    """某税号在某个税则版本下的完整要素清单。"""

    hs_code: str = Field(min_length=8, max_length=10)
    description: str = ""
    version: str = Field(min_length=1)
    elements: list[ElementSpec] = Field(min_length=1)
    source: SourceRef
    note: str = ""

    @model_validator(mode="after")
    def _check_schema(self) -> "ElementSchema":
        if not self.hs_code.isdigit() or len(self.hs_code) not in (8, 10):
            raise ValueError(f"要素清单的 hs_code 必须是 8 或 10 位数字：{self.hs_code!r}")
        keys = [item.key for item in self.elements]
        if len(set(keys)) != len(keys):
            raise ValueError(f"要素清单存在重复 key：{self.hs_code} / {self.version}")
        orders = [item.order for item in self.elements]
        if len(set(orders)) != len(orders):
            raise ValueError(f"要素清单存在重复顺序：{self.hs_code} / {self.version}")
        return self

    def ordered(self) -> list[ElementSpec]:
        return sorted(self.elements, key=lambda item: item.order)


class ElementQuery(Schema):
    """核对输入：编码 + 查询基准 + 已申报要素（结构化或原文，至少给一个）。"""

    hs_code_raw: str = ""
    basis: QueryBasis = Field(default_factory=QueryBasis)
    declared: dict[str, str] = Field(
        default_factory=dict, description="结构化要素：键可以是来源 key/label/同义名"
    )
    declared_raw: str = Field(
        default="", description="供应商申报要素原文；支持 | 位置式、编号式、键值式"
    )


# ---------------------------------------------------------------------------
# 解析与逐项结果
# ---------------------------------------------------------------------------


class ElementParseReport(Schema):
    """原文解析报告；``status`` 不是 parsed 时不得给出“充分/不足”的结论。"""

    mode: ParseMode = ParseMode.NONE
    status: ParseStatus = ParseStatus.EMPTY
    token_count: int = Field(default=0, ge=0)
    matched_count: int = Field(default=0, ge=0)
    unrecognized_keys: list[str] = Field(default_factory=list)
    ambiguous_keys: list[str] = Field(default_factory=list)
    missing_orders: list[int] = Field(
        default_factory=list, description="位置式/编号式中没有对应值的要素序号"
    )
    conflicts: list[str] = Field(
        default_factory=list, description="结构化值与原文不一致的要素；以结构化值为准"
    )
    note: str = ""


class ElementCheck(Schema):
    """一个要素的核对结果；原因写清“缺什么/为什么不能判”。"""

    key: str
    label: str
    order: int
    role: ElementRole
    requirement: Requirement
    condition: str = ""
    declared_key: str | None = None
    declared_value: str | None = None
    state: CheckState
    reason: str = ""
    example: str = ""

    @property
    def blocking(self) -> bool:
        return self.state in (
            CheckState.MISSING,
            CheckState.EMPTY,
            CheckState.INVALID_ENUM,
            CheckState.INVALID_NUMBER,
        )


class SupplementItem(Schema):
    """需要供应商补充或修正的具体事实；条件要素未提供时仍转专家，不进入本清单。"""

    key: str
    label: str
    requirement: Requirement
    role: ElementRole
    example: str = ""
    reason: str = ""
    suggested_role: str = "supplier"


class DeclarationElementsResult(Schema):
    """一次要素核对结果；字段一致性由校验器强制。"""

    query: ElementQuery | None = None
    query_status: QueryStatus
    schema_status: EvidenceStatus | None = Field(
        default=None, description="清单来源的 D4 四态；输入不合法时为 null"
    )
    sufficiency: Sufficiency
    version: VersionResolution
    format: HsFormat
    schema_record: ElementSchema | None = Field(
        default=None, description="取到的要素清单；字段名为 schema_record 以避免与 pydantic 概念混淆"
    )
    checks: list[ElementCheck] = Field(default_factory=list)
    supplement_items: list[SupplementItem] = Field(default_factory=list)
    gaps: list[IssueEntry] = Field(default_factory=list)
    parse: ElementParseReport = Field(default_factory=ElementParseReport)
    authoritative: bool = False
    required_total: int = Field(default=0, ge=0)
    satisfied_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    invalid_count: int = Field(default=0, ge=0)
    unverifiable_count: int = Field(default=0, ge=0)
    reason: str = ""
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    dataset: DatasetInfo | None = None
    searched_at: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "DeclarationElementsResult":
        if self.query_status is QueryStatus.INVALID_INPUT:
            if self.schema_status is not None or self.schema_record is not None:
                raise ValueError("invalid_input 必须先于查询，不得携带清单")
            if self.sufficiency is not Sufficiency.NOT_CHECKED:
                raise ValueError("invalid_input 的 sufficiency 必须是 not_checked")
            if self.checks or self.supplement_items or self.evidence:
                raise ValueError("invalid_input 不得携带逐项结论、补料或证据")
            return self
        if self.query_status is not QueryStatus.OK:
            if self.schema_status is not EvidenceStatus.QUERY_FAILED:
                raise ValueError(f"{self.query_status.value} 必须对应 query_failed")
            if self.schema_record is not None:
                raise ValueError("查询失败时不得携带清单")
            if self.sufficiency is not Sufficiency.UNVERIFIED:
                raise ValueError("查询失败时不得声称充分或不足")
            if self.checks or self.supplement_items or self.evidence:
                raise ValueError("查询失败时不得携带逐项结论、补料或证据")
            return self

        if self.schema_record is None:
            if self.version.verified:
                if self.schema_status is not EvidenceStatus.NOT_FOUND:
                    raise ValueError("版本已核且未取到清单时，schema_status 必须是 not_found")
            elif self.schema_status is not EvidenceStatus.VERSION_UNVERIFIED:
                raise ValueError("版本待核且未取到清单时，schema_status 必须是 version_unverified")
            if self.sufficiency is not Sufficiency.UNVERIFIED:
                raise ValueError("没有清单时不能判充分或不足")
            if self.checks or self.supplement_items or self.evidence:
                raise ValueError("未取到清单时不得携带逐项结论、补料或证据")
            return self

        expected = (
            EvidenceStatus.OBTAINED
            if self.version.verified
            else EvidenceStatus.VERSION_UNVERIFIED
        )
        if self.schema_status is not expected:
            raise ValueError(f"取到清单时 schema_status 必须是 {expected.value}")
        if self.dataset is not None and self.authoritative != self.dataset.authoritative:
            raise ValueError("authoritative 必须由 dataset 的来源/复核状态计算，不能手工覆盖")
        if self.dataset is None:
            raise ValueError("取到清单时必须携带 dataset，不能省略来源元数据")
        if len(self.evidence) != 1 or self.evidence[0].status is not self.schema_status:
            raise ValueError("取到清单时必须且只能携带一条同状态的可定位证据")
        evidence = self.evidence[0]
        if not _strip_invisible(evidence.document) or not _strip_invisible(evidence.locator):
            raise ValueError("证据必须带非空 document 与 locator，否则无法回查")
        for field in ("retrieved_at", "effective_from", "effective_to"):
            value = getattr(evidence, field)
            if value and _parse_date(value) is None:
                raise ValueError(f"证据的 {field} 必须是 ISO 日期/时间")
        if self.authoritative and not evidence.retrieved_at:
            raise ValueError("权威来源的证据必须记录 retrieved_at（取得时间）")
        if self.parse.status in (ParseStatus.PARTIAL, ParseStatus.UNPARSED) and (
            self.sufficiency is not Sufficiency.UNVERIFIED
        ):
            raise ValueError("解析不确定时只能判 unverified，不能给充分/不足结论")

        schema_keys = [item.key for item in self.schema_record.ordered()]
        if [item.key for item in self.checks] != schema_keys:
            raise ValueError("checks 必须完整、按顺序覆盖清单中的每个要素")
        expected_counts = {
            "required_total": sum(
                1 for item in self.checks if item.requirement is Requirement.REQUIRED
            ),
            "satisfied_count": sum(
                1 for item in self.checks if item.state is CheckState.SATISFIED
            ),
            "missing_count": sum(
                1 for item in self.checks if item.state in (CheckState.MISSING, CheckState.EMPTY)
            ),
            "invalid_count": sum(
                1
                for item in self.checks
                if item.state in (CheckState.INVALID_ENUM, CheckState.INVALID_NUMBER)
            ),
            "unverifiable_count": sum(
                1 for item in self.checks if item.state is CheckState.UNVERIFIABLE
            ),
        }
        for field, value in expected_counts.items():
            if getattr(self, field) != value:
                raise ValueError(f"{field} 必须由 checks 派生，期望 {value}")

        if self.sufficiency is Sufficiency.SUFFICIENT:
            if not self.authoritative or not self.version.verified:
                raise ValueError("sufficient 要求来源权威且版本已核")
            if self.parse.status not in (
                ParseStatus.PARSED,
                ParseStatus.TEMPLATE_ONLY,
                ParseStatus.EMPTY,
            ):
                raise ValueError("sufficient 要求原文解析确定")
            if self.missing_count or self.invalid_count or self.unverifiable_count:
                raise ValueError("存在缺失/非法/待确认项时不能判 sufficient")
            if any(
                item.state not in (CheckState.SATISFIED, CheckState.NOT_APPLICABLE)
                for item in self.checks
            ):
                raise ValueError("sufficient 的每个要素都必须是 satisfied 或 not_applicable")
            if self.gaps:
                raise ValueError("sufficient 不得仍携带待核/补料问题")
            if (
                self.parse.conflicts
                or self.parse.ambiguous_keys
                or self.parse.unrecognized_keys
                or self.parse.missing_orders
            ):
                raise ValueError("sufficient 不得携带冲突/歧义/未识别/缺号")
        if self.sufficiency is Sufficiency.INSUFFICIENT:
            if not self.authoritative or not self.version.verified:
                raise ValueError("insufficient 要求来源权威且版本已核")
            if not any(item.blocking for item in self.checks):
                raise ValueError("insufficient 必须至少有一个缺失或非法的逐项状态")
        if self.supplement_items and self.sufficiency is not Sufficiency.INSUFFICIENT:
            raise ValueError(
                "只有 sufficiency=insufficient 才能生成供应商补料项；"
                "版本/来源/解析不确定时不得对外发起补料"
            )
        expected_supplements = [
            {
                "key": item.key,
                "label": item.label,
                "requirement": item.requirement.value,
                "role": item.role.value,
                "suggested_role": "supplier",
            }
            for item in self.checks
            if self.sufficiency is Sufficiency.INSUFFICIENT and item.blocking
        ]
        actual_supplements = [
            {
                "key": item.key,
                "label": item.label,
                "requirement": item.requirement.value,
                "role": item.role.value,
                "suggested_role": item.suggested_role,
            }
            for item in self.supplement_items
        ]
        if actual_supplements != expected_supplements:
            raise ValueError("supplement_items 必须由 insufficient 的缺失/非法项派生且字段一致")
        return self

    def to_evidence_entries(self) -> list[EvidenceEntry]:
        """返回清单来源的证据；没取到清单时为空，不生成虚构条款。"""

        return list(self.evidence)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class ElementStoreError(RuntimeError):
    """要素清单存储不可读/格式不可用；以 query_failed 表达。"""


class ElementStoreNotConfigured(ElementStoreError):
    """没有配置要素来源，与“文件坏了”分开。"""


@dataclass(frozen=True)
class ElementSnapshot:
    dataset: DatasetInfo
    versions: tuple[VersionWindow, ...]
    schemas: tuple[ElementSchema, ...]


class ElementStore(Protocol):
    def load(self) -> ElementSnapshot: ...


def default_elements_path() -> Path | None:
    override = os.environ.get(ENV_ELEMENTS_PATH, "").strip()
    return Path(override).expanduser() if override else None


def _summarize(exc: Exception) -> str:
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
        raise ElementStoreError(f"要素文件的 dataset 必须是对象：{source_name}")
    try:
        dataset = DatasetInfo.model_validate(dict(payload))
    except ValidationError as exc:
        raise ElementStoreError(
            f"要素文件的 dataset 描述不可用（{_summarize(exc)}）"
        ) from exc
    if not dataset.dataset_id:
        dataset = dataset.model_copy(update={"dataset_id": source_name})
    return dataset


def _snapshot_from_payload(payload: Any, *, source_name: str) -> ElementSnapshot:
    if not isinstance(payload, Mapping):
        raise ElementStoreError(f"要素文件顶层必须是对象：{source_name}")
    version = payload.get("schema_version")
    if version is None:
        raise ElementStoreError("要素文件缺少 schema_version，不能按未知结构静默导入")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ElementStoreError(
            f"要素文件结构版本不受支持：{version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )
    dataset = _dataset_from_payload(payload.get("dataset"), source_name=source_name)

    versions_raw = payload.get("versions")
    if not isinstance(versions_raw, list) or not versions_raw:
        raise ElementStoreError(f"要素文件缺少非空 versions 数组：{source_name}")
    versions: list[VersionWindow] = []
    for index, item in enumerate(versions_raw):
        try:
            versions.append(VersionWindow.model_validate(item))
        except ValidationError as exc:
            raise ElementStoreError(
                f"第 {index + 1} 个税则版本不符合契约（{_summarize(exc)}）"
            ) from exc
    version_names = {item.version for item in versions}
    if len(version_names) != len(versions):
        raise ElementStoreError(f"要素文件存在重复的版本号：{source_name}")

    schemas_raw = payload.get("schemas")
    if not isinstance(schemas_raw, list):
        raise ElementStoreError(f"要素文件缺少 schemas 数组：{source_name}")
    schemas: list[ElementSchema] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(schemas_raw):
        try:
            record = ElementSchema.model_validate(item)
        except ValidationError as exc:
            raise ElementStoreError(
                f"第 {index + 1} 份要素清单不符合契约（{_summarize(exc)}）"
            ) from exc
        if record.version not in version_names:
            raise ElementStoreError(
                f"第 {index + 1} 份要素清单引用未声明的版本：{record.version}"
            )
        key = (record.version, record.hs_code)
        if key in seen:
            raise ElementStoreError(f"要素清单重复：{record.version} / {record.hs_code}")
        seen.add(key)
        schemas.append(record)
    return ElementSnapshot(dataset=dataset, versions=tuple(versions), schemas=tuple(schemas))


class JsonElementStore:
    """本地 JSON 要素清单存储；每次 load 重新读取，便于年度导入。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ElementSnapshot:
        try:
            payload = read_json_document(self._path, label="申报要素")
        except TariffStoreError as exc:
            raise ElementStoreError(str(exc)) from exc
        return _snapshot_from_payload(payload, source_name=self._path.name)


class EnvElementStore(JsonElementStore):
    """按 ``CUSTOMS_DECLARATION_ELEMENTS_PATH`` 读取；未配置时在 ``load()`` 报错。"""

    def __init__(self) -> None:
        self._path = default_elements_path()

    @property
    def path(self) -> Path | None:
        return self._path

    def load(self) -> ElementSnapshot:
        if self._path is None:
            raise ElementStoreNotConfigured(
                f"未配置申报要素来源：设置环境变量 {ENV_ELEMENTS_PATH} 指向导入的要素 JSON；"
                "本包不提供默认样本，避免把演示数据当成真实目录"
            )
        try:
            payload = read_json_document(self._path, label="申报要素")
        except TariffStoreError as exc:
            raise ElementStoreError(str(exc)) from exc
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryElementStore:
    """内存存储：测试或调用方已持有清单时注入。"""

    def __init__(
        self,
        schemas: Iterable[ElementSchema | Mapping[str, Any]],
        versions: Iterable[VersionWindow | Mapping[str, Any]],
        *,
        dataset: DatasetInfo,
    ):
        self._raw_schemas = tuple(schemas)
        self._raw_versions = tuple(versions)
        self._dataset = dataset

    def load(self) -> ElementSnapshot:
        versions: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_versions):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, VersionWindow)
                    else VersionWindow.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise ElementStoreError(
                    f"第 {index + 1} 个税则版本不符合契约（{_summarize(exc)}）"
                ) from exc
            versions.append(parsed)
        schemas: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_schemas):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, ElementSchema)
                    else ElementSchema.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise ElementStoreError(
                    f"第 {index + 1} 份要素清单不符合契约（{_summarize(exc)}）"
                ) from exc
            schemas.append(parsed)
        # 与 JSON 存储共用同一套结构校验：重复清单/版本引用/枚举缺失一律拒绝。
        return _snapshot_from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": self._dataset.model_dump(mode="json"),
                "versions": versions,
                "schemas": schemas,
            },
            source_name="in-memory",
        )


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _norm_text(value: Any) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", "" if value is None else str(value))).strip()


def _name_variants(value: str) -> set[str]:
    """要素名的保守归一：去空白/标点/编号/括号注释，不做同义词扩展。"""

    text = unicodedata.normalize("NFKC", value or "").strip().casefold()
    text = _PAREN.sub("", text)
    text = re.sub(r"^\s*\d+\s*[.、:：)）]?\s*", "", text)
    text = _WHITESPACE.sub("", text)
    return {_NAME_PUNCT.sub("", text)} if text else set()


def _build_index(schema: ElementSchema) -> dict[str, list[ElementSpec]]:
    index: dict[str, list[ElementSpec]] = {}
    for spec in schema.elements:
        for name in [spec.key, spec.label, *spec.aliases]:
            for variant in _name_variants(name):
                index.setdefault(variant, []).append(spec)
    return index


@dataclass(frozen=True)
class NameMatch:
    spec: ElementSpec | None = None
    candidates: tuple[ElementSpec, ...] = ()
    problem: str = ""

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def _match_name(name: str, index: Mapping[str, list[ElementSpec]]) -> NameMatch:
    variants = _name_variants(name)
    candidates: list[ElementSpec] = []
    for variant in variants:
        for spec in index.get(variant, ()):
            if spec not in candidates:
                candidates.append(spec)
    if len(candidates) == 1:
        return NameMatch(spec=candidates[0])
    if len(candidates) > 1:
        return NameMatch(candidates=tuple(candidates), problem="要素名同时匹配多个要素，无法唯一对应")
    return NameMatch(problem="未识别的要素名")


def _keyed_segments(text: str, schema: ElementSchema) -> list[str]:
    """把键值式原文切成段：显式分隔符 + 后跟已知要素名的空白。"""

    parts = re.split(r"[;；\r\n\t]", text)
    terms = sorted(
        {
            re.escape(spec.label)
            for spec in schema.elements
        }
        | {re.escape(spec.key) for spec in schema.elements}
        | {
            re.escape(alias)
            for spec in schema.elements
            for alias in spec.aliases
            if alias.strip()
        },
        key=len,
        reverse=True,
    )
    pattern = None
    if terms:
        # 只在“空白 + 已知要素名 + 冒号”处切，尾随无冒号的要素名不切，
        # 否则会把必填值的一部分误当成独立空要素。
        pattern = re.compile(r"\s+(?=(?:" + "|".join(terms) + r")\s*[:：])")
    segments: list[str] = []
    for part in parts:
        pieces = pattern.split(part) if pattern is not None else [part]
        segments.extend(piece.strip() for piece in pieces if piece.strip())
    return segments


def _parse_raw(raw: str, schema: ElementSchema) -> tuple[dict[str, str], dict[str, str], ElementParseReport]:
    """解析原文，返回 (key→值, key→原文名, 报告)。

    任何不能唯一对应的情况都降到 partial/unparsed，绝不通过“猜位置”把缺项洗成已填。
    """

    text = unicodedata.normalize("NFKC", raw or "").strip()
    if not text:
        return {}, {}, ElementParseReport(mode=ParseMode.NONE, status=ParseStatus.EMPTY)
    if len(text) > MAX_RAW_LENGTH:
        # 不能先截断再解析：NFKC 可能把 ﬃ 扩成 ffi，截断会漏掉尾部冲突。
        return {}, {}, ElementParseReport(
            status=ParseStatus.UNPARSED,
            note=(
                f"原文归一化后长度为 {len(text)}，超过上限 {MAX_RAW_LENGTH}；"
                "未解析，避免截断后漏掉冲突，需人工核对原文"
            ),
        )

    index = _build_index(schema)
    values: dict[str, str] = {}
    labels: dict[str, str] = {}
    report = ElementParseReport(token_count=0)

    has_pipe = "|" in text
    has_semi = bool(re.search(r"[;；]", text))
    has_hard_ws = bool(re.search(r"[\r\n\t]", text))
    if has_pipe and (has_semi or has_hard_ws):
        report.status = ParseStatus.PARTIAL
        report.note = "原文混用了多种分隔符（| 与 ;/换行/制表），无法确定各段边界"
        return values, labels, report

    if has_pipe and not has_semi:
        report.mode = ParseMode.POSITIONAL
        tokens = [item.strip() for item in text.split("|")]
        report.token_count = len(tokens)
        specs = schema.ordered()

        # 先判别“竖线分隔的值”与“竖线分隔的模板/键值段”。
        # 仅按数量对应会把 品牌类型|用途|型号 当成三条已填事实，属于高危误判。
        token_matches = [_match_name(token, index) for token in tokens]
        has_colon = [bool(re.search(r"[:：]", token)) for token in tokens]
        if any(has_colon):
            if all(has_colon):
                # 名称:值|名称:值 是键值式，不是位置值；交给统一的键值解析。
                text = ";".join(tokens)
            else:
                report.status = ParseStatus.PARTIAL
                report.note = "竖线原文混用了“名称:值”和纯位置值，无法确定对应关系"
                report.unrecognized_keys = [
                    token for token, colon in zip(tokens, has_colon) if colon
                ]
                return values, labels, report
        elif all(match.spec is not None for match in token_matches):
            matched_specs = [match.spec for match in token_matches]
            if len({spec.key for spec in matched_specs}) != len(matched_specs):
                report.status = ParseStatus.PARTIAL
                report.note = "竖线模板中同一要素名重复出现，无法唯一对应"
                return values, labels, report
            for spec, token in zip(matched_specs, tokens):
                values[spec.key] = ""
                labels[spec.key] = token
            report.matched_count = len(values)
            report.status = ParseStatus.TEMPLATE_ONLY
            report.note = "竖线原文只有要素名称、没有具体值；按未填写处理"
            return values, labels, report
        elif any(match.spec is not None or match.ambiguous for match in token_matches):
            report.status = ParseStatus.PARTIAL
            report.note = "竖线原文混用了要素名称和值，不能把名称当成已填事实"
            report.ambiguous_keys = sorted(
                {token for token, match in zip(tokens, token_matches) if match.ambiguous}
            )
            return values, labels, report
        else:
            report.missing_orders = [spec.order for spec in specs[len(tokens) :]]
            for spec, token in zip(specs, tokens):
                values[spec.key] = token
                labels[spec.key] = spec.label
            report.matched_count = min(len(tokens), len(specs))
            if len(tokens) == len(specs):
                report.status = ParseStatus.PARSED
                report.note = "按 | 位置对应全部要素"
            else:
                report.status = ParseStatus.PARTIAL
                report.note = (
                    f"位置式原文有 {len(tokens)} 段、清单有 {len(specs)} 项；"
                    "多出/缺少的段落无法确定对应关系，需人工核对原文"
                )
            return values, labels, report

    # 编号式 / 键值式：显式分隔符 + 后跟已知要素名的空白切段，名称匹配优先。
    segments = _keyed_segments(text, schema)
    report.token_count = len(segments)
    if not segments:
        report.status = ParseStatus.UNPARSED
        report.note = "原文没有可解析的要素段"
        return values, labels, report

    mode = ParseMode.KEYED
    label_only = 0
    value_segments = 0
    ambiguous: list[str] = []
    unrecognized: list[str] = []
    missing_orders: set[int] = set()
    numbered_seen: set[int] = set()
    matched_specs_seen: set[str] = set()

    for segment in segments:
        number_match = _NUMBERED.match(segment)
        order: int | None = None
        rest = segment
        if number_match:
            raw_order = number_match.group(1)
            if len(raw_order) > 6:
                # 超长“编号”不可能是要素序号；直接 int() 会抛 ValueError 并把整次核对变成 500。
                unrecognized.append(segment[:80])
                continue
            order = int(raw_order)
            rest = number_match.group(2).strip()
            mode = ParseMode.NUMBERED
            if order in numbered_seen:
                ambiguous.append(f"编号 {order} 重复")
            numbered_seen.add(order)

        label_part: str | None = None
        value_part: str | None = None
        if re.search(r"[:：]", rest):
            label_part, value_part = re.split(r"[:：]", rest, maxsplit=1)
        else:
            label_part = rest

        label_match = _match_name(label_part, index)
        if label_match.spec is not None:
            spec = label_match.spec
            if value_part is None and order is None:
                # 键值式里只有名称、没有值且无编号：无法区分“空要素”与“值的一部分”，
                # 转人工，不静默把必填值截断。
                unrecognized.append(f"{label_part.strip()}（只有名称没有值）")
                continue
            if spec.key in matched_specs_seen:
                # 同名重复不能后值覆盖前值；两份都可能被业务当作有效事实。
                ambiguous.append(f"要素名重复：{label_part.strip()}")
                continue
            matched_specs_seen.add(spec.key)
            if value_part is None:
                # “1:品牌类型”只有名称没有值：可能是模板，也可能是空值。
                label_only += 1
                values.setdefault(spec.key, "")
            else:
                if re.search(r"[:：]", value_part):
                    # 值里再出现“名称:值”式冒号，多半是下一段被吞进来了，不洗成已填。
                    unrecognized.append(f"{label_part.strip()}（值内出现冒号）")
                    continue
                value_segments += 1
                values[spec.key] = value_part.strip()
            labels[spec.key] = label_part.strip()
            continue

        if label_match.ambiguous:
            ambiguous.append(label_part.strip())
            for candidate in label_match.candidates:
                labels.setdefault(candidate.key, "")
            continue

        # 名称不认识时绝不按编号把“牌子类型/作用”这类文本硬塞成值。
        # 纯值应使用位置式（|）或结构化输入；编号式只接受可识别的 名称[:值]。
        unrecognized.append(label_part.strip())
        if order is not None:
            missing_orders.add(order)

    report.ambiguous_keys = sorted(set(ambiguous))
    report.unrecognized_keys = sorted(set(unrecognized))
    report.missing_orders = sorted(missing_orders)
    report.matched_count = len(values)

    if mode is ParseMode.NUMBERED and numbered_seen:
        expected = {spec.order for spec in schema.elements}
        absent = sorted(expected - numbered_seen)
        # 只把“位于已给编号之间”的缺口算作无法确定；尾部缺号可能只是选填项省略。
        between = {order for order in absent if order < max(numbered_seen)}
        if between:
            report.missing_orders = sorted(set(report.missing_orders) | between)

    if ambiguous:
        report.status = ParseStatus.PARTIAL
        report.note = "存在无法唯一对应的要素名：" + "、".join(sorted(set(ambiguous)))
    elif unrecognized:
        report.status = ParseStatus.PARTIAL
        report.note = "存在未识别的要素段：" + "、".join(sorted(set(unrecognized)))
    elif report.missing_orders:
        report.status = ParseStatus.PARTIAL
        report.note = (
            "编号/位置存在缺口，无法确定未出现的要素是“未填”还是“原文省略”："
            + "、".join(str(order) for order in report.missing_orders)
        )
    elif label_only and value_segments == 0:
        report.status = ParseStatus.TEMPLATE_ONLY
        report.note = "原文只有要素名称、没有具体值；按未填写处理"
    else:
        report.status = ParseStatus.PARSED
        report.note = "按要素名与编号对应"
    report.mode = mode
    return values, labels, report


def _merge_mapping(
    declared: Mapping[str, str],
    raw_values: dict[str, str],
    raw_labels: dict[str, str],
    schema: ElementSchema,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str], list[str]]:
    """把结构化值叠加到原文解析结果上；结构化值优先，冲突逐项记录。"""

    index = _build_index(schema)
    values = dict(raw_values)
    labels = dict(raw_labels)
    conflicts: list[str] = []
    unrecognized: list[str] = []
    ambiguous: list[str] = []
    seen: set[str] = set()
    for name, value in declared.items():
        match = _match_name(str(name), index)
        if match.spec is None:
            (ambiguous if match.ambiguous else unrecognized).append(str(name))
            continue
        spec = match.spec
        if spec.key in seen:
            ambiguous.append(f"要素名重复：{str(name).strip()}")
            continue
        seen.add(spec.key)
        cleaned = _norm_text(value)
        if spec.key in values and _norm_text(values[spec.key]) and _norm_text(values[spec.key]) != cleaned:
            conflicts.append(spec.label)
        values[spec.key] = cleaned
        labels[spec.key] = str(name).strip()
    return values, labels, conflicts, unrecognized, ambiguous


# ---------------------------------------------------------------------------
# 逐项核对
# ---------------------------------------------------------------------------


def _value_state(spec: ElementSpec, value: str) -> tuple[CheckState, str]:
    text = _norm_text(value)
    if not text:
        if spec.requirement is Requirement.OPTIONAL:
            return CheckState.NOT_APPLICABLE, "选填项未提供，不影响充分性"
        if spec.requirement is Requirement.CONDITIONAL:
            return CheckState.UNVERIFIABLE, f"条件要素（{spec.condition}），未提供时不能按缺项判定"
        if spec.requirement is Requirement.UNKNOWN:
            return CheckState.UNVERIFIABLE, "来源未说明是否必填，未提供时不能按缺项判定"
        return CheckState.EMPTY, "必填项为空"
    if spec.value_type is ValueType.ENUM:
        allowed = {_norm_text(item).casefold() for item in spec.enum_values}
        if text.casefold() not in allowed:
            return (
                CheckState.INVALID_ENUM,
                "取值不在来源枚举内：允许 " + "、".join(spec.enum_values),
            )
    if spec.value_type is ValueType.NUMBER and not _NUMBER.match(text):
        return CheckState.INVALID_NUMBER, "取值不是数字"
    if spec.value_type is ValueType.PERCENTAGE:
        match = _PERCENT.match(text)
        if not match:
            return CheckState.INVALID_NUMBER, "取值不是百分比（如 3.2% 或 3.2）"
        if not 0 <= float(match.group(1)) <= 100:
            return CheckState.INVALID_NUMBER, "百分比应在 0~100 之间"
    return CheckState.SATISFIED, "已提供且通过来源类型校验"


def _check_elements(
    schema: ElementSchema, values: Mapping[str, str], labels: Mapping[str, str]
) -> list[ElementCheck]:
    checks: list[ElementCheck] = []
    for spec in schema.ordered():
        value = values.get(spec.key)
        if value is None and spec.requirement is Requirement.REQUIRED:
            state, reason = CheckState.MISSING, "必填项未提供"
        else:
            state, reason = _value_state(spec, "" if value is None else value)
        checks.append(
            ElementCheck(
                key=spec.key,
                label=spec.label,
                order=spec.order,
                role=spec.role,
                requirement=spec.requirement,
                condition=spec.condition,
                declared_key=labels.get(spec.key),
                declared_value=value,
                state=state,
                reason=reason,
                example=spec.example,
            )
        )
    return checks


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


class DeclarationElementsSource:
    """按 HS 编码核对申报要素是否充足。

    典型用法::

        source = DeclarationElementsSource()          # 读 CUSTOMS_DECLARATION_ELEMENTS_PATH
        result = source.check(ElementQuery(
            hs_code_raw="8544422900",
            basis=QueryBasis(declaration_date="2026-05-20"),
            declared_raw="4|1|深沟球轴承|...",
        ))
        if result.sufficiency is Sufficiency.INSUFFICIENT:
            ask_supplier(result.supplement_items)
        elif result.sufficiency is Sufficiency.UNVERIFIED:
            ask_expert(result.gaps)

    未配置来源时返回 ``query_status=source_unconfigured``，``sufficiency=unverified``，
    不会用合成样本冒充真实目录，也不会抛 500。
    """

    def __init__(self, store: ElementStore | None = None, *, clock: Any = None):
        self._store = store or EnvElementStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "DeclarationElementsSource":
        return cls(JsonElementStore(path), **kwargs)

    def check(
        self, query: ElementQuery | Mapping[str, Any] | None = None
    ) -> DeclarationElementsResult:
        prepared, problem = self._prepare(query)
        if prepared is None:
            return self._invalid(problem, raw=_raw_code(query))

        fmt = classify_hs_format(prepared.hs_code_raw)
        now = _as_aware(self._clock()).isoformat(timespec="seconds")

        if fmt.state in (HsFormatState.EMPTY, HsFormatState.NONDIGIT, HsFormatState.WRONG_LENGTH):
            return self._invalid(
                f"编码格式不可核对：{fmt.reason}；未发起查询，不能据此判定要素齐全或缺失",
                raw=prepared.hs_code_raw,
                fmt=fmt,
                query=prepared,
                searched_at=now,
            )

        try:
            snapshot = self._store.load()
        except ElementStoreNotConfigured as exc:
            return self._failed(QueryStatus.SOURCE_UNCONFIGURED, str(exc), prepared, fmt, now)
        except ElementStoreError as exc:
            return self._failed(QueryStatus.SOURCE_UNREADABLE, str(exc), prepared, fmt, now)
        except Exception as exc:
            return self._failed(
                QueryStatus.SOURCE_ERROR,
                f"申报要素查询发生未预期异常（{type(exc).__name__}）；"
                "这不等于该编码无需申报要素，需修复存储后重试",
                prepared,
                fmt,
                now,
            )

        version = resolve_version(prepared.basis, snapshot.versions, dataset=snapshot.dataset)
        record = self._find_schema(snapshot, version, fmt.normalized)

        if record is None:
            return self._not_found(prepared, fmt, version, snapshot, now)

        values, labels, report = self._parse(prepared, record)
        checks = _check_elements(record, values, labels)
        authoritative = snapshot.dataset.authoritative
        sufficiency, reason = self._judge(
            checks=checks,
            report=report,
            version=version,
            authoritative=authoritative,
            dataset=snapshot.dataset,
            fmt=fmt,
        )
        supplement_items = [
            SupplementItem(
                key=item.key,
                label=item.label,
                requirement=item.requirement,
                role=item.role,
                example=item.example,
                reason=item.reason,
                suggested_role="supplier",
            )
            for item in checks
            if sufficiency is Sufficiency.INSUFFICIENT and item.blocking
        ]
        gaps = self._gaps(
            checks=checks,
            report=report,
            version=version,
            authoritative=authoritative,
            dataset=snapshot.dataset,
            fmt=fmt,
        )
        counts = self._counts(checks)
        evidence = self._evidence(record, version, prepared, snapshot)

        return DeclarationElementsResult(
            query=prepared,
            query_status=QueryStatus.OK,
            schema_status=evidence.status if evidence else (
                EvidenceStatus.OBTAINED
                if version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            ),
            sufficiency=sufficiency,
            version=version,
            format=fmt,
            schema_record=record,
            checks=checks,
            supplement_items=supplement_items,
            gaps=gaps,
            parse=report,
            authoritative=authoritative,
            required_total=counts["required_total"],
            satisfied_count=counts["satisfied"],
            missing_count=counts["missing"],
            invalid_count=counts["invalid"],
            unverifiable_count=counts["unverifiable"],
            reason=reason,
            evidence=[evidence] if evidence else [],
            dataset=snapshot.dataset,
            searched_at=now,
        )

    # --- 构造 ---

    @staticmethod
    def _prepare(
        query: ElementQuery | Mapping[str, Any] | None,
    ) -> tuple[ElementQuery | None, str]:
        if isinstance(query, ElementQuery):
            raw = query
        elif isinstance(query, Mapping):
            try:
                raw = ElementQuery.model_validate(dict(query))
            except (ValidationError, TypeError, ValueError) as exc:
                return None, f"核对输入不符合契约（{_summarize(exc)}）；未发起查询"
        elif query is None:
            return None, "核对输入为空；未发起查询"
        else:
            return None, f"核对输入类型不受支持（{type(query).__name__}）；未发起查询"

        if len(raw.declared) > MAX_ELEMENTS:
            return None, f"结构化要素超过 {MAX_ELEMENTS} 项；未发起核对"
        if len(raw.declared_raw) > MAX_RAW_LENGTH:
            return None, f"要素原文超过 {MAX_RAW_LENGTH} 字符；未发起核对"
        try:
            prepared = ElementQuery(
                hs_code_raw="" if raw.hs_code_raw is None else str(raw.hs_code_raw),
                basis=raw.basis if isinstance(raw.basis, QueryBasis) else QueryBasis.model_validate(raw.basis),
                declared={str(key): "" if value is None else str(value) for key, value in raw.declared.items()},
                declared_raw=str(raw.declared_raw or ""),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return None, f"核对输入无法解析（{_summarize(exc)}）；未发起查询"
        return prepared, ""

    @staticmethod
    def _find_schema(
        snapshot: ElementSnapshot, version: VersionResolution, code: str
    ) -> ElementSchema | None:
        if version.version:
            for record in snapshot.schemas:
                if record.version == version.version and record.hs_code == code:
                    return record
            return None
        # 版本未核实且来源只有一个版本时，允许取出来标注待核；多版本不猜。
        if len(snapshot.versions) == 1:
            only = snapshot.versions[0].version
            for record in snapshot.schemas:
                if record.version == only and record.hs_code == code:
                    return record
        return None

    @staticmethod
    def _parse(
        query: ElementQuery, schema: ElementSchema
    ) -> tuple[dict[str, str], dict[str, str], ElementParseReport]:
        if query.declared_raw.strip():
            values, labels, report = _parse_raw(query.declared_raw, schema)
        else:
            values, labels, report = {}, {}, ElementParseReport()

        if query.declared:
            merged, merged_labels, conflicts, unrecognized, ambiguous = _merge_mapping(
                query.declared, values, labels, schema
            )
            values, labels = merged, merged_labels
            if report.mode is ParseMode.NONE:
                report.mode = ParseMode.MAPPING
            elif report.mode is not ParseMode.MAPPING:
                report.mode = ParseMode.MIXED
            if report.status is ParseStatus.EMPTY:
                report.status = ParseStatus.PARSED
                report.note = "使用结构化要素；未提供原文"
            report.conflicts = conflicts
            if conflicts and report.status in (ParseStatus.PARSED, ParseStatus.TEMPLATE_ONLY):
                report.status = ParseStatus.PARTIAL
                report.note = "结构化要素与原文取值冲突：" + "、".join(conflicts)
            if ambiguous:
                report.ambiguous_keys = sorted(set(report.ambiguous_keys) | set(ambiguous))
                report.status = ParseStatus.PARTIAL
                report.note = "存在无法唯一对应的结构化要素名：" + "、".join(sorted(set(ambiguous)))
            if unrecognized:
                report.unrecognized_keys = sorted(set(report.unrecognized_keys) | set(unrecognized))
                if report.status is ParseStatus.PARSED:
                    report.status = ParseStatus.PARTIAL
                    report.note = "存在未识别的结构化要素名：" + "、".join(sorted(set(unrecognized)))
            report.matched_count = len(values)
        return values, labels, report

    @staticmethod
    def _judge(
        *,
        checks: Sequence[ElementCheck],
        report: ElementParseReport,
        version: VersionResolution,
        authoritative: bool,
        dataset: DatasetInfo,
        fmt: HsFormat,
    ) -> tuple[Sufficiency, str]:
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            return (
                Sufficiency.UNVERIFIED,
                "只有 8 位税目/子目，无法确认 10 位申报编码对应的要素清单；不能判充分性",
            )
        if report.status in (ParseStatus.PARTIAL, ParseStatus.UNPARSED):
            return (
                Sufficiency.UNVERIFIED,
                f"要素原文解析不确定（{report.note or report.status.value}）；"
                "先人工核对原文，不能判充分或不足",
            )
        if not version.verified:
            return (
                Sufficiency.UNVERIFIED,
                f"适用税则版本未核实（{version.reason}）；清单可能不是当期版本，不能判充分性",
            )
        if not authoritative:
            return (
                Sufficiency.UNVERIFIED,
                f"来源为 {dataset.source_type.value}/{dataset.authority.value}，"
                "只能验证接口与流程，不能作为申报要素是否充足的业务结论",
            )
        missing = sum(1 for item in checks if item.state in (CheckState.MISSING, CheckState.EMPTY))
        invalid = sum(
            1 for item in checks if item.state in (CheckState.INVALID_ENUM, CheckState.INVALID_NUMBER)
        )
        unverifiable = sum(1 for item in checks if item.state is CheckState.UNVERIFIABLE)
        required_total = sum(1 for item in checks if item.requirement is Requirement.REQUIRED)
        if missing or invalid:
            parts = []
            if missing:
                parts.append(f"必填项缺失/为空 {missing} 项")
            if invalid:
                parts.append(f"取值非法 {invalid} 项")
            return (
                Sufficiency.INSUFFICIENT,
                f"按税则版本 {version.version} 的要素清单核对（必填 {required_total} 项）："
                + "、".join(parts)
                + "；事实不足，需补充或修正",
            )
        if unverifiable:
            return (
                Sufficiency.UNVERIFIED,
                f"必填项未见缺失，但有 {unverifiable} 项条件/口径待确认，"
                "转专家确认适用条件后再判",
            )
        return (
            Sufficiency.SUFFICIENT,
            f"按税则版本 {version.version} 的要素清单核对：必填 {required_total} 项均已提供且通过来源类型校验",
        )

    @staticmethod
    def _counts(checks: Sequence[ElementCheck]) -> dict[str, int]:
        required = [item for item in checks if item.requirement is Requirement.REQUIRED]
        return {
            "required_total": len(required),
            "satisfied": sum(1 for item in checks if item.state is CheckState.SATISFIED),
            "missing": sum(
                1 for item in checks if item.state in (CheckState.MISSING, CheckState.EMPTY)
            ),
            "invalid": sum(
                1
                for item in checks
                if item.state in (CheckState.INVALID_ENUM, CheckState.INVALID_NUMBER)
            ),
            "unverifiable": sum(
                1 for item in checks if item.state is CheckState.UNVERIFIABLE
            ),
        }

    @staticmethod
    def _gaps(
        *,
        checks: Sequence[ElementCheck],
        report: ElementParseReport,
        version: VersionResolution,
        authoritative: bool,
        dataset: DatasetInfo,
        fmt: HsFormat,
    ) -> list[IssueEntry]:
        gaps: list[IssueEntry] = []
        detailed = (
            authoritative
            and version.verified
            and fmt.state is HsFormatState.VALID
            and report.status
            in (ParseStatus.PARSED, ParseStatus.TEMPLATE_ONLY, ParseStatus.EMPTY)
        )
        if detailed:
            for item in checks:
                field = f"elements.{item.key}"
                if item.state is CheckState.MISSING or item.state is CheckState.EMPTY:
                    gaps.append(
                        IssueEntry(
                            code="ELEMENT_MISSING",
                            message=f"缺少申报要素「{item.label}」",
                            field=field,
                            missing=item.label,
                            suggested_role="supplier",
                        )
                    )
                elif item.state in (CheckState.INVALID_ENUM, CheckState.INVALID_NUMBER):
                    gaps.append(
                        IssueEntry(
                            code="ELEMENT_VALUE_INVALID",
                            message=f"要素「{item.label}」取值不合规：{item.reason}",
                            field=field,
                            missing=f"合规的{item.label}",
                            suggested_role="supplier",
                        )
                    )
                elif item.state is CheckState.UNVERIFIABLE:
                    gaps.append(
                        IssueEntry(
                            code="ELEMENT_CONDITION_UNVERIFIED",
                            message=f"要素「{item.label}」无法判定：{item.reason}",
                            field=field,
                            missing="适用条件或来源必填口径",
                            suggested_role="expert",
                        )
                    )
        if report.status in (ParseStatus.PARTIAL, ParseStatus.UNPARSED):
            gaps.append(
                IssueEntry(
                    code="ELEMENT_PARSE_PARTIAL",
                    message=f"申报要素原文无法完整对应：{report.note}",
                    field="supplier_elements_raw",
                    missing="可结构化核对的要素原文",
                    suggested_role="operator",
                )
            )
        if not version.verified:
            gaps.append(
                IssueEntry(
                    code="ELEMENT_VERSION_UNVERIFIED",
                    message=f"适用税则版本未核实：{version.reason}",
                    field="query_basis",
                    missing="申报日期或税则版本",
                    suggested_role="expert",
                )
            )
        if not authoritative:
            gaps.append(
                IssueEntry(
                    code="ELEMENT_AUTHORITY_NOT_OFFICIAL",
                    message=(
                        f"要素清单来源为 {dataset.source_type.value}/{dataset.authority.value}，"
                        "不能作为申报要素是否充足的业务结论"
                    ),
                    field="evidence",
                    missing="官方规范申报目录",
                    suggested_role="operator",
                )
            )
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            gaps.append(
                IssueEntry(
                    code="ELEMENT_HS_INCOMPLETE",
                    message="只提供 8 位税目/子目，无法确认 10 位编码对应的要素清单",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        return gaps

    @staticmethod
    def _evidence(
        record: ElementSchema,
        version: VersionResolution,
        query: ElementQuery,
        snapshot: ElementSnapshot,
    ) -> EvidenceEntry:
        source = record.source
        if not source.retrieved_at and snapshot.dataset.retrieved_at:
            source = source.model_copy(update={"retrieved_at": snapshot.dataset.retrieved_at})
        labels = "；".join(f"{item.order}:{item.label}" for item in record.ordered())
        return EvidenceEntry(
            evidence_id=f"elements:{record.version}:{record.hs_code}",
            source_type=snapshot.dataset.source_type.value,
            title=f"申报要素清单 {record.version}：{record.hs_code} {record.description}".strip(),
            status=(
                EvidenceStatus.OBTAINED
                if version.verified
                else EvidenceStatus.VERSION_UNVERIFIED
            ),
            url=source.url,
            document=source.document,
            locator=source.locator,
            excerpt=f"应申报要素：{labels}",
            retrieved_at=source.retrieved_at,
            effective_from=version.window.effective_from if version.window else None,
            effective_to=version.window.effective_to if version.window else None,
            scope=f"税则版本 {record.version}",
            query={
                "hs_code": query.hs_code_raw,
                "tariff_version": record.version,
                "basis": query.basis.model_dump(),
                "declared_keys": sorted(query.declared),
            },
            note=(
                f"来源类型 {snapshot.dataset.source_type.value}；权威级别 "
                f"{snapshot.dataset.authority.value}；清单只说明“要哪些要素”，"
                "不判断具体商品是否归该码"
            ),
        )

    def _invalid(
        self,
        reason: str,
        *,
        raw: str,
        fmt: HsFormat | None = None,
        query: ElementQuery | None = None,
        searched_at: str | None = None,
    ) -> DeclarationElementsResult:
        fmt = fmt or classify_hs_format(raw)
        basis = query.basis if query else QueryBasis()
        return DeclarationElementsResult(
            query=query or ElementQuery(hs_code_raw=raw, basis=basis),
            query_status=QueryStatus.INVALID_INPUT,
            schema_status=None,
            sufficiency=Sufficiency.NOT_CHECKED,
            version=resolve_version(basis, ()),
            format=fmt,
            schema_record=None,
            reason=reason,
            gaps=[
                IssueEntry(
                    code="ELEMENT_INPUT_INVALID",
                    message=reason,
                    field="supplier_elements_raw",
                    missing="可核对的编码与要素",
                    suggested_role="supplier",
                )
            ],
            searched_at=searched_at,
        )

    def _failed(
        self,
        status: QueryStatus,
        reason: str,
        query: ElementQuery,
        fmt: HsFormat,
        now: str,
    ) -> DeclarationElementsResult:
        return DeclarationElementsResult(
            query=query,
            query_status=status,
            schema_status=EvidenceStatus.QUERY_FAILED,
            sufficiency=Sufficiency.UNVERIFIED,
            version=resolve_version(query.basis, ()),
            format=fmt,
            schema_record=None,
            reason=f"申报要素查询失败：{reason}；这不等于该编码无需申报要素",
            gaps=[
                IssueEntry(
                    code="ELEMENT_QUERY_FAILED",
                    message=f"申报要素来源不可用：{reason}",
                    field="supplier_elements_raw",
                    missing="可用的规范申报目录来源",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    def _not_found(
        self,
        query: ElementQuery,
        fmt: HsFormat,
        version: VersionResolution,
        snapshot: ElementSnapshot,
        now: str,
    ) -> DeclarationElementsResult:
        if version.verified:
            reason = (
                f"税则版本 {version.version} 未收录编码 {fmt.normalized} 的要素清单；"
                "这是来源覆盖缺口，不等于该编码无需申报要素"
            )
            gaps = [
                IssueEntry(
                    code="ELEMENT_SCHEMA_NOT_FOUND",
                    message=reason,
                    field="supplier_elements_raw",
                    missing=f"{version.version} 版规范申报目录对应条目",
                    suggested_role="expert",
                )
            ]
            schema_status = EvidenceStatus.NOT_FOUND
        else:
            reason = (
                f"未能核实编码 {fmt.normalized or fmt.raw} 适用的要素清单：{version.reason}；"
                "不能宣称该编码无需申报要素"
            )
            gaps = [
                IssueEntry(
                    code="ELEMENT_VERSION_UNVERIFIED",
                    message=reason,
                    field="query_basis",
                    missing="申报日期或税则版本",
                    suggested_role="expert",
                )
            ]
            schema_status = EvidenceStatus.VERSION_UNVERIFIED
        if fmt.state is HsFormatState.SUBHEADING_ONLY:
            gaps.append(
                IssueEntry(
                    code="ELEMENT_HS_INCOMPLETE",
                    message="只提供 8 位税目/子目，无法确认 10 位编码对应的要素清单",
                    field="supplier_hs_raw",
                    missing="10 位申报编码",
                    suggested_role="supplier",
                )
            )
        return DeclarationElementsResult(
            query=query,
            query_status=QueryStatus.OK,
            schema_status=schema_status,
            sufficiency=Sufficiency.UNVERIFIED,
            version=version,
            format=fmt,
            schema_record=None,
            reason=reason,
            gaps=gaps,
            dataset=snapshot.dataset,
            searched_at=now,
        )


def _raw_code(query: Any) -> str:
    if isinstance(query, ElementQuery):
        return query.hs_code_raw
    if isinstance(query, Mapping):
        value = query.get("hs_code_raw")
        return "" if value is None else str(value)
    return ""


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def configured_elements_path() -> Path | None:
    """当前环境是否配置了要素文件；供 D6 自检。"""

    return default_elements_path()
