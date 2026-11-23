"""超管（is_superuser、空 role）登录与只读全览的回归验收。

契约：超管是独立超级身份，不占用业务角色——
- 能登录业务接口并进入工作台（登录端点 / 会话判定 / 可见范围三处放行）；
- 可见全部任务，不按 supplier/assignment 过滤；
- 动作仍走角色矩阵：空 role ⇒ 只读，写动作一律 403。
普通空 role 账号（含 is_staff）不受影响，仍被拒。
"""
import json

from django.core.exceptions import PermissionDenied
from django.test import Client, override_settings

from apps.accounts import permissions
from apps.accounts.models import Role
from apps.reviews.models import Confirmation
from apps.reviews.serializers import allowed_actions

from .factories import PASSWORD, make_user
from .permission_fixture import PermissionFixture


def make_superuser(username="superadmin"):
    user = make_user(username, "", "超级管理员")
    user.is_staff = True
    user.is_superuser = True
    user.save()
    return user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SuperuserLoginTests(PermissionFixture):
    def setUp(self):
        super().setUp()
        self.superuser = make_superuser()

    def _login(self, client, username, password):
        # 未登录 GET /me 返回 401，同时种下 csrftoken cookie；不产生业务副作用。
        self.assertEqual(client.get("/api/v1/me").status_code, 401)
        token = client.cookies["csrftoken"].value
        return client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": username, "password": password}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def test_superuser_can_login_without_business_role(self):
        client = Client(enforce_csrf_checks=True)
        response = self._login(client, "superadmin", PASSWORD)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["username"], "superadmin")
        self.assertEqual(body["role"], "")
        self.assertIs(body["is_readonly"], True)
        me = client.get("/api/v1/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json(), body)

    def test_superuser_with_business_role_keeps_assigned_scope(self):
        operator = self.operator_a
        operator.is_superuser = True
        operator.save(update_fields=["is_superuser"])
        response = self._login(self.client, operator.username, PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], Role.OPERATOR)
        self.assertIs(response.json()["is_readonly"], False)
        tasks = self.client.get("/api/v1/tasks")
        self.assertEqual(tasks.status_code, 200)
        self.assertEqual([row["task_id"] for row in tasks.json()["items"]], [str(self.task_a.pk)])
        self.assertEqual(self.client.get(f"/api/v1/tasks/{self.task_b.pk}").status_code, 404)

    def test_readonly_session_cannot_confirm_with_valid_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self._login(client, self.superuser.username, PASSWORD).status_code, 200)
        item = self.item_b
        version = self.version_b
        path = f"/api/v1/versions/{version.pk}/items/{item.pk}"
        self.assertEqual(client.get(path).status_code, 200)
        revision = item.revision
        response = client.patch(
            f"{path}/confirmation",
            data=json.dumps({"confirmed_hs": "8544422900", "expected_revision": revision}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")
        item.refresh_from_db()
        self.assertEqual(item.revision, revision)
        self.assertFalse(Confirmation.objects.filter(item=item).exists())

    def test_staff_flag_alone_does_not_open_business_login(self):
        staff = make_user("staff_only", "")
        staff.is_staff = True
        staff.save()
        response = self._login(Client(enforce_csrf_checks=True), "staff_only", PASSWORD)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "role_missing")

    def test_superuser_session_opens_workbench_entry(self):
        self.client.force_login(self.superuser)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/app/assets/production.js", b"".join(page.streaming_content))
        self.assertRedirects(self.client.get("/login/"), "/", fetch_redirect_response=False)

    def test_staff_only_session_still_redirected_to_login(self):
        staff = make_user("staff_only", "")
        staff.is_staff = True
        staff.save()
        self.client.force_login(staff)
        self.assertRedirects(self.client.get("/"), "/login/")


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SuperuserVisibilityTests(PermissionFixture):
    def setUp(self):
        super().setUp()
        self.superuser = make_superuser()

    def test_superuser_sees_all_tasks_without_assignments(self):
        self.assertEqual(
            set(permissions.visible_tasks(self.superuser)),
            {self.task_a, self.task_b},
        )
        for task in (self.task_a, self.task_b):
            self.assertEqual(permissions.get_visible_task(self.superuser, task.pk), task)

    def test_superuser_reads_every_business_object(self):
        for obj in (self.version_a, self.item_a, self.run_a, self.confirmation_a,
                    self.submission_a, self.decision_a, self.version_b, self.item_b, self.run_b):
            with self.subTest(model=obj._meta.object_name):
                permissions.require_action(self.superuser, permissions.READ, obj)

    def test_task_list_api_returns_all_tasks(self):
        self.client.force_login(self.superuser)
        response = self.client.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 200)
        ids = {row["task_id"] for row in response.json()["items"]}
        self.assertEqual(ids, {str(self.task_a.pk), str(self.task_b.pk)})

    def test_superuser_write_actions_are_denied(self):
        for action, obj in (
            (permissions.UPLOAD_VERSION, self.task_a),
            (permissions.CONFIRM, self.item_a),
            (permissions.SUBMIT, self.submission_a),
            (permissions.DECIDE, self.submission_a),
            (permissions.RETURN_TASK, self.task_a),
        ):
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                permissions.require_action(self.superuser, action, obj)

    def test_superuser_has_no_display_actions(self):
        self.assertEqual(allowed_actions(self.superuser), [])
