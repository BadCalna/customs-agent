"""只读端点的 HTTP 级验证：身份、越权、分页、无副作用与 501。

数据来自 seed-demo + seed-contracts，因此这里同时验证“真实 ORM 保存的 mock
结果能被读取”，不是只对序列化器做单测。
"""
from django.core.management import call_command
from django.test import Client, TestCase

from apps.accounts.models import User
from apps.reviews.management.commands.seed_contracts import MAIN_TITLE, RECODE_TITLE
from apps.reviews.models import Task
from apps.runs.models import Run


class ReadApiTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        call_command("seed_contracts", verbosity=0)
        cls.main = Task.objects.get(title=MAIN_TITLE)
        cls.recode = Task.objects.get(title=RECODE_TITLE)
        cls.v2 = cls.main.versions.get(number=2)
        cls.v1 = cls.main.versions.get(number=1)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def login(self, username="operator_a"):
        self.client.force_login(User.objects.get(username=username))
        self.client.get("/api/v1/me")  # 种下 csrftoken

    def csrf_headers(self):
        cookie = self.client.cookies.get("csrftoken")
        return {"HTTP_X_CSRFTOKEN": cookie.value} if cookie else {}

    def get(self, path, **extra):
        return self.client.get(path, **extra)

    def test_anonymous_is_401_json(self):
        for path in ("/api/v1/tasks", f"/api/v1/tasks/{self.main.pk}", f"/api/v1/versions/{self.v2.pk}/items"):
            response = self.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_operator_reads_tasks_and_detail(self):
        self.login()
        response = self.get("/api/v1/tasks")
        self.assertEqual(response.status_code, 200)
        titles = {entry["title"] for entry in response.json()["items"]}
        self.assertIn(MAIN_TITLE, titles)
        self.assertIn(RECODE_TITLE, titles)

        detail = self.get(f"/api/v1/tasks/{self.main.pk}").json()
        self.assertEqual(detail["current_version_number"], 2)
        self.assertEqual(len(detail["versions"]), 2)
        self.assertIn("submission.create", detail["allowed_actions"])
        self.assertEqual(detail["result_mode"], "mock")

    def test_items_cover_six_comparison_values(self):
        self.login()
        response = self.get(f"/api/v1/versions/{self.v2.pk}/items?limit=50")
        rows = {entry["source_location"]["row_number"]: entry for entry in response.json()["items"]}
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[5]["comparison_status"], "consistent")
        self.assertEqual(rows[6]["comparison_status"], "inconsistent")
        self.assertEqual(rows[7]["comparison_status"], "needs_confirmation")
        self.assertEqual(rows[9]["comparison_status"], "supplier_missing")
        self.assertEqual(rows[10]["comparison_status"], "supplier_invalid")
        self.assertEqual(rows[12]["comparison_status"], "needs_confirmation")
        self.assertEqual(rows[9]["supplier_hs_raw"], "")

    def test_card_keeps_null_stage_with_run_reference(self):
        self.login()
        item = self.v2.items.get(source_row=7)
        card = self.get(f"/api/v1/versions/{self.v2.pk}/items/{item.pk}").json()
        self.assertIsNone(card["precheck_suggested_hs"]["value"])
        self.assertIsNotNone(card["precheck_suggested_hs"]["source_id"])
        self.assertIsNone(card["business_confirmed_hs"]["value"])
        self.assertIsNone(card["business_confirmed_hs"]["source_id"])
        self.assertEqual([issue["code"] for issue in card["issues"]], ["MISSING_USAGE"])

    def test_run_progress_and_failed_row(self):
        self.login()
        run = Run.objects.get(version=self.v2, kind="precheck", is_current=True)
        payload = self.get(f"/api/v1/runs/{run.pk}").json()
        self.assertEqual(payload["progress"], {"total": 8, "queued": 0, "running": 0, "succeeded": 7, "failed": 1})
        failed = [row for row in payload["items"] if row["status"] == "failed"]
        self.assertTrue(failed[0]["error_message"])

    def test_current_run_is_per_version(self):
        self.login()
        current = Run.objects.filter(task=self.main, kind="precheck", is_current=True)
        self.assertEqual({run.version_id for run in current}, {self.v1.pk, self.v2.pk})

    def test_recode_card_exposes_target_and_final(self):
        self.login()
        version = self.recode.versions.get(number=1)
        item = version.items.get()
        card = self.get(f"/api/v1/versions/{version.pk}/items/{item.pk}").json()
        self.assertEqual(card["latest_expert_check"]["target_hs"], card["submitted_hs"]["value"])
        self.assertNotEqual(card["expert_final_hs"]["value"], card["submitted_hs"]["value"])
        self.assertEqual(card["task_stage"], "approved")

    def test_supplier_b_gets_404_not_data(self):
        self.login("supplier_b")
        for path in (
            f"/api/v1/tasks/{self.main.pk}",
            f"/api/v1/versions/{self.v2.pk}/items",
            f"/api/v1/runs/{Run.objects.get(version=self.v2).pk}",
        ):
            response = self.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        response = self.get("/api/v1/tasks")
        self.assertEqual([entry["title"] for entry in response.json()["items"]], ["演示任务-供应商乙"])

    def test_invalid_uuid_is_404_not_500(self):
        self.login()
        run = Run.objects.get(version=self.v2)
        paths = (
            "/api/v1/tasks/not-a-uuid",
            "/api/v1/versions/not-a-uuid/items",
            f"/api/v1/versions/{self.v2.pk}/items/not-a-uuid",
            "/api/v1/runs/not-a-uuid",
            f"/api/v1/tasks/{self.main.pk}",
        )
        for path in paths[:-1]:
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "not_found")
        # 非法游标保持 400，不与 404 混淆
        self.assertEqual(self.get("/api/v1/tasks?cursor=not-base64").status_code, 400)

    def test_tampered_cursor_is_400_not_500(self):
        import base64
        import json

        def cursor(payload):
            return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        self.login()
        bad_task_cursors = (
            {"created_at": 123, "id": "x"},
            {"created_at": "2026-01-01T00:00:00+08:00", "id": 123},
            {"created_at": "not-a-time", "id": "00000000-0000-0000-0000-000000000000"},
            {"created_at": "2026-01-01T00:00:00+08:00", "id": "not-a-uuid"},
        )
        for payload in bad_task_cursors:
            with self.subTest(kind="tasks", payload=payload):
                response = self.get(f"/api/v1/tasks?cursor={cursor(payload)}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_cursor")
        bad_item_cursors = (
            {"sheet": "申报要素", "row": "x"},
            {"sheet": 1, "row": 1},
            {"sheet": "申报要素", "row": 0},
        )
        for payload in bad_item_cursors:
            with self.subTest(kind="items", payload=payload):
                response = self.get(f"/api/v1/versions/{self.v2.pk}/items?cursor={cursor(payload)}")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_cursor")

    def test_pagination_and_bad_params(self):
        self.login()
        first = self.get("/api/v1/tasks?limit=2").json()
        self.assertEqual(len(first["items"]), 2)
        self.assertTrue(first["next_cursor"])
        second = self.get(f"/api/v1/tasks?limit=2&cursor={first['next_cursor']}").json()
        self.assertTrue({i["task_id"] for i in first["items"]}.isdisjoint({i["task_id"] for i in second["items"]}))
        self.assertEqual(self.get("/api/v1/tasks?limit=999").status_code, 400)
        self.assertEqual(self.get("/api/v1/tasks?stage=nonsense").status_code, 400)
        self.assertEqual(self.get("/api/v1/tasks?cursor=not-base64").status_code, 400)

    def test_get_has_no_side_effects(self):
        self.login()
        run_count = Run.objects.count()
        revision = self.main.revision
        self.get("/api/v1/tasks")
        self.get(f"/api/v1/versions/{self.v2.pk}/items?limit=50")
        self.get(f"/api/v1/runs/{Run.objects.get(version=self.v2).pk}")
        self.assertEqual(Run.objects.count(), run_count)
        self.assertEqual(Task.objects.get(pk=self.main.pk).revision, revision)

    def test_delivered_messages_and_upload_replace_pending_endpoints(self):
        self.login()
        # P09（确认/提交）与 P10（决定/退回/历史）已交付，改由 tests/p09、tests/p10 覆盖；
        # 这里只锁仍然待交付的端点，避免把已实现端点当成占位。
        response = self.get("/api/v1/messages")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "next_cursor": None, "unread_count": 0})

        response = self.client.post("/api/v1/tasks", {}, content_type="application/json", **self.csrf_headers())
        # P06 已接管上传；缺少 multipart 文件是结构化校验失败。
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_write_without_csrf_is_403_not_501(self):
        self.login()
        response = self.client.post("/api/v1/tasks", {}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")

    def test_anonymous_write_is_401(self):
        response = self.client.post("/api/v1/tasks", {}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
