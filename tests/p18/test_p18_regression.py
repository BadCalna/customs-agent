from django.test import SimpleTestCase
from adapters.engines.expert_check import ExpertCheckEngine
from adapters.engines.protocols import ExpertCheckInput, ExecutionContext
from evaluation.expert.cases import HS_EIGHT, HS_MAIN, SourceModes, ModelFixture
from evaluation.expert.runner import build_source_bundle
import hashlib

def _payload(submitted_hs: str = HS_MAIN) -> ExpertCheckInput:
    return ExpertCheckInput(
        submitted_hs=submitted_hs,
        name="测试商品",
        elements_text="1:品牌类型:无;",
        images=[],
        evidence=[],
        submission_id="sub-p18-reg",
        context=ExecutionContext(
            item_id="item-p18-reg",
            run_id="run-p18-reg",
            task_id="task-p18-reg",
            version_id="v1",
            input_revision=1,
            input_hash=hashlib.sha256(b"p18-reg").hexdigest(),
        ),
    )

from evaluation.expert.cases import ExpertEvalCase, SourceModes, ModelFixture

def _mock_case():
    return ExpertEvalCase(
        id="mock",
        description="mock",
        submitted_hs=HS_MAIN,
        elements_text="1:品牌类型:无;",
        supplier_tax_rate_raw="13%",
        direction="import",
        images=[],
        sources=SourceModes(),
        model=ModelFixture(mode="none"),
        expected_verdicts={"elements": "matched", "images": "matched", "tax": "matched", "regulatory": "matched"},
    )

class TestP18Regression(SimpleTestCase):
    def test_p1_cache_mismatch_elements(self):
        # Setup bundle
        bundle = build_source_bundle(_mock_case()) # Basic bundle
        engine = bundle.engine
        
        payload = _payload()
        
        with self.assertRaises(ValueError) as cm:
            engine.assemble(payload, declared_elements={"wrong": "key"})
        self.assertIn("P14 要素响应申报要素不匹配", str(cm.exception))

    def test_p2_eight_digit_image_unknown(self):
        # Need a case with images
        bundle = build_source_bundle(_mock_case())
        engine = bundle.engine
        
        from adapters.engines.protocols import ImageFact
        payload = _payload(submitted_hs=HS_EIGHT)
        payload.images = [ImageFact(image_id="i1", status="available", source_cell="H1", visible_description="金属块")]
        
        # Assemble calls _derive_images_aspect which now checks length 8
        out = engine.assemble(payload)
        img_finding = next(f for f in out.aspects if f.finding.aspect == "images")
        self.assertEqual(img_finding.finding.verdict, "unknown")
        self.assertIn("IMAGE_8_DIGIT_HS", img_finding.reason_codes)
