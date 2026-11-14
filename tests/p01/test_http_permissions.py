"""HTTP 级对象权限证据：401/403/404 都按 D3 第 6 节的 JSON 返回。"""
import uuid

from django.test import Client, override_settings

from .permission_fixture import PermissionFixture


@override_settings(
    ROOT_URLCONF="tests.p01.urls_for_permissions",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class HttpPermissionTests(PermissionFixture):
    def test_anonymous_gets_401_json(self):
        response = Client().get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_supplier_reads_own_task(self):
        client = Client()
        client.force_login(self.supplier_a)
        response = client.get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "任务甲")

    def test_supplier_cannot_read_other_task(self):
        client = Client()
        client.force_login(self.supplier_a)
        response = client.get(f"/probe/perm/tasks/{self.task_b.pk}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_tampered_id_gets_404_not_500(self):
        client = Client()
        client.force_login(self.supplier_a)
        for task_id in (uuid.uuid4(), "not-a-uuid"):
            with self.subTest(task_id=str(task_id)):
                response = client.get(f"/probe/perm/tasks/{task_id}")
                self.assertEqual(response.status_code, 404)

    def test_operator_approval_is_403(self):
        client = Client()
        client.force_login(self.operator_a)
        response = client.post(f"/probe/perm/submissions/{self.submission_a.pk}/decision")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_expert_approval_is_allowed(self):
        client = Client()
        client.force_login(self.expert)
        response = client.post(f"/probe/perm/submissions/{self.submission_a.pk}/decision")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["decided"])

    def test_operator_cannot_read_task_of_other_operator(self):
        client = Client()
        client.force_login(self.operator_b)
        response = client.get(f"/probe/perm/tasks/{self.task_a.pk}")
        self.assertEqual(response.status_code, 404)
