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
    attempts: int = Field(default=1, ge=1, description="已尝试次数，重试另计")


class RunDetail(Schema):
    run_id: str
    task_id: str
    version_id: str
    submission_id: str | None = None
    kind: RunKind
    status: RunStatus
    is_current: bool = Field(description="旧执行保留为历史，不参与当前卡片")
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
