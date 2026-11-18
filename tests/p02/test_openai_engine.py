"""Real engine contract: tentative advice, no fabricated evidence or mock fallback."""
import io
import json
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from adapters.engines.mock import EngineTechnicalError
from adapters.engines.openai import OpenAICompatibleEngine
from adapters.engines.protocols import CompareInput, ExecutionContext, ExpertCheckInput, RecommendInput, get_engine
from apps.api.schemas import ComparisonStatus, EvidenceStatus, ExpertVerdict
from apps.reviews.serializers import result_mode


def response(content):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode())


@override_settings(ENGINE_MODE="openai", CUSTOMS_AI_API_BASE="https://ai.example.invalid/v1",
                   CUSTOMS_AI_API_KEY="test-only", CUSTOMS_AI_MODEL="test-model",
                   CUSTOMS_AI_TIMEOUT_SECONDS=3)
class OpenAIEngineTests(SimpleTestCase):
    def test_recommendation_is_tentative_and_original_hs_is_not_sent(self):
        captured = []

        def send(req, timeout):
            captured.append(json.loads(req.data))
            return response({"suggested_hs": "8544422900", "candidates": [{"hs": "8544422900", "reason": "线缆"}],
                             "gaps": []})

        payload = RecommendInput(context=ExecutionContext(task_id="t", version_id="v", item_id="i",
                                 run_id="r", input_revision=1), name="电缆", attributes={"material": "铜"})
        with patch("adapters.engines.openai.request.urlopen", side_effect=send):
            engine = get_engine()
            result = engine.recommend(payload)
        self.assertIsInstance(engine, OpenAICompatibleEngine)
        self.assertNotIn("supplier_hs_raw", json.dumps(captured[0], ensure_ascii=False))
        self.assertEqual(result.suggested_hs, "8544422900")
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIsNone(result.confidence)
        compared = engine.compare(CompareInput(context=payload.context, recommendation=result,
                                               supplier_hs_raw="8544422900"))
        self.assertEqual(compared.comparison_status, ComparisonStatus.NEEDS_CONFIRMATION)

    def test_expert_check_never_marks_unverified_as_matched(self):
        with patch("adapters.engines.openai.request.urlopen", return_value=response({
                "elements_conflict": False, "reason": "模型认为一致", "suggested_hs": None,
                "missing_facts": []})):
            output = get_engine().expert_check(ExpertCheckInput(
                context=ExecutionContext(task_id="t", version_id="v", item_id="i", run_id="r", input_revision=1),
                submission_id="s", submitted_hs="8544422900", name="电缆"))
        self.assertEqual(output.target_hs, "8544422900")
        self.assertEqual({finding.verdict for finding in output.findings}, {ExpertVerdict.UNKNOWN})
        self.assertEqual(output.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)

    def test_invalid_hs_is_retryable_technical_failure(self):
        payload = RecommendInput(context=ExecutionContext(task_id="t", version_id="v", item_id="i",
                                 run_id="r", input_revision=1), name="电缆")
        with patch("adapters.engines.openai.request.urlopen", return_value=response({
                "suggested_hs": "ABC", "candidates": [], "gaps": []})):
            with self.assertRaises(EngineTechnicalError):
                get_engine().recommend(payload)

    def test_historical_unmarked_row_remains_mock(self):
        self.assertEqual(result_mode({"candidates": []}).value, "mock")
        self.assertEqual(result_mode({"result_mode": "live"}).value, "live")
