"""P01 跨表归属校验：快照内部一致、执行归属、专家终值，以及“时效检查不归 P01”。

分工（Jay 2026-11-14 补充意见）：
- P01 只保证一份快照/执行内部自洽：任务、版本、商品、确认记录、HS 互相匹配；
- “提交必须使用当时最新的确认”（旧确认已过期 → 409）由 P09 在提交事务里做；
- 因此旧快照在商品后续被修改后仍应原样可读，P01 的校验不得把它判成非法。
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
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
class InvariantFixture(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.task = make_task(self.supplier, title="甲任务")
        self.other_task = make_task(self.supplier, title="乙任务")
        self.v1 = FileVersion.objects.create(
            task=self.task, number=1, original_filename="v1.xlsx", uploaded_by=self.supplier,
        )
        self.v2 = FileVersion.objects.create(
            task=self.task, number=2, original_filename="v2.xlsx", uploaded_by=self.supplier,
        )
        self.other_version = FileVersion.objects.create(
            task=self.other_task, number=1, original_filename="other.xlsx", uploaded_by=self.supplier,
        )
        self.item_v1 = Item.objects.create(
            version=self.v1, source_sheet="申报要素", source_row=8, supplier_hs_raw="8544421900",
        )
        self.item_v2 = Item.objects.create(
            version=self.v2, source_sheet="申报要素", source_row=8, supplier_hs_raw="8544421900",
        )
        self.extra_item_v2 = Item.objects.create(
            version=self.v2, source_sheet="申报要素", source_row=9, supplier_hs_raw="8544422900",
        )
        self.confirmation_v1 = Confirmation.objects.create(
            item=self.item_v1, revision=2, confirmed_hs="8544422900", created_by=self.operator,
        )
        self.confirmation_v2 = Confirmation.objects.create(
            item=self.item_v2, revision=2, confirmed_hs="8544422900", created_by=self.operator,
        )

    # 便于构造合法的 V2 提交
    def make_submission_v2(self, revision=1):
        return Submission.objects.create(
            task=self.task, version=self.v2, revision=revision, created_by=self.operator,
        )

    def make_submission_item(self, submission, item=None, confirmation=None, hs="8544422900", revision=2):
        return SubmissionItem.objects.create(
            submission=submission,
            item=item or self.item_v2,
            confirmation=confirmation or self.confirmation_v2,
            submitted_hs=hs,
            item_revision=revision,
        )


class SubmissionInvariantTests(InvariantFixture):
    def test_submission_accepts_version_of_same_task(self):
        submission = self.make_submission_v2()
        self.assertEqual(submission.version_id, self.v2.pk)

    def test_submission_rejects_version_of_another_task(self):
        with self.assertRaises(ValidationError):
            Submission.objects.create(
                task=self.task, version=self.other_version, revision=1, created_by=self.operator,
            )

    def test_submission_keeps_integrity_error_for_duplicate_revision(self):
        self.make_submission_v2()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Submission.objects.create(
                task=self.task, version=self.v2, revision=1, created_by=self.operator,
            )


class SubmissionItemInvariantTests(InvariantFixture):
    def test_accepts_item_of_same_version(self):
        submission = self.make_submission_v2()
        row = self.make_submission_item(submission)
        self.assertEqual(row.item_id, self.item_v2.pk)

    def test_rejects_item_of_old_version(self):
        # 审核意见中的场景：V1 商品行被放进 V2 提交。
        submission = self.make_submission_v2()
        with self.assertRaises(ValidationError):
            self.make_submission_item(
                submission, item=self.item_v1, confirmation=self.confirmation_v1,
            )

    def test_accepts_historical_snapshot_after_item_gets_newer_confirmation(self):
        # 旧快照仍然合法：P01 不做“必须最新确认”的校验，时效检查属 P09。
        submission = self.make_submission_v2()
        snapshot = self.make_submission_item(submission)
        Confirmation.objects.create(
            item=self.item_v2, revision=3, previous_hs="8544422900",
            confirmed_hs="8544422910", reason="提交后再次修改", created_by=self.operator,
        )
        snapshot.refresh_from_db()
        snapshot.save()  # 不应因商品有更新版本而失败
        self.assertEqual(snapshot.item_revision, 2)
        self.assertEqual(snapshot.submitted_hs, "8544422900")

    def test_rejects_confirmation_of_another_item(self):
        submission = self.make_submission_v2()
        with self.assertRaises(ValidationError):
            self.make_submission_item(submission, confirmation=self.confirmation_v1)

    def test_rejects_item_revision_not_matching_confirmation(self):
        submission = self.make_submission_v2()
        with self.assertRaises(ValidationError):
            self.make_submission_item(submission, revision=3)

    def test_rejects_submitted_hs_not_taken_from_confirmation(self):
        submission = self.make_submission_v2()
        with self.assertRaises(ValidationError):
            self.make_submission_item(submission, hs="8544429999")


class DecisionItemInvariantTests(InvariantFixture):
    def _decision(self):
        submission = self.make_submission_v2()
        self.make_submission_item(submission)
        return Decision.objects.create(
            submission=submission, action=Decision.Action.APPROVE, created_by=self.expert,
        )

    def test_accepts_item_in_submission(self):
        decision = self._decision()
        row = DecisionItem.objects.create(
            decision=decision, item=self.item_v2, previous_hs="8544422900", final_hs="8544422910",
        )
        self.assertEqual(row.final_hs, "8544422910")

    def test_rejects_item_not_in_submission(self):
        decision = self._decision()
        with self.assertRaises(ValidationError):
            DecisionItem.objects.create(
                decision=decision, item=self.item_v1, previous_hs="8544421900", final_hs="8544421900",
            )

    def test_rejects_previous_hs_not_matching_snapshot(self):
        decision = self._decision()
        with self.assertRaises(ValidationError):
            DecisionItem.objects.create(
                decision=decision, item=self.item_v2, previous_hs="9999999999", final_hs="8544422910",
            )


class RunInvariantTests(InvariantFixture):
    def test_accepts_run_of_same_task_and_version(self):
        run = Run.objects.create(task=self.task, version=self.v2, kind=Run.Kind.PRECHECK)
        self.assertEqual(run.version_id, self.v2.pk)

    def test_rejects_version_of_another_task(self):
        with self.assertRaises(ValidationError):
            Run.objects.create(task=self.task, version=self.other_version, kind=Run.Kind.PRECHECK)

    def test_rejects_expert_check_bound_to_other_version_submission(self):
        submission_v1 = Submission.objects.create(
            task=self.task, version=self.v1, revision=1, created_by=self.operator,
        )
        with self.assertRaises(ValidationError):
            Run.objects.create(
                task=self.task, version=self.v2, submission=submission_v1,
                kind=Run.Kind.EXPERT_CHECK,
            )

    def test_rejects_submission_of_another_task(self):
        other_submission = Submission.objects.create(
            task=self.other_task, version=self.other_version, revision=1, created_by=self.operator,
        )
        with self.assertRaises(ValidationError):
            Run.objects.create(
                task=self.task, version=self.v2, submission=other_submission,
                kind=Run.Kind.EXPERT_CHECK,
            )


class RunItemInvariantTests(InvariantFixture):
    def test_precheck_accepts_item_of_run_version(self):
        run = Run.objects.create(task=self.task, version=self.v2, kind=Run.Kind.PRECHECK)
        row = RunItem.objects.create(run=run, item=self.item_v2)
        self.assertEqual(row.item_id, self.item_v2.pk)

    def test_rejects_old_version_item_on_new_run(self):
        # 审核补充意见：V1 商品不能被挂到 V2 的分析执行上。
        run = Run.objects.create(task=self.task, version=self.v2, kind=Run.Kind.PRECHECK)
        with self.assertRaises(ValidationError):
            RunItem.objects.create(run=run, item=self.item_v1)

    def test_expert_check_accepts_item_in_submission(self):
        submission = self.make_submission_v2()
        self.make_submission_item(submission)
        run = Run.objects.create(
            task=self.task, version=self.v2, submission=submission, kind=Run.Kind.EXPERT_CHECK,
        )
        row = RunItem.objects.create(run=run, item=self.item_v2)
        self.assertEqual(row.item_id, self.item_v2.pk)

    def test_expert_check_rejects_item_outside_submission(self):
        submission = self.make_submission_v2()
        self.make_submission_item(submission)  # 只提交了 item_v2
        run = Run.objects.create(
            task=self.task, version=self.v2, submission=submission, kind=Run.Kind.EXPERT_CHECK,
        )
        with self.assertRaises(ValidationError):
            RunItem.objects.create(run=run, item=self.extra_item_v2)
