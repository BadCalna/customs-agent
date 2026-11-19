"""Unit tests for P18 ExpertCheckEngine & all 12 redlines / invariants."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from django.test import SimpleTestCase
from pydantic import ValidationError

from adapters.engines.expert_check import (
    ANCHORING_NOTICE,
    ENGINE_VERSION,
    EXPERT_ASPECTS,
    PROMPT_VERSION,
    RULES_VERSION,
    AspectEvaluation,
    ExpertCheckAssembly,
    ExpertCheckEngine,
    ModelExpertJudgment,
)
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput, ImageFact
from adapters.sources.history_search import (
    HistoryDatasetInfo,
    HistoryRecord,
    HistorySearchSource,
    HistorySourceRef,
)
from adapters.sources.hs_lookup import Authority, QueryBasis, ReviewStatus, SourceKind
from adapters.sources.regulatory_lookup import (
    Direction,
    DualUseItem,
    DualUseVerdict,
    InMemoryRegulatoryStore,
    MeasureCategory,
    RecordState,
    RegulatoryDatasetInfo,
    RegulatoryMeasure,
    RegulatoryRecord,
    RegulatorySource,
    RegulatorySourceKind,
)
from adapters.sources.tax_lookup import (
    InMemoryTaxRateStore,
    RateKind,
    RateState,
    RebateCondition,
    RebateSourceKind,
    TaxRateDatasetInfo,
    TaxRateRecord,
    TaxRateSource,
)
from apps.api.schemas import (
    EvidenceEntry,
    EvidenceStatus,
    ExpertFinding,
    ExpertVerdict,
    IssueEntry,
    ResultMode,
)
from evaluation.expert.cases import (
    HS_ALT,
    HS_EIGHT,
    HS_MAIN,
    HS_ZERO,
    ExpertEvalCase,
    ImageFixture,
    ModelFixture,
    SourceModes,
)
from evaluation.expert.runner import FIXED_TIME_FUNC, _source_ref, _version_window, build_source_bundle, run_case


def _payload(
    *,
    submitted_hs: str = HS_MAIN,
    supplier_tax_rate_raw: str = "13%",
    elements_text: str = "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:",
    images: list[ImageFact] | None = None,
    evidence: list[EvidenceEntry] | None = None,
    attributes: dict[str, str] | None = None,
    result_mode: ResultMode = ResultMode.LIVE,
    engine_version: str = "",
    prompt_version: str = "",
    rules_version: str = "",
) -> ExpertCheckInput:
    attrs = {"direction": "import", "declaration_date": "2026-05-20", "tariff_version": "2026"}
    if attributes is not None:
        attrs = dict(attributes)
    return ExpertCheckInput(
        submitted_hs=submitted_hs,
        supplier_tax_rate_raw=supplier_tax_rate_raw,
        name="深沟球轴承",
        elements_text=elements_text,
        attributes=attrs,
        images=images or [],
        evidence=evidence or [],
        submission_id="sub-p18-1",
        context=ExecutionContext(
            item_id="item-p18-1",
            run_id="run-p18-1",
            task_id="task-p18-1",
            version_id="v1",
            input_revision=1,
            input_hash=hashlib.sha256(b"p18").hexdigest(),
            result_mode=result_mode,
            engine_version=engine_version,
            prompt_version=prompt_version,
            rules_version=rules_version,
        ),
    )


def _official_case(
    case_id: str = "base",
    *,
    sources: SourceModes | None = None,
    model: ModelFixture | None = None,
    images: list[ImageFixture] | None = None,
    submitted_hs: str = HS_MAIN,
    supplier_tax_rate_raw: str = "13%",
    elements_text: str = "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:",
    direction: str = "import",
) -> ExpertEvalCase:
    return ExpertEvalCase(
        id=case_id,
        description="unit test case",
        submitted_hs=submitted_hs,
        elements_text=elements_text,
        supplier_tax_rate_raw=supplier_tax_rate_raw,
        direction=direction,
        images=images or [],
        sources=sources or SourceModes(),
        model=model or ModelFixture(mode="none"),
        expected_verdicts={"elements": "matched", "images": "unknown", "tax": "matched", "regulatory": "matched"},
    )


class TestExpertCheckConstructionInvariants(SimpleTestCase):
    """构造期强校验：非正向缺原因码、无usable判matched、悬空证据、缺条件给供应商补料必报错。"""

    def test_unknown_without_reason_code_fails_construction(self):
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="tax",
                finding=ExpertFinding(aspect="tax", verdict=ExpertVerdict.UNKNOWN, reason="未核实"),
                reason_codes=[],
                issues=[],
                evidence=[],
                source_usable=False,
                expert_review_required=True,
            )

    def test_conflict_requires_matching_issue_code_and_nonblank_reason(self):
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="elements",
                finding=ExpertFinding(aspect="elements", verdict=ExpertVerdict.CONFLICT, reason="   "),
                reason_codes=["ELEMENT_MISSING"],
                issues=[IssueEntry(code="ELEMENT_MISSING", message="缺型号", severity="warning", field="elements")],
            )
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="elements",
                finding=ExpertFinding(aspect="elements", verdict=ExpertVerdict.CONFLICT, reason="要素冲突"),
                reason_codes=["ELEMENT_MISSING"],
                issues=[],
            )

    def test_matched_forbidden_when_source_not_usable_or_expert_required(self):
        ev = EvidenceEntry(
            evidence_id="ev-1",
            source_type="official_tax",
            title="税率表",
            status=EvidenceStatus.OBTAINED,
            document="官方税率表",
            locator="条目 1",
        )
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="regulatory",
                finding=ExpertFinding(aspect="regulatory", verdict=ExpertVerdict.MATCHED, reason="伪正向", evidence_ids=["ev-1"]),
                reason_codes=[],
                issues=[],
                evidence=[ev],
                source_usable=False,
                expert_review_required=False,
            )
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="tax",
                finding=ExpertFinding(aspect="tax", verdict=ExpertVerdict.MATCHED, reason="仍需专家", evidence_ids=["ev-1"]),
                reason_codes=[],
                issues=[],
                evidence=[ev],
                source_usable=True,
                expert_review_required=True,
            )
        with self.assertRaises(ValidationError):
            AspectEvaluation(
                aspect="images",
                finding=ExpertFinding(aspect="images", verdict=ExpertVerdict.MATCHED, reason="无证据判相符", evidence_ids=[]),
                reason_codes=[],
                issues=[],
                evidence=[],
                source_usable=True,
                expert_review_required=False,
            )

    def test_assembly_rejects_dangling_evidence_and_target_hs_mutation(self):
        tax_ev = EvidenceEntry(
            evidence_id="ev-tax-1",
            source_type="official_tax",
            title="税率表",
            status=EvidenceStatus.OBTAINED,
            document="官方税率表",
            locator="条目 1",
        )
        reg_ev = EvidenceEntry(
            evidence_id="ev-reg-1",
            source_type="official_regulatory",
            title="监管表",
            status=EvidenceStatus.OBTAINED,
            document="官方监管表",
            locator="条目 1",
        )
        elem_ev = EvidenceEntry(
            evidence_id="ev-elem-1",
            source_type="official_declaration",
            title="申报要素表",
            status=EvidenceStatus.OBTAINED,
            document="官方申报要素表",
            locator="条目 1",
        )
        valid_tax = AspectEvaluation(
            aspect="tax",
            finding=ExpertFinding(aspect="tax", verdict=ExpertVerdict.MATCHED, reason="已核", evidence_ids=["ev-tax-1"]),
            evidence=[tax_ev],
            source_usable=True,
            expert_review_required=False,
        )
        valid_reg = AspectEvaluation(
            aspect="regulatory",
            finding=ExpertFinding(aspect="regulatory", verdict=ExpertVerdict.MATCHED, reason="已核", evidence_ids=["ev-reg-1"]),
            evidence=[reg_ev],
            source_usable=True,
            expert_review_required=False,
        )
        valid_img = AspectEvaluation(
            aspect="images",
            finding=ExpertFinding(aspect="images", verdict=ExpertVerdict.UNKNOWN, reason="无图"),
            reason_codes=["IMAGE_ABSENT"],
            issues=[IssueEntry(code="IMAGE_ABSENT", message="无图")],
        )
        with self.assertRaises(ValidationError):
            ExpertCheckAssembly(
                target_hs=HS_MAIN,
                submitted_hs=HS_MAIN,
                aspects=[
                    AspectEvaluation(
                        aspect="elements",
                        finding=ExpertFinding(
                            aspect="elements",
                            verdict=ExpertVerdict.MATCHED,
                            reason="已核",
                            evidence_ids=["missing-ev"],
                        ),
                        source_usable=True,
                        expert_review_required=False,
                    ),
                    valid_img,
                    valid_tax,
                    valid_reg,
                ],
            )
        with self.assertRaises(ValidationError):
            ExpertCheckAssembly(
                target_hs=HS_ALT,
                submitted_hs=HS_MAIN,
                aspects=[
                    AspectEvaluation(
                        aspect="elements",
                        finding=ExpertFinding(
                            aspect="elements",
                            verdict=ExpertVerdict.MATCHED,
                            reason="已核",
                            evidence_ids=["ev-elem-1"],
                        ),
                        evidence=[elem_ev],
                        source_usable=True,
                        expert_review_required=False,
                    ),
                    valid_img,
                    valid_tax,
                    valid_reg,
                ],
            )


class TestExpertCheckRedlinesAndComposition(SimpleTestCase):
    """P18 组合引擎与 12 条业务红线集成测试。"""

    def test_redline_target_hs_preserved_when_model_suggests_recode(self):
        case = _official_case(
            "elements-conflict",
            images=[ImageFixture(status="available", visible_description="继电器塑料壳体")],
            model=ModelFixture(mode="elements_conflict", suggested_hs=HS_ALT, reason="申报品名与轴承不符"),
        )
        output, verdicts = run_case(case)
        self.assertEqual(output.target_hs, HS_MAIN)
        self.assertEqual(output.suggested_hs, HS_ALT)
        self.assertEqual(verdicts["elements"], "conflict")
        self.assertEqual([f.aspect for f in output.findings], list(EXPERT_ASPECTS))

    def test_redline_images_read_failed_vs_absent_and_no_speculation(self):
        bundle = build_source_bundle(_official_case("img-guard", model=ModelFixture(mode="matched")))
        failed_out = bundle.engine.expert_check(
            _payload(
                images=[ImageFact(image_id="img-fail", status="read_failed", source_cell="H9", error_message="DISPIMG 缺失")]
            )
        )
        absent_out = bundle.engine.expert_check(
            _payload(images=[ImageFact(image_id="img-abs", status="absent", source_cell="H9")])
        )
        blank_desc_out = bundle.engine.expert_check(
            _payload(images=[ImageFact(image_id="img-blank", status="available", source_cell="H9", visible_description="")])
        )
        for res in (failed_out, absent_out, blank_desc_out):
            img_finding = next(f for f in res.findings if f.aspect == "images")
            self.assertEqual(img_finding.verdict, ExpertVerdict.UNKNOWN)
            self.assertNotIn("已核实", img_finding.reason)
            self.assertNotIn("通过", img_finding.reason)

        failed_codes = {i.code for i in failed_out.issues}
        absent_codes = {i.code for i in absent_out.issues}
        blank_codes = {i.code for i in blank_desc_out.issues}
        self.assertIn("IMAGE_READ_FAILED", failed_codes)
        self.assertNotIn("IMAGE_ABSENT", failed_codes)
        self.assertIn("IMAGE_ABSENT", absent_codes)
        self.assertNotIn("IMAGE_READ_FAILED", absent_codes)
        self.assertIn("IMAGE_FACTS_UNVERIFIED", blank_codes)

    def test_redline_image_matched_with_valid_evidence_locator_and_model_prompt_guard(self):
        class _InspectModel:
            def __init__(self):
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                return {
                    "elements_semantic_conflict": False,
                    "elements_conflict_reason": "",
                    "image_verdict": "matched",
                    "image_reason": "图片可见深沟球轴承金属外形与申报品名一致",
                    "suggested_hs": None,
                }

        base_bundle = build_source_bundle(_official_case())
        inspector = _InspectModel()
        engine = ExpertCheckEngine(
            elements_source=base_bundle.engine.elements_source,
            tax_source=base_bundle.engine.tax_source,
            regulatory_source=base_bundle.engine.regulatory_source,
            hs_source=base_bundle.engine.hs_source,
            model=inspector,
        )
        out = engine.expert_check(
            _payload(
                images=[ImageFact(image_id="img-1", status="available", source_cell="H3", visible_description="金属深沟球轴承")],
                evidence=[
                    EvidenceEntry(
                        evidence_id="img-fact:H3:img-1",
                        source_type="image_fact",
                        title="申报表附图 H3",
                        status=EvidenceStatus.OBTAINED,
                        document="申报要素表.xlsx",
                        locator="cell H3",
                    )
                ],
            )
        )
        verdicts = {f.aspect: f for f in out.findings}
        self.assertEqual(verdicts["images"].verdict, ExpertVerdict.MATCHED)
        self.assertEqual(verdicts["images"].evidence_ids, ["img-fact:H3:img-1"])
        ev_map = {e.evidence_id: e for e in out.evidence}
        self.assertIn("img-fact:H3:img-1", ev_map)
        self.assertEqual(ev_map["img-fact:H3:img-1"].locator, "cell H3")
        self.assertEqual(len(inspector.requests), 1)
        self.assertEqual(inspector.requests[0]["facts"]["anchoring_risk"], ANCHORING_NOTICE)

    def test_redline_conditional_elements_never_produce_supplier_supplements(self):
        case = _official_case(
            "cond-elem",
            elements_text="1:品牌类型:境外品牌（其它）;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:",
            sources=SourceModes(elements="official_conditional"),
        )
        bundle = build_source_bundle(case)
        asm = bundle.engine.assemble(
            _payload(elements_text=case.elements_text),
            direction="import",
        )
        elem_aspect = next(a for a in asm.aspects if a.aspect == "elements")
        self.assertEqual(elem_aspect.finding.verdict, ExpertVerdict.UNKNOWN)
        self.assertEqual(elem_aspect.supplement_items, [])
        self.assertTrue(all(issue.suggested_role != "supplier" for issue in elem_aspect.issues))

    def test_redline_not_found_emits_zero_fabricated_evidence(self):
        case = _official_case(
            "all-not-found",
            sources=SourceModes(elements="not_found", tax="not_found", regulatory="not_found"),
            supplier_tax_rate_raw="",
        )
        bundle = build_source_bundle(case)
        engine = ExpertCheckEngine(
            elements_source=bundle.engine.elements_source,
            tax_source=bundle.engine.tax_source,
            regulatory_source=bundle.engine.regulatory_source,
            hs_source=None,
        )
        output = engine.expert_check(_payload(supplier_tax_rate_raw=""))
        verdicts = {f.aspect: f.verdict.value for f in output.findings}
        self.assertEqual(verdicts, {"elements": "unknown", "images": "unknown", "tax": "unknown", "regulatory": "unknown"})
        self.assertEqual(output.evidence, [])
        self.assertEqual(output.evidence_status, EvidenceStatus.NOT_FOUND)
        for finding in output.findings:
            self.assertEqual(finding.evidence_ids, [])

    def test_redline_tax_not_effective_is_unknown_and_zero_rate_only_explicit(self):
        dataset = TaxRateDatasetInfo(
            dataset_id="tax-expired-test",
            source_type=RebateSourceKind.OFFICIAL_TAX.value,
            authority=Authority.AUTHORITATIVE,
            synthetic=False,
            description="已废止税率测试库",
            document="2026年退税废止公告",
            retrieved_at="2026-05-01T00:00:00Z",
            review_status=ReviewStatus.REVIEWED,
            reviewed_by="tester",
            reviewed_at="2026-05-01T00:00:00Z",
        )
        rec = TaxRateRecord(
            version="2026",
            hs_code=HS_MAIN,
            product_name="深沟球轴承",
            rate_kind=RateKind.EXPORT_REBATE,
            rate_raw="13%",
            rate_percent=13.0,
            rate_state=RateState.ABOLISHED,
            effective_from="2025-01-01",
            effective_to="2025-12-31",
            conditions=[],
            source=_source_ref("2026年退税废止公告", f"条目 {HS_MAIN}"),
        )
        base = build_source_bundle(_official_case())
        engine = ExpertCheckEngine(
            elements_source=base.engine.elements_source,
            tax_source=TaxRateSource(store=InMemoryTaxRateStore([rec], [_version_window("2026")], dataset=dataset), clock=FIXED_TIME_FUNC),
            regulatory_source=base.engine.regulatory_source,
        )
        out = engine.expert_check(_payload(supplier_tax_rate_raw="9%"))
        tax_finding = next(f for f in out.findings if f.aspect == "tax")
        # Not-effective policy must stay unknown (never report conflict on abolished rate)
        self.assertEqual(tax_finding.verdict, ExpertVerdict.UNKNOWN)

    def test_redline_eight_digit_always_unknown_even_on_tax_and_reg(self):
        case = _official_case(
            "eight-digit",
            submitted_hs=HS_EIGHT,
            sources=SourceModes(elements="official_eight_digit", tax="official_eight_digit", regulatory="official_eight_digit"),
        )
        output, verdicts = run_case(case)
        self.assertEqual(verdicts, {"elements": "unknown", "images": "unknown", "tax": "unknown", "regulatory": "unknown"})
        codes = {i.code for i in output.issues}
        self.assertTrue({"ELEMENTS_SUBHEADING_ONLY", "TAX_SUBHEADING_ONLY", "REG_SUBHEADING_ONLY"}.issubset(codes))

    def test_redline_history_synthetic_excluded_in_live_mode_and_never_upgrades(self):
        history = HistorySearchSource(
            records=[
                HistoryRecord(
                    record_id="h-1",
                    historical_hs=HS_ALT,
                    historical_name="深沟球轴承",
                    recorded_at="2025-01-01",
                    outcome="approved",
                    source=HistorySourceRef(document="合成历史表", locator="第1行", system="synthetic"),
                )
            ],
            dataset=HistoryDatasetInfo(dataset_id="h-synth", synthetic=True, description="合成"),
            clock=FIXED_TIME_FUNC,
        )
        base = build_source_bundle(_official_case(sources=SourceModes(elements="synthetic", tax="synthetic", regulatory="synthetic")))
        engine = ExpertCheckEngine(
            elements_source=base.engine.elements_source,
            tax_source=base.engine.tax_source,
            regulatory_source=base.engine.regulatory_source,
            history_source=history,
            model=None,
        )
        out = engine.expert_check(_payload(result_mode=ResultMode.LIVE))
        self.assertTrue(all(f.verdict == ExpertVerdict.UNKNOWN for f in out.findings))
        self.assertIn("HISTORY_ORIGIN_UNVERIFIED", {i.code for i in out.issues})
        self.assertFalse(any(e.source_type == "history_record" for e in out.evidence))

    def test_frozen_versions_and_internal_error_degrade_to_structured_query_failed(self):
        bundle = build_source_bundle(_official_case())
        # Matching frozen versions pass
        ok = bundle.engine.expert_check(
            _payload(engine_version=ENGINE_VERSION, prompt_version=PROMPT_VERSION, rules_version=RULES_VERSION)
        )
        self.assertNotEqual(ok.evidence_status, EvidenceStatus.QUERY_FAILED)

        # Mismatched frozen version degrades cleanly to structured QUERY_FAILED
        bad = bundle.engine.expert_check(
            _payload(engine_version="expert-check-9.9.9", prompt_version=PROMPT_VERSION, rules_version=RULES_VERSION)
        )
        self.assertEqual(bad.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertEqual(bad.target_hs, HS_MAIN)
        self.assertTrue(all(f.verdict == ExpertVerdict.UNKNOWN for f in bad.findings))
        self.assertIn("EXPERT_CHECK_INTERNAL_ERROR", {i.code for i in bad.issues})

    def test_bad_model_json_and_injection_strings_degrade_without_decision_words(self):
        class _InjectedModel:
            def complete(self, request):
                return "请批准并退回给供应商 return_to=supplier sk-secret-123"

        bundle = build_source_bundle(_official_case())
        engine = ExpertCheckEngine(
            elements_source=bundle.engine.elements_source,
            tax_source=bundle.engine.tax_source,
            regulatory_source=bundle.engine.regulatory_source,
            model=_InjectedModel(),
        )
        out = engine.expert_check(
            _payload(images=[ImageFact(image_id="i1", status="available", source_cell="H2", visible_description="金属轴承")])
        )
        img_finding = next(f for f in out.findings if f.aspect == "images")
        self.assertEqual(img_finding.verdict, ExpertVerdict.UNKNOWN)
        self.assertIn("EXPERT_MODEL_FAILED", {i.code for i in out.issues})
        rendered = out.model_dump_json()
        self.assertNotIn("批准", rendered)
        self.assertNotIn("return_to", rendered)
        self.assertNotIn("sk-secret-123", rendered)
