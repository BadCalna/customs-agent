"""P09 业务员确认：追加留痕、修订冲突、角色与 CSRF、原值不被覆盖。"""
import json

from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import AuditEvent, Confirmation, Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

HS = "8544421900"
HS_ALT = "8536901900"


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConfirmationApiTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_a", Role.SUPPLIER)
        self.other_supplier = make_user("supplier_b", Role.SUPPLIER)
        self.operator = make_user("operator_a", Role.OPERATOR)
        self.other_operator = make_user("operator_b", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version, supplier_hs="7304210000")
        self.url = f"/api/v1/versions/{self.version.pk}/items/{self.item.pk}/confirmation"
        self.client.force_login(self.operator)

    def payload(self, **overrides):
        body = {"confirmed_hs": HS, "reason": "按预审建议确认", "expected_revision": 1}
        body.update(overrides)
        return body

    def patch(self, url=None, **overrides):
        return self.client.patch(
            url or self.url, data=json.dumps(self.payload(**overrides)), content_type="application/json",
        )

    def test_confirm_appends_history_and_bumps_revisions(self):
        response = self.patch()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["confirmed_hs"], HS)
        self.assertEqual(body["revision"], 2)
        self.assertEqual(body["task_revision"], 2)
        self.assertFalse(body["idempotent_replay"])

        confirmation = Confirmation.objects.get(item=self.item)
        self.assertEqual(confirmation.revision, 2)
        self.assertEqual(confirmation.previous_hs, "")
        self.item.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.item.revision, 2)
        self.assertEqual(self.task.revision, 2)
        self.assertEqual(self.task.stage, Task.Stage.AWAITING_CONFIRMATION)

        event = AuditEvent.objects.get(task=self.task, action="confirmation.updated")
        self.assertEqual(event.new_value, HS)
        self.assertEqual(event.item_id, self.item.pk)
        self.assertEqual(event.actor_id, self.operator.pk)

    def test_second_change_records_previous_value(self):
        self.patch()
        response = self.patch(confirmed_hs=HS_ALT, expected_revision=2, reason="改按另一要素确认")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["previous_hs"], HS)
        self.assertEqual(Confirmation.objects.filter(item=self.item).count(), 2)

    def test_same_value_same_revision_is_idempotent_replay(self):
        self.patch()
        response = self.patch(expected_revision=2)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["idempotent_replay"])
        self.assertEqual(Confirmation.objects.filter(item=self.item).count(), 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.revision, 2)

    def test_stale_revision_returns_409(self):
        response = self.patch(expected_revision=99)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error"]["code"], "stale_revision")
        self.assertEqual(body["error"]["details"]["current_revision"], 1)
        self.assertEqual(Confirmation.objects.count(), 0)

    def test_invalid_hs_rejected(self):
        for value in ("", "ABC", "123", "85444219001", "8544 421900"):
            response = self.patch(confirmed_hs=value)
            self.assertEqual(response.status_code, 422, value)
            self.assertEqual(response.json()["error"]["code"], "invalid_hs")
        self.assertEqual(Confirmation.objects.count(), 0)

    def test_supplier_cannot_confirm(self):
        self.client.force_login(self.supplier)
        response = self.patch()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_expert_cannot_confirm(self):
        self.client.force_login(self.expert)
        self.assertEqual(self.patch().status_code, 403)

    def test_unassigned_operator_gets_404(self):
        self.client.force_login(self.other_operator)
        response = self.patch()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_anonymous_gets_401(self):
        self.client.logout()
        response = self.patch()
        self.assertEqual(response.status_code, 401)

    def test_missing_csrf_token_gets_403(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.patch(self.url, data=json.dumps(self.payload()), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")

    def test_supplier_raw_value_never_overwritten(self):
        self.patch()
        self.item.refresh_from_db()
        self.assertEqual(self.item.supplier_hs_raw, "7304210000")
        self.assertEqual(Confirmation.objects.get(item=self.item).confirmed_hs, HS)

    def test_approved_task_is_closed_for_editing(self):
        self.task.stage = Task.Stage.APPROVED
        self.task.save(update_fields=["stage"])
        response = self.patch()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "task_closed")

    def test_item_must_belong_to_version(self):
        other_version = make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        response = self.client.patch(
            f"/api/v1/versions/{other_version.pk}/items/{self.item.pk}/confirmation",
            data=json.dumps(self.payload()), content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    def test_malformed_ids_return_404_not_500(self):
        response = self.client.patch(
            "/api/v1/versions/not-a-uuid/items/not-a-uuid/confirmation",
            data=json.dumps(self.payload()), content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
