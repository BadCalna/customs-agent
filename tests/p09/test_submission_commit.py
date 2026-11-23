"""真实事务提交后的派发失败：不能返回假 500，重试也不能重写快照/Run。"""
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import Confirmation, IdempotencyRecord, Submission, Task
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubmissionCommitTests(TransactionTestCase):
    def setUp(self):
        self.supplier = make_user("commit_supplier", Role.SUPPLIER)
        self.operator = make_user("commit_operator", Role.OPERATOR)
        self.expert = make_user("commit_expert", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        version = make_version(self.task, self.supplier)
        item = make_item(version)
        confirmation = Confirmation.objects.create(
            item=item, revision=1, confirmed_hs="8544421900", created_by=self.operator,
        )
        self.payload = {
            "version_id": str(version.pk), "revision": self.task.revision,
            "items": [{"item_id": str(item.pk), "confirmation_id": str(confirmation.pk)}],
        }
        self.url = f"/api/v1/tasks/{self.task.pk}/submissions"
        self.client.force_login(self.operator)

    def _post(self, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            self.url, data=json.dumps(self.payload), content_type="application/json", **headers,
        )

    def _assert_dispatch_failure_is_replayable(self, key=None):
        # TransactionTestCase 不包外层回滚事务：这里的 on_commit 确实发生于 durable commit 后。
        with patch("apps.runs.inline.execute_expert_check", side_effect=RuntimeError("executor unavailable")) as dispatch:
            with self.assertLogs("django.db.backends.base", level="ERROR"):
                first = self._post(key)
            self.assertEqual(first.status_code, 200, first.content)
            first_body = first.json()
            submission = Submission.objects.get(pk=first_body["submission_id"])
            run = Run.objects.get(pk=first_body["expert_run_id"])
            self.assertEqual(submission.status, Submission.Status.PENDING)
            self.assertEqual(run.status, Run.Status.QUEUED)
            self.assertEqual(run.submission_id, submission.pk)
            self.task.refresh_from_db()
            self.assertEqual(self.task.stage, Task.Stage.SUBMITTED)
            self.assertEqual(self.task.revision, 2)
            second = self._post(key)
            self.assertEqual(second.status_code, 200, second.content)
            self.assertEqual(second.json()["submission_id"], str(submission.pk))
            self.assertEqual(second.json()["expert_run_id"], str(run.pk))
            self.assertTrue(second.json()["idempotent_replay"])
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(Submission.objects.count(), 1)
            self.assertEqual(Run.objects.count(), 1)
            self.task.refresh_from_db()
            self.assertEqual(self.task.revision, 2)
            if key:
                self.assertEqual(IdempotencyRecord.objects.get(key=key).response["submission_id"], str(submission.pk))

    def test_callback_failure_after_commit_with_key_preserves_snapshot_and_original_response(self):
        self._assert_dispatch_failure_is_replayable(key="committed-but-not-dispatched")

    def test_callback_failure_after_commit_without_key_allows_original_revision_replay(self):
        self._assert_dispatch_failure_is_replayable()
