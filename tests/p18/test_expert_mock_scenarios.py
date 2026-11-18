"""Tests for P18 MockEngine expert_check scenarios and serializer integration."""
from __future__ import annotations

from django.test import SimpleTestCase

from adapters.engines.mock import SCENARIOS, EngineTechnicalError, MockEngine
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput
from apps.api.schemas import EvidenceStatus, ExpertVerdict, ResultMode
from apps.reviews.serializers import item_to_card
from apps.runs.models import Run, RunItem
from tests.p10.factories import SubmissionFixture


def _mock_payload(scenario: str) -> ExpertCheckInput:
    return ExpertCheckInput(
        context=ExecutionContext(
            task_id="t-mock",
            version_id="v1",
            item_id="i-mock",
            run_id="r-mock",
            input_revision=1,
            input_hash="a" * 64,
            result_mode=ResultMode.MOCK,
            mock_scenario=scenario,
        ),
        submission_id="sub-mock",
        submitted_hs="8544422900",
        name="屏蔽控制电缆",
        elements_text="1:品牌类型:无品牌;2:用途:信号传输;3:额定电压:300V",
    )


class TestExpertMockScenarios(SimpleTestCase):
    def setUp(self):
        self.engine = MockEngine()

    def test_required_scenarios_registered(self):
        for name in (
            "expert_conflict",
            "expert_elements_unknown",
            "expert_images_unknown",
            "expert_tax_unknown",
            "expert_regulatory_unknown",
        ):
            self.assertIn(name, SCENARIOS)

    def test_expert_conflict_preserves_baseline_contract(self):
        out = self.engine.expert_check(_mock_payload("expert_conflict"))
        self.assertEqual(out.target_hs, "8544422900")
        self.assertEqual(out.suggested_hs, "8536901900")
        self.assertEqual([f.aspect for f in out.findings], ["elements", "images", "tax", "regulatory"])
        verdicts = {f.aspect: f.verdict for f in out.findings}
        self.assertEqual(
            verdicts,
            {
                "elements": ExpertVerdict.CONFLICT,
                "images": ExpertVerdict.UNKNOWN,
                "tax": ExpertVerdict.UNKNOWN,
                "regulatory": ExpertVerdict.UNKNOWN,
            },
        )
        self.assertEqual(out.evidence_status, EvidenceStatus.OBTAINED)
        self.assertEqual(out.findings[0].evidence_ids, ["mock-obtained"])
        self.assertTrue(all(e.document and e.locator for e in out.evidence))

    def test_each_aspect_unknown_scenario_marks_only_designated_aspect_unknown(self):
        scenarios = {
            "expert_elements_unknown": "elements",
            "expert_images_unknown": "images",
            "expert_tax_unknown": "tax",
            "expert_regulatory_unknown": "regulatory",
        }
        for scenario, target_aspect in scenarios.items():
            out = self.engine.expert_check(_mock_payload(scenario))
            self.assertEqual(out.target_hs, "8544422900")
            self.assertIsNone(out.suggested_hs)
            ev_ids = {e.evidence_id for e in out.evidence}
            for finding in out.findings:
                self.assertTrue(finding.reason.strip())
                if finding.aspect == target_aspect:
                    self.assertEqual(finding.verdict, ExpertVerdict.UNKNOWN)
                    self.assertEqual(finding.evidence_ids, [])
                else:
                    self.assertEqual(finding.verdict, ExpertVerdict.MATCHED)
                    self.assertTrue(set(finding.evidence_ids).issubset(ev_ids))
                    self.assertTrue(bool(finding.evidence_ids))
            self.assertTrue(any(target_aspect in (i.field or i.code).lower() or i.code for i in out.issues))

    def test_tech_failure_still_raises_for_inline_runner_fallback(self):
        with self.assertRaises(EngineTechnicalError):
            self.engine.expert_check(_mock_payload("tech_failure"))


class TestExpertMockCardSerialization(SubmissionFixture):
    def test_item_to_card_serializes_expert_scenarios_without_leaking_into_precheck_evidence(self):
        item = self.item_a
        sub = self.submission
        task = self.task
        engine = MockEngine()
        out = engine.expert_check(
            ExpertCheckInput(
                context=ExecutionContext(
                    task_id=str(task.id),
                    version_id=str(self.version.id),
                    item_id=str(item.id),
                    run_id="r1",
                    input_revision=1,
                    input_hash="a" * 64,
                    result_mode=ResultMode.MOCK,
                    mock_scenario="expert_tax_unknown",
                ),
                submission_id=str(sub.id),
                submitted_hs="8544421900",
                name=item.name,
            )
        )
        run = Run.objects.create(
            task=task,
            version=self.version,
            kind=Run.Kind.EXPERT_CHECK,
            status=Run.Status.SUCCEEDED,
            is_current=True,
            submission=sub,
        )
        RunItem.objects.create(run=run, item=item, result=out.model_dump(mode="json"))

        card = item_to_card(self.expert, item)
        ref = card.latest_expert_check
        self.assertIsNotNone(ref)
        self.assertEqual(str(ref.run_id), str(run.id))
        self.assertEqual(str(ref.submission_id), str(sub.id))
        self.assertEqual(ref.target_hs, "8544421900")
        verdicts = {f.aspect: f.verdict for f in ref.findings}
        self.assertEqual(
            verdicts,
            {
                "elements": ExpertVerdict.MATCHED,
                "images": ExpertVerdict.MATCHED,
                "tax": ExpertVerdict.UNKNOWN,
                "regulatory": ExpertVerdict.MATCHED,
            },
        )
        # card.evidence comes strictly from precheck run, not expert_check run
        self.assertEqual(card.evidence, [])
