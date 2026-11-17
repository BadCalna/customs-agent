"""P09 审查遗留回归：M-02 幂等绑目标、M-03 派发异常不伪装 500、M-07 无 key 重放、M-11 HS 仅 ASCII。"""
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.errors import ApiError
from apps.accounts.models import Role
from apps.reviews.models import Confirmation, IdempotencyRecord, Submission, Task
from apps.reviews.services import idempotency
from apps.reviews.services.confirmations import normalize_hs
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS_A = "8544421900"
HS_B = "7304210000"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubmissionReviewFixTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_rf", Role.SUPPLIER)
        self.operator = make_user("operator_rf", Role.OPERATOR)
        self.expert = make_user("expert_rf", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item_a = make_item(self.version, row=5, name="合成商品A")
        self.item_b = make_item(self.version, row=6, name="合成商品B", supplier_hs=HS_B)
        self.confirm_a = self._confirm(self.item_a, HS_A)
        self.confirm_b = self._confirm(self.item_b, HS_B)
        self.url = f"/api/v1/tasks/{self.task.pk}/submissions"
        self.client.force_login(self.operator)

    def _confirm(self, item, hs):
        return Confirmation.objects.create(
            item=item, revision=1, previous_hs="", confirmed_hs=hs,
            reason="合成确认", created_by=self.operator,
        )

    def payload(self, **overrides):
        body = {
            "version_id": str(self.version.pk),
            "revision": self.task.revision,
            "items": [
                {"item_id": str(self.item_a.pk), "confirmation_id": str(self.confirm_a.pk)},
                {"item_id": str(self.item_b.pk), "confirmation_id": str(self.confirm_b.pk)},
            ],
        }
        body.update(overrides)
        return body

    def post(self, body=None, key=None, url=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            url or self.url, data=json.dumps(body or self.payload()),
            content_type="application/json", **headers,
        )

    def submit(self, body=None, key=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.post(body=body, key=key)

    # --- M-07：无幂等键的重复提交必须重放，不能 409 -------------------------
    def test_no_key_repeat_replays_same_snapshot(self):
        first = self.submit().json()
        second = self.submit().json()
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertEqual(first["expert_run_id"], second["expert_run_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(Run.objects.count(), 1)

    def test_unrelated_stale_revision_is_not_accepted_as_replay(self):
        self.submit()
        response = self.post(self.payload(revision=99))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_revision")

    # --- M-02：幂等键只能绑定自己那个任务 ---------------------------------
    def test_idempotency_key_cannot_replay_on_another_task(self):
        self.submit(key="task-bound")
        other = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(other, self.operator, Role.OPERATOR)
        version = make_version(other, self.supplier)
        item = make_item(version, row=7, name="合成商品C")
        confirm = self._confirm(item, HS_A)
        body = {
            "version_id": str(version.pk), "revision": other.revision,
            "items": [{"item_id": str(item.pk), "confirmation_id": str(confirm.pk)}],
        }
        response = self.post(body, key="task-bound", url=f"/api/v1/tasks/{other.pk}/submissions")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
        self.assertEqual(Submission.objects.count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.revision, 1)

    def test_legacy_payload_hash_replays_and_is_upgraded(self):
        body = self.payload()
        first = self.submit(body, key="legacy-hash").json()
        record = IdempotencyRecord.objects.get(key="legacy-hash")
        record.request_hash = idempotency.legacy_request_hash(body)
        record.save(update_fields=["request_hash"])

        response = self.submit(body, key="legacy-hash")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["submission_id"], first["submission_id"])
        self.assertTrue(response.json()["idempotent_replay"])
        record.refresh_from_db()
        self.assertEqual(
            record.request_hash,
            idempotency.request_hash(body, str(self.task.pk)),
        )

    def test_pending_replay_with_new_key_stores_completed_response(self):
        first = self.submit().json()
        second = self.submit(key="added-later").json()
        third = self.submit(key="added-later").json()
        self.assertEqual(first["submission_id"], second["submission_id"])
        self.assertEqual(second, third)
        self.assertTrue(IdempotencyRecord.objects.get(key="added-later").response)

    # --- M-03：派发异常不能伪装成 500 --------------------------------------
    def test_dispatch_exception_keeps_success_and_queued_run(self):
        with patch("apps.runs.inline.execute_expert_check", side_effect=RuntimeError("派发中断")):
            response = self.submit()
        self.assertEqual(response.status_code, 200)
        submission = Submission.objects.get(pk=response.json()["submission_id"])
        self.assertEqual(submission.status, Submission.Status.PENDING)
        self.assertEqual(Run.objects.filter(submission=submission, status=Run.Status.QUEUED).count(), 1)

    # --- M-11：HS 只收 ASCII 数字 -----------------------------------------
    def test_fullwidth_digits_are_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            normalize_hs("８５４４４２１９００")   # 全角
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "invalid_hs")

    def test_ascii_digits_pass(self):
        self.assertEqual(normalize_hs(" 8544421900 "), "8544421900")
