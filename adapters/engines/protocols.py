"""两阶段引擎协议（D4 第 1/2 节）。

边界：
- 独立 HS 推荐阶段**不接收供应商原 HS**，输入 DTO 里没有该字段，从结构上防锚定；
- 原码比较是独立阶段，推荐结果 + 原码 + 关联/有效性信息才进入比较；
- 引擎只返回结构化结果，不选择写入目标、不批准、不决定退回对象；
- 专家核查必须绑定提交快照，核查对象是业务员提交的 HS。
"""
from typing import Protocol

from ninja import Schema
from pydantic import Field

from apps.api.schemas import (
    ComparisonStatus,
    EvidenceEntry,
    EvidenceStatus,
    ExpertFinding,
    IssueEntry,
    ResultMode,
)


class ExecutionContext(Schema):
    """共有执行上下文，标识一律由服务端注入。"""

    task_id: str
    version_id: str
    item_id: str
    run_id: str
    input_revision: int
    input_hash: str = ""
    engine_version: str = ""
    prompt_version: str = ""
    rules_version: str = ""
    result_mode: ResultMode = ResultMode.MOCK
    mock_scenario: str | None = Field(
        default=None,
        description="仅 mock 引擎使用的固定场景选择键；真实引擎忽略，不参与业务判断",
    )


class ImageFact(Schema):
    """图片只支持可见事实；读取失败不得推断工艺、材质或税务条件。"""

    image_id: str
    status: str
    source_cell: str
    visible_description: str = ""
    error_message: str = ""


class RecommendInput(Schema):
    """独立推荐输入：精简 DTO，刻意不含 supplier_hs_raw 与任何阶段结果。"""

    context: ExecutionContext
    name: str
    attributes: dict[str, str] = Field(default_factory=dict)
    elements_text: str = Field(
        default="",
        description="申报要素原文；其中可能自然出现编码，由输入构造器在 anchoring_notes 记录",
    )
    anchoring_notes: list[str] = Field(default_factory=list)
    images: list[ImageFact] = Field(default_factory=list)
    query_basis: dict[str, str] = Field(
        default_factory=dict, description="查询基准；系统不填拟申报日期"
    )


class Candidate(Schema):
    hs: str
    reason: str = ""
    confidence: float | None = None


class RecommendOutput(Schema):
    candidates: list[Candidate] = Field(default_factory=list)
    suggested_hs: str | None = Field(default=None, description="不确定时为 null，不填假码凑格式")
    confidence: float | None = None
    confidence_note: str = ""
    gaps: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    evidence_status: EvidenceStatus | None = None


class CompareInput(Schema):
    """原码比较输入：独立推荐结果 + 供应商原码 + 关联与有效性信息。"""

    context: ExecutionContext
    recommendation: RecommendOutput
    supplier_hs_raw: str = Field(description="原样保留，空白即空白")
    association_ok: bool = True
    association_note: str = ""
    supplier_hs_format_valid: bool = True
    supplier_hs_validity_note: str = ""


class CompareOutput(Schema):
    comparison_status: ComparisonStatus
    reason: str = ""
    issues: list[IssueEntry] = Field(default_factory=list)


class PrecheckOutput(Schema):
    """一次预审的完整行结果：推荐与比较字段互不覆盖。"""

    suggested_hs: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    comparison_status: ComparisonStatus | None = None
    comparison_reason: str = ""
    confidence: float | None = None
    evidence_status: EvidenceStatus | None = None
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    issues: list[IssueEntry] = Field(default_factory=list)


class ExpertCheckInput(Schema):
    """专家核查输入：绑定不可变提交快照，核查对象是提交码。"""

    context: ExecutionContext
    submission_id: str
    submitted_hs: str
    name: str
    attributes: dict[str, str] = Field(default_factory=dict)
    elements_text: str = ""
    supplier_tax_rate_raw: str = ""
    images: list[ImageFact] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)


class ExpertCheckOutput(Schema):
    target_hs: str = Field(description="本次核查针对的提交码；专家改码后仍保留原值")
    findings: list[ExpertFinding] = Field(default_factory=list)
    suggested_hs: str | None = None
    issues: list[IssueEntry] = Field(default_factory=list)
    evidence: list[EvidenceEntry] = Field(default_factory=list)
    evidence_status: EvidenceStatus | None = None


class Engine(Protocol):
    """引擎协议。mock 与真实实现公开同一组方法。"""

    def recommend(self, payload: RecommendInput) -> RecommendOutput: ...

    def compare(self, payload: CompareInput) -> CompareOutput: ...

    def expert_check(self, payload: ExpertCheckInput) -> ExpertCheckOutput: ...


def get_engine() -> Engine:
    """按配置返回引擎实例；未实现的模式在启动期就被 settings 拒绝。"""

    from django.conf import settings

    from .mock import MockEngine

    if settings.ENGINE_MODE == "mock":
        return MockEngine()
    raise RuntimeError(f"未实现的引擎模式：{settings.ENGINE_MODE}")
