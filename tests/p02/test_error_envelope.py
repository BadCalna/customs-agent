"""M-12：Ninja 的 ValidationError 必须走统一 422 信封，前端才能拿到字段级错误。"""
import json

from django.test import Client, TestCase, override_settings


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ValidationEnvelopeTests(TestCase):
    def _client(self):
        return Client(enforce_csrf_checks=True)

    def _csrf_token(self, client):
        response = client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        return client.cookies["csrftoken"].value

    def test_missing_required_field_returns_422_envelope(self):
        client = self._client()
        token = self._csrf_token(client)
        response = client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "operator_a"}),   # 缺 password
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertIn("request_id", body)
        self.assertIn("errors", body["error"]["details"])
