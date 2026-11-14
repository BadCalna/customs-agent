"""P01 权限测试共用的合成数据（不在 test_*.py 中，避免被重复发现）。"""
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import (
    Confirmation,
    Decision,
    ItemImage,
    Submission,
    SubmissionItem,
)
from apps.runs.models import Run, RunItem

from .factories import assign, make_item, make_task, make_user, make_version

class PermissionFixture(TestCase):
    def setUp(self):
        self.supplier_a = make_user("supplier_a", Role.SUPPLIER)
        self.supplier_b = make_user("supplier_b", Role.SUPPLIER)
        self.operator_a = make_user("operator_a", Role.OPERATOR)
        self.operator_b = make_user("operator_b", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)

        self.task_a = make_task(self.supplier_a, title="任务甲")
        self.task_b = make_task(self.supplier_b, title="任务乙")
        assign(self.task_a, self.operator_a, Role.OPERATOR)
        assign(self.task_a, self.expert, Role.EXPERT)
        assign(self.task_b, self.operator_b, Role.OPERATOR)

        self.version_a = make_version(self.task_a, self.supplier_a)
        self.version_b = make_version(self.task_b, self.supplier_b)
        self.item_a = make_item(self.version_a, row=8)
        self.item_b = make_item(self.version_b, row=8)
        self.image_a = ItemImage.objects.create(
            item=self.item_a, source_sheet="申报要素", source_cell="Y8",
            status=ItemImage.Status.ABSENT,
        )
        self.run_a = Run.objects.create(task=self.task_a, version=self.version_a, kind=Run.Kind.PRECHECK)
        self.run_item_a = RunItem.objects.create(run=self.run_a, item=self.item_a)
        self.run_b = Run.objects.create(task=self.task_b, version=self.version_b, kind=Run.Kind.PRECHECK)
        self.confirmation_a = Confirmation.objects.create(
            item=self.item_a, revision=2, confirmed_hs="8544422900", created_by=self.operator_a,
        )
        self.submission_a = Submission.objects.create(
            task=self.task_a, version=self.version_a, revision=1, created_by=self.operator_a,
        )
        SubmissionItem.objects.create(
            submission=self.submission_a, item=self.item_a,
            confirmation=self.confirmation_a, submitted_hs="8544422900", item_revision=2,
        )
        self.decision_a = Decision.objects.create(
            submission=self.submission_a, action=Decision.Action.APPROVE, created_by=self.expert,
        )
