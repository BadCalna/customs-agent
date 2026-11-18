"""mock 引擎与比较优先级：D4 第 2 节的判定顺序只能有这一份实现。"""
from django.test import SimpleTestCase

from adapters.engines.mock import (
    SAMPLE_HS,
    EngineTechnicalError,
    MockEngine,
    compare_original,
)
from adapters.engines.protocols import (
    Candidate,
    CompareInput,
    ExecutionContext,
    ExpertCheckInput,
    RecommendInput,
    RecommendOutput,
)
from apps.api.schemas import ComparisonStatus, EvidenceStatus, IssueEntry


def context(scenario="normal"):
    return ExecutionContext(
        task_id="t", version_id="v", item_id="i", run_id="r", input_revision=1,
        mock_scenario=scenario,
    )


def recommend_input(scenario="normal"):
    return RecommendInput(context=context(scenario), name="合成商品", attributes={})


def recommendation(**overrides):
    payload = {
        "candidates": [Candidate(hs=SAMPLE_HS)],
        "suggested_hs": SAMPLE_HS,
        "confidence": 0.62,
        "evidence_status": EvidenceStatus.OBTAINED,
    }
    payload.update(overrides)
    return RecommendOutput(**payload)


def compare(raw, rec=None, **overrides):
    payload = {
        "context": context(),
        "recommendation": rec or recommendation(),
        "supplier_hs_raw": raw,
    }
    payload.update(overrides)
    return compare_original(CompareInput(**payload)).comparison_status


class ComparisonPriorityTest(SimpleTestCase):
    def test_association_error_outranks_everything(self):
        self.assertEqual(
            compare(SAMPLE_HS, association_ok=False), ComparisonStatus.ASSOCIATION_ERROR,
        )
        # 即使原码空白，也先报关联异常。
        self.assertEqual(compare("", association_ok=False), ComparisonStatus.ASSOCIATION_ERROR)

    def test_missing_outranks_invalid_and_mismatch(self):
        self.assertEqual(compare(""), ComparisonStatus.SUPPLIER_MISSING)
        self.assertEqual(compare("   "), ComparisonStatus.SUPPLIER_MISSING)

    def test_invalid_outranks_evidence_and_mismatch(self):
        self.assertEqual(
            compare("ABC", supplier_hs_format_valid=False), ComparisonStatus.SUPPLIER_INVALID,
        )

    def test_invalid_raw_is_detected_even_when_caller_omits_format_flag(self):
        for raw in ("123456789", "12345678901", "123456789A", "１２３４５６７８９０", "12345 67890"):
            for rec in (recommendation(), recommendation(evidence_status=EvidenceStatus.VERSION_UNVERIFIED),
                        recommendation(suggested_hs=None, gaps=[IssueEntry(code="MISSING", message="缺资料")])):
                with self.subTest(raw=raw, evidence=rec.evidence_status):
                    self.assertEqual(compare(raw, rec=rec), ComparisonStatus.SUPPLIER_INVALID)
                    self.assertEqual(compare(raw, rec=rec, association_ok=False), ComparisonStatus.ASSOCIATION_ERROR)

    def test_format_valid_does_not_mean_evidence_verified(self):
        rec = recommendation(suggested_hs="0123456789", evidence_status=EvidenceStatus.VERSION_UNVERIFIED)
        self.assertEqual(compare("0123456789", rec=rec), ComparisonStatus.NEEDS_CONFIRMATION)
        self.assertEqual(compare(f" {SAMPLE_HS} "), ComparisonStatus.CONSISTENT)
        self.assertEqual(compare("0123456789"), ComparisonStatus.INCONSISTENT)

    def test_same_hs_without_evidence_is_needs_confirmation(self):
        rec = recommendation(evidence_status=EvidenceStatus.QUERY_FAILED)
        self.assertEqual(compare(SAMPLE_HS, rec=rec), ComparisonStatus.NEEDS_CONFIRMATION)

    def test_same_hs_with_gaps_is_needs_confirmation(self):
        rec = recommendation(gaps=[IssueEntry(code="MISSING_USAGE", message="请补充用途")])
        self.assertEqual(compare(SAMPLE_HS, rec=rec), ComparisonStatus.NEEDS_CONFIRMATION)

    def test_no_suggestion_is_needs_confirmation(self):
        rec = recommendation(suggested_hs=None, candidates=[])
        self.assertEqual(compare(SAMPLE_HS, rec=rec), ComparisonStatus.NEEDS_CONFIRMATION)

    def test_consistent_and_inconsistent(self):
        self.assertEqual(compare(SAMPLE_HS), ComparisonStatus.CONSISTENT)
        self.assertEqual(compare("7306290000"), ComparisonStatus.INCONSISTENT)

    def test_unverified_source_does_not_invalidate_code(self):
        rec = recommendation(evidence_status=EvidenceStatus.VERSION_UNVERIFIED)
        self.assertEqual(compare(SAMPLE_HS, rec=rec), ComparisonStatus.NEEDS_CONFIRMATION)


class RecommendInputTest(SimpleTestCase):
    def test_no_supplier_hs_field_by_design(self):
        """防原码锚定靠结构保证，而不是靠调用方自觉不传。"""

        self.assertNotIn("supplier_hs_raw", RecommendInput.model_fields)
        self.assertNotIn("supplier_hs_raw", RecommendInput.model_json_schema()["properties"])

    def test_mock_is_deterministic(self):
        engine = MockEngine()
        first = engine.recommend(recommend_input())
        second = engine.recommend(recommend_input())
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_unknown_scenario_fails_loudly(self):
        with self.assertRaises(ValueError):
            MockEngine().recommend(recommend_input("not-a-scenario"))

    def test_technical_failure_is_not_business_result(self):
        with self.assertRaises(EngineTechnicalError):
            MockEngine().recommend(recommend_input("tech_failure"))

    def test_missing_facts_gives_no_confidence(self):
        output = MockEngine().recommend(recommend_input("missing_facts"))
        self.assertIsNone(output.suggested_hs)
        self.assertIsNone(output.confidence)
        self.assertEqual(output.evidence_status, EvidenceStatus.NOT_FOUND)


class ExpertCheckTest(SimpleTestCase):
    def test_target_is_always_submitted_hs(self):
        for scenario in ("normal", "expert_conflict"):
            output = MockEngine().expert_check(ExpertCheckInput(
                context=context(scenario), submission_id="s", submitted_hs="7306290000", name="合成商品",
            ))
            self.assertEqual(output.target_hs, "7306290000")

    def test_unknown_is_not_treated_as_safe(self):
        output = MockEngine().expert_check(ExpertCheckInput(
            context=context(), submission_id="s", submitted_hs=SAMPLE_HS, name="合成商品",
        ))
        verdicts = {finding.aspect: finding.verdict.value for finding in output.findings}
        self.assertEqual(verdicts["tax"], "unknown")
        self.assertEqual(verdicts["regulatory"], "unknown")
