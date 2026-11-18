"""Strict fixtures for post-recommendation logic, not model classification quality."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.schemas import ComparisonStatus, EvidenceStatus

SUITE_VERSION = "p16-synthetic-postcompare-1.0.0"
HS = "8544422900"
ALT_HS = "8536901900"


class StrictFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class EvidenceFixture(StrictFixture):
    document: str | None = "SYNTHETIC-TECHNICAL-ONLY"
    locator: str | None = "fixture-row-1-not-a-real-clause"
    status: EvidenceStatus = EvidenceStatus.OBTAINED

    @field_validator("status", mode="before")
    @classmethod
    def enum_value(cls, value):
        return EvidenceStatus(value) if isinstance(value, str) else value


class RecommendationFixture(StrictFixture):
    suggested_hs: str | None = HS
    evidence_status: EvidenceStatus | None = EvidenceStatus.OBTAINED
    evidence: list[EvidenceFixture] = Field(default_factory=lambda: [EvidenceFixture()])
    gap_codes: list[str] = Field(default_factory=list)

    @field_validator("evidence_status", mode="before")
    @classmethod
    def enum_value(cls, value):
        return EvidenceStatus(value) if isinstance(value, str) else value

    @field_validator("gap_codes")
    @classmethod
    def codes(cls, values):
        if any(not x.strip() for x in values) or len(values) != len(set(values)):
            raise ValueError("gap codes must be nonblank and distinct")
        return values


class SyntheticCase(StrictFixture):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    synthetic: Literal[True] = True
    supplier_hs_raw: str = HS
    association_ok: bool = True
    supplier_hs_format_valid: bool = True
    recommendation: RecommendationFixture = Field(default_factory=RecommendationFixture)
    source: Literal["none", "not_found", "query_failed", "version_unverified"] = "none"
    expected_status: ComparisonStatus
    expected_issue_codes: list[str]

    @field_validator("synthetic", mode="before")
    @classmethod
    def true_boolean_only(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("synthetic must be the boolean true")
        return value

    @field_validator("expected_status", mode="before")
    @classmethod
    def enum_value(cls, value):
        return ComparisonStatus(value) if isinstance(value, str) else value

    @field_validator("description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("description must not be blank")
        return value

    @field_validator("expected_issue_codes")
    @classmethod
    def codes(cls, values):
        if any(not x.strip() for x in values) or len(values) != len(set(values)):
            raise ValueError("expected issue codes must be nonblank and distinct")
        return values


def fixed_cases() -> list[SyntheticCase]:
    """Fresh immutable-by-convention fixed inputs; no files, randomness or clock."""
    rows = [
        dict(id="association", description="关联异常优先", association_ok=False,
             expected_status="association_error", expected_issue_codes=["ASSOCIATION_AMBIGUOUS"]),
        dict(id="supplier-missing", description="原码空白不回填", supplier_hs_raw=" \n",
             expected_status="supplier_missing", expected_issue_codes=["SUPPLIER_HS_MISSING"]),
        dict(id="supplier-invalid", description="原码非法，不洗白", supplier_hs_raw="bad-code",
             expected_status="supplier_invalid", expected_issue_codes=["SUPPLIER_HS_INVALID"]),
        dict(id="missing-facts", description="同码缺事实仍待核", recommendation={"gap_codes": ["FACTS_PENDING"]},
             expected_status="needs_confirmation", expected_issue_codes=["FACTS_PENDING"]),
        dict(id="consistent", description="仅后置合成逻辑一致，不证明归类正确",
             expected_status="consistent", expected_issue_codes=[]),
        dict(id="inconsistent", description="完整码后置不一致", supplier_hs_raw=ALT_HS,
             expected_status="inconsistent", expected_issue_codes=["HS_MISMATCH"]),
        dict(id="priority-association-all", description="关联高于缺码、非法标记和证据异常",
             association_ok=False, supplier_hs_raw="", supplier_hs_format_valid=False,
             recommendation={"evidence_status": "obtained", "evidence": [], "gap_codes": ["FACTS_PENDING"]},
             expected_status="association_error", expected_issue_codes=["ASSOCIATION_AMBIGUOUS", "SUPPLIER_HS_MISSING", "FACTS_PENDING", "EVIDENCE_EMPTY"]),
        dict(id="priority-missing-invalid", description="缺码高于非法标记和缺事实",
             supplier_hs_raw="", supplier_hs_format_valid=False, recommendation={"gap_codes": ["FACTS_PENDING"]},
             expected_status="supplier_missing", expected_issue_codes=["SUPPLIER_HS_MISSING", "FACTS_PENDING"]),
        dict(id="priority-invalid-pending", description="非法原码高于推荐未知和资料待核",
             supplier_hs_raw="123", recommendation={"suggested_hs": None, "gap_codes": ["FACTS_PENDING"]},
             expected_status="supplier_invalid", expected_issue_codes=["SUPPLIER_HS_INVALID", "FACTS_PENDING", "RECOMMENDATION_PENDING"]),
        dict(id="priority-pending-mismatch", description="缺资料高于异码不一致",
             supplier_hs_raw=ALT_HS, recommendation={"gap_codes": ["FACTS_PENDING"]},
             expected_status="needs_confirmation", expected_issue_codes=["FACTS_PENDING"]),
        dict(id="same-no-evidence-state", description="同码没有证据状态，不判一致",
             recommendation={"evidence_status": None, "evidence": []},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_PENDING"]),
        dict(id="same-empty-obtained", description="已取得但空证据，不判一致",
             recommendation={"evidence": []}, expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_EMPTY"]),
        dict(id="same-query-failed", description="来源失败不等于查无或无效",
             recommendation={"evidence_status": "query_failed", "evidence": []},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_PENDING"]),
        dict(id="same-not-found", description="证据未查到不等于错码",
             recommendation={"evidence_status": "not_found", "evidence": []},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_PENDING"]),
        dict(id="same-version-unverified", description="版本待核不判一致",
             recommendation={"evidence_status": "version_unverified", "evidence": []},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_PENDING"]),
        dict(id="empty-locator", description="不可定位的合成证据不足",
             recommendation={"evidence": [{"locator": " "}]},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_UNVERIFIED"]),
        dict(id="evidence-state-unverified", description="单条依据未核实不能判一致",
             recommendation={"evidence": [{"status": "version_unverified"}]},
             expected_status="needs_confirmation", expected_issue_codes=["EVIDENCE_UNVERIFIED"]),
        dict(id="invalid-recommendation", description="非法推荐编码不比较",
             recommendation={"suggested_hs": "bad"},
             expected_status="needs_confirmation", expected_issue_codes=["SUGGESTED_HS_INVALID"]),
        dict(id="supplier-eight-digits", description="8位子目不是错码也不是完整码", supplier_hs_raw="85444229",
             expected_status="needs_confirmation", expected_issue_codes=["SUPPLIER_HS_SUBHEADING_ONLY"]),
        dict(id="recommendation-eight-digits", description="推荐8位不能当完整编码比较",
             recommendation={"suggested_hs": "85444229"},
             expected_status="needs_confirmation", expected_issue_codes=["SUGGESTED_HS_INVALID"]),
        dict(id="supplier-unicode-digits", description="非ASCII数字原码不能洗白", supplier_hs_raw="８５４４４２２９００",
             expected_status="supplier_invalid", expected_issue_codes=["SUPPLIER_HS_INVALID"]),
        dict(id="source-not-found", description="内存合成来源查无不宣称原码无效", source="not_found",
             expected_status="needs_confirmation", expected_issue_codes=["HS_CODE_NOT_FOUND", "SUPPLIER_HS_VALIDITY_PENDING"]),
        dict(id="source-query-failed", description="注入内存来源失败，不读文件", source="query_failed",
             expected_status="needs_confirmation", expected_issue_codes=["HS_QUERY_FAILED", "SUPPLIER_HS_VALIDITY_PENDING"]),
        dict(id="source-version-unverified", description="来源缺查询基准不填本机日期", source="version_unverified",
             expected_status="needs_confirmation", expected_issue_codes=["HS_VERSION_UNVERIFIED", "SUPPLIER_HS_VALIDITY_PENDING"]),
    ]
    return [SyntheticCase.model_validate(row) for row in rows]
