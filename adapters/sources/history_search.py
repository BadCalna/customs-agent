"""历史成功记录检索适配（P15）。

定位：历史成功记录只是**参考线索**，不是当前归类结论
------------------------------------------------------
历史申报记录里的 HS 是“当时按当时税则、当时要素成功申报过”的结果，
它既不能证明当前商品仍归该码，也不能替代现行税则与申报要素核对。本模块
从结构上限制误用，而不是只靠注释提醒：

- 输出类型是 :class:`HistorySearchResult`，不是 ``RecommendOutput``；
- 候选字段叫 ``historical_hs``，刻意不叫 ``hs``/``suggested_hs``，
  因此不经显式映射就塞不进 ``RecommendOutput.candidates``（P15 测试锁定）；
- 结果与候选都有 ``reference_only=True``（``Literal[True]``，构造为 False 会校验失败）
  和固定的 ``usage_note``。

检索方式
--------
纯规则打分：品名词元重合（中文二元组 + 英文/数字词元）+ 申报要素/规格材质一致性。
不引入向量数据库、不引入新依赖、不访问网络。存储通过 :class:`HistoryRecordStore`
协议替换；默认 :class:`JsonHistoryStore` 读本地 JSON，可用环境变量
``CUSTOMS_HISTORY_RECORDS_PATH`` 指向真实导出文件。

可解释性
--------
每个候选都带 ``matched_keys`` / ``diff_keys`` / ``unverified_keys``。
关键属性不一致必然出现在 ``diff_keys`` 并置 ``key_attribute_conflict=True``，
同时扣分，但**不会因为总分高就被吞掉**——差异是独立字段，不是分数的一部分。

四态
----
:class:`HistorySearchOutcome` 明确区分：

- ``matched``：检索成功且有候选（证据状态 ``obtained``）；
- ``no_match``：检索成功但确实没查到（证据状态 ``not_found``）；
- ``query_failed``：存储不可读/格式异常，查询失败（证据状态 ``query_failed``）；
- ``invalid_query``：输入为空或类型不合法，**未发起检索**（证据状态 ``None``）。

``evidence_status`` 由结果校验器强制与 ``outcome`` 对应，``no_match`` 不能被写成
``query_failed``，反之亦然。D4 的 ``version_unverified`` 用在候选级
``version_status``：记录取到了，但它的 HS 版本/适用范围在本包内无法核实。

本包用的是明确标注的合成样本，未接真实历史库，检索效果未验证。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from ninja import Schema
from pydantic import ConfigDict, Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus

#: 本模块数据文件的结构版本；结构变化时递增并保留旧格式读取或明确报错。
SCHEMA_VERSION = "p15.history.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 默认样本（合成数据）。真实数据用环境变量或 ``HistorySearchSource.load(path)`` 指定。
DEFAULT_RECORDS_PATH = Path(__file__).resolve().parent / "fixtures" / "history_records.sample.json"
ENV_RECORDS_PATH = "CUSTOMS_HISTORY_RECORDS_PATH"

#: 打分权重与阈值。规则公开、可复算，不做“黑盒相似度”。
NAME_WEIGHT = 0.6
ATTRIBUTE_WEIGHT = 0.4
KEY_CONFLICT_PENALTY = 0.7
MISSING_NAME_DISCOUNT = 0.8
MISSING_ELEMENTS_DISCOUNT = 0.8
DEFAULT_MIN_SCORE = 0.35
DEFAULT_LIMIT = 5
DEFAULT_STALE_DAYS = 730

#: 输入上限：脏数据/超长文本不应拖垮检索。
MAX_NAME_LENGTH = 200
MAX_ATTRIBUTES = 50

USAGE_NOTE = "历史成功码仅作参考线索，不能作为当前归类结论；须按现行税则与申报要素重新核对。"

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ALNUM_RUN = re.compile(r"[0-9a-z]+")
_TOKEN_SEPARATOR = re.compile(r"[\s,，、;；/|\\()（）\[\]【】{}《》<>·.。:：\"'“”‘’_\-—]+")
_WHITESPACE = re.compile(r"\s+")


class HistorySearchOutcome(str, Enum):
    """检索结果状态。``no_match`` 与 ``query_failed`` 必须分开，不能互相顶替。"""

    MATCHED = "matched"
    NO_MATCH = "no_match"
    QUERY_FAILED = "query_failed"
    INVALID_QUERY = "invalid_query"


#: outcome 与 D4 证据状态的唯一映射；结果校验器按此强制一致。
OUTCOME_EVIDENCE_STATUS: dict[HistorySearchOutcome, EvidenceStatus | None] = {
    HistorySearchOutcome.MATCHED: EvidenceStatus.OBTAINED,
    HistorySearchOutcome.NO_MATCH: EvidenceStatus.NOT_FOUND,
    HistorySearchOutcome.QUERY_FAILED: EvidenceStatus.QUERY_FAILED,
    # 输入不合法时根本没有发起查询，按 D4 “null 表示未查询”留空，不冒充查无结果。
    HistorySearchOutcome.INVALID_QUERY: None,
}


class HistorySourceRef(Schema):
    """历史记录的来源引用，必须能定位到具体单据与项号。"""

    document: str = Field(min_length=1, description="单据/文件标识，原样保留")
    locator: str = Field(min_length=1, description="单据内的项号、行号或页签定位")
    url: str | None = Field(default=None, description="可打开的来源地址；无则为空")
    system: str = Field(default="", description="来源系统标识，便于区分真实库与合成样本")


class HistoryDatasetInfo(Schema):
    """数据集说明。``synthetic`` 是给下游的诚实标记，不允许默认当成真实数据。"""

    dataset_id: str = ""
    synthetic: bool | None = Field(
        default=None,
        description="true=合成样本；false=调用方明确声明为真实数据；null=未声明来源",
    )
    description: str = ""
    generated_at: str | None = None


class HistoryRecord(Schema):
    """一条历史成功记录（存储层 DTO，字段对应关系见交接文档）。

    ``recorded_at`` 是历史业务日期，**原样保存**：不解析成统一格式、不补时区、
    不用查询时间替换。``elements`` 是当时的申报要素键值。
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, extra="ignore")

    record_id: str = Field(min_length=1)
    historical_hs: str = Field(min_length=1, description="历史成功 HS，仅作参考")
    historical_name: str = Field(min_length=1)
    elements: dict[str, str] = Field(default_factory=dict, description="当时申报要素键值")
    material: str = ""
    spec: str = ""
    attributes: dict[str, str] = Field(default_factory=dict, description="其他结构化属性")
    recorded_at: str = Field(min_length=1, description="历史业务日期，原样保留")
    outcome: str = Field(default="approved", description="历史结果，如 approved / 放行")
    hs_version: str | None = Field(default=None, description="记录声明的税则/HS 版本，未知为空")
    applicable_scope: str | None = Field(default=None, description="适用范围，未知为空")
    valid_until: str | None = Field(default=None, description="有效期，未知为空且不推断")
    source: HistorySourceRef
    notes: str = ""


