"""新上传任务不预先授权专家；提交成功必须交接到真实可访问的待审任务。"""
import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role, TaskAssignment
from apps.reviews.models import AuditEvent, Confirmation, Submission, Task
from apps.runs.models import Run
from tests.p01.factories import assign, make_item, make_task, make_user, make_version


@override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME="expert", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DefaultExpertTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.other_expert = make_user("expert_b", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version)
        self.confirmation = Confirmation.objects.create(item=self.item, revision=1, confirmed_hs="8544421900", created_by=self.operator)
        self.client = Client(enforce_csrf_checks=True, SERVER_NAME="localhost")
        self.client.force_login(self.operator)
        self.client.get("/api/v1/me")
        self.url = f"/api/v1/tasks/{self.task.pk}/submissions"
        self.body = {"version_id": str(self.version.pk), "revision": self.task.revision,
                     "items": [{"item_id": str(self.item.pk), "confirmation_id": str(self.confirmation.pk)}]}

    def submit(self, *, body=None, key="submit-1"):
        headers = {"HTTP_X_CSRFTOKEN": self.client.cookies["csrftoken"].value}
        if key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, json.dumps(body or self.body), content_type="application/json", **headers)

    def reader(self, user):
        client = Client(SERVER_NAME="localhost")
        client.force_login(user)
        return client

    def grants(self):
        return TaskAssignment.objects.filter(task=self.task, role=Role.EXPERT)

    def test_successful_submission_assigns_only_default_and_expert_can_review(self):
        expert = self.reader(self.expert)
        self.assertEqual(expert.get(f"/api/v1/tasks/{self.task.pk}").status_code, 404)
        self.assertEqual(self.grants().count(), 0)
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(list(self.grants().values_list("user_id", flat=True)), [self.expert.pk])
        self.assertEqual(expert.get(f"/api/v1/tasks/{self.task.pk}").status_code, 200)
        self.assertIn(str(self.task.pk), [r["task_id"] for r in expert.get("/api/v1/tasks").json()["items"]])
        self.assertEqual(expert.get(f"/api/v1/versions/{self.version.pk}/items/{self.item.pk}").status_code, 200)
        self.assertEqual(expert.get(f"/api/v1/tasks/{self.task.pk}/history").status_code, 200)
        self.assertEqual(expert.get(f"/api/v1/runs/{result['expert_run_id']}").status_code, 200)
        self.assertEqual(self.reader(self.other_expert).get(f"/api/v1/tasks/{self.task.pk}").status_code, 404)
        expert.get("/api/v1/me")
        decision = expert.post(f"/api/v1/submissions/{result['submission_id']}/decisions",
                               json.dumps({"action": "approve", "reason": "合成权限测试",
                                           "items": [{"item_id": str(self.item.pk), "final_hs": "8544421900"}]}),
                               content_type="application/json", HTTP_X_CSRFTOKEN=expert.cookies["csrftoken"].value)
        self.assertEqual(decision.status_code, 200, decision.content)

    def test_existing_active_expert_is_preserved_without_adding_default(self):
        assign(self.task, self.other_expert, Role.EXPERT)
        with override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME="missing"):
            response = self.submit()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(self.grants().values_list("user_id", flat=True)), [self.other_expert.pk])
        self.assertEqual(self.reader(self.expert).get(f"/api/v1/tasks/{self.task.pk}").status_code, 404)

    def test_configured_account_used_instead_of_demo_username(self):
        with override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME="expert_b"):
            self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(list(self.grants().values_list("user_id", flat=True)), [self.other_expert.pk])

    def assert_unavailable_rolls_back(self):
        response = self.submit()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "default_expert_unavailable")
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Run.objects.exists())
        self.assertFalse(self.grants().exists())
        self.task.refresh_from_db()
        self.assertEqual((self.task.stage, self.task.revision), (Task.Stage.AWAITING_CONFIRMATION, 1))

    def test_missing_default_rejects_without_orphan_pending(self):
        with override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME="missing"):
            self.assert_unavailable_rolls_back()

    def test_blank_default_does_not_pick_arbitrary_expert(self):
        with override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME=""):
            self.assert_unavailable_rolls_back()

    def test_inactive_default_rejected(self):
        self.expert.is_active = False
        self.expert.save(update_fields=["is_active"])
        self.assert_unavailable_rolls_back()

    def test_wrong_role_default_rejected(self):
        self.expert.role = Role.SUPPLIER
        self.expert.save(update_fields=["role"])
        self.assert_unavailable_rolls_back()

    def test_inactive_assignment_does_not_block_usable_default(self):
        assign(self.task, self.other_expert, Role.EXPERT)
        self.other_expert.is_active = False
        self.other_expert.save(update_fields=["is_active"])
        self.assertEqual(self.submit().status_code, 200)
        self.assertTrue(self.grants().filter(user=self.expert).exists())

    def test_existing_wrong_role_assignment_is_not_silently_upgraded(self):
        assign(self.task, self.expert, Role.OPERATOR)
        response = self.submit()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "expert_assignment_conflict")
        self.assertEqual(TaskAssignment.objects.get(task=self.task, user=self.expert).role, Role.OPERATOR)
        self.assertFalse(Submission.objects.exists())

    def test_validation_failure_does_not_grant_expert(self):
        response = self.submit(body={**self.body, "revision": 99})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.grants().exists())

    def test_later_failure_rolls_back_submission_grant_and_audit_together(self):
        with patch("apps.reviews.services.submissions.Run.objects.create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaisesMessage(RuntimeError, "synthetic failure"):
                self.submit()
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(self.grants().exists())
        self.assertFalse(AuditEvent.objects.filter(task=self.task).exists())

    def test_duplicate_submit_has_one_grant_and_audit(self):
        first = self.submit().json()
        second = self.submit().json()
        self.assertEqual(second["submission_id"], first["submission_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.grants().count(), 1)
        self.assertEqual(AuditEvent.objects.filter(task=self.task, action="task.expert_assigned").count(), 1)

    def test_idempotent_replay_repairs_legacy_pending(self):
        first = self.submit().json()
        self.grants().delete()  # 模拟修复前已经提交、却没有专家授权的任务。
        second = self.submit()
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["idempotent_replay"])
        self.assertEqual(second.json()["submission_id"], first["submission_id"])
        self.assertTrue(self.grants().filter(user=self.expert).exists())
        self.assertEqual(Submission.objects.count(), 1)

    def test_repeat_pending_without_key_repairs_grant(self):
        self.submit(key=None)
        self.grants().delete()
        self.task.refresh_from_db()
        response = self.submit(key=None, body={**self.body, "revision": self.task.revision})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["idempotent_replay"])
        self.assertEqual(self.grants().count(), 1)

    def test_closed_replay_does_not_regrant_removed_access(self):
        self.submit()
        Submission.objects.update(status=Submission.Status.APPROVED)
        Task.objects.filter(pk=self.task.pk).update(stage=Task.Stage.APPROVED)
        self.grants().delete()
        self.assertEqual(self.submit().status_code, 200)
        self.assertFalse(self.grants().exists())

    def test_old_version_replay_does_not_grant_access_to_new_version(self):
        self.submit()
        make_version(self.task, self.supplier, number=2)
        self.grants().delete()
        self.assertEqual(self.submit().status_code, 200)
        self.assertFalse(self.grants().exists())

    def test_repair_command_previews_then_applies_idempotently(self):
        self.submit()
        self.grants().delete()
        before = AuditEvent.objects.count()
        output = io.StringIO()
        call_command("repair_pending_expert", str(self.task.pk), stdout=output)
        self.assertIn("预览", output.getvalue())
        self.assertFalse(self.grants().exists())
        self.assertEqual(AuditEvent.objects.count(), before)
        call_command("repair_pending_expert", str(self.task.pk), apply=True, stdout=io.StringIO())
        call_command("repair_pending_expert", str(self.task.pk), apply=True, stdout=io.StringIO())
        self.assertEqual(self.grants().count(), 1)
        event = AuditEvent.objects.filter(task=self.task, action="task.expert_assigned").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.details["source"], "pending_repair")
        self.assertEqual(AuditEvent.objects.count(), before + 1)

    def test_repair_command_refuses_nonpending_task(self):
        with self.assertRaises(CommandError):
            call_command("repair_pending_expert", str(self.task.pk), apply=True, stdout=io.StringIO())
        self.assertFalse(self.grants().exists())
