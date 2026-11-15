"""P09 整批提交：快照不可变、逐行确认时效、幂等、提交后自动专家核查（R09/R13）。"""
import json
import uuid

from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import AuditEvent, Confirmation, Submission, Task
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS_A = "8544421900"
HS_B = "7304210000"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SubmissionApiTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
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

    def _confirm(self, item, hs, revision=1):
        return Confirmation.objects.create(
            item=item, revision=revision, previous_hs="", confirmed_hs=hs,
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

    def post(self, body=None, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            self.url, data=json.dumps(body or self.payload()), content_type="application/json", **headers,
        )

    def submit(self, body=None, key=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.post(body=body, key=key)

    def test_submit_freezes_snapshot_and_auto_starts_expert_check(self):
        response = self.submit(key="submit-1")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        submission = Submission.objects.get(pk=body["submission_id"])
        self.assertEqual(submission.status, Submission.Status.PENDING)
        self.assertEqual(submission.items.count(), 2)
        self.assertEqual(submission.items.get(item=self.item_a).submitted_hs, HS_A)
        self.assertEqual(submission.items.get(item=self.item_a).item_revision, 1)

        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.SUBMITTED)
        self.assertEqual(self.task.revision, 2)

        run = Run.objects.get(pk=body["expert_run_id"])
        self.assertEqual(run.kind, Run.Kind.EXPERT_CHECK)
        self.assertEqual(run.submission_id, submission.pk)
        self.assertEqual(run.version_id, self.version.pk)
        self.assertTrue(run.is_current)
        self.assertEqual(run.status, Run.Status.SUCCEEDED)  # inline mock 在提交后执行
        self.assertEqual(run.items.count(), 2)
        for row in run.items.all():
            line = submission.items.get(item=row.item)
            self.assertEqual(row.result["target_hs"], line.submitted_hs)

        event = AuditEvent.objects.get(task=self.task, action="submission.created")
        self.assertEqual(event.details["expert_run_id"], str(run.pk))

    def test_submission_requires_every_item_of_the_version(self):
        body = self.payload(items=[self.payload()["items"][0]])
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "incomplete_items")
        self.assertEqual(payload["error"]["details"]["missing_item_ids"], [str(self.item_b.pk)])
        self.assertEqual(Submission.objects.count(), 0)

    def test_item_from_another_version_rejected(self):
        other_task = make_task(self.supplier, title="另一个任务")
        other_version = make_version(other_task, self.supplier)
        stranger = make_item(other_version, row=5, name="别任务商品")
        stranger_confirmation = self._confirm(stranger, HS_A)
        body = self.payload(items=[
            {"item_id": str(self.item_a.pk), "confirmation_id": str(self.confirm_a.pk)},
            {"item_id": str(stranger.pk), "confirmation_id": str(stranger_confirmation.pk)},
        ])
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "item_not_in_version")

    def test_stale_confirmation_returns_409(self):
        self._confirm(self.item_a, HS_B, revision=2)
        response = self.post()
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"]["code"], "stale_confirmation")
        self.assertEqual(body["error"]["details"]["item_id"], str(self.item_a.pk))

    def test_item_without_confirmation_cannot_borrow_ai_value(self):
        task = make_task(self.supplier, title="无确认任务", stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(task, self.operator, Role.OPERATOR)
        version = make_version(task, self.supplier)
        item = make_item(version, row=5, name="未确认商品")
        body = {
            "version_id": str(version.pk), "revision": task.revision,
            "items": [{"item_id": str(item.pk), "confirmation_id": str(uuid.uuid4())}],
        }
        response = self.client.post(
            f"/api/v1/tasks/{task.pk}/submissions", data=json.dumps(body), content_type="application/json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "missing_confirmation")

    def test_stale_task_revision_returns_409(self):
        response = self.post(self.payload(revision=99))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_revision")

    def test_only_latest_version_can_be_submitted(self):
        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_version")

    def test_same_key_same_payload_replays_without_new_rows(self):
        first = self.submit(key="k-1").json()
        second = self.submit(key="k-1").json()
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertEqual(second["expert_run_id"], first["expert_run_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(Run.objects.filter(kind=Run.Kind.EXPERT_CHECK).count(), 1)

    def test_same_key_different_payload_returns_409(self):
        self.submit(key="k-2")
        self.task.refresh_from_db()
        body = self.payload(revision=self.task.revision)
        response = self.post(body, key="k-2")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")

    def test_repeat_without_key_same_content_replays_pending(self):
        first = self.submit().json()
        self.task.refresh_from_db()
        second = self.submit(self.payload(revision=self.task.revision)).json()
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(Submission.objects.count(), 1)

    def test_different_content_while_pending_returns_409(self):
        self.submit()
        newer = self._confirm(self.item_a, HS_B, revision=2)
        self.task.refresh_from_db()
        body = self.payload(revision=self.task.revision)
        body["items"][0]["confirmation_id"] = str(newer.pk)
        response = self.post(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "submission_pending")

    def test_expert_cannot_submit(self):
        self.client.force_login(self.expert)
        self.assertEqual(self.post().status_code, 403)

    def test_supplier_cannot_submit(self):
        self.client.force_login(self.supplier)
        self.assertEqual(self.post().status_code, 403)

    def test_anonymous_gets_401(self):
        self.client.logout()
        self.assertEqual(self.post().status_code, 401)

    def test_edit_after_submit_is_rejected_and_snapshot_untouched(self):
        submission_id = self.submit().json()["submission_id"]
        response = self.client.patch(
            f"/api/v1/versions/{self.version.pk}/items/{self.item_a.pk}/confirmation",
            data=json.dumps({"confirmed_hs": HS_B, "reason": "提交后改主意", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "submission_pending")
        submission = Submission.objects.get(pk=submission_id)
        self.assertEqual(submission.items.get(item=self.item_a).submitted_hs, HS_A)
        self.assertEqual(Confirmation.objects.filter(item=self.item_a).count(), 1)
        run = Run.objects.get(submission=submission)
        self.assertEqual(run.items.get(item=self.item_a).result["target_hs"], HS_A)

    def test_unknown_submission_ids_do_not_leak_existence(self):
        body = self.payload(items=[
            {"item_id": str(self.item_a.pk), "confirmation_id": str(uuid.uuid4())},
            {"item_id": str(self.item_b.pk), "confirmation_id": str(self.confirm_b.pk)},
        ])
        response = self.post(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_confirmation")
