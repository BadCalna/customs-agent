"""品目注释/子目注释读取适配（P23）。

定位
----
P14 的两个适配器只回答「编码在税则里是什么」与「该编码要哪些申报要素」，
**没有**注释正文的字段或检索入口。所以本模块提供的是**独立的、版本化的**
解释资料读取接口，数据来自 P23 导入流水线产出的 ``p23.commentary.1`` 快照。

三个边界（写进代码而不只是文档）：

1. **层级键不是申报编码**：注释按品目（4 位，如 ``0101``）、章（``CH1``）、类（``CA1``）、
   其他注释（``ot3``/``ot4``）组织。传入 8/10 位编码一律 ``invalid_input`` 并说明原因，
   避免把“该品目有注释”读成“该 10 位编码存在/有效”。
2. **还没有消费方**：结果里的 ``engine_integration="not_wired_p16"`` 明示“已导入、
   尚未接入 P16”；不引入向量库/Embedding，只做确定性查找与子串检索。
3. **正文不改写**：返回的分段正文与导入快照逐字一致；``text_all`` 与分段正文的
   一致性在**加载期**校验，不一致直接报存储不可读，而不是悄悄降级。

存储
----
默认读环境变量 ``CUSTOMS_TARIFF_COMMENTARY_PATH`` 指向的本地 JSON；未配置时
返回 ``query_status=source_unconfigured``，不回退到任何样本。
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from ninja import Schema
from pydantic import Field, ValidationError, model_validator

from apps.api.schemas import EvidenceEntry, EvidenceStatus, IssueEntry

from adapters.sources.hs_lookup import (
    DatasetInfo,
    QueryStatus,
    SourceRef,
    _strip_invisible,
    _summarize_validation_error,
    read_json_document,
)
from adapters.sources.hs_lookup import TariffStoreError

#: 注释快照的结构版本；结构变化时递增，旧格式明确报错而不是猜。
SCHEMA_VERSION = "p23.commentary.1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

#: 注释文件路径的环境变量。**没有默认样本**。
ENV_COMMENTARY_PATH = "CUSTOMS_TARIFF_COMMENTARY_PATH"

#: 引擎接入状态；当前没有消费方，不得声称已接入。
ENGINE_INTEGRATION_NOT_WIRED = "not_wired_p16"

_HEADING = re.compile(r"^\d{4}$")
_DIGITS = re.compile(r"^\d+$")
_PREFIXED = re.compile(r"^(CH|CA|OT)(\d+)$")
_SPACES = re.compile(r"[\s\u3000]+")


class CommentaryLevel(str, Enum):
    """注释条目所属的税则层级。"""

    HEADING = "heading"
    """4 位品目，如 0101。"""

    CHAPTER = "chapter_note"
    """章注，如 CH1。"""

    SECTION = "section_note"
    """类注，如 CA1。"""

    OTHER = "other_note"
    """归类总规则/HS 公约等其他注释，如 ot3。"""


_LEVEL_BY_KIND = {
    "品目": CommentaryLevel.HEADING,
    "章注": CommentaryLevel.CHAPTER,
    "类注": CommentaryLevel.SECTION,
    "其他注释": CommentaryLevel.OTHER,
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class CommentarySection(Schema):
    """一条注释正文分段；``anchor`` 是该段在该层级键内的确定性定位。"""

    label: str = Field(min_length=1, description="来源分段标签：类注/章注/总注释/品目注释/子目注释")
    text: str = Field(description="正文原文，逐字保留，不改写不截断")
    anchor: str = Field(min_length=1, description="段定位，如 0101#子目注释-1")

    @model_validator(mode="after")
    def _check_anchor(self) -> "CommentarySection":
        if not _strip_invisible(self.label):
            raise ValueError("分段标签去不可见字符后不能为空")
        if not _strip_invisible(self.anchor):
            raise ValueError("分段定位不能为空，否则无法回查原文")
        return self


class CommentaryRecord(Schema):
    """某个税则层级键（品目/章/类/其他）下的注释全文。"""

    tariff_no: str = Field(min_length=1, max_length=8, description="层级键：0101 / CH1 / CA1 / ot3")
    kind: str = Field(default="", description="来源条目类型原文")
    chapter: str = ""
    title: str = ""
    sections: list[CommentarySection] = Field(min_length=1)
    text_all: str = Field(description="全文；与 sections 的重新拼接结果必须一致")
    text_sha256: str = Field(min_length=64, max_length=64)
    text_length: int = Field(ge=0, description="text_all 的字符长度")
    section_char_total: int = Field(
        ge=0, description="各分段正文长度之和（来源 char_count 的口径）"
    )
    source_char_count: int | None = Field(
        default=None, description="来源自己写的 char_count；有值时须与分段合计一致"
    )
    source: SourceRef
    note: str = ""

    @model_validator(mode="after")
    def _check(self) -> "CommentaryRecord":
        import hashlib

        if not _strip_invisible(self.tariff_no):
            raise ValueError("tariff_no 不能为空")
        if len(self.text_all) != self.text_length:
            raise ValueError(
                f"text_length 与正文长度不一致：{self.text_length} != {len(self.text_all)}"
            )
        digest = hashlib.sha256(self.text_all.encode("utf-8")).hexdigest()
        if digest != self.text_sha256:
            raise ValueError(f"条目 {self.tariff_no} 的正文哈希不匹配，正文可能被改写")
        section_total = sum(len(item.text) for item in self.sections)
        if section_total != self.section_char_total:
            raise ValueError(
                f"条目 {self.tariff_no} 的分段字数合计不一致：{self.section_char_total} != {section_total}"
            )
        if self.source_char_count is not None and self.source_char_count != section_total:
            raise ValueError(
                f"条目 {self.tariff_no} 的来源 char_count 与分段合计不一致"
            )
        return self

    @property
    def level(self) -> CommentaryLevel | None:
        return _LEVEL_BY_KIND.get(self.kind)

    def section(self, label: str) -> list[CommentarySection]:
        return [item for item in self.sections if item.label == label]


class CommentaryDatasetInfo(DatasetInfo):
    """注释来源的数据集说明；``authoritative`` 仍要求“官方 + 已复核”。"""


class CommentaryQuery(Schema):
    """注释查询输入：层级键或关键词，二选一。"""

    key_raw: str = Field(default="", description="层级键原文：0101 / CH1 / CA1 / ot3")
    keyword: str = Field(default="", description="正文关键词（子串匹配，不分词、不做向量检索）")
    label: str = Field(default="", description="只看某个分段标签，如 子目注释")
    limit: int = Field(default=20, ge=1, le=200)


class CommentaryLookupResult(Schema):
    """一次注释查询的完整结果。"""

    query: CommentaryQuery | None = None
    query_status: QueryStatus
    evidence_status: EvidenceStatus | None = Field(
        default=None, description="D4 四态；输入不合法时为 null（未发起查询）"
    )
    level: CommentaryLevel | None = None
    records: list[CommentaryRecord] = Field(default_factory=list)
    version: str = ""
    reason: str = ""
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    dataset: CommentaryDatasetInfo | None = None
    engine_integration: str = Field(
        default=ENGINE_INTEGRATION_NOT_WIRED,
        description="当前没有引擎消费方；不得读成“已接入 P16 检索”",
    )
    searched_at: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "CommentaryLookupResult":
        if self.query_status is QueryStatus.INVALID_INPUT:
            if self.evidence_status is not None or self.records or self.evidence:
                raise ValueError("invalid_input 必须先于查询，不得携带记录或证据")
            return self
        if self.query_status is not QueryStatus.OK:
            if self.evidence_status is not EvidenceStatus.QUERY_FAILED:
                raise ValueError(f"{self.query_status.value} 必须对应 query_failed")
            if self.records or self.evidence:
                raise ValueError("查询失败时不得生成记录或虚构证据")
            return self
        if self.records:
            if self.evidence_status not in (
                EvidenceStatus.OBTAINED,
                EvidenceStatus.VERSION_UNVERIFIED,
            ):
                raise ValueError("取到注释时 evidence_status 必须是 obtained/version_unverified")
            if not self.evidence:
                raise ValueError("取到注释时必须带可定位证据")
            return self
        if self.evidence_status is not EvidenceStatus.NOT_FOUND:
            raise ValueError("未取到注释时 evidence_status 必须是 not_found")
        if self.evidence:
            raise ValueError("未取到注释时不得生成证据")
        return self


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentarySnapshot:
    dataset: CommentaryDatasetInfo
    version: str
    records: tuple[CommentaryRecord, ...]
    integration: Mapping[str, Any]


class CommentaryStoreError(RuntimeError):
    """注释存储不可读/结构不可用；以 query_failed 表达，不能伪装成查无。"""


class CommentaryStoreNotConfigured(CommentaryStoreError):
    """没有配置注释来源。"""


class CommentaryStore(Protocol):
    def load(self) -> CommentarySnapshot: ...


def default_commentary_path() -> Path | None:
    override = os.environ.get(ENV_COMMENTARY_PATH, "").strip()
    return Path(override).expanduser() if override else None


def _dataset_from_payload(payload: Any, *, source_name: str) -> CommentaryDatasetInfo:
    if payload is None:
        payload = {
            "dataset_id": source_name,
            "source_type": "synthetic",
            "authority": "unspecified",
            "synthetic": None,
            "description": "数据文件未声明 dataset；按未声明来源处理",
        }
    if not isinstance(payload, Mapping):
        raise CommentaryStoreError(f"注释文件的 dataset 必须是对象：{source_name}")
    try:
        return CommentaryDatasetInfo.model_validate(dict(payload))
    except ValidationError as exc:
        raise CommentaryStoreError(
            f"注释文件的 dataset 描述不可用（{_summarize_validation_error(exc)}）"
        ) from exc


def _snapshot_from_payload(payload: Any, *, source_name: str) -> CommentarySnapshot:
    if not isinstance(payload, Mapping):
        raise CommentaryStoreError(f"注释文件顶层必须是对象：{source_name}")
    version = payload.get("schema_version")
    if version is None:
        raise CommentaryStoreError("注释文件缺少 schema_version，不能按未知结构静默读取")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise CommentaryStoreError(
            f"注释文件结构版本不受支持：{version}；支持 {SUPPORTED_SCHEMA_VERSIONS}"
        )
    dataset = _dataset_from_payload(payload.get("dataset"), source_name=source_name)
    meta = payload.get("version")
    label = ""
    if isinstance(meta, Mapping):
        label = str(meta.get("version") or "")
    elif isinstance(meta, str):
        label = meta
    records_raw = payload.get("records")
    if not isinstance(records_raw, list):
        raise CommentaryStoreError(f"注释文件缺少 records 数组：{source_name}")
    records: list[CommentaryRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(records_raw):
        try:
            record = CommentaryRecord.model_validate(item)
        except ValidationError as exc:
            raise CommentaryStoreError(
                f"第 {index + 1} 条注释记录不符合契约（{_summarize_validation_error(exc)}）"
            ) from exc
        if record.tariff_no in seen:
            raise CommentaryStoreError(f"注释记录重复：{record.tariff_no}")
        seen.add(record.tariff_no)
        records.append(record)
    integration = payload.get("integration")
    return CommentarySnapshot(
        dataset=dataset,
        version=label,
        records=tuple(records),
        integration=dict(integration) if isinstance(integration, Mapping) else {},
    )


class JsonCommentaryStore:
    """本地 JSON 注释存储；每次 ``load()`` 重新读文件。"""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> CommentarySnapshot:
        try:
            payload = read_json_document(self._path, label="品目注释")
        except TariffStoreError as exc:
            raise CommentaryStoreError(str(exc)) from exc
        return _snapshot_from_payload(payload, source_name=self._path.name)


class EnvCommentaryStore(JsonCommentaryStore):
    """按 ``CUSTOMS_TARIFF_COMMENTARY_PATH`` 读取；未配置时明确报“未配置”。"""

    def __init__(self) -> None:
        self._path = default_commentary_path()

    @property
    def path(self) -> Path | None:  # type: ignore[override]
        return self._path

    def load(self) -> CommentarySnapshot:
        if self._path is None:
            raise CommentaryStoreNotConfigured(
                "未配置品目注释来源：设置环境变量 "
                f"{ENV_COMMENTARY_PATH} 指向 P23 导入的注释 JSON；"
                "本包不提供默认样本，避免把演示数据当成官方注释"
            )
        try:
            payload = read_json_document(self._path, label="品目注释")
        except TariffStoreError as exc:
            raise CommentaryStoreError(str(exc)) from exc
        return _snapshot_from_payload(payload, source_name=self._path.name)


class InMemoryCommentaryStore:
    """内存存储：测试或上游已持有快照时注入。"""

    def __init__(
        self,
        records: Iterable[CommentaryRecord | Mapping[str, Any]],
        *,
        dataset: CommentaryDatasetInfo | Mapping[str, Any],
        version: str = "",
        integration: Mapping[str, Any] | None = None,
    ):
        self._raw_records = tuple(records)
        self._dataset = dataset
        self._version = version
        self._integration = dict(integration or {})

    def load(self) -> CommentarySnapshot:
        records: list[dict[str, Any]] = []
        for index, item in enumerate(self._raw_records):
            try:
                parsed = (
                    item.model_dump(mode="json")
                    if isinstance(item, CommentaryRecord)
                    else CommentaryRecord.model_validate(item).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise CommentaryStoreError(
                    f"第 {index + 1} 条注释记录不符合契约（{_summarize_validation_error(exc)}）"
                ) from exc
            records.append(parsed)
        dataset = (
            self._dataset.model_dump(mode="json")
            if isinstance(self._dataset, CommentaryDatasetInfo)
            else CommentaryDatasetInfo.model_validate(self._dataset).model_dump(mode="json")
        )
        return _snapshot_from_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": dataset,
                "version": {"version": self._version},
                "integration": dict(self._integration),
                "records": records,
            },
            source_name="in-memory",
        )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _search_keyword(text: str) -> str:
    """检索用归一：NFKC + 空白折叠成单个空格；不改正文、不做同义词扩展。"""

    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


def _normalize_key(raw: str) -> tuple[str, CommentaryLevel | None, str]:
    """把层级键原文归一成 ``(key, level, problem)``；问题为空表示可用。"""

    text = _SPACES.sub("", unicodedata.normalize("NFKC", raw or "")).strip()
    if not text:
        return "", None, "层级键为空"
    if _HEADING.match(text):
        return text, CommentaryLevel.HEADING, ""
    upper = text.upper()
    match = _PREFIXED.match(upper)
    if match:
        prefix, number = match.groups()
        if prefix == "CH":
            return f"CH{int(number)}", CommentaryLevel.CHAPTER, ""
        if prefix == "CA":
            return f"CA{int(number)}", CommentaryLevel.SECTION, ""
        return f"ot{int(number)}", CommentaryLevel.OTHER, ""
    if _DIGITS.match(text):
        if len(text) == 4:
            return text, CommentaryLevel.HEADING, ""
        return (
            text,
            None,
            f"输入 {text!r} 是 {len(text)} 位数字：品目注释按 4 位品目、章注按 CH、"
            "类注按 CA 组织；8/10 位申报编码不属于注释层级，"
            "不能用该品目有注释来证明编码存在或有效",
        )
    return text, None, f"无法识别的层级键：{text!r}（支持 0101 / CH1 / CA1 / ot3）"


class TariffCommentarySource:
    """按层级键或关键词读取品目注释/子目注释。

    典型用法::

        source = TariffCommentarySource()          # 读 CUSTOMS_TARIFF_COMMENTARY_PATH
        result = source.lookup("0101")
        result.records[0].section("子目注释")

    未配置来源时返回 ``source_unconfigured``/``query_failed``，不回退到样本。
    """

    def __init__(self, store: CommentaryStore | None = None, *, clock: Any = None):
        self._store = store or EnvCommentaryStore()
        self._clock = clock or (
            lambda: __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            )
        )

    @classmethod
    def load(cls, path: str | Path, **kwargs: Any) -> "TariffCommentarySource":
        return cls(JsonCommentaryStore(path), **kwargs)

    def lookup(self, query: CommentaryQuery | Mapping[str, Any] | str | None = None) -> CommentaryLookupResult:
        prepared, problem = self._prepare(query)
        now = self._as_aware(self._clock()).isoformat(timespec="seconds")
        if prepared is None:
            return self._invalid(problem, now=now)
        key, level, key_problem = _normalize_key(prepared.key_raw)
        keyword = _SPACES.sub(" ", unicodedata.normalize("NFKC", prepared.keyword or "")).strip()
        if not key and not keyword:
            return self._invalid("没有层级键也没有关键词；未发起查询", prepared=prepared, now=now)
        if key and key_problem:
            return self._invalid(key_problem, prepared=prepared, now=now)
        try:
            snapshot = self._store.load()
        except CommentaryStoreNotConfigured as exc:
            return self._failed(QueryStatus.SOURCE_UNCONFIGURED, str(exc), prepared, now)
        except CommentaryStoreError as exc:
            return self._failed(QueryStatus.SOURCE_UNREADABLE, str(exc), prepared, now)
        except Exception as exc:  # 存储实现自身缺陷不能变成 500
            return self._failed(
                QueryStatus.SOURCE_ERROR,
                f"注释读取发生未预期异常（{type(exc).__name__}）；这不等于查无注释",
                prepared,
                now,
            )
        label = _SPACES.sub("", unicodedata.normalize("NFKC", prepared.label or "")).strip()
        if key:
            record = next((item for item in snapshot.records if item.tariff_no == key), None)
            matches = [record] if record is not None else []
        else:
            # 关键词按“NFKC + 空白归一”比对：正文里有换行，用户输入里是空格，
            # 不归一就会明明有这段文字却查不到。这是子串匹配，不做分词/向量检索。
            needle = _search_keyword(keyword)
            matches = [
                item
                for item in snapshot.records
                if needle and needle in _search_keyword(item.text_all)
            ][: prepared.limit]
        if label:
            matches = [item for item in matches if item.section(label)]
        if not matches:
            return CommentaryLookupResult(
                query=prepared,
                query_status=QueryStatus.OK,
                evidence_status=EvidenceStatus.NOT_FOUND,
                level=level,
                version=snapshot.version,
                reason=(
                    f"注释快照里没有匹配的条目（层级键={key or '无'}，关键词={keyword or '无'}）；"
                    "这是本快照未收录，不等于该品目/章没有注释"
                ),
                issues=[
                    IssueEntry(
                        code="COMMENTARY_NOT_FOUND",
                        message="本注释快照未收录该层级键/关键词",
                        field="commentary_key",
                        suggested_role="operator",
                    )
                ],
                dataset=snapshot.dataset,
                engine_integration=ENGINE_INTEGRATION_NOT_WIRED,
                searched_at=now,
            )
        evidence = [self._evidence(item, snapshot, label=label) for item in matches]
        return CommentaryLookupResult(
            query=prepared,
            query_status=QueryStatus.OK,
            evidence_status=EvidenceStatus.OBTAINED,
            level=level,
            records=matches,
            version=snapshot.version,
            reason=(
                f"在注释快照 {snapshot.version or '未标版本'} 中取到 {len(matches)} 条记录；"
                "正文原样返回，未接入 P16 检索"
            ),
            evidence=evidence,
            dataset=snapshot.dataset,
            engine_integration=ENGINE_INTEGRATION_NOT_WIRED,
            searched_at=now,
        )

    # --- 内部

    @staticmethod
    def _prepare(
        query: CommentaryQuery | Mapping[str, Any] | str | None,
    ) -> tuple[CommentaryQuery | None, str]:
        if isinstance(query, CommentaryQuery):
            return query, ""
        if isinstance(query, str):
            return CommentaryQuery(key_raw=query), ""
        if isinstance(query, Mapping):
            try:
                return CommentaryQuery.model_validate(dict(query)), ""
            except (ValidationError, TypeError, ValueError) as exc:
                return None, f"查询输入不符合契约（{_summarize_validation_error(exc)}）；未发起查询"
        if query is None:
            return None, "查询为空；未发起查询"
        return None, f"查询输入类型不受支持（{type(query).__name__}）；未发起查询"

    def _invalid(
        self,
        reason: str,
        *,
        prepared: CommentaryQuery | None = None,
        now: str,
        code: str = "COMMENTARY_QUERY_INVALID",
    ) -> CommentaryLookupResult:
        return CommentaryLookupResult(
            query=prepared,
            query_status=QueryStatus.INVALID_INPUT,
            evidence_status=None,
            reason=reason,
            issues=[
                IssueEntry(
                    code=code,
                    message=reason,
                    field="commentary_key",
                    missing="可识别的层级键（0101 / CH1 / CA1 / ot3）或关键词",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    def _failed(
        self,
        status: QueryStatus,
        reason: str,
        query: CommentaryQuery,
        now: str,
    ) -> CommentaryLookupResult:
        return CommentaryLookupResult(
            query=query,
            query_status=status,
            evidence_status=EvidenceStatus.QUERY_FAILED,
            reason=reason,
            issues=[
                IssueEntry(
                    code="COMMENTARY_QUERY_FAILED",
                    message=reason,
                    field="commentary_source",
                    missing="可读取的品目注释快照",
                    suggested_role="operator",
                )
            ],
            searched_at=now,
        )

    @staticmethod
    def _evidence(
        record: CommentaryRecord, snapshot: CommentarySnapshot, *, label: str
    ) -> EvidenceEntry:
        source = record.source
        excerpt = record.text_all
        if label:
            sections = record.section(label)
            excerpt = "\n\n".join(
                f"【{item.label}】\n{item.text}" for item in sections
            ) or record.text_all
        return EvidenceEntry(
            evidence_id=f"commentary:{snapshot.version or 'unversioned'}:{record.tariff_no}",
            source_type="official_tariff",
            title=f"品目注释 {record.tariff_no} {record.title}".strip(),
            status=EvidenceStatus.OBTAINED,
            url=source.url,
            document=source.document,
            locator=source.locator,
            excerpt=excerpt[:500],
            retrieved_at=source.retrieved_at,
            scope=(
                f"品目注释层级键 {record.tariff_no}（{record.kind or '未标注类型'}）；"
                "不代表任何 10 位编码存在或有效"
            ),
            query={"tariff_no": record.tariff_no, "label": label or ""},
            note=(
                "已导入、尚未接入 P16 检索：这是确定性查找结果，不是检索召回"
                if not record.note
                else f"已导入、尚未接入 P16；{record.note}"
            ),
        )

    @staticmethod
    def _as_aware(moment: Any):
        from datetime import datetime, timezone

        if isinstance(moment, datetime) and moment.tzinfo is not None:
            return moment
        if isinstance(moment, datetime):
            return moment.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)


__all__ = [
    "CommentaryDatasetInfo",
    "CommentaryLevel",
    "CommentaryLookupResult",
    "CommentaryQuery",
    "CommentaryRecord",
    "CommentarySection",
    "CommentarySnapshot",
    "CommentaryStore",
    "CommentaryStoreError",
    "CommentaryStoreNotConfigured",
    "ENV_COMMENTARY_PATH",
    "ENGINE_INTEGRATION_NOT_WIRED",
    "EnvCommentaryStore",
    "InMemoryCommentaryStore",
    "JsonCommentaryStore",
    "SCHEMA_VERSION",
    "TariffCommentarySource",
    "default_commentary_path",
]