class HistoryQuery(Schema):
    """检索输入：品名 + 申报要素键值 + 规格/材质。

    刻意不复用 ``RecommendInput``：本适配器不接收执行上下文，也不接收供应商原码，
    避免历史检索被误当成一次推荐执行。
    """

    name: str = ""
    elements: dict[str, str] = Field(default_factory=dict)
    spec: str = ""
    material: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)


class HistoryCandidate(Schema):
    """一条历史参考候选。字段名与 ``Candidate`` 不同，防止直接当推荐结果使用。"""

    record_id: str
    historical_hs: str = Field(description="历史成功 HS；仅参考，不是当前建议")
    historical_name: str
    recorded_at: str = Field(description="历史业务日期，原样保留，不解析、不补时区")
    source: HistorySourceRef
    score: float = Field(ge=0, le=1, description="规则相似度，可复算，不是概率")
    matched_keys: list[str] = Field(default_factory=list, description="查询与历史一致的属性键")
    diff_keys: list[str] = Field(
        default_factory=list, description="查询与历史不一致的属性键；关键差异不会被总分吞掉"
    )
    unverified_keys: list[str] = Field(
        default_factory=list, description="查询有、历史记录未提供，无法核对的属性键"
    )
    key_attribute_conflict: bool = Field(
        default=False, description="是否至少有一项关键属性不一致"
    )
    similarity_note: str = Field(default="", description="可读的相似依据与差异说明")
    historical_outcome: str = ""
    hs_version: str | None = None
    version_status: EvidenceStatus = Field(
        default=EvidenceStatus.VERSION_UNVERIFIED,
        description="历史记录的 HS 版本在本包内无法核实，固定为 version_unverified",
    )
    applicable_scope: str | None = None
    valid_until: str | None = None
    validity_note: str = Field(default="", description="适用范围/有效期未知或过期的标注")
    stale: bool = Field(default=False, description="按历史业务日期判断是否过旧")
    age_days: int | None = Field(default=None, description="历史业务日期距今天数；无法解析时为空")
    usage_note: str = USAGE_NOTE
    reference_only: Literal[True] = True


