"""P10 测试共用：直接构造已提交快照，避免每个用例都跑一遍 P09 流程。"""
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import Confirmation, Submission, SubmissionItem, Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS_A = "8544421900"
HS_B = "7304210000"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubmissionFixture(TestCase):
    """一条任务、一个版本、两条已确认商品、一份待审提交。"""

    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.other_supplier = make_user("supplier_b", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.other_operator = make_user("operator_b", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.other_expert = make_user("expert_b", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.SUBMITTED)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item_a = make_item(self.version, row=5, name="合成商品A")
        self.item_b = make_item(self.version, row=6, name="合成商品B", supplier_hs=HS_B)
        self.confirm_a = self._confirm(self.item_a, HS_A)
        self.confirm_b = self._confirm(self.item_b, HS_B)
        self.submission = Submission.objects.create(
            task=self.task, version=self.version, revision=self.task.revision, created_by=self.operator,
        )
        self.line_a = SubmissionItem.objects.create(
            submission=self.submission, item=self.item_a, confirmation=self.confirm_a,
            submitted_hs=HS_A, item_revision=1,
        )
        self.line_b = SubmissionItem.objects.create(
            submission=self.submission, item=self.item_b, confirmation=self.confirm_b,
            submitted_hs=HS_B, item_revision=1,
        )

    def _confirm(self, item, hs):
        return Confirmation.objects.create(
            item=item, revision=1, previous_hs="", confirmed_hs=hs,
            reason="合成确认", created_by=self.operator,
        )
