"""P19 真实业务入口产生定向消息/outbox，重放与回滚不会多发。"""
import json

from django.db import transaction
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.notifications.models import Message, NotificationDelivery
from apps.reviews.models import Confirmation, Decision, Submission, Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS = "8544421900"


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CUSTOMS_DEFAULT_EXPERT_USERNAME="p19_expert",
)
class NotificationEventTests(TestCase):
    def setUp(self):
        self.supplier = make_user("p19_supplier", Role.SUPPLIER)
        self.other_supplier = make_user("p19_other_supplier", Role.SUPPLIER)
        self.operator = make_user("p19_operator", Role.OPERATOR)
        self.other_operator = make_user("p19_other_operator", Role.OPERATOR)
        self.expert = make_user("p19_expert", Role.EXPERT)
        self.other_expert = make_user("p19_other_expert", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version, row=5)
        self.confirmation = Confirmation.objects.create(
            item=self.item, revision=1, previous_hs="", confirmed_hs=HS,
            reason="合成确认", created_by=self.operator,
        )
        self.client.force_login(self.operator)

    def post(self, url, payload, key):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json", HTTP_IDEMPOTENCY_KEY=key,
        )

    def submission_payload(self):
        self.task.refresh_from_db()
        return {
            "version_id": str(self.version.pk), "revision": self.task.revision,
            "items": [{"item_id": str(self.item.pk), "confirmation_id": str(self.confirmation.pk)}],
        }

    def submit(self, key="p19-submit", payload=None):
        return self.post(f"/api/v1/tasks/{self.task.pk}/submissions", payload or self.submission_payload(), key)

    def make_submission(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.content)
        return Submission.objects.get(pk=response.json()["submission_id"])

    def decide(self, submission, payload=None, key="p19-decision"):
        self.client.force_login(self.expert)
        body = payload or {"action": "approve", "reason": "合成审核通过",
                           "items": [{"item_id": str(self.item.pk), "final_hs": HS}]}
        return self.post(f"/api/v1/submissions/{submission.pk}/decisions", body, key)

    def event_messages(self, kind):
        return Message.objects.filter(task=self.task, kind=kind)

    def test_submit_notifies_only_selected_expert_after_explicit_grant(self):
        submission = self.make_submission()
        messages = self.event_messages("submission.created")
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.recipient_id, self.expert.pk)
        self.assertEqual(message.version_id, self.version.pk)
        self.assertEqual(message.submission_id, submission.pk)
        self.assertTrue(message.event_key)
        self.assertIsNone(message.decision_id)
        self.assertEqual(NotificationDelivery.objects.filter(message=message).count(), 1)
        self.assertTrue(self.task.assignments.filter(user=self.expert, role=Role.EXPERT).exists())
        self.assertFalse(Message.objects.filter(recipient=self.other_expert).exists())
        self.assertFalse(Message.objects.filter(recipient=self.other_operator).exists())

    def test_existing_task_expert_takes_priority_over_global_default(self):
        assign(self.task, self.other_expert, Role.EXPERT)
        self.make_submission()
        messages = self.event_messages("submission.created")
        self.assertEqual(messages.count(), 1)
        self.assertEqual(messages.get().recipient_id, self.other_expert.pk)
        self.assertFalse(messages.filter(recipient=self.expert).exists())

    def test_submission_idempotent_replay_does_not_duplicate_message_or_delivery(self):
        payload = self.submission_payload()
        first = self.submit(payload=payload)
        second = self.submit(payload=payload)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["submission_id"], second.json()["submission_id"])
        self.assertEqual(self.event_messages("submission.created").count(), 1)
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    def test_pending_content_replay_without_key_does_not_duplicate_notification(self):
        first = self.submit(key="")
        second = self.submit(key="")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(self.event_messages("submission.created").count(), 1)
        self.assertEqual(NotificationDelivery.objects.count(), 1)

    def test_failed_submission_creates_neither_message_nor_outbox(self):
        payload = self.submission_payload()
        payload["revision"] += 99
        response = self.submit(payload=payload)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_outer_transaction_rollback_removes_submission_and_notifications(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                response = self.submit()
                self.assertEqual(response.status_code, 200, response.content)
                self.assertTrue(self.event_messages("submission.created").exists())
                transaction.set_rollback(True)
        self.assertEqual(callbacks, [])
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Message.objects.exists())
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_approval_notifies_submitting_operator_and_supplier_with_exact_snapshot(self):
        submission = self.make_submission()
        response = self.decide(submission)
        self.assertEqual(response.status_code, 200, response.content)
        decision = Decision.objects.get(pk=response.json()["decision_id"])
        messages = self.event_messages("decision.approved")
        self.assertEqual(set(messages.values_list("recipient_id", flat=True)),
                         {self.operator.pk, self.supplier.pk})
        self.assertEqual(messages.count(), 2)
        for message in messages:
            self.assertEqual(message.version_id, self.version.pk)
            self.assertEqual(message.submission_id, submission.pk)
            self.assertEqual(message.decision_id, decision.pk)
            self.assertTrue(message.event_key)
        operator_message = messages.get(recipient=self.operator)
        supplier_message = messages.get(recipient=self.supplier)
        self.assertTrue(NotificationDelivery.objects.filter(message=operator_message).exists())
        self.assertFalse(NotificationDelivery.objects.filter(message=supplier_message).exists())

    def test_approval_replay_keeps_single_effective_reminder_per_recipient(self):
        submission = self.make_submission()
        first, second = self.decide(submission), self.decide(submission)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["decision_id"], second.json()["decision_id"])
        self.assertEqual(self.event_messages("decision.approved").count(), 2)
        self.assertEqual(NotificationDelivery.objects.filter(message__kind="decision.approved").count(), 1)

    def test_supplier_return_notifies_only_supplier_without_dws(self):
        submission = self.make_submission()
        response = self.decide(submission, {
            "action": "return", "return_to": "supplier", "reason": "缺事实",
            "issues": [{"item_id": str(self.item.pk), "message": "请补充用途"}],
        })
        self.assertEqual(response.status_code, 200, response.content)
        messages = self.event_messages("decision.returned")
        self.assertEqual(list(messages.values_list("recipient_id", flat=True)), [self.supplier.pk])
        self.assertFalse(NotificationDelivery.objects.filter(message__in=messages).exists())
        self.assertEqual(messages.get().decision_id, Decision.objects.get(submission=submission).pk)

    def test_operator_return_notifies_actual_submitter_not_all_operators(self):
        assign(self.task, self.other_operator, Role.OPERATOR)
        submission = self.make_submission()
        response = self.decide(submission, {
            "action": "return", "return_to": "operator", "reason": "确认值需复核",
            "issues": [{"message": "请复核供应商说明"}],
        })
        self.assertEqual(response.status_code, 200, response.content)
        messages = self.event_messages("decision.returned")
        self.assertEqual(list(messages.values_list("recipient_id", flat=True)), [self.operator.pk])
        self.assertEqual(NotificationDelivery.objects.filter(message__in=messages).count(), 1)
        self.assertFalse(messages.filter(recipient=self.other_operator).exists())

    def test_invalid_decision_does_not_emit_return_notification(self):
        submission = self.make_submission()
        response = self.decide(submission, {
            "action": "return", "return_to": "supplier", "reason": "缺事实", "issues": [],
        })
        self.assertEqual(response.status_code, 422, response.content)
        self.assertFalse(self.event_messages("decision.returned").exists())
        self.assertFalse(Decision.objects.exists())

    def test_decision_outer_rollback_preserves_pending_submission_without_new_notifications(self):
        submission = self.make_submission()
        before = Message.objects.count(), NotificationDelivery.objects.count()
        with transaction.atomic():
            response = self.decide(submission)
            self.assertEqual(response.status_code, 200, response.content)
            transaction.set_rollback(True)
        self.assertEqual((Message.objects.count(), NotificationDelivery.objects.count()), before)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.PENDING)
        self.assertFalse(Decision.objects.exists())

    def test_operator_returns_once_to_supplier_and_replay_does_not_resend(self):
        payload = {
            "version_id": str(self.version.pk), "revision": self.task.revision,
            "reason": "需要补料", "issues": [{"item_id": str(self.item.pk), "message": "请补充材质"}],
        }
        url = f"/api/v1/tasks/{self.task.pk}/returns"
        first = self.post(url, payload, "p19-return")
        second = self.post(url, payload, "p19-return")
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        messages = self.event_messages("task.returned")
        self.assertEqual(messages.count(), 1)
        message = messages.get()
        self.assertEqual(message.recipient_id, self.supplier.pk)
        self.assertEqual(message.version_id, self.version.pk)
        self.assertIsNone(message.submission_id)
        self.assertIsNone(message.decision_id)
        self.assertFalse(NotificationDelivery.objects.exists())

    def test_new_upload_never_rebinds_old_event_to_current_version(self):
        submission = self.make_submission()
        original = self.event_messages("submission.created").get()
        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        original.refresh_from_db()
        self.assertEqual(original.version_id, self.version.pk)
        self.assertEqual(original.submission_id, submission.pk)
        self.assertEqual(self.event_messages("submission.created").count(), 1)
