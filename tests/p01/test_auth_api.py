"""P01 身份端点验收：401 JSON、登录/退出、session 与 CSRF 实测。"""
import json

from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role

from .factories import PASSWORD, make_user


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthApiTests(TestCase):
    def setUp(self):
        self.operator = make_user("operator_a", Role.OPERATOR, "业务员甲")
        self.roleless = make_user("local_admin", "")

    def _client(self):
        return Client(enforce_csrf_checks=True)

    def _csrf_token(self, client):
        # 未登录 GET /me 返回 401，同时种下 csrftoken cookie；不产生业务副作用。
        response = client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        return client.cookies["csrftoken"].value

    def _login(self, client, username, password):
        token = self._csrf_token(client)
        return client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": username, "password": password}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def test_anonymous_me_returns_401_json(self):
        response = self._client().get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertIn("request_id", body)
        self.assertNotIn("Location", response.headers)

    def test_login_without_csrf_token_is_rejected(self):
        response = self._client().post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "operator_a", "password": PASSWORD}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")

    def test_login_flow_then_me(self):
        client = self._client()
        response = self._login(client, "operator_a", PASSWORD)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["role"], Role.OPERATOR)
        self.assertEqual(body["role_label"], "业务员")
        self.assertEqual(body["display_name"], "业务员甲")

        me = client.get("/api/v1/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "operator_a")
        self.assertEqual(me.json()["id"], str(self.operator.pk))

    def test_wrong_password_is_401(self):
        response = self._login(self._client(), "operator_a", "wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

    def test_unknown_user_gets_same_error(self):
        response = self._login(self._client(), "no-such-user", PASSWORD)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")

    def test_roleless_account_cannot_login_business_api(self):
        response = self._login(self._client(), "local_admin", PASSWORD)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "role_missing")

    def test_logout_requires_csrf_and_ends_session(self):
        client = self._client()
        self.assertEqual(self._login(client, "operator_a", PASSWORD).status_code, 200)
        token = client.cookies["csrftoken"].value

        without_csrf = client.post("/api/v1/auth/logout")
        self.assertEqual(without_csrf.status_code, 403)

        response = client.post("/api/v1/auth/logout", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        # 退出后访问一律拒绝。
        self.assertEqual(client.get("/api/v1/me").status_code, 401)

    def test_openapi_lists_p01_endpoints(self):
        schema = self._client().get("/api/v1/openapi.json").json()
        for path in ("/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/me"):
            self.assertIn(path, schema["paths"])

    def test_p00_health_endpoint_still_works(self):
        response = Client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
