"""契约 v1：分析执行与逐行结果。

执行状态是技术维度，比较结论与证据状态是业务维度，三者不合并。
"""
from ninja import Schema
from pydantic import Field

from apps.api.schemas import (
    ComparisonStatus,
    EvidenceEntry,
    EvidenceStatus,
    IssueEntry,
    ResultMode,
    RunKind,
    RunProgress,
    RunStatus,
    SourceLocation,
)


class AttemptEntry(Schema):
    number: int
    status: str
    input_hash: str = ""
    error_code: str = ""
    started_at: str
    finished_at: str | None = None


class PrecheckIn(Schema):
    revision: int = Field(ge=1)


class RetryIn(Schema):
    item_ids: list[str] = Field(min_length=1, max_length=10000)


class RunCreated(Schema):
    run_id: str
    task_id: str
    version_id: str
    status: RunStatus
    parent_run_id: str | None = None
    idempotent_replay: bool = False


class RunItemEntry(Schema):
    item_id: str
    source_location: SourceLocation
    status: RunStatus
    suggested_hs: str | None = Field(default=None, description="未生成建议为 null，不填假码")
    comparison_status: ComparisonStatus | None = None
    confidence: float | None = None
    evidence_status: EvidenceStatus | None = None
    issues: list[IssueEntry] = Field(default_factory=list)
    error_message: str = Field(default="", description="技术失败原因；缺事实不算技术失败")
    attempts: int = Field(default=0, ge=0, description="本执行行实际调用次数；排队未调用为 0")
    attempt_history: list[AttemptEntry] = Field(default_factory=list)
    origin_run_id: str | None = None


class RunDetail(Schema):
    run_id: str
    task_id: str
    version_id: str
    submission_id: str | None = None
    kind: RunKind
    status: RunStatus
    is_current: bool = Field(description="旧执行保留为历史，不参与当前卡片")
    parent_run_id: str | None = None
    retry_allowed: bool = False
    retry_item_ids: list[str] = Field(default_factory=list)
    result_mode: ResultMode
    progress: RunProgress
    items: list[RunItemEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    engine_version: str = ""
    prompt_version: str = ""
    rules_version: str = ""
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
