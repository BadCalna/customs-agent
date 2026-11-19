"""Regression tests for P18 adversarial review findings (P1 query identity & P2 image gates)."""
from __future__ import annotations

import hashlib

from django.test import SimpleTestCase

from adapters.engines.expert_check import ExpertCheckEngine
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput, ImageFact
from adapters.sources.declaration_elements import ElementQuery, Sufficiency
from adapters.sources.hs_lookup import QueryBasis
from adapters.sources.tax_lookup import TaxRateQuery
from apps.api.schemas import EvidenceEntry, EvidenceStatus, ExpertVerdict, ResultMode
from evaluation.expert.cases import (
    HS_EIGHT,
    HS_MAIN,
    ExpertEvalCase,
    ImageFixture,
    ModelFixture,
    SourceModes,
)
from evaluation.expert.runner import build_source_bundle


def _make_payload(
    *,
    submitted_hs: str = HS_MAIN,
    elements_text: str = "1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:",
    supplier_tax_rate_raw: str = "13%",
    images: list[ImageFact] | None = None,
    evidence: list[EvidenceEntry] | None = None,
) -> ExpertCheckInput:
    return ExpertCheckInput(
        submitted_hs=submitted_hs,
        supplier_tax_rate_raw=supplier_tax_rate_raw,
        name="深沟球轴承",
        elements_text=elements_text,
        attributes={
            "direction": "import",
            "declaration_date": "2026-05-20",
            "tariff_version": "2026",
        },
        images=images or [],
        evidence=evidence or [],
        submission_id="sub-p18-reg",
        context=ExecutionContext(
            item_id="item-p18-reg",
            run_id="run-p18-reg",
            task_id="task-p18-reg",
            version_id="v1",
            input_revision=1,
            input_hash=hashlib.sha256(b"p18-reg").hexdigest(),
            result_mode=ResultMode.LIVE,
        ),
    )


def _make_case(
    *,
    submitted_hs: str = HS_MAIN,
    sources: SourceModes | None = None,
    model: ModelFixture | None = None,
    images: list[ImageFixture] | None = None,
) -> ExpertEvalCase:
    return ExpertEvalCase(
        id="p18-regression",
        description="regression case",
        submitted_hs=submitted_hs,
        elements_text="1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:",
        supplier_tax_rate_raw="13%",
        direction="import",
        images=images or [],
        sources=sources or SourceModes(),
        model=model or ModelFixture(mode="none"),
        expected_verdicts={
            "elements": "matched",
            "images": "unknown",
            "tax": "matched",
            "regulatory": "matched",
        },
    )