class HistorySearchResult(Schema):
    """一次检索的结构化结果。

    ``outcome`` 与 ``evidence_status`` 由校验器绑定，避免调用方把“没查到”
    写成“查询失败”或反过来。``to_evidence_entries`` 只对 matched 产出条目，
    no_match / query_failed 不会生成貌似可点击的虚构引用。
    """

    outcome: HistorySearchOutcome
    evidence_status: EvidenceStatus | None = Field(
        default=None, description="D4 四态；invalid_query 时为 null（未发起查询）"
    )
    reason: str = ""
    query: HistoryQuery | None = None
    candidates: list[HistoryCandidate] = Field(default_factory=list)
    dataset: HistoryDatasetInfo | None = None
    searched_at: str | None = Field(default=None, description="本次检索时间；未检索时为空")
    scanned_records: int = Field(default=0, ge=0)
    query_gaps: list[str] = Field(
        default_factory=list, description="输入缺口，如缺品名/缺申报要素；不阻断检索但降低可信度"
    )
    truncated: bool = Field(default=False, description="输入超长被截断，仅用截断后的内容检索")
    reference_only: Literal[True] = True
    usage_note: str = USAGE_NOTE

    @model_validator(mode="after")
    def _check_outcome_consistency(self) -> "HistorySearchResult":
        expected = OUTCOME_EVIDENCE_STATUS[self.outcome]
        if self.evidence_status != expected:
            raise ValueError(
                f"outcome={self.outcome.value} 必须对应 evidence_status="
                f"{expected.value if expected else None}，不能混用"
            )
        if self.outcome is HistorySearchOutcome.MATCHED and not self.candidates:
            raise ValueError("outcome=matched 必须至少有一条候选")
        if self.outcome is not HistorySearchOutcome.MATCHED and self.candidates:
            raise ValueError(f"outcome={self.outcome.value} 不允许携带候选")
        return self

    def to_evidence_entries(self) -> list[EvidenceEntry]:
        """转成 D4 证据条目，供 P16 挂到推荐/比较结果上。

        只有 ``matched`` 会产出条目；``no_match``/``query_failed`` 返回空列表，
        由调用方按 ``evidence_status`` 表达状态，不生成虚构条款。
        条目状态是 ``obtained``（记录确实取到了）；版本是否可沿用看候选的
        ``version_status``/``validity_note``，不要读成“已核实为现行依据”。
        """

        if self.outcome is not HistorySearchOutcome.MATCHED:
            return []
        if self.dataset is not None and self.dataset.synthetic is True:
            origin_note = "（合成样本，非真实历史库）"
        elif self.dataset is not None and self.dataset.synthetic is False:
            origin_note = ""
        else:
            origin_note = "（数据来源未声明）"
        query_payload = self.query.model_dump() if self.query else None
        entries: list[EvidenceEntry] = []
        for candidate in self.candidates:
            diff_note = (
                f"差异：{'、'.join(candidate.diff_keys)}"
                if candidate.diff_keys
                else "关键属性未见差异"
            )
            entries.append(
                EvidenceEntry(
                    evidence_id=f"history:{candidate.record_id}",
                    source_type="history",
                    title=f"历史成功记录{origin_note}：{candidate.historical_name}",
                    status=EvidenceStatus.OBTAINED,
                    url=candidate.source.url,
                    document=candidate.source.document,
                    locator=candidate.source.locator,
                    excerpt=(
                        f"{candidate.historical_name} / 历史 HS {candidate.historical_hs}"
                        f"（历史结果 {candidate.historical_outcome or '未记录'}）"
                    ),
                    retrieved_at=self.searched_at,
                    effective_to=candidate.valid_until,
                    scope=candidate.applicable_scope,
                    query=query_payload,
                    note=(
                        f"历史业务日期 {candidate.recorded_at}；相似度 {candidate.score:.2f}；"
                        f"{diff_note}；{candidate.validity_note}；{USAGE_NOTE}"
                    ),
                )
            )
        return entries


