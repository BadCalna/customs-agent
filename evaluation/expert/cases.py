"""Strict evaluation fixtures and gold-standard schema for P18 expert checks."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from adapters.sources.hs_lookup import _parse_date, _strip_invisible
from apps.api.schemas import EvidenceStatus, ExpertVerdict

SUITE_VERSION = "p18-synthetic-expert-1.0.0"
HS_MAIN = "8482102000"
HS_ALT = "8536490090"
HS_ZERO = "7304190000"
HS_EIGHT = "72081000"


class StrictEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ImageFixture(StrictEvalModel):
    image_id: str = "img-1"
    status: Literal["available", "absent", "read_failed"] = "absent"
    source_cell: str = "H3"
    visible_description: str = ""
    error_message: str = ""


class ModelFixture(StrictEvalModel):
    mode: Literal["none", "matched", "elements_conflict", "images_conflict", "raise_error", "bad_json"] = "none"
    suggested_hs: str | None = None
    reason: str = ""


class SourceModes(StrictEvalModel):
    elements: Literal[
        "official_sufficient",
        "official_insufficient",
        "official_conditional",
        "official_eight_digit",
        "synthetic",
        "not_found",
        "query_failed",
        "version_unverified",
        "raise_error",
    ] = "official_sufficient"
    tax: Literal[
        "official_match_13",
        "official_zero_rate",
        "official_not_refundable",
        "official_conditional",
        "official_conditions_missing",
        "official_eight_digit",
        "synthetic",
        "not_found",
        "query_failed",
        "version_unverified",
        "raise_error",
    ] = "official_match_13"
    regulatory: Literal[
        "official_declared_none",
        "official_measures",
        "official_dual_use",
        "official_measure_dual_use",
        "official_eight_digit",
        "synthetic",
        "not_found",
        "query_failed",
        "version_unverified",
        "raise_error",
    ] = "official_declared_none"
    history: Literal["none", "matched_different_hs"] = "none"


class ExpectedVerdicts(StrictEvalModel):
    elements: ExpertVerdict
    images: ExpertVerdict
    tax: ExpertVerdict
    regulatory: ExpertVerdict

    @field_validator("elements", "images", "tax", "regulatory", mode="before")
    @classmethod
    def _parse_verdict(cls, value):
        return ExpertVerdict(value) if isinstance(value, str) else value


class EvaluationThresholds(StrictEvalModel):
    min_unknown_coverage: float = Field(ge=0.0, le=1.0, default=1.0)
    max_false_matched_on_unknown: int = Field(ge=0, default=0)
    min_overall_accuracy: float = Field(ge=0.0, le=1.0, default=0.85)
    min_conflict_recall: float = Field(ge=0.0, le=1.0, default=0.8)


class ExpertEvalCase(StrictEvalModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    synthetic: bool = True
    submitted_hs: str = HS_MAIN
    name: str = "深沟球轴承（合成测试商品）"
    elements_text: str = "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:"
    supplier_tax_rate_raw: str = "13%"
    declaration_date: str | None = "2026-05-20"
    tariff_version: str | None = "2026"
    direction: Literal["import", "export", "unknown"] = "import"
    images: list[ImageFixture] = Field(default_factory=list)
    sources: SourceModes = Field(default_factory=SourceModes)
    model: ModelFixture = Field(default_factory=ModelFixture)
    expected_verdicts: ExpectedVerdicts
    expected_evidence_status: EvidenceStatus | None = EvidenceStatus.OBTAINED
    expected_issue_codes: list[str] = Field(default_factory=list)
    expected_suggested_hs: str | None = None

    @field_validator("synthetic", mode="before")
    @classmethod
    def _bool_only(cls, value):
        if type(value) is not bool:
            raise ValueError("synthetic must be a boolean")
        return value

    @field_validator("expected_evidence_status", mode="before")
    @classmethod
    def _parse_ev_status(cls, value):
        return EvidenceStatus(value) if isinstance(value, str) else value

    @field_validator("description")
    @classmethod
    def _nonblank(cls, value):
        if not _strip_invisible(value):
            raise ValueError("description must not be blank")
        return value

    @field_validator("expected_issue_codes")
    @classmethod
    def _codes(cls, values):
        if any(not _strip_invisible(item) for item in values) or len(values) != len(set(values)):
            raise ValueError("expected_issue_codes must be nonblank and distinct")
        return values


class ExpertGoldSuite(StrictEvalModel):
    """Jay/关务专家标注的金标准样本集契约；非金标准只能用于技术验证。"""

    suite_id: str = Field(min_length=1)
    gold_standard: Literal[True] = True
    synthetic: Literal[False] = False
    annotated_by: str = Field(min_length=1)
    annotated_at: str = Field(min_length=1)
    thresholds: EvaluationThresholds = Field(default_factory=EvaluationThresholds)
    cases: list[ExpertEvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_gold(self) -> "ExpertGoldSuite":
        if not _strip_invisible(self.annotated_by):
            raise ValueError("gold suite requires nonblank annotated_by")
        parsed = _parse_date(self.annotated_at)
        if parsed is None:
            raise ValueError("gold suite annotated_at must be valid ISO date/datetime")
        if any(case.synthetic is not False for case in self.cases):
            raise ValueError("gold suite cannot contain synthetic=true cases")
        return self


def fixed_cases() -> list[ExpertEvalCase]:
    """18 条固定合成技术样本：覆盖四类分项 matched/conflict/unknown 与全部口径红线。"""
    rows = [
        dict(
            id="all-matched-official-sim",
            description="四类均来自模拟已复核官方来源且可见图片一致，产出四项 matched",
            images=[ImageFixture(status="available", visible_description="金属深沟球轴承实物，无品牌标记")],
            model=ModelFixture(mode="matched"),
            sources=SourceModes(
                elements="official_sufficient",
                tax="official_match_13",
                regulatory="official_declared_none",
            ),
            expected_verdicts={"elements": "matched", "images": "matched", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=[],
        ),
        dict(
            id="elements-insufficient-conflict",
            description="已核官方清单下缺少必填申报要素型号，elements 判 conflict",
            elements_text="1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:;4:品牌（中文或外文名称）:无",
            images=[ImageFixture(status="available", visible_description="金属轴承实物")],
            model=ModelFixture(mode="matched"),
            sources=SourceModes(elements="official_sufficient"),
            expected_verdicts={"elements": "conflict", "images": "matched", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["ELEMENT_MISSING"],
        ),
        dict(
            id="elements-semantic-conflict",
            description="要素形式齐全但模型发现与提交码语义矛盾并给出替代建议，target_hs 保持不变",
            images=[ImageFixture(status="available", visible_description="塑料管件实物")],
            model=ModelFixture(mode="elements_conflict", suggested_hs=HS_ALT, reason="申报品名及可见特征与轴承税号不符"),
            sources=SourceModes(elements="official_sufficient"),
            expected_verdicts={"elements": "conflict", "images": "matched", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["EXPERT_ELEMENTS_SEMANTIC_CONFLICT"],
            expected_suggested_hs=HS_ALT,
        ),
        dict(
            id="elements-conditional-unverified",
            description="条件要素未提供（C-03），elements 保持 unknown 且不生成供应商补料",
            elements_text="1:品牌类型:境外品牌（其它）;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:",
            sources=SourceModes(elements="official_conditional"),
            expected_verdicts={"elements": "unknown", "images": "unknown", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["ELEMENT_CONDITION_UNVERIFIED", "ELEMENTS_UNVERIFIED", "IMAGE_ABSENT"],
        ),
        dict(
            id="eight-digit-subheading-unknown",
            description="8 位子目在要素/退税/监管一律 usable=false 且保持 unknown",
            submitted_hs=HS_EIGHT,
            supplier_tax_rate_raw="10%",
            sources=SourceModes(
                elements="official_eight_digit",
                tax="official_eight_digit",
                regulatory="official_eight_digit",
            ),
            expected_verdicts={"elements": "unknown", "images": "unknown", "tax": "unknown", "regulatory": "unknown"},
            expected_evidence_status="obtained",
            expected_issue_codes=["ELEMENT_HS_INCOMPLETE", "ELEMENTS_SUBHEADING_ONLY", "IMAGE_ABSENT", "TAX_SUBHEADING_ONLY", "REG_SUBHEADING_ONLY"],
        ),
        dict(
            id="images-read-failed-distinct",
            description="read_failed 即使模型响应 matched 也强制 unknown，且不混同 absent",
            images=[ImageFixture(status="read_failed", source_cell="H8", error_message="DISPIMG 缺失")],
            model=ModelFixture(mode="matched"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_READ_FAILED"],
        ),
        dict(
            id="images-available-blank-description",
            description="附图 available 但无可见描述，读不出不得推断，保持 unknown",
            images=[ImageFixture(status="available", visible_description="   ")],
            model=ModelFixture(mode="matched"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_FACTS_UNVERIFIED"],
        ),
        dict(
            id="images-visible-conflict",
            description="附图可见描述与申报品名冲突，images 判 conflict",
            images=[ImageFixture(status="available", visible_description="明显为塑料外壳电线接头")],
            model=ModelFixture(mode="images_conflict", suggested_hs=HS_ALT, reason="图片可见为塑料接头而非金属轴承"),
            expected_verdicts={"elements": "matched", "images": "conflict", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_FACT_CONFLICT"],
            expected_suggested_hs=HS_ALT,
        ),
        dict(
            id="tax-rate-conflict-9-vs-13",
            description="已核适用退税率 13% 与原表 9% 数值冲突，tax 判 conflict",
            supplier_tax_rate_raw="9%",
            sources=SourceModes(tax="official_match_13"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "conflict", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "TAX_RATE_CONFLICT"],
        ),
        dict(
            id="tax-not-refundable-c18-unknown",
            description="C-18：原表 13% 与来源不予退税不可比对，必须 unknown",
            supplier_tax_rate_raw="13%",
            sources=SourceModes(tax="official_not_refundable"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "unknown", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "TAX_CHECK_UNVERIFIED"],
        ),
        dict(
            id="tax-zero-rate-matched",
            description="来源明确声明 rate_kind=zero_rate 且原表 0%，tax 判 matched",
            submitted_hs=HS_ZERO,
            supplier_tax_rate_raw="0%",
            sources=SourceModes(tax="official_zero_rate"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT"],
        ),
        dict(
            id="tax-not-found-blank-no-zero",
            description="退税查不到且原表空白，不按 0% 处理且不造证据，保持 unknown",
            supplier_tax_rate_raw="",
            sources=SourceModes(tax="not_found"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "unknown", "regulatory": "matched"},
            expected_evidence_status="not_found",
            expected_issue_codes=["IMAGE_ABSENT", "TAX_RATE_NOT_FOUND", "TAX_ORIGINAL_RATE_BLANK"],
        ),
        dict(
            id="tax-conditions-not-declared-unknown",
            description="退税条件字段缺失（not_declared）不等同于无条件，保持 unknown",
            sources=SourceModes(tax="official_conditions_missing"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "unknown", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "TAX_CONDITION_NOT_DECLARED"],
        ),
        dict(
            id="regulatory-measures-verified-matched",
            description="官方已复核监管表明确列出有名称有定位的非两用监管措施，regulatory 判 matched",
            sources=SourceModes(regulatory="official_measures"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "matched"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT"],
        ),
        dict(
            id="regulatory-dual-use-unknown",
            description="两用物项线索（dual_use[] 与 measures[].category=dual_use）恒为 unknown",
            direction="export",
            sources=SourceModes(regulatory="official_dual_use"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "unknown"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "REG_DUAL_USE_UNVERIFIED"],
        ),
        dict(
            id="regulatory-direction-unknown",
            description="缺进出口方向时监管必须保持 unknown，不默认按进口或双向处理",
            direction="unknown",
            sources=SourceModes(regulatory="official_declared_none"),
            expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "unknown"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "REG_DIRECTION_UNKNOWN"],
        ),
        dict(
            id="history-synthetic-cannot-upgrade",
            description="来源为合成数据时即使历史命中不同码，四分项仍全部保持 unknown",
            sources=SourceModes(
                elements="synthetic",
                tax="synthetic",
                regulatory="synthetic",
                history="matched_different_hs",
            ),
            expected_verdicts={"elements": "unknown", "images": "unknown", "tax": "unknown", "regulatory": "unknown"},
            expected_evidence_status="obtained",
            expected_issue_codes=["IMAGE_ABSENT", "TAX_AUTHORITY_NOT_OFFICIAL", "REG_AUTHORITY_NOT_OFFICIAL"],
        ),
        dict(
            id="source-exception-structured-query-failed",
            description="内部来源抛出异常时结构化降级为 query_failed 与四项 unknown，不 500",
            sources=SourceModes(tax="raise_error"),
            expected_verdicts={"elements": "unknown", "images": "unknown", "tax": "unknown", "regulatory": "unknown"},
            expected_evidence_status="query_failed",
            expected_issue_codes=["EXPERT_CHECK_INTERNAL_ERROR"],
        ),
    ]
    return [ExpertEvalCase.model_validate(row) for row in rows]
