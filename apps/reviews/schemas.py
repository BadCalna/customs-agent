"""契约 v1：任务、版本、商品列表与商品卡片。

字段组严格对应 D3 第 3 节；列表项是卡片的子集，不用展示文案反推状态。
"""
from typing import Any

from ninja import Schema
from pydantic import Field

from apps.api.schemas import (
    Action,
    ComparisonStatus,
    EvidenceEntry,
    EvidenceStatus,
    ExpertCheckRef,
    ImageEntry,
    ImageStatus,
    IssueEntry,
    ResultMode,
    RunKind,
    RunStatus,
    SourceLocation,
    StageValue,
    SubmissionStatus,
    TaskStage,
)


class VersionEntry(Schema):
    version_id: str
    number: int = Field(ge=1, description="V1 为 1；同任务内唯一")
    original_filename: str
    uploaded_at: str
    is_current: bool


class TaskListEntry(Schema):
    task_id: str
    title: str
    stage: TaskStage
    supplier: str = Field(description="供应商显示名")
    revision: int
    current_version_id: str | None = None
    current_version_number: int | None = None
    latest_run_id: str | None = None
    latest_run_kind: RunKind | None = None
    latest_run_status: RunStatus | None = None
    allowed_actions: list[Action]
    result_mode: ResultMode
    created_at: str
    updated_at: str


class TaskPage(Schema):
    """D3 第 1 节分页形状；游标为空表示没有下一页。"""

    items: list[TaskListEntry]
    next_cursor: str | None = None


class TaskDetail(TaskListEntry):
    versions: list[VersionEntry] = Field(default_factory=list)
    current_submission_id: str | None = None
    current_submission_status: SubmissionStatus | None = None


class ItemListEntry(Schema):
    """商品列表行：身份、位置、原值与各阶段编码的当前值。"""

    task_id: str
    version_id: str
    item_id: str
    revision: int
    source_location: SourceLocation
    name: str
    supplier_hs_raw: str = Field(description="供应商原 HS，空白原样保留")
    precheck_suggested_hs: str | None = None
    business_confirmed_hs: str | None = None
    submitted_hs: str | None = None
    expert_final_hs: str | None = None
    comparison_status: ComparisonStatus | None = None
    image_status: ImageStatus
    latest_run_id: str | None = None
    result_mode: ResultMode


class ItemPage(Schema):
    items: list[ItemListEntry]
    next_cursor: str | None = None


class ItemFacts(Schema):
    """原文与结构化提取分开保存，任何阶段结果都不回写这里。"""

    name: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    supplier_hs_raw: str
    supplier_elements_raw: str
    supplier_tax_rate_raw: str


class ItemCard(Schema):
    """商品卡片完整字段组（D3 第 3 节）。"""

    task_id: str
    version_id: str
    item_id: str
    revision: int
    source_location: SourceLocation
    facts: ItemFacts
    images: list[ImageEntry] = Field(default_factory=list)
    image_status: ImageStatus
    precheck_suggested_hs: StageValue
    business_confirmed_hs: StageValue
    submitted_hs: StageValue
    expert_suggested_hs: StageValue
    expert_final_hs: StageValue
    comparison_status: ComparisonStatus | None = None
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence_status: EvidenceStatus | None = None
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    confidence: float | None = None
    latest_run_id: str | None = None
    latest_expert_check: ExpertCheckRef | None = None
    result_mode: ResultMode
    task_stage: TaskStage
    submission_status: SubmissionStatus | None = None
    allowed_actions: list[Action]
