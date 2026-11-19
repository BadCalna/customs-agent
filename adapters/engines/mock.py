"""固定场景 mock 引擎。

- 结果由 ``ExecutionContext.mock_scenario`` 显式选择，不用随机数、不依赖轮询次数；
- 比较优先级在这里实现一次，供 P16 真实引擎复用，避免各处各写一套判断；
- 所有证据都是明确标注的合成样例，不构成真实归类依据；
- 本模块不产生任何数据库写入。
"""
import re

from apps.api.schemas import (
    ComparisonStatus,
    EvidenceEntry,
    EvidenceStatus,
    ExpertFinding,
    ExpertVerdict,
    IssueEntry,
)

from .protocols import (
    Candidate,
    CompareInput,
    CompareOutput,
    ExpertCheckInput,
    ExpertCheckOutput,
    RecommendInput,
    RecommendOutput,
)

ENGINE_VERSION = "mock-1.0.0"
PROMPT_VERSION = "mock-none"
RULES_VERSION = "d4-0.3"

# 明确标注为合成样例的编码，不能当作真实归类结论。
SAMPLE_HS = "8544422900"
SAMPLE_HS_ALT = "8536901900"

SCENARIOS = (
    "normal",
    "missing_facts",
    "image_failed",
    "query_failed",
    "tech_failure",
    "association_error",
    "supplier_invalid",
    "expert_conflict",
    "expert_elements_unknown",
    "expert_images_unknown",
    "expert_tax_unknown",
    "expert_regulatory_unknown",
)


class EngineTechnicalError(RuntimeError):
    """技术失败：模型超时、限流、来源故障等。与业务缺事实分开记录。"""


def _synthetic_evidence(status: EvidenceStatus, note: str) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_id=f"mock-{status.value}",
        source_type="synthetic",
        title="mock 合成依据（非真实法规）",
        status=status,
        document="MOCK-ONLY",
        locator="合成样例，无真实条款",
        retrieved_at="2026-11-14T00:00:00+08:00",
        query={"mode": "mock"},
        note=note,
    )


def compare_original(payload: CompareInput) -> CompareOutput:
    """D4 主比较结论的判定顺序，P02 发布、P16 复用。

    关联异常 → 原码缺失 → 原码异常 → 资料或证据不足 → 一致/不一致。
    同码但缺决定性事实或证据时仍是 needs_confirmation，不能判一致。
    来源不可达只记待核，不据此宣称编码无效。
    """
    if not payload.association_ok:
        return CompareOutput(
            comparison_status=ComparisonStatus.ASSOCIATION_ERROR,
            reason=payload.association_note or "申报要素与装箱成员关联存在歧义，需人工确认",
            issues=[IssueEntry(code="ASSOCIATION_AMBIGUOUS", message=payload.association_note or "项号关联异常")],
        )
    raw = payload.supplier_hs_raw.strip()
    if not raw:
        return CompareOutput(
            comparison_status=ComparisonStatus.SUPPLIER_MISSING,
            reason="供应商未填原 HS；原值保持空白，不用建议值回填",
            issues=[IssueEntry(code="SUPPLIER_HS_MISSING", message="供应商原 HS 为空", field="supplier_hs_raw")],
        )
    # Validate the raw value here, not just an optimistic caller-supplied flag.
    # Both queue and inline engines share this priority boundary. Eight-digit
    # subheadings are incomplete, not malformed (P16); never pad them to ten.
    # Trimming is comparison-only: never overwrite Item.supplier_hs_raw.
    subheading = re.fullmatch(r"[0-9]{8}", raw) is not None
    if not payload.supplier_hs_format_valid or (not subheading and re.fullmatch(r"[0-9]{10}", raw) is None):
        return CompareOutput(
            comparison_status=ComparisonStatus.SUPPLIER_INVALID,
            reason=payload.supplier_hs_validity_note or "原 HS 格式异常，原文保留",
            issues=[IssueEntry(code="SUPPLIER_HS_INVALID", message="原 HS 格式异常", field="supplier_hs_raw")],
        )
    if subheading:
        return CompareOutput(
            comparison_status=ComparisonStatus.NEEDS_CONFIRMATION,
            reason="原码仅为 8 位税目/子目，需确认完整申报编码；不补零或判为错码",
            issues=[IssueEntry(code="SUPPLIER_HS_SUBHEADING_ONLY", field="supplier_hs_raw",
                               message="原码仅为 8 位税目/子目，不等于错码，需确认完整申报编码",
                               suggested_role="operator")],
        )
    recommendation = payload.recommendation
    if recommendation.suggested_hs is None or recommendation.gaps:
        return CompareOutput(
            comparison_status=ComparisonStatus.NEEDS_CONFIRMATION,
            reason="缺少决定性事实或证据，需补充资料后再比较",
            issues=list(recommendation.gaps),
        )
    if recommendation.evidence_status in (
        EvidenceStatus.QUERY_FAILED,
        EvidenceStatus.NOT_FOUND,
        EvidenceStatus.VERSION_UNVERIFIED,
        None,
    ):
        return CompareOutput(
            comparison_status=ComparisonStatus.NEEDS_CONFIRMATION,
            reason="证据未取得或版本未核实，不生成已核实结论",
            issues=[IssueEntry(code="EVIDENCE_PENDING", message="依据待核，不能据此判定编码有效或无效")],
        )
    if recommendation.suggested_hs == raw:
        return CompareOutput(comparison_status=ComparisonStatus.CONSISTENT, reason="建议与供应商原码一致")
    return CompareOutput(
        comparison_status=ComparisonStatus.INCONSISTENT,
        reason=f"建议 {recommendation.suggested_hs} 与原码 {raw} 不一致",
        issues=[IssueEntry(code="HS_MISMATCH", message="建议编码与原码不一致", field="supplier_hs_raw")],
    )


