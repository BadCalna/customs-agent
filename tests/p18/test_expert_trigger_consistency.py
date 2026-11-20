"""Integration tests for P18 trigger consistency, replay idempotency, withdraw/recode, and inline ExpertCheckEngine."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from adapters.engines.expert_check import ENGINE_VERSION, PROMPT_VERSION, RULES_VERSION
from adapters.engines.mock import MockEngine
from apps.accounts.models import Role
from apps.api.schemas import ExpertVerdict
from apps.reviews.models import Confirmation, Item, Submission, Task
from apps.reviews.schemas import DecisionIn, DecisionLineIn, SubmissionIn, SubmissionLineIn
from apps.reviews.serializers import item_to_card
from apps.reviews.services.decisions import decide
from apps.reviews.services.submissions import create_submission, withdraw_submission
from apps.runs.inline import execute_expert_check
from apps.runs.models import Run, RunItem
from evaluation.expert.cases import HS_ALT, HS_MAIN, ExpertEvalCase, ImageFixture, ModelFixture, SourceModes
from evaluation.expert.runner import build_source_bundle
from tests.p01.factories import assign, make_item, make_user, make_version


class _InlineExpertAdapter:
    """Wrap standalone P18 ExpertCheckEngine to satisfy MockEngine/Engine call conventions."""

    def __init__(self, real_engine):
        self._real = real_engine
        self.engine_version = ENGINE_VERSION
        self.prompt_version = PROMPT_VERSION
        self.rules_version = RULES_VERSION

    def precheck(self, payload):
        return MockEngine().precheck(payload)

    def compare_supplier_hs(self, payload):
        return MockEngine().compare_supplier_hs(payload)

    def expert_check(self, payload):
        return self._real.expert_check(payload, direction="import")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TestExpertTriggerConsistency(TestCase):
    def setUp(self):
        self.supplier = make_user("p18_sup", Role.SUPPLIER)
        self.operator = make_user("p18_op", Role.OPERATOR)
        self.expert = make_user("p18_exp", Role.EXPERT)
        self.task = Task.objects.create(
            supplier=self.supplier,
            title="P18 提交触发一致性任务",
            stage=Task.Stage.AWAITING_CONFIRMATION,
        )
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item = Item.objects.create(
            version=self.version,
            source_sheet="申报要素",
            source_row=5,
            source_cells="A5:Y5",
            name="深沟球轴承",
            supplier_hs_raw=HS_MAIN,
            supplier_elements_raw="1:品牌类型:无品牌;2:用途:工业设备连接;3:型号:X-100;4:品牌（中文或外文名称）:无;5:GTIN:",
            supplier_tax_rate_raw="13%",
            attributes={"declaration_date": "2026-05-20", "tariff_version": "2026", "direction": "import"},
        )
        self.confirm = Confirmation.objects.create(
            item=self.item,
            revision=1,
            previous_hs="",
            confirmed_hs=HS_MAIN,
            reason="确认提交",
            created_by=self.operator,
        )

    def _submit(self, idempotency_key: str | None = None):
        payload = SubmissionIn(
            version_id=str(self.version.id),
            revision=self.task.revision,
            items=[SubmissionLineIn(item_id=str(self.item.id), confirmation_id=str(self.confirm.id))],
        )
        with self.captureOnCommitCallbacks(execute=True):
            return create_submission(self.operator, str(self.task.id), payload, idempotency_key=idempotency_key)

    def test_submit_creates_exactly_one_current_expert_run_and_replays_do_not_duplicate(self):
        first = self._submit(idempotency_key="sub-key-1")
        runs = Run.objects.filter(task=self.task, kind=Run.Kind.EXPERT_CHECK)
        self.assertEqual(runs.count(), 1)
        self.assertEqual(runs.filter(is_current=True).count(), 1)
        current = runs.get(is_current=True)
        self.assertEqual(current.status, Run.Status.SUCCEEDED)
        self.assertEqual(str(first["submission_id"]), str(current.submission_id))
        self.assertEqual(RunItem.objects.filter(run=current, item=self.item).count(), 1)

        # Idempotent replay by key or same revision/content must NOT add a second run
        replay_key = self._submit(idempotency_key="sub-key-1")
        self.assertTrue(replay_key["idempotent_replay"])
        replay_content = self._submit(idempotency_key="sub-key-2")
        self.assertTrue(replay_content["idempotent_replay"])
        self.assertEqual(Run.objects.filter(task=self.task, kind=Run.Kind.EXPERT_CHECK).count(), 1)

        # Repeated retry of execute_expert_check on succeeded run stays idempotent
        sub = Submission.objects.get(id=first["submission_id"])
        retried = execute_expert_check(sub, run=current)
        self.assertEqual(str(retried.id), str(current.id))
        self.assertEqual(Run.objects.filter(task=self.task, kind=Run.Kind.EXPERT_CHECK, is_current=True).count(), 1)

    def test_withdraw_and_resubmit_keeps_single_current_run(self):
        first = self._submit(idempotency_key="w-sub-1")
        withdraw_submission(self.operator, str(first["submission_id"]), idempotency_key="w-wd-1")
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.AWAITING_CONFIRMATION)

        second = self._submit(idempotency_key="w-sub-2")
        self.assertFalse(second["idempotent_replay"])
        self.assertNotEqual(first["submission_id"], second["submission_id"])

        all_runs = Run.objects.filter(task=self.task, kind=Run.Kind.EXPERT_CHECK)
        self.assertEqual(all_runs.count(), 2)
        self.assertEqual(all_runs.filter(is_current=True).count(), 1)
        self.assertEqual(str(all_runs.get(is_current=True).submission_id), str(second["submission_id"]))

    def test_expert_recode_never_mutates_latest_expert_check_target_hs(self):
        first = self._submit(idempotency_key="rec-sub-1")
        decide(
            self.expert,
            str(first["submission_id"]),
            DecisionIn(
                action="approve",
                reason="专家人工改码后核定",
                items=[DecisionLineIn(item_id=str(self.item.id), final_hs=HS_ALT, change_reason="规格更符合 8536")],
            ),
            idempotency_key="rec-dec-1",
        )
        card = item_to_card(self.expert, self.item)
        self.assertEqual(card.expert_final_hs.value, HS_ALT)
        self.assertIsNotNone(card.latest_expert_check)
        self.assertEqual(card.latest_expert_check.target_hs, HS_MAIN)
        self.assertNotEqual(card.latest_expert_check.target_hs, card.expert_final_hs.value)

    def test_inline_execution_with_expert_check_engine_populates_contract_and_frozen_versions(self):
        case = ExpertEvalCase(
            id="inline-integration",
            description="inline submit integration",
            submitted_hs=HS_MAIN,
            images=[ImageFixture(status="available", visible_description="金属深沟球轴承")],
            sources=SourceModes(elements="official_sufficient", tax="official_match_13", regulatory="official_declared_none"),
            model=ModelFixture(mode="elements_conflict", suggested_hs=HS_ALT, reason="要素特征与 8536 更吻合"),
            expected_verdicts={"elements": "conflict", "images": "matched", "tax": "matched", "regulatory": "matched"},
        )
        bundle = build_source_bundle(case)
        adapter = _InlineExpertAdapter(bundle.engine)

        # Registration and execution must use one identity: results inherit the frozen Run snapshot.
        with patch("apps.runs.engine_config.get_engine", return_value=adapter), patch(
            "apps.runs.inline.get_engine", return_value=adapter
        ):
            sub_out = self._submit(idempotency_key="inline-real-1")

        run = Run.objects.get(task=self.task, kind=Run.Kind.EXPERT_CHECK, is_current=True)
        self.assertEqual(str(run.submission_id), str(sub_out["submission_id"]))
        self.assertEqual(run.engine_snapshot["engine_version"], ENGINE_VERSION)
        self.assertEqual(run.engine_snapshot["prompt_version"], PROMPT_VERSION)
        self.assertEqual(run.engine_snapshot["rules_version"], RULES_VERSION)
        run_item = RunItem.objects.get(run=run, item=self.item)
        self.assertEqual(run_item.result.get("engine_version"), ENGINE_VERSION)
        self.assertEqual(run_item.result.get("prompt_version"), PROMPT_VERSION)
        self.assertEqual(run_item.result.get("rules_version"), RULES_VERSION)
        self.assertTrue(len(run_item.result.get("evidence", [])) > 0)

        card = item_to_card(self.expert, self.item)
        ref = card.latest_expert_check
        self.assertIsNotNone(ref)
        self.assertEqual(ref.target_hs, HS_MAIN)
        verdicts = {f.aspect: f.verdict for f in ref.findings}
        self.assertEqual(
            verdicts,
            {
                "elements": ExpertVerdict.CONFLICT,
                "images": ExpertVerdict.UNKNOWN,  # DB item has no images stored yet -> honest unknown
                "tax": ExpertVerdict.MATCHED,
                "regulatory": ExpertVerdict.MATCHED,
            },
        )
