"""P01 历史记录不可变：确认、决定、执行既不能改写，也不能单独删除。

对应复审残余三处（2026-11-14）：
1. 确认记录（Confirmation）内容可被事后改写，破坏追加式历史；
2. 专家决定（Decision / DecisionItem）可被改写或删除，留痕可被覆盖；
3. 执行（Run）与结果行（RunItem）可被直接删除，核查结果会静默消失。

规则：历史行内容固定且禁止单独删除；随父记录（任务/提交）级联清理仍允许。
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import (
    Confirmation,
    Decision,
    DecisionItem,
    FileVersion,
    Item,
    Submission,
    SubmissionItem,
    Task,
)
from apps.runs.models import Run, RunItem

from .factories import make_task, make_user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HistoryImmutabilityTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.task = make_task(self.supplier, title="历史任务")
        self.version = FileVersion.objects.create(
            task=self.task, number=1, original_filename="v1.xlsx", uploaded_by=self.supplier,
        )
        self.item = Item.objects.create(
            version=self.version, source_sheet="申报要素", source_row=8, supplier_hs_raw="111111",
        )
        self.confirmation = Confirmation.objects.create(
            item=self.item, revision=2, confirmed_hs="111111", created_by=self.operator,
        )
        self.submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        self.line = SubmissionItem.objects.create(
            submission=self.submission, item=self.item, confirmation=self.confirmation,
            submitted_hs="111111", item_revision=2,
        )
        self.decision = Decision.objects.create(
            submission=self.submission, action=Decision.Action.APPROVE, created_by=self.expert,
        )
        self.decision_item = DecisionItem.objects.create(
            decision=self.decision, item=self.item, previous_hs="111111", final_hs="111111",
        )
        self.run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK)
        self.run_item = RunItem.objects.create(run=self.run, item=self.item)

    def assert_delete_blocked(self, action):
        # 删除保护在 collector 阶段抛错，需要 savepoint 回滚，否则测试事务不可继续。
        with self.assertRaises(ProtectedError), transaction.atomic():
            action()

    # ---- ① 确认记录 ----

    def test_confirmation_cannot_be_updated(self):
        with self.assertRaises(ValidationError):
            Confirmation.objects.filter(pk=self.confirmation.pk).update(confirmed_hs="999999")
        self.confirmation.confirmed_hs = "999999"
        with self.assertRaises(ValidationError):
            self.confirmation.save()
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.confirmed_hs, "111111")

    def test_unreferenced_confirmation_cannot_be_deleted(self):
        extra = Confirmation.objects.create(
            item=self.item, revision=3, confirmed_hs="222222", created_by=self.operator,
        )
        self.assert_delete_blocked(extra.delete)

    # ---- ② 专家决定与逐行终值 ----

    def test_decision_cannot_be_updated(self):
        with self.assertRaises(ValidationError):
            Decision.objects.filter(pk=self.decision.pk).update(
                action=Decision.Action.RETURN, reason="事后改",
            )
        self.decision.action = Decision.Action.RETURN
        with self.assertRaises(ValidationError):
            self.decision.save()
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.action, Decision.Action.APPROVE)

    def test_decision_cannot_be_deleted(self):
        self.assert_delete_blocked(self.decision.delete)
        self.assertTrue(Decision.objects.filter(pk=self.decision.pk).exists())

    def test_decision_item_cannot_be_updated(self):
        with self.assertRaises(ValidationError):
            DecisionItem.objects.filter(pk=self.decision_item.pk).update(final_hs="222222")
        self.decision_item.final_hs = "222222"
        with self.assertRaises(ValidationError):
            self.decision_item.save()

    def test_decision_item_cannot_be_deleted(self):
        self.assert_delete_blocked(self.decision_item.delete)
        self.assertTrue(DecisionItem.objects.filter(pk=self.decision_item.pk).exists())

    # ---- ③ 执行与结果行 ----

    def test_run_cannot_be_deleted(self):
        self.assert_delete_blocked(self.run.delete)
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_run_item_cannot_be_deleted(self):
        self.assert_delete_blocked(self.run_item.delete)
        self.assert_delete_blocked(lambda: RunItem.objects.filter(pk=self.run_item.pk).delete())
        self.assertTrue(RunItem.objects.filter(pk=self.run_item.pk).exists())

    def test_progress_updates_still_allowed(self):
        self.run.status = Run.Status.RUNNING
        self.run.save(update_fields=["status"])
        self.run_item.status = RunItem.Status.SUCCEEDED
        self.run_item.save(update_fields=["status"])
        Run.objects.filter(pk=self.run.pk).update(status=Run.Status.SUCCEEDED)
        self.assertEqual(Run.objects.get(pk=self.run.pk).status, Run.Status.SUCCEEDED)

    # ---- 级联清理仍可用 ----

    def test_cascade_from_parent_still_allows_cleanup(self):
        other = make_task(self.supplier, title="待清理任务")
        version = FileVersion.objects.create(
            task=other, number=1, original_filename="x.xlsx", uploaded_by=self.supplier,
        )
        item = Item.objects.create(version=version, source_sheet="申报要素", source_row=8)
        Confirmation.objects.create(item=item, revision=2, confirmed_hs="333333", created_by=self.operator)
        other_pk = other.pk
        other.delete()
        self.assertFalse(Task.objects.filter(pk=other_pk).exists())
        self.assertFalse(Confirmation.objects.filter(item__version__task_id=other_pk).exists())
