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
    precheck_allowed: bool = False
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


# --- P09 业务员确认与整批提交（D3 第 2 节） ---

class ConfirmationIn(Schema):
    """确认一次 HS；``expected_revision`` 是商品的当前修订号，不是任务修订号。"""

    confirmed_hs: str = Field(description="10 位数字编码；不合法返回 422")
    reason: str = ""
    expected_revision: int = Field(ge=1)


class ConfirmationOut(Schema):
    confirmation_id: str
    task_id: str
    version_id: str
    item_id: str
    revision: int = Field(description="确认后商品的新修订号")
    task_revision: int
    previous_hs: str
    confirmed_hs: str
    reason: str
    created_by: str
    created_at: str
    task_stage: TaskStage
    idempotent_replay: bool = False


class SubmissionLineIn(Schema):
    """逐行显式引用确认记录：专家审核的就是这一条确认的 HS。"""

    item_id: str
    confirmation_id: str


class SubmissionIn(Schema):
    version_id: str
    revision: int = Field(ge=1, description="提交时的任务修订号 Task.revision")
    items: list[SubmissionLineIn]


class SubmissionLineOut(Schema):
    item_id: str
    confirmation_id: str
    submitted_hs: str
    item_revision: int


class SubmissionOut(Schema):
    submission_id: str
    task_id: str
    version_id: str
    revision: int
    status: SubmissionStatus
    items: list[SubmissionLineOut]
    expert_run_id: str | None = None
    expert_run_status: RunStatus | None = None
    task_stage: TaskStage
    task_revision: int
    created_at: str
    idempotent_replay: bool = False


# --- P10 专家处置、业务员退回与只读历史 ---

class DecisionLineIn(Schema):
    item_id: str
    final_hs: str
    change_reason: str = ""


class ReturnIssueIn(Schema):
    item_id: str = ""
    message: str


class DecisionIn(Schema):
    action: str = Field(description="approve 整批通过 / return 退回")
    reason: str = ""
    return_to: str = Field(default="", description="退回时使用：supplier 或 operator")
    items: list[DecisionLineIn] = Field(default_factory=list)
    issues: list[ReturnIssueIn] = Field(default_factory=list)


class DecisionLineOut(Schema):
    item_id: str
    previous_hs: str
    final_hs: str
    change_reason: str


class DecisionOut(Schema):
    decision_id: str
    submission_id: str
    task_id: str
    version_id: str
    action: str
    return_to: str | None = None
    reason: str
    items: list[DecisionLineOut]
    submission_status: SubmissionStatus
    task_stage: TaskStage
    task_revision: int
    created_at: str
    idempotent_replay: bool = False


class TaskReturnIn(Schema):
    version_id: str
    revision: int = Field(ge=1)
    reason: str = ""
    issues: list[ReturnIssueIn] = Field(default_factory=list)


class TaskReturnOut(Schema):
    task_id: str
    version_id: str
    stage: TaskStage
    task_revision: int
    issue_ids: list[str]
    idempotent_replay: bool = False


class HistoryItemRef(Schema):
    item_id: str
    name: str
    supplier_hs_raw: str
    revision: int
    source_location: SourceLocation


class HistoryConfirmation(Schema):
    confirmation_id: str
    item_id: str
    revision: int
    previous_hs: str
    confirmed_hs: str
    reason: str
    created_by: str
    created_at: str


class HistoryRun(Schema):
    run_id: str
    kind: RunKind
    status: RunStatus
    is_current: bool
    submission_id: str | None = None
    created_at: str
    finished_at: str | None = None


class HistorySubmission(Schema):
    submission_id: str
    revision: int
    status: SubmissionStatus
    created_by: str
    created_at: str
    items: list[SubmissionLineOut]
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class HistoryVersion(Schema):
    version_id: str
    number: int
    original_filename: str
    uploaded_at: str
    is_current: bool
    items: list[HistoryItemRef]
    confirmations: list[HistoryConfirmation]
    runs: list[HistoryRun]
    submissions: list[HistorySubmission]


class CrossVersionCandidate(Schema):
    """跨版本对照只是候选展示，不建立稳定的商品身份。"""

    item_id: str
    candidate_item_id: str
    candidate_version_id: str
    basis: str
    note: str


class HistoryIssue(Schema):
    issue_id: str
    item_id: str | None = None
    kind: str
    message: str
    status: str
    created_at: str


class HistoryAuditEvent(Schema):
    audit_id: str
    action: str
    stage: str
    version_id: str | None = None
    item_id: str | None = None
    actor: str
    previous_value: str
    new_value: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class TaskHistoryOut(Schema):
    task_id: str
    stage: TaskStage
    revision: int
    versions: list[HistoryVersion]
    cross_version_candidates: list[CrossVersionCandidate]
    issues: list[HistoryIssue]
    audit_events: list[HistoryAuditEvent]


class WithdrawalOut(Schema):
    """撤回提交的结果：提交转为 superseded，任务回到待业务员确认。"""

    submission_id: str
    task_id: str
    version_id: str
    status: SubmissionStatus
    task_stage: TaskStage
    task_revision: int
    idempotent_replay: bool = False
