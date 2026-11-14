"""P10 业务员退回：问题落库、待审提交不被绕过、版本与修订冲突。"""
import json

from apps.reviews.models import AuditEvent, Issue, Submission, Task
from tests.p01.factories import make_version

from .factories import SubmissionFixture


class OperatorReturnTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        # 待审提交由具体用例决定是否需要；默认走"未提交"的退回路径。
        self.url = f"/api/v1/tasks/{self.task.pk}/returns"
        self.client.force_login(self.operator)

    def payload(self, **overrides):
        body = {
            "version_id": str(self.version.pk),
            "revision": self.task.revision,
            "reason": "退回供应商补料",
            "issues": [{"item_id": str(self.item_a.pk), "message": "请补充规格书"}],
        }
        body.update(overrides)
        return body

    def post(self, body=None, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            self.url, data=json.dumps(body or self.payload()), content_type="application/json", **headers,
        )

    def _discard_pending(self):
        """把预置提交改成已退回：历史保留，只是不再处于待审。"""
        self.submission.status = Submission.Status.RETURNED
        self.submission.save(update_fields=["status"])

    def test_return_creates_issues_and_marks_stage(self):
        self._discard_pending()
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["stage"], "returned")
        self.assertEqual(len(body["issue_ids"]), 1)
        issue = Issue.objects.get(task=self.task, kind="operator_return")
        self.assertEqual(issue.item_id, self.item_a.pk)
        self.assertEqual(issue.created_by, self.operator)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.RETURNED)
        self.assertEqual(self.task.revision, 2)
        event = AuditEvent.objects.get(task=self.task, action="task.returned")
        self.assertEqual(event.details["return_to"], "supplier")

    def test_pending_submission_blocks_operator_return(self):
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "submission_pending")
        self.assertEqual(Issue.objects.count(), 0)

    def test_stale_revision_returns_409(self):
        self._discard_pending()
        response = self.post(self.payload(revision=99))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_revision")

    def test_old_version_cannot_be_returned(self):
        self._discard_pending()
        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_version")

    def test_return_requires_concrete_issue(self):
        self._discard_pending()
        response = self.post(self.payload(issues=[{"message": "  "}]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "missing_issues")

    def test_issue_item_must_belong_to_version(self):
        self._discard_pending()
        from tests.p01.factories import make_item, make_task

        other_task = make_task(self.supplier, title="另一个任务")
        other_version = make_version(other_task, self.supplier)
        stranger = make_item(other_version, row=7, name="别任务商品")
        response = self.post(self.payload(issues=[{"item_id": str(stranger.pk), "message": "x"}]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "item_not_in_version")

    def test_approved_task_cannot_be_returned(self):
        self._discard_pending()
        self.task.stage = Task.Stage.APPROVED
        self.task.save(update_fields=["stage"])
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "task_closed")

    def test_supplier_cannot_return(self):
        self.client.force_login(self.supplier)
        self.assertEqual(self.post().status_code, 403)

    def test_expert_cannot_use_operator_return(self):
        self.client.force_login(self.expert)
        self.assertEqual(self.post().status_code, 403)

    def test_unassigned_operator_gets_404(self):
        self.client.force_login(self.other_operator)
        self.assertEqual(self.post().status_code, 404)

    def test_idempotent_replay(self):
        self._discard_pending()
        first = self.post(key="r-1").json()
        second = self.post(key="r-1").json()
        self.assertEqual(second["issue_ids"], first["issue_ids"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(Issue.objects.count(), 1)
