"""HTTP 级对象权限与 CSRF 证据：401/403/404 都按 D3 第 6 节的 JSON 返回。

验收口径（Jay 2026-11-14 补充意见，必须用带 CSRF 检查的客户端）：
1. 匿名访问受保护写接口 → 401；
2. 已登录但缺令牌 → 403 csrf_failed；
3. 业务员带有效令牌审批 → 403 forbidden（角色不允许，不是 CSRF 问题）；
4. 专家带有效令牌审批 → 200。

探针路由复用生产公共认证器 `session_auth`，所以“认证层统一校验 CSRF”是否真的
生效，由本文件决定，而不是由某两个视图各自调用决定。
"""
import json
import uuid

from django.test import Client, override_settings

from .factories import PASSWORD
from .permission_fixture import PermissionFixture


@override_settings(
    ROOT_URLCONF="tests.p01.urls_for_permissions",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class HttpPermissionTests(PermissionFixture):
    def client_for(self, user=None):
        client = Client(enforce_csrf_checks=True)
        if user is not None:
            client.force_login(user)
        return client

    def csrf_token(self, client, task=None):
        # 任意受保护 GET 都会种下 csrftoken（匿名返回 401 也会种）；登录后令牌轮换，需重新读取。
        client.get(f"/probe/perm/tasks/{(task or self.task_a).pk}")
        cookie = client.cookies.get("csrftoken")
        return cookie.value if cookie else ""

    def decide(self, client, submission=None, token=None):
        headers = {"HTTP_X_CSRFTOKEN": token} if token else {}
        return client.post(
            f"/probe/perm/submissions/{(submission or self.submission_a).pk}/decision", **headers,
        )

    # ---- GET：只有对象权限，不涉及 CSRF ----

    def test_anonymous_gets_401_json(self):
        response = self.client_for().get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_supplier_reads_own_task(self):
        response = self.client_for(self.supplier_a).get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "任务甲")

    def test_supplier_cannot_read_other_task(self):
        response = self.client_for(self.supplier_a).get(f"/probe/perm/tasks/{self.task_b.pk}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_tampered_id_gets_404_not_500(self):
        client = self.client_for(self.supplier_a)
        for task_id in (uuid.uuid4(), "not-a-uuid"):
            with self.subTest(task_id=str(task_id)):
                response = client.get(f"/probe/perm/tasks/{task_id}")
                self.assertEqual(response.status_code, 404)

    def test_operator_cannot_read_task_of_other_operator(self):
        response = self.client_for(self.operator_b).get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 404)

    # ---- POST：公共认证器的 CSRF + 角色矩阵 ----

    def test_anonymous_write_is_401_not_csrf_error(self):
        response = self.decide(self.client_for())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_logged_in_write_without_token_is_403_csrf_failed(self):
        client = self.client_for(self.expert)
        response = self.decide(client)  # 故意不带 X-CSRFToken
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")

    def test_operator_with_valid_token_is_still_403_forbidden(self):
        client = self.client_for(self.operator_a)
        response = self.decide(client, token=self.csrf_token(client))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_expert_with_valid_token_is_allowed(self):
        client = self.client_for(self.expert)
        response = self.decide(client, token=self.csrf_token(client))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["decided"])

    def test_stale_token_after_login_rotation_is_rejected(self):
        # 走真实登录端点：登录会轮换 CSRF 令牌，旧令牌必须失效。
        client = Client(enforce_csrf_checks=True)
        client.get("/api/v1/me")  # 401 同时种下初始 cookie
        stale = client.cookies["csrftoken"].value
        login = client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "expert", "password": PASSWORD}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=stale,
        )
        self.assertEqual(login.status_code, 200)
        fresh = client.cookies["csrftoken"].value
        self.assertNotEqual(stale, fresh)

        rejected = self.decide(client, token=stale)
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["error"]["code"], "csrf_failed")

        allowed = self.decide(client, token=fresh)
        self.assertEqual(allowed.status_code, 200)
