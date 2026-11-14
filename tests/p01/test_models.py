"""P01 模型验收：唯一约束、原值与阶段结果分离、状态维度分开。"""
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.accounts.models import Role, TaskAssignment
from apps.notifications.models import Message
from apps.reviews.models import (
    AuditEvent,
    Confirmation,
    Decision,
    DecisionItem,
    FileVersion,
    Item,
    ItemImage,
    ItemMember,
    Issue,
    Submission,
    SubmissionItem,
    Task,
)
from apps.runs.models import Run, RunItem

from .factories import assign, make_item, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ModelConstraintTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.task = make_task(self.supplier)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version)

    def test_ids_are_opaque_uuids(self):
        for obj in (self.task, self.version, self.item):
            self.assertIsInstance(obj.pk, uuid.UUID)

    def test_file_version_number_unique_per_task(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_version(self.task, self.supplier, number=1, filename="重复.xlsx")

    def test_same_version_number_allowed_on_another_task(self):
        other_task = make_task(self.supplier, title="另一个任务")
        other_version = make_version(other_task, self.supplier, number=1)
        self.assertEqual(other_version.number, 1)

    def test_item_source_row_unique_per_version(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_item(self.version, row=self.item.source_row)

    def test_item_member_unique(self):
        ItemMember.objects.create(
            item=self.item, source_sheet="装箱明细", source_row=8, contract_no="C1", item_no="1",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemMember.objects.create(
                item=self.item, source_sheet="装箱明细", source_row=8, contract_no="C1", item_no="1",
            )

    def test_item_image_cell_unique(self):
        ItemImage.objects.create(
            item=self.item, source_sheet="申报要素", source_cell="Y8", status=ItemImage.Status.ABSENT,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemImage.objects.create(
                item=self.item, source_sheet="申报要素", source_cell="Y8", status=ItemImage.Status.ABSENT,
            )

    def test_confirmation_revision_unique(self):
        Confirmation.objects.create(
            item=self.item, revision=2, confirmed_hs="8544", created_by=self.operator,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Confirmation.objects.create(
                item=self.item, revision=2, confirmed_hs="8545", created_by=self.operator,
            )

    def test_run_item_unique(self):
        run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK)
        RunItem.objects.create(run=run, item=self.item)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RunItem.objects.create(run=run, item=self.item)

    def test_submission_unique_by_task_version_revision(self):
        Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Submission.objects.create(
                task=self.task, version=self.version, revision=1, created_by=self.operator,
            )

    def test_submission_item_unique(self):
        confirmation = Confirmation.objects.create(
            item=self.item, revision=2, confirmed_hs="854449", created_by=self.operator,
        )
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        SubmissionItem.objects.create(
            submission=submission, item=self.item, confirmation=confirmation,
            submitted_hs="854449", item_revision=2,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionItem.objects.create(
                submission=submission, item=self.item, confirmation=confirmation,
                submitted_hs="854449", item_revision=2,
            )

    def test_decision_item_unique(self):
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        decision = Decision.objects.create(
            submission=submission, action=Decision.Action.APPROVE, created_by=self.expert,
        )
        DecisionItem.objects.create(decision=decision, item=self.item, previous_hs="8544", final_hs="8544")
        with self.assertRaises(IntegrityError), transaction.atomic():
            DecisionItem.objects.create(decision=decision, item=self.item)

    def test_task_assignment_unique_per_user_and_task(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAssignment.objects.create(task=self.task, user=self.operator, role=Role.OPERATOR)

    def test_task_assignment_rejects_supplier_role(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            assign(self.task, self.supplier, Role.SUPPLIER)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ValueSeparationTests(TestCase):
    """原值、预审建议、业务员确认、提交快照、专家终值各自留存，不互相覆盖。"""

    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.task = make_task(self.supplier)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version, supplier_hs="8544421900")
        self.run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK)
        self.run_item = RunItem.objects.create(
            run=self.run, item=self.item, status=RunItem.Status.SUCCEEDED,
            suggested_hs="8544422900", comparison_status=RunItem.ComparisonStatus.INCONSISTENT,
            confidence=0.82, result={"candidates": [{"hs": "8544422900", "reason": "合成理由"}]},
        )

    def test_confirmation_does_not_touch_supplier_raw_or_run_result(self):
        Confirmation.objects.create(
            item=self.item, revision=2, previous_hs="", confirmed_hs="8544422900",
            reason="合成确认", created_by=self.operator,
        )
        self.item.refresh_from_db()
        self.run_item.refresh_from_db()
        self.assertEqual(self.item.supplier_hs_raw, "8544421900")
        self.assertEqual(self.run_item.suggested_hs, "8544422900")
        self.assertEqual(self.run_item.comparison_status, RunItem.ComparisonStatus.INCONSISTENT)

    def test_confirmation_is_append_only(self):
        first = Confirmation.objects.create(
            item=self.item, revision=2, confirmed_hs="8544422900", created_by=self.operator,
        )
        second = Confirmation.objects.create(
            item=self.item, revision=3, previous_hs="8544422900", confirmed_hs="8544422910",
            reason="改码", created_by=self.operator,
        )
        first.refresh_from_db()
        self.assertEqual(first.confirmed_hs, "8544422900")
        self.assertEqual(second.previous_hs, "8544422900")
        self.assertEqual(self.item.confirmations.count(), 2)

    def test_submission_snapshot_keeps_submitted_value_after_later_edits(self):
        confirmation = Confirmation.objects.create(
            item=self.item, revision=2, confirmed_hs="8544422900", created_by=self.operator,
        )
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        snapshot = SubmissionItem.objects.create(
            submission=submission, item=self.item, confirmation=confirmation,
            submitted_hs="8544422900", item_revision=2,
        )
        Confirmation.objects.create(
            item=self.item, revision=3, previous_hs="8544422900", confirmed_hs="8544429999",
            reason="提交后再次修改", created_by=self.operator,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.submitted_hs, "8544422900")
        self.assertEqual(snapshot.item_revision, 2)

    def test_decision_records_change_before_and_after(self):
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        decision = Decision.objects.create(
            submission=submission, action=Decision.Action.APPROVE,
            reason="专家改码通过", created_by=self.expert,
        )
        DecisionItem.objects.create(
            decision=decision, item=self.item, previous_hs="8544422900",
            final_hs="8544422910", change_reason="合成改码原因",
        )
        row = DecisionItem.objects.get(decision=decision, item=self.item)
        self.assertEqual(row.previous_hs, "8544422900")
        self.assertEqual(row.final_hs, "8544422910")
        self.assertEqual(row.change_reason, "合成改码原因")

    def test_status_dimensions_are_separate(self):
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=1, created_by=self.operator,
        )
        Decision.objects.create(
            submission=submission, action=Decision.Action.RETURN,
            return_to=Decision.ReturnTo.SUPPLIER, created_by=self.expert,
        )
        # 技术执行成功、业务结论待确认、图片读取失败、问题未解决可以同时存在。
        ItemImage.objects.create(
            item=self.item, source_sheet="申报要素", source_cell="Y8",
            status=ItemImage.Status.READ_FAILED, error_message="DISPIMG 资源缺失",
        )
        self.task.stage = Task.Stage.RETURNED
        self.task.save(update_fields=["stage"])
        Issue.objects.create(task=self.task, item=self.item, kind="image", message="附图读取失败")
        self.task.refresh_from_db()
        self.run_item.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.RETURNED)
        self.assertEqual(self.run.status, Run.Status.QUEUED)
        self.assertEqual(self.run_item.status, RunItem.Status.SUCCEEDED)
        self.assertEqual(
            ItemImage.objects.get(item=self.item).status, ItemImage.Status.READ_FAILED,
        )
        self.assertEqual(Issue.objects.get(item=self.item).status, Issue.Status.OPEN)

    def test_image_status_distinguishes_absent_and_read_failed(self):
        absent = ItemImage.objects.create(
            item=self.item, source_sheet="申报要素", source_cell="Y9", status=ItemImage.Status.ABSENT,
        )
        failed = ItemImage.objects.create(
            item=self.item, source_sheet="申报要素", source_cell="Y10",
            status=ItemImage.Status.READ_FAILED, error_message="无法解析",
        )
        self.assertNotEqual(absent.status, failed.status)
        self.assertEqual(failed.error_message, "无法解析")

    def test_audit_event_records_previous_and_new_values(self):
        event = AuditEvent.objects.create(
            task=self.task, version=self.version, item=self.item, actor=self.operator,
            action="confirmation.updated", stage="business",
            previous_value="8544421900", new_value="8544422900", reason="合成原因",
        )
        self.assertEqual(event.previous_value, "8544421900")
        self.assertEqual(event.new_value, "8544422900")
        self.assertEqual(event.reason, "合成原因")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MessageTests(TestCase):
    def test_message_only_belongs_to_recipient(self):
        supplier = make_user("supplier_a", Role.SUPPLIER)
        operator = make_user("operator_a", Role.OPERATOR)
        message = Message.objects.create(recipient=operator, title="待确认预审结果")
        self.assertEqual(message.recipient_id, operator.pk)
        self.assertFalse(Message.objects.filter(recipient=supplier).exists())