class TestP18Regression(SimpleTestCase):
    def test_p1_elements_stale_cache_declared_mismatch_rejected(self):
        """P14 旧缓存响应含不同 query.declared 时必须被拒绝，正常当前响应保持成功。"""
        bundle = build_source_bundle(_make_case())
        real_elements = bundle.engine.elements_source
        basis = QueryBasis(declaration_date="2026-05-20", tariff_version="2026")
        full_declared = {
            "品牌类型": "无品牌",
            "用途": "工业设备连接",
            "型号": "X-100",
            "品牌（中文或外文名称）": "无",
        }

        # 用真实 P14 内存来源构造合法旧响应：同编码、同 basis、同空原文，但 query.declared 齐全
        stale_response = real_elements.check(
            ElementQuery(
                hs_code_raw=HS_MAIN,
                basis=basis,
                declared=full_declared,
                declared_raw="",
            )
        )
        self.assertEqual(stale_response.sufficiency, Sufficiency.SUFFICIENT)

        payload_empty_elements = _make_payload(elements_text="")

        # 1) 正常真实来源（本次 declared 为空、原文为空）-> elements 应为 conflict，绝非 matched
        normal_empty_out = bundle.engine.expert_check(payload_empty_elements)
        normal_empty_verdicts = {f.aspect: f.verdict for f in normal_empty_out.findings}
        self.assertEqual(normal_empty_verdicts["elements"], ExpertVerdict.CONFLICT)

        # 2) 正常真实来源（显式传入当前 declared_elements，来源原样回显）-> elements 应为 matched
        normal_declared_out = bundle.engine.expert_check(
            payload_empty_elements,
            declared_elements=full_declared,
        )
        normal_declared_verdicts = {f.aspect: f.verdict for f in normal_declared_out.findings}
        self.assertEqual(normal_declared_verdicts["elements"], ExpertVerdict.MATCHED)
        self.assertEqual(normal_declared_out.evidence_status, EvidenceStatus.OBTAINED)

        # 3) 注入持有旧缓存响应的来源桩 -> assemble 抛 ValueError，expert_check 结构化降级 query_failed/unknown
        class _StaleElementsCache:
            def __init__(self):
                self.calls = 0

            def check(self, query):
                self.calls += 1
                return stale_response.model_copy(deep=True)

        stale_source = _StaleElementsCache()
        stale_engine = ExpertCheckEngine(
            elements_source=stale_source,
            tax_source=bundle.engine.tax_source,
            regulatory_source=bundle.engine.regulatory_source,
            hs_source=bundle.engine.hs_source,
            model=bundle.engine.model,
        )

        with self.assertRaises(ValueError) as cm:
            stale_engine.assemble(payload_empty_elements)
        self.assertIn("P14 要素响应查询参数与本次请求不匹配", str(cm.exception))
        self.assertEqual(stale_source.calls, 1)

        out = stale_engine.expert_check(payload_empty_elements)
        self.assertEqual(stale_source.calls, 2)
        self.assertEqual(out.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertEqual(out.target_hs, HS_MAIN)
        verdicts = {f.aspect: f.verdict for f in out.findings}
        self.assertEqual(
            verdicts,
            {
                "elements": ExpertVerdict.UNKNOWN,
                "images": ExpertVerdict.UNKNOWN,
                "tax": ExpertVerdict.UNKNOWN,
                "regulatory": ExpertVerdict.UNKNOWN,
            },
        )
        self.assertNotEqual(verdicts["elements"], ExpertVerdict.MATCHED)
        self.assertIn("EXPERT_CHECK_INTERNAL_ERROR", {i.code for i in out.issues})

    def test_p1_tax_stale_cache_rebate_code_mismatch_rejected(self):
        """P17 旧缓存响应含不同 query.rebate_code 时必须被拒绝，正常当前响应保持成功。"""
        bundle = build_source_bundle(_make_case())
        real_tax = bundle.engine.tax_source
        basis = QueryBasis(declaration_date="2026-05-20", tariff_version="2026")

        # 用真实 P17 内存来源构造合法旧响应：同编码、同 basis、同原税率 13%，但 rebate_code=""
        stale_tax_response = real_tax.lookup(
            TaxRateQuery(
                hs_code_raw=HS_MAIN,
                rebate_code="",
                basis=basis,
                original_rate_raw="13%",
            )
        )
        self.assertTrue(stale_tax_response.usable_as_basis)
        self.assertFalse(stale_tax_response.expert_review_required)

        payload = _make_payload(supplier_tax_rate_raw="13%")

        # 1) 正常真实来源请求 rebate_code="SYN-OTHER-CODE" -> tax 应为 unknown
        normal_other_out = bundle.engine.expert_check(payload, rebate_code="SYN-OTHER-CODE")
        normal_other_verdicts = {f.aspect: f.verdict for f in normal_other_out.findings}
        self.assertEqual(normal_other_verdicts["tax"], ExpertVerdict.UNKNOWN)

        # 2) 正常真实来源请求 rebate_code="" -> tax 应为 matched
        normal_match_out = bundle.engine.expert_check(payload, rebate_code="")
        normal_match_verdicts = {f.aspect: f.verdict for f in normal_match_out.findings}
        self.assertEqual(normal_match_verdicts["tax"], ExpertVerdict.MATCHED)

        # 3) 注入持有旧缓存响应的来源桩 -> assemble 抛 ValueError，expert_check 结构化降级 query_failed/unknown
        class _StaleTaxCache:
            def __init__(self):
                self.calls = 0

            def lookup(self, query):
                self.calls += 1
                return stale_tax_response.model_copy(deep=True)

        stale_source = _StaleTaxCache()
        stale_engine = ExpertCheckEngine(
            elements_source=bundle.engine.elements_source,
            tax_source=stale_source,
            regulatory_source=bundle.engine.regulatory_source,
            hs_source=bundle.engine.hs_source,
            model=bundle.engine.model,
        )

        with self.assertRaises(ValueError) as cm:
            stale_engine.assemble(payload, rebate_code="SYN-OTHER-CODE")
        self.assertIn("P17 退税响应查询参数与本次请求不匹配", str(cm.exception))
        self.assertEqual(stale_source.calls, 1)

        out = stale_engine.expert_check(payload, rebate_code="SYN-OTHER-CODE")
        self.assertEqual(stale_source.calls, 2)
        self.assertEqual(out.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertEqual(out.target_hs, HS_MAIN)
        verdicts = {f.aspect: f.verdict for f in out.findings}
        self.assertEqual(
            verdicts,
            {
                "elements": ExpertVerdict.UNKNOWN,
                "images": ExpertVerdict.UNKNOWN,
                "tax": ExpertVerdict.UNKNOWN,
                "regulatory": ExpertVerdict.UNKNOWN,
            },
        )
        self.assertNotEqual(verdicts["tax"], ExpertVerdict.MATCHED)
        self.assertIn("EXPERT_CHECK_INTERNAL_ERROR", {i.code for i in out.issues})

    def test_p2_eight_digit_with_whitespace_available_image_and_matched_model_stays_all_unknown(self):
        """8 位提交码（含首尾空白、换行、点分隔）配可用附图与 matched 模型时，四分项均须 unknown 且保留 target_hs 原文。"""
        bundle = build_source_bundle(
            _make_case(
                submitted_hs=HS_EIGHT,
                sources=SourceModes(
                    elements="official_eight_digit",
                    tax="official_eight_digit",
                    regulatory="official_eight_digit",
                ),
                model=ModelFixture(mode="matched"),
            )
        )
        valid_img = ImageFact(
            image_id="img-h9",
            status="available",
            source_cell="H9",
            visible_description="热轧卷板金属卷材实物",
        )
        valid_ev = EvidenceEntry(
            evidence_id="img-fact:H9:img-h9",
            source_type="image_fact",
            title="申报表附图 H9",
            status=EvidenceStatus.OBTAINED,
            document="申报要素表.xlsx",
            locator="cell H9",
            query={"image_id": "img-h9", "source_cell": "H9"},
        )

        for raw_hs in (HS_EIGHT, f" {HS_EIGHT}", f"{HS_EIGHT} ", f"{HS_EIGHT}\n", "7208.10.00"):
            for evidence in ([], [valid_ev]):
                with self.subTest(raw_hs=raw_hs, has_evidence=bool(evidence)):
                    out = bundle.engine.expert_check(
                        _make_payload(
                            submitted_hs=raw_hs,
                            images=[valid_img],
                            evidence=evidence,
                        )
                    )
                    self.assertEqual(out.target_hs, raw_hs)
                    verdicts = {f.aspect: f.verdict for f in out.findings}
                    self.assertEqual(
                        verdicts,
                        {
                            "elements": ExpertVerdict.UNKNOWN,
                            "images": ExpertVerdict.UNKNOWN,
                            "tax": ExpertVerdict.UNKNOWN,
                            "regulatory": ExpertVerdict.UNKNOWN,
                        },
                    )
                    codes = {i.code for i in out.issues}
                    self.assertIn("IMAGE_SUBHEADING_ONLY", codes)

    def test_p2_image_matched_requires_bound_locatable_obtained_evidence(self):
        """完整 10 位码附图仅在绑定当前附图、可定位且 status=obtained 时允许 matched，缺失或错绑均须 unknown。"""
        bundle = build_source_bundle(_make_case(model=ModelFixture(mode="matched")))
        img_h9 = ImageFact(
            image_id="img-h9",
            status="available",
            source_cell="H9",
            visible_description="金属深沟球轴承实物",
        )
        img_h10 = ImageFact(
            image_id="img-h10",
            status="available",
            source_cell="H10",
            visible_description="金属深沟球轴承侧面实物",
        )
        bound_ev_h9 = EvidenceEntry(
            evidence_id="img-fact:H9:img-h9",
            source_type="image_fact",
            title="申报表附图 H9",
            status=EvidenceStatus.OBTAINED,
            document="申报要素表.xlsx",
            locator="cell H9",
            query={"image_id": "img-h9", "source_cell": "H9"},
        )

        # 有效绑定证据 -> images=matched 且带证据 ID
        ok_out = bundle.engine.expert_check(
            _make_payload(images=[img_h9], evidence=[bound_ev_h9])
        )
        ok_img = next(f for f in ok_out.findings if f.aspect == "images")
        self.assertEqual(ok_img.verdict, ExpertVerdict.MATCHED)
        self.assertEqual(ok_img.evidence_ids, ["img-fact:H9:img-h9"])

        # 负例：缺证据、错误 image_id、失败状态证据、空 locator、多图仅部分有证据
        negative_cases = [
            ("missing_evidence", [img_h9], []),
            (
                "wrong_image_id",
                [img_h9],
                [
                    EvidenceEntry(
                        evidence_id="img-fact:H9:img-other",
                        source_type="image_fact",
                        title="另一附图",
                        status=EvidenceStatus.OBTAINED,
                        document="申报要素表.xlsx",
                        locator="cell H9",
                        query={"image_id": "img-other", "source_cell": "H9"},
                    )
                ],
            ),
            (
                "failed_status_evidence",
                [img_h9],
                [
                    EvidenceEntry(
                        evidence_id="img-fact:H9:img-h9",
                        source_type="image_fact",
                        title="申报表附图 H9",
                        status=EvidenceStatus.QUERY_FAILED,
                        document="申报要素表.xlsx",
                        locator="cell H9",
                        query={"image_id": "img-h9", "source_cell": "H9"},
                    )
                ],
            ),
            (
                "blank_locator_evidence",
                [img_h9],
                [
                    EvidenceEntry(
                        evidence_id="img-fact:H9:img-h9",
                        source_type="image_fact",
                        title="申报表附图 H9",
                        status=EvidenceStatus.OBTAINED,
                        document="申报要素表.xlsx",
                        locator="   ",
                        query={"image_id": "img-h9", "source_cell": "H9"},
                    )
                ],
            ),
            ("partial_coverage_two_images", [img_h9, img_h10], [bound_ev_h9]),
        ]
        for label, images, evidence in negative_cases:
            with self.subTest(case=label):
                out = bundle.engine.expert_check(
                    _make_payload(images=images, evidence=evidence)
                )
                img_finding = next(f for f in out.findings if f.aspect == "images")
                self.assertEqual(img_finding.verdict, ExpertVerdict.UNKNOWN)
                self.assertIn("IMAGE_EVIDENCE_MISSING", {i.code for i in out.issues})
