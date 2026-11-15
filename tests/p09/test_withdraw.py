"""P09 撤回提交：待审提交可撤回到待确认，旧快照与专家核查结果保留。"""
import json

from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import AuditEvent, Confirmation, Submission, SubmissionItem, Task
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS_A = "8544421900"
HS_B = "7304210000"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WithdrawSubmissionTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.other_operator = make_user("operator_b", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
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
        for item, confirmation, hs in (
            (self.item_a, self.confirm_a, HS_A), (self.item_b, self.confirm_b, HS_B),
        ):
            SubmissionItem.objects.create(
                submission=self.submission, item=item, confirmation=confirmation,
                submitted_hs=hs, item_revision=1,
            )
        Run.objects.filter(version=self.version, kind=Run.Kind.EXPERT_CHECK, is_current=True).update(is_current=False)
        self.run = Run.objects.create(
            task=self.task, version=self.version, kind=Run.Kind.EXPERT_CHECK,
            submission=self.submission, status=Run.Status.SUCCEEDED, is_current=True,
        )
        self.url = f"/api/v1/submissions/{self.submission.pk}/withdraw"
        self.client.force_login(self.operator)

    def _confirm(self, item, hs):
        return Confirmation.objects.create(
            item=item, revision=1, previous_hs="", confirmed_hs=hs,
            reason="合成确认", created_by=self.operator,
        )

    def post(self, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(self.url, data="{}", content_type="application/json", **headers)

    def test_withdraw_returns_task_to_confirmation_and_keeps_history(self):
        response = self.post(key="w-1")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["status"], "superseded")
        self.assertEqual(body["task_stage"], "awaiting_confirmation")
        self.assertEqual(body["task_revision"], 2)

        self.submission.refresh_from_db()
        self.task.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.submission.status, Submission.Status.SUPERSEDED)
        self.assertEqual(self.submission.items.count(), 2)  # 快照行原样保留
        self.assertEqual(self.task.stage, Task.Stage.AWAITING_CONFIRMATION)
        self.assertFalse(self.run.is_current)
        event = AuditEvent.objects.get(task=self.task, action="submission.withdrawn")
        self.assertEqual(event.details["submission_id"], str(self.submission.pk))

    def test_can_edit_and_resubmit_after_withdraw(self):
        self.post()
        patched = self.client.patch(
            f"/api/v1/versions/{self.version.pk}/items/{self.item_a.pk}/confirmation",
            data=json.dumps({"confirmed_hs": HS_B, "reason": "撤回后修正", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content)

        self.task.refresh_from_db()
        body = {
            "version_id": str(self.version.pk),
            "revision": self.task.revision,
            "items": [
                {"item_id": str(self.item_a.pk), "confirmation_id": patched.json()["confirmation_id"]},
                {"item_id": str(self.item_b.pk), "confirmation_id": str(self.confirm_b.pk)},
            ],
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/v1/tasks/{self.task.pk}/submissions",
                data=json.dumps(body), content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Submission.objects.filter(task=self.task, status=Submission.Status.PENDING).count(), 1)
        self.assertEqual(response.json()["items"][0]["submitted_hs"], HS_B)

    def test_withdraw_non_pending_submission_is_rejected(self):
        self.submission.status = Submission.Status.APPROVED
        self.submission.save(update_fields=["status"])
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "submission_closed")

    def test_withdraw_old_version_submission_is_rejected(self):
        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_submission")

    def test_supplier_and_expert_cannot_withdraw(self):
        for user in (self.supplier, self.expert):
            self.client.force_login(user)
            self.assertEqual(self.post().status_code, 403)

    def test_unassigned_operator_gets_404(self):
        self.client.force_login(self.other_operator)
        self.assertEqual(self.post().status_code, 404)

    def test_anonymous_gets_401(self):
        self.client.logout()
        self.assertEqual(self.post().status_code, 401)

    def test_idempotent_replay(self):
        first = self.post(key="w-2").json()
        second = self.post(key="w-2").json()
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(AuditEvent.objects.filter(action="submission.withdrawn").count(), 1)