@dataclass(frozen=True)
class HistorySnapshot:
    """一次存储读取的完整快照：数据集说明 + 记录。"""

    dataset: HistoryDatasetInfo
    records: tuple[HistoryRecord, ...]


class HistoryStoreError(RuntimeError):
    """历史记录存储不可读或格式不可用。

    调用方以 ``query_failed`` 表达，不能伪装成“没查到”，也不能静默返回空候选。
    """


class HistoryRecordStore(Protocol):
    """可替换的存储接口：实现 ``load()`` 即可接入数据库、导出文件或测试桩。"""

    def load(self) -> HistorySnapshot: ...


def default_records_path() -> Path:
    """默认数据文件路径；环境变量优先，便于 P16/部署指向真实导出。"""

    override = os.environ.get(ENV_RECORDS_PATH, "").strip()
    return Path(override).expanduser() if override else DEFAULT_RECORDS_PATH


class JsonHistoryStore:
    """本地 JSON 存储（默认实现）。

    文件格式::

        {
          "schema_version": "p15.history.1",
          "dataset": {"dataset_id": "...", "synthetic": true, "description": "..."},
          "records": [{...HistoryRecord 字段...}]
        }

    也接受顶层直接是记录数组（此时数据来源标记为未声明）。每次 ``load()`` 重新读取，
    文件缺失、JSON 损坏或记录不合契约都会抛 :class:`HistoryStoreError`，由检索入口
    转成 ``query_failed``。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> HistorySnapshot:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HistoryStoreError(
                f"历史记录文件不可读：{self._path.name}（{type(exc).__name__}）"
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HistoryStoreError(
                f"历史记录文件不是合法 JSON：{self._path.name}（第 {exc.lineno} 行）"
            ) from exc
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryHistoryStore:
    """内存存储：调用方直接注入记录，便于测试与后续接 ORM/接口。"""

    def __init__(
        self,
        records: Iterable[HistoryRecord | Mapping[str, Any]],
        *,
        dataset: HistoryDatasetInfo | None = None,
    ):
        self._raw = tuple(records)
        self._dataset = dataset or HistoryDatasetInfo(
            dataset_id="in-memory",
            synthetic=None,
            description="调用方注入的内存记录，未声明数据来源",
        )

    def load(self) -> HistorySnapshot:
        parsed: list[HistoryRecord] = []
        for index, item in enumerate(self._raw):
            try:
                parsed.append(
                    item if isinstance(item, HistoryRecord) else HistoryRecord.model_validate(item)
                )
            except (ValidationError, TypeError, ValueError) as exc:
                raise HistoryStoreError(
                    f"第 {index + 1} 条历史记录不符合契约（{type(exc).__name__}）"
                ) from exc
        return HistorySnapshot(dataset=self._dataset, records=tuple(parsed))


def _snapshot_from_payload(payload: Any, *, source_name: str) -> HistorySnapshot:
    if isinstance(payload, list):
        dataset_raw: Any = {}
        records_raw = payload
        schema_version = None
    elif isinstance(payload, dict):
        schema_version = payload.get("schema_version")
        dataset_raw = payload.get("dataset") or {}
        records_raw = payload.get("records")
    else:
        raise HistoryStoreError(f"历史记录文件顶层必须是对象或数组：{source_name}")

    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise HistoryStoreError(
            f"历史记录文件结构版本不受支持：{schema_version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )
    if not isinstance(records_raw, list):
        raise HistoryStoreError(f"历史记录文件缺少 records 数组：{source_name}")
    if dataset_raw is not None and not isinstance(dataset_raw, dict):
        raise HistoryStoreError(f"历史记录文件的 dataset 必须是对象：{source_name}")

    try:
        dataset = HistoryDatasetInfo.model_validate(dataset_raw or {})
    except ValidationError as exc:
        raise HistoryStoreError(f"历史记录文件的 dataset 描述不可用：{source_name}") from exc
    if not dataset.dataset_id:
        dataset = dataset.model_copy(update={"dataset_id": source_name})

    records: list[HistoryRecord] = []
    for index, item in enumerate(records_raw):
        if not isinstance(item, dict):
            raise HistoryStoreError(f"第 {index + 1} 条历史记录必须是对象：{source_name}")
        try:
            records.append(HistoryRecord.model_validate(item))
        except ValidationError as exc:
            raise HistoryStoreError(
                f"第 {index + 1} 条历史记录不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
    return HistorySnapshot(dataset=dataset, records=tuple(records))


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _compact(value: Any) -> str:
    return _WHITESPACE.sub("", _normalize(value))


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _name_tokens(name: str) -> set[str]:
    """品名词元：中文取二元组，英文/数字取整段，标点作为分隔。

    中文没有空格，二元组在“不锈钢法兰盘 / 不锈钢法兰”这类写法差异上比整词更稳；
    单字中文单独成词，避免“钢”这类噪声被系统性放大。
    """

    normalized = unicodedata.normalize("NFKC", name).casefold()
    tokens: set[str] = set()
    for part in _TOKEN_SEPARATOR.split(normalized):
        if not part:
            continue
        for run in _CJK_RUN.findall(part):
            if len(run) == 1:
                tokens.add(run)
            else:
                tokens.update(run[index : index + 2] for index in range(len(run) - 1))
        for run in _ALNUM_RUN.findall(part):
            tokens.add(run)
    return tokens


def _name_similarity(query_name: str, record_name: str) -> float:
    left, right = _compact(query_name), _compact(record_name)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = _name_tokens(query_name), _name_tokens(record_name)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _clean_attributes(values: Mapping[str, Any] | None) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    if not values:
        return cleaned
    for index, (key, value) in enumerate(values.items()):
        if index >= MAX_ATTRIBUTES:
            break
        cleaned_key = _clean_text(key)
        cleaned_value = _clean_text(value)
        if cleaned_key and cleaned_value:
            cleaned[cleaned_key] = cleaned_value
    return cleaned


def _query_attributes(query: HistoryQuery) -> dict[str, str]:
    attributes = dict(query.elements)
    if _clean_text(query.material):
        attributes.setdefault("材质", _clean_text(query.material))
    if _clean_text(query.spec):
        attributes.setdefault("规格", _clean_text(query.spec))
    for key, value in query.attributes.items():
        attributes.setdefault(_clean_text(key), _clean_text(value))
    return {key: value for key, value in attributes.items() if key and value}


def _record_attributes(record: HistoryRecord) -> dict[str, str]:
    attributes = dict(record.elements)
    if _clean_text(record.material):
        attributes.setdefault("材质", _clean_text(record.material))
    if _clean_text(record.spec):
        attributes.setdefault("规格", _clean_text(record.spec))
    for key, value in record.attributes.items():
        attributes.setdefault(_clean_text(key), _clean_text(value))
    return {key: value for key, value in attributes.items() if key and value}


def _compare_attributes(
    query_attributes: Mapping[str, str], record_attributes: Mapping[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """返回 (一致键, 差异键, 历史未提供键)，均排序以保证输出稳定。"""

    matched: list[str] = []
    diff: list[str] = []
    unverified: list[str] = []
    for key, query_value in query_attributes.items():
        record_value = record_attributes.get(key)
        if not _clean_text(record_value):
            unverified.append(key)
        elif _compact(query_value) == _compact(record_value):
            matched.append(key)
        else:
            diff.append(key)
    return sorted(matched), sorted(diff), sorted(unverified)


def _parse_time(value: str | None) -> datetime | None:
    """尽力解析时间，仅用于年龄/过期判断；解析失败返回 None，不影响原值保留。"""

    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _as_aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _summarize_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError) and exc.errors():
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "query"
        return f"{location}: {first.get('msg', '输入不合法')}"
    return type(exc).__name__


class HistorySearchSource:
    """历史成功记录检索适配器。

    典型用法::

        source = HistorySearchSource.load("data/history.json")
        result = source.search(HistoryQuery(name="不锈钢法兰盘", material="不锈钢",
                                            elements={"用途": "管道连接"}, spec="DN50"))
        if result.outcome is HistorySearchOutcome.MATCHED:
            engine.attach(result.to_evidence_entries())   # 只当证据，不当建议

    也可以 ``HistorySearchSource(records=[...])`` 直接注入记录，或用 ``store=``
    传入自定义 :class:`HistoryRecordStore`。无参构造读取默认 JSON（合成样本）。
    """

    def __init__(
        self,
        records: Iterable[HistoryRecord | Mapping[str, Any]] | None = None,
        *,
        store: HistoryRecordStore | None = None,
        dataset: HistoryDatasetInfo | None = None,
        clock: Any = None,
        limit: int = DEFAULT_LIMIT,
        min_score: float = DEFAULT_MIN_SCORE,
        stale_days: int = DEFAULT_STALE_DAYS,
    ):
        if records is not None and store is not None:
            raise ValueError("records 与 store 只能二选一")
        if dataset is not None and store is not None:
            raise ValueError("dataset 只与 records 一起使用；自定义 store 请自带数据集说明")
        if records is not None:
            self._store: HistoryRecordStore = InMemoryHistoryStore(records, dataset=dataset)
        elif store is not None:
            self._store = store
        else:
            self._store = JsonHistoryStore(default_records_path())
        if limit < 1:
            raise ValueError("limit 必须 >= 1")
        if not 0 <= min_score <= 1:
            raise ValueError("min_score 必须在 0~1 之间")
        if stale_days < 0:
            raise ValueError("stale_days 必须 >= 0")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.limit = limit
        self.min_score = min_score
        self.stale_days = stale_days

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "HistorySearchSource":
        """从本地 JSON 文件构造；文件问题在 ``search`` 时表现为 ``query_failed``。"""

        return cls(store=JsonHistoryStore(path), **kwargs)

    # --- 入口 ---

    def search(self, query: HistoryQuery | Mapping[str, Any] | None = None) -> HistorySearchResult:
        """执行一次检索。输入不合法或存储异常都返回结构化结果，不向上抛异常。"""

        prepared, problem, truncated = self._prepare_query(query)
        if prepared is None:
            return HistorySearchResult(
                outcome=HistorySearchOutcome.INVALID_QUERY,
                evidence_status=None,
                reason=problem,
                query=None,
                searched_at=None,
            )

        attributes = _query_attributes(prepared)
        query_gaps: list[str] = []
        if not _compact(prepared.name):
            query_gaps.append("品名")
        if not attributes:
            query_gaps.append("申报要素")
        if not _compact(prepared.name) and not attributes:
            return HistorySearchResult(
                outcome=HistorySearchOutcome.INVALID_QUERY,
                evidence_status=None,
                reason="查询为空：品名与申报要素都没有提供；未发起检索",
                query=None,
                searched_at=None,
            )

        now = _as_aware(self._clock())
        searched_at = now.isoformat(timespec="seconds")
        try:
            snapshot = self._store.load()
        except HistoryStoreError as exc:
            return HistorySearchResult(
                outcome=HistorySearchOutcome.QUERY_FAILED,
                evidence_status=EvidenceStatus.QUERY_FAILED,
                reason=f"历史记录检索失败：{exc}；这不等于没查到，不能记作 no_match",
                query=prepared,
                searched_at=searched_at,
            )
        except Exception as exc:  # 存储实现自身的缺陷也不能变成 500
            return HistorySearchResult(
                outcome=HistorySearchOutcome.QUERY_FAILED,
                evidence_status=EvidenceStatus.QUERY_FAILED,
                reason=(
                    f"历史记录检索发生未预期异常（{type(exc).__name__}）；"
                    "这不等于没查到，需修复存储实现后重试"
                ),
                query=prepared,
                searched_at=searched_at,
            )

        candidates = [
            candidate
            for record in snapshot.records
            if (candidate := self._build_candidate(prepared, record, now)) is not None
            and candidate.score >= self.min_score
        ]
        candidates.sort(key=lambda item: (-item.score, item.record_id))
        candidates = candidates[: self.limit]

        if not candidates:
            return HistorySearchResult(
                outcome=HistorySearchOutcome.NO_MATCH,
                evidence_status=EvidenceStatus.NOT_FOUND,
                reason=(
                    f"检索执行成功，未取得达到阈值 {self.min_score:.2f} 的历史参考记录"
                    f"（扫描 {len(snapshot.records)} 条）；这是没查到，不是查询失败"
                ),
                query=prepared,
                dataset=snapshot.dataset,
                searched_at=searched_at,
                scanned_records=len(snapshot.records),
                query_gaps=query_gaps,
                truncated=truncated,
            )

        gap_note = f"；输入缺口：{'、'.join(query_gaps)}" if query_gaps else ""
        return HistorySearchResult(
            outcome=HistorySearchOutcome.MATCHED,
            evidence_status=EvidenceStatus.OBTAINED,
            reason=(
                f"检索到 {len(candidates)} 条历史参考记录（扫描 {len(snapshot.records)} 条）"
                f"{gap_note}；历史成功码仅作参考，不能作为当前归类结论"
            ),
            query=prepared,
            candidates=candidates,
            dataset=snapshot.dataset,
            searched_at=searched_at,
            scanned_records=len(snapshot.records),
            query_gaps=query_gaps,
            truncated=truncated,
        )

    # --- 内部 ---

    @staticmethod
    def _prepare_query(
        query: HistoryQuery | Mapping[str, Any] | None,
    ) -> tuple[HistoryQuery | None, str, bool]:
        if isinstance(query, HistoryQuery):
            raw = query
        elif isinstance(query, Mapping):
            try:
                raw = HistoryQuery.model_validate(dict(query))
            except (ValidationError, TypeError, ValueError) as exc:
                return (
                    None,
                    f"查询输入不符合契约（{_summarize_validation_error(exc)}）；未发起检索",
                    False,
                )
        elif query is None:
            return None, "查询为空；未发起检索", False
        else:
            return None, f"查询输入类型不受支持（{type(query).__name__}）；未发起检索", False

        try:
            name = _clean_text(raw.name)
            truncated = len(name) > MAX_NAME_LENGTH
            truncated = truncated or len(raw.elements) > MAX_ATTRIBUTES
            truncated = truncated or len(raw.attributes) > MAX_ATTRIBUTES
            prepared = HistoryQuery(
                name=name[:MAX_NAME_LENGTH],
                elements=_clean_attributes(raw.elements),
                spec=_clean_text(raw.spec),
                material=_clean_text(raw.material),
                attributes=_clean_attributes(raw.attributes),
            )
        except (TypeError, ValueError) as exc:
            # 例如调用方用 model_construct 绕过了字段校验，仍不能变成 500。
            return None, f"查询输入无法解析（{type(exc).__name__}）；未发起检索", False
        return prepared, "", truncated

    def _build_candidate(
        self, query: HistoryQuery, record: HistoryRecord, now: datetime
    ) -> HistoryCandidate | None:
        query_attributes = _query_attributes(query)
        record_attributes = _record_attributes(record)
        name_score = _name_similarity(query.name, record.historical_name)
        matched, diff, unverified = _compare_attributes(query_attributes, record_attributes)

        total = len(matched) + len(diff) + len(unverified)
        attribute_score = (
            (len(matched) + 0.5 * len(unverified)) / total if total else 0.0
        )
        if not _compact(query.name):
            # 缺品名：只靠要素，即使全一致也打折，并已在 query_gaps 标注。
            score = MISSING_NAME_DISCOUNT * attribute_score
        elif not total:
            # 缺要素：只靠品名，不能算强匹配。
            score = MISSING_ELEMENTS_DISCOUNT * name_score
        else:
            score = NAME_WEIGHT * name_score + ATTRIBUTE_WEIGHT * attribute_score
        if diff:
            # 关键属性不一致要扣分，但差异本身仍完整保留在 diff_keys，不被分数掩盖。
            score *= KEY_CONFLICT_PENALTY
        score = round(min(max(score, 0.0), 1.0), 3)

        recorded_time = _parse_time(record.recorded_at)
        age_days = max((now - recorded_time).days, 0) if recorded_time else None
        valid_until = _parse_time(record.valid_until)
        expired = bool(valid_until and valid_until < now)
        stale = expired or (age_days is not None and age_days > self.stale_days)
        validity_note = self._validity_note(record, age_days, expired, stale)

        return HistoryCandidate(
            record_id=record.record_id,
            historical_hs=record.historical_hs,
            historical_name=record.historical_name,
            recorded_at=record.recorded_at,
            source=record.source,
            score=score,
            matched_keys=matched,
            diff_keys=diff,
            unverified_keys=unverified,
            key_attribute_conflict=bool(diff),
            similarity_note=self._similarity_note(
                name_score, query_attributes, record_attributes, matched, diff, unverified
            ),
            historical_outcome=record.outcome,
            hs_version=record.hs_version,
            applicable_scope=record.applicable_scope,
            valid_until=record.valid_until,
            validity_note=validity_note,
            stale=stale,
            age_days=age_days,
        )

    @staticmethod
    def _similarity_note(
        name_score: float,
        query_attributes: Mapping[str, str],
        record_attributes: Mapping[str, str],
        matched: list[str],
        diff: list[str],
        unverified: list[str],
    ) -> str:
        parts = [
            f"品名词元重合 {name_score:.2f}" if name_score else "品名无词元重合"
        ]
        if matched:
            parts.append("要素一致：" + "、".join(matched))
        if diff:
            parts.append(
                "要素差异："
                + "、".join(
                    f"{key}（查询 {query_attributes.get(key, '')} / 历史 {record_attributes.get(key, '')}）"
                    for key in diff
                )
            )
        if unverified:
            parts.append("历史记录未提供：" + "、".join(unverified))
        if diff:
            parts.append("关键属性存在差异，不得直接沿用历史码")
        elif matched:
            parts.append("关键属性一致，仍仅作参考")
        return "；".join(parts)

    @staticmethod
    def _validity_note(
        record: HistoryRecord, age_days: int | None, expired: bool, stale: bool
    ) -> str:
        parts: list[str] = []
        if _clean_text(record.applicable_scope):
            parts.append(f"历史记录声明适用范围：{record.applicable_scope}")
        else:
            parts.append("历史记录未提供适用范围，是否适用当前商品需人工确认")
        if _clean_text(record.valid_until):
            parts.append(f"记录声明有效期至 {record.valid_until}")
        else:
            parts.append("未提供有效期，不推断其仍有效")
        if expired:
            parts.append("按记录声明判断已过期，HS 版本与当前税则是否一致未核实，只能作为线索")
        elif stale:
            parts.append(f"历史业务日期已 {age_days} 天，HS 版本与当前税则是否一致未核实")
        else:
            parts.append("HS 版本与当前税则是否一致未核实")
        return "；".join(parts)