class MockEngine:
    """按固定场景返回结构化结果；场景未登记时显式报错而非随意成功。"""

    engine_version = ENGINE_VERSION
    prompt_version = PROMPT_VERSION
    rules_version = RULES_VERSION

    def _scenario(self, payload) -> str:
        scenario = payload.context.mock_scenario or "normal"
        if scenario not in SCENARIOS:
            raise ValueError(f"未登记的 mock 场景：{scenario}；可选 {SCENARIOS}")
        return scenario

    def recommend(self, payload: RecommendInput) -> RecommendOutput:
        scenario = self._scenario(payload)
        if scenario == "tech_failure":
            raise EngineTechnicalError("mock 模拟的技术失败：上游模型超时")
        if scenario == "missing_facts":
            return RecommendOutput(
                suggested_hs=None,
                confidence=None,
                confidence_note="缺关键事实，不给置信度",
                gaps=[IssueEntry(
                    code="MISSING_USAGE",
                    message="请补充具体用途",
                    field="attributes.usage",
                    missing="具体用途",
                    suggested_role="supplier",
                )],
                evidence=[_synthetic_evidence(EvidenceStatus.NOT_FOUND, "缺事实，未进行有效检索")],
                evidence_status=EvidenceStatus.NOT_FOUND,
            )
        if scenario == "query_failed":
            return RecommendOutput(
                candidates=[Candidate(hs=SAMPLE_HS, reason="mock 候选，依据未取得")],
                suggested_hs=SAMPLE_HS,
                confidence=None,
                confidence_note="查询失败，不给高置信结论",
                evidence=[_synthetic_evidence(EvidenceStatus.QUERY_FAILED, "mock 模拟的来源查询失败")],
                evidence_status=EvidenceStatus.QUERY_FAILED,
                gaps=[IssueEntry(code="SOURCE_QUERY_FAILED", message="税则来源查询失败，依据待核")],
            )
        gaps = []
        if scenario == "image_failed":
            gaps.append(IssueEntry(
                code="IMAGE_READ_FAILED",
                message="附图读取失败，可见事实不足",
                field="images",
                missing="附图内容",
                suggested_role="supplier",
            ))
        return RecommendOutput(
            candidates=[Candidate(hs=SAMPLE_HS, reason="mock 固定候选，非真实归类")],
            suggested_hs=SAMPLE_HS,
            confidence=0.62,
            confidence_note="mock 固定置信度，未经校准，不是客观概率",
            gaps=gaps,
            evidence=[_synthetic_evidence(EvidenceStatus.OBTAINED, "mock 合成依据，仅用于演示")],
            evidence_status=EvidenceStatus.OBTAINED,
        )

    def compare(self, payload: CompareInput) -> CompareOutput:
        self._scenario(payload)
        return compare_original(payload)

    def expert_check(self, payload: ExpertCheckInput) -> ExpertCheckOutput:
        scenario = self._scenario(payload)
        if scenario == "tech_failure":
            raise EngineTechnicalError("mock 模拟的技术失败：上游模型超时")

        unknown_scenarios = {
            "expert_elements_unknown": (
                "elements",
                "mock：申报要素未核实（条件或版本待核），不视为相符",
                IssueEntry(
                    code="MOCK_ELEMENTS_UNKNOWN",
                    message="mock：申报要素条件或版本待核",
                    field="elements_text",
                    suggested_role="expert",
                ),
            ),
            "expert_images_unknown": (
                "images",
                "mock：附图未取得或读取失败，不推断材质、工艺或税率",
                IssueEntry(
                    code="MOCK_IMAGES_UNKNOWN",
                    message="mock：附图事实未核或读取失败",
                    field="images",
                    suggested_role="operator",
                ),
            ),
            "expert_tax_unknown": (
                "tax",
                "mock：退税率查无或条件未核，不按 0% 处理",
                IssueEntry(
                    code="MOCK_TAX_UNKNOWN",
                    message="mock：退税率未核实，不得读作 0% 或无风险",
                    field="supplier_tax_rate_raw",
                    suggested_role="expert",
                ),
            ),
            "expert_regulatory_unknown": (
                "regulatory",
                "mock：监管条件方向不明或含两用物项线索，不得读作无监管",
                IssueEntry(
                    code="MOCK_REGULATORY_UNKNOWN",
                    message="mock：监管条件或两用物项线索待专家核定",
                    field="regulatory",
                    suggested_role="expert",
                ),
            ),
        }
        if scenario in unknown_scenarios:
            target_aspect, unknown_reason, issue = unknown_scenarios[scenario]
            matched_reasons = {
                "elements": "mock：申报要素与提交码的合成核对相符",
                "images": "mock：附图可见事实与申报品名合成核对相符",
                "tax": "mock：退税率与原表数值合成核对相符",
                "regulatory": "mock：监管条件合成核对无未决项",
            }
            findings = [
                ExpertFinding(
                    aspect=aspect,
                    verdict=ExpertVerdict.UNKNOWN if aspect == target_aspect else ExpertVerdict.MATCHED,
                    reason=unknown_reason if aspect == target_aspect else matched_reasons[aspect],
                    evidence_ids=[] if aspect == target_aspect else ["mock-obtained"],
                )
                for aspect in ("elements", "images", "tax", "regulatory")
            ]
            return ExpertCheckOutput(
                target_hs=payload.submitted_hs,
                findings=findings,
                suggested_hs=None,
                issues=[issue],
                evidence=[_synthetic_evidence(EvidenceStatus.OBTAINED, "mock 专家核查合成依据")],
                evidence_status=EvidenceStatus.OBTAINED,
            )

        findings = [
            ExpertFinding(
                aspect="elements",
                verdict=ExpertVerdict.MATCHED,
                reason="mock：申报要素与提交码的合成核对通过",
                evidence_ids=["mock-obtained"],
            ),
            ExpertFinding(aspect="images", verdict=ExpertVerdict.UNKNOWN, reason="mock：图片事实未核，不视为无风险"),
            ExpertFinding(aspect="tax", verdict=ExpertVerdict.UNKNOWN, reason="mock：退税率未接真实来源，保持待核"),
            ExpertFinding(aspect="regulatory", verdict=ExpertVerdict.UNKNOWN, reason="mock：监管条件未接真实来源，保持待核"),
        ]
        suggested_hs = None
        issues = []
        if scenario == "expert_conflict":
            findings[0] = ExpertFinding(
                aspect="elements",
                verdict=ExpertVerdict.CONFLICT,
                reason="mock：申报要素与提交码不一致，建议另行核对",
                evidence_ids=["mock-obtained"],
            )
            suggested_hs = SAMPLE_HS_ALT
            issues.append(IssueEntry(code="EXPERT_HS_CONFLICT", message="提交码与要素核对冲突", field="submitted_hs"))
        return ExpertCheckOutput(
            target_hs=payload.submitted_hs,
            findings=findings,
            suggested_hs=suggested_hs,
            issues=issues,
            evidence=[_synthetic_evidence(EvidenceStatus.OBTAINED, "mock 专家核查合成依据")],
            evidence_status=EvidenceStatus.OBTAINED,
        )
