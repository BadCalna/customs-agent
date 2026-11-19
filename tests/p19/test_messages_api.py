"""P19 本人消息 API：会话、CSRF、分页、已读与历史版本定位。"""
import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Role
from apps.notifications.models import Message
from apps.api.pagination import encode_cursor
from tests.p01.factories import assign, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class MessageApiTests(TestCase):
    def setUp(self):
        self.operator = make_user("p19_operator", Role.OPERATOR)
        self.other_operator = make_user("p19_other_operator", Role.OPERATOR)
        self.supplier = make_user("p19_supplier", Role.SUPPLIER)
        self.task = make_task(self.supplier)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)
        self.message = self.make_message(self.operator, "本人的提醒")
        self.stranger = self.make_message(self.other_operator, "他人的私有提醒")
        self.client.force_login(self.operator)

    def test_revoked_task_permission_does_not_grant_target_access(self):
        self.task.assignments.filter(user=self.operator).delete()
        response = self.client.get("/api/v1/messages")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["items"][0]["target_url"])
        self.assertEqual(self.client.get(f"/api/v1/tasks/{self.task.pk}").status_code, 404)

    def test_cursor_extreme_offset_date_returns_400(self):
        for stamp in ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"):
            with self.subTest(stamp=stamp):
                cursor = encode_cursor({"id": str(self.message.pk), "created_at": stamp})
                response = self.client.get("/api/v1/messages", {"cursor": cursor})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_cursor")

    def make_message(self, recipient, title, **extra):
        return Message.objects.create(
            recipient=recipient, task=self.task, version=self.version,
            kind="submission.created", title=title, body="请打开对应文件版本处理", **extra,
        )

    def mark(self, message_id=None, payload=None, client=None, **headers):
        return (client or self.client).patch(
            f"/api/v1/messages/{message_id or self.message.pk}",
            data=json.dumps({"is_read": True} if payload is None else payload),
            content_type="application/json", **headers,
        )

    def test_list_requires_authenticated_session(self):
        self.client.logout()
        response = self.client.get("/api/v1/messages")
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_list_contains_only_recipient_messages_and_explicit_state(self):
        response = self.client.get("/api/v1/messages")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual([row["message_id"] for row in body["items"]], [str(self.message.pk)])
        self.assertEqual(body["unread_count"], 1)
        self.assertIsNone(body["next_cursor"])
        row = body["items"][0]
        self.assertEqual(row["task_id"], str(self.task.pk))
        self.assertEqual(row["version_id"], str(self.version.pk))
        self.assertFalse(row["is_read"])
        self.assertIsNone(row["read_at"])
        self.assertIsNone(row["submission_id"])
        self.assertIsNone(row["decision_id"])
        self.assertNotIn("recipient", row)
        self.assertNotIn("他人的私有提醒", response.content.decode())

    def test_empty_list_is_success_with_zero_unread(self):
        self.client.force_login(self.supplier)
        response = self.client.get("/api/v1/messages")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"items": [], "next_cursor": None, "unread_count": 0})

    def test_unread_filter_does_not_include_read_or_other_user(self):
        self.make_message(self.operator, "已读提醒", is_read=True, read_at=timezone.now())
        response = self.client.get("/api/v1/messages", {"unread_only": "true"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["message_id"] for row in response.json()["items"]], [str(self.message.pk)])
        self.assertEqual(response.json()["unread_count"], 1)

    def test_false_unread_filter_includes_read_records(self):
        read = self.make_message(self.operator, "历史已读", is_read=True, read_at=timezone.now())
        response = self.client.get("/api/v1/messages", {"unread_only": "false"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual({row["message_id"] for row in response.json()["items"]},
                         {str(self.message.pk), str(read.pk)})

    def test_pagination_returns_each_personal_record_once_and_total_unread(self):
        messages = [self.message] + [self.make_message(self.operator, f"通知 {i}") for i in range(4)]
        now = timezone.now()
        for i, message in enumerate(messages):
            Message.objects.filter(pk=message.pk).update(created_at=now + timedelta(seconds=i))
        found, cursor = [], None
        for _ in range(4):
            query = {"limit": 2}
            if cursor:
                query["cursor"] = cursor
            response = self.client.get("/api/v1/messages", query)
            self.assertEqual(response.status_code, 200, response.content)
            page = response.json()
            self.assertEqual(page["unread_count"], len(messages))
            self.assertLessEqual(len(page["items"]), 2)
            found.extend(row["message_id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertIsNone(cursor)
        self.assertEqual(found, [str(message.pk) for message in reversed(messages)])

    def test_invalid_cursor_returns_structured_client_error(self):
        response = self.client.get("/api/v1/messages", {"cursor": "not-a-valid-message-cursor"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("error", response.json())

    def test_mark_read_persists_without_changing_another_message(self):
        response = self.mark()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["is_read"])
        self.message.refresh_from_db()
        self.stranger.refresh_from_db()
        self.assertTrue(self.message.is_read)
        self.assertIsNotNone(self.message.read_at)
        self.assertFalse(self.stranger.is_read)
        self.assertEqual(self.client.get("/api/v1/messages").json()["unread_count"], 0)

    def test_mark_read_retry_keeps_original_timestamp(self):
        first = self.mark()
        second = self.mark()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()["read_at"], second.json()["read_at"])

    def test_mark_unread_is_rejected_and_does_not_mutate_message(self):
        response = self.mark(payload={"is_read": False})
        self.assertEqual(response.status_code, 422, response.content)
        self.message.refresh_from_db()
        self.assertFalse(self.message.is_read)
        self.assertIsNone(self.message.read_at)

    def test_other_recipient_and_nonexistent_message_are_both_404(self):
        for message_id in (str(self.stranger.pk), str(uuid.uuid4()), "malformed-uuid"):
            with self.subTest(message_id=message_id):
                response = self.mark(message_id)
                self.assertEqual(response.status_code, 404, response.content)
                self.assertEqual(response.json()["error"]["code"], "not_found")
        self.stranger.refresh_from_db()
        self.assertFalse(self.stranger.is_read)

    def test_anonymous_patch_is_401(self):
        self.client.logout()
        self.assertEqual(self.mark().status_code, 401)

    def test_patch_enforces_csrf_then_accepts_valid_session_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        rejected = self.mark(client=client)
        self.assertEqual(rejected.status_code, 403, rejected.content)
        self.assertEqual(rejected.json()["error"]["code"], "csrf_failed")
        self.message.refresh_from_db()
        self.assertFalse(self.message.is_read)
        client.get("/api/v1/me")
        token = client.cookies["csrftoken"].value
        accepted = self.mark(client=client, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertTrue(accepted.json()["is_read"])

    def test_historical_message_keeps_its_original_version_target(self):
        make_version(self.task, self.supplier, number=2, filename="新版本.xlsx")
        response = self.client.get("/api/v1/messages")
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()["items"][0]
        self.assertEqual(row["version_id"], str(self.version.pk))
        target = urlsplit(row["target_url"])
        self.assertFalse(target.scheme)
        self.assertFalse(target.netloc)
        query = parse_qs(target.query)
        self.assertEqual(query.get("task"), [str(self.task.pk)])
        self.assertEqual(query.get("version"), [str(self.version.pk)])
