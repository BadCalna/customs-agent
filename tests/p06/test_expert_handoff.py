"""PR #22 回归：真实上传/认领/确认/提交后，专家自动取得该任务与文件权限。"""
import json
import tempfile
import uuid
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role, TaskAssignment
from tests.p01.factories import make_user
from tests.p06.samples import sample


@override_settings(CUSTOMS_DEFAULT_EXPERT_USERNAME="expert", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UploadedExpertHandoffTests(TestCase):
    def test_uploaded_task_submission_is_visible_without_manual_expert_assignment(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        actors = {}
        for name, role in (("supplier_a", Role.SUPPLIER), ("operator_a", Role.OPERATOR),
                           ("expert", Role.EXPERT), ("expert_b", Role.EXPERT)):
            user = make_user(name, role)
            client = Client(enforce_csrf_checks=True, SERVER_NAME="localhost")
            client.force_login(user)
            client.get("/api/v1/me")
            actors[name] = client

        def call(name, method, path, body=None):
            client = actors[name]
            response = client.generic(method, path, data=json.dumps(body) if body is not None else "",
                                      content_type="application/json", HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
                                      HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            self.assertEqual(response.status_code, 200, response.content[:1000])
            return response.json()

        with override_settings(CUSTOMS_FILE_ROOT=Path(root.name)):
            supplier = actors["supplier_a"]
            data = sample()
            uploaded = supplier.post("/api/v1/tasks", {"file": SimpleUploadedFile("synthetic.xlsx", data)},
                                     HTTP_X_CSRFTOKEN=supplier.cookies["csrftoken"].value,
                                     HTTP_IDEMPOTENCY_KEY="new-upload")
            self.assertEqual(uploaded.status_code, 200, uploaded.content)
            result = uploaded.json()
            tid, vid = result["task_id"], result["version_id"]
            self.assertFalse(TaskAssignment.objects.filter(task_id=tid, role=Role.EXPERT).exists())
            self.assertEqual(actors["expert"].get(f"/api/v1/tasks/{tid}").status_code, 404)
            call("operator_a", "POST", f"/api/v1/tasks/{tid}/claim")
            rows = call("operator_a", "GET", f"/api/v1/versions/{vid}/items")["items"]
            confirmations = []
            for row in rows:
                confirmation = call("operator_a", "PATCH", f"/api/v1/versions/{vid}/items/{row['item_id']}/confirmation",
                                    {"confirmed_hs": "8544421900", "expected_revision": row["revision"], "reason": "仅权限回归测试"})
                confirmations.append({"item_id": row["item_id"], "confirmation_id": confirmation["confirmation_id"]})
            task = call("operator_a", "GET", f"/api/v1/tasks/{tid}")
            with self.captureOnCommitCallbacks(execute=True):
                submitted = call("operator_a", "POST", f"/api/v1/tasks/{tid}/submissions",
                                 {"version_id": vid, "revision": task["revision"], "items": confirmations})
            self.assertEqual(submitted["status"], "pending")
            self.assertEqual(TaskAssignment.objects.filter(task_id=tid, role=Role.EXPERT).count(), 1)
            call("expert", "GET", f"/api/v1/tasks/{tid}")
            call("expert", "GET", f"/api/v1/tasks/{tid}/history")
            report = call("expert", "GET", f"/api/v1/versions/{vid}/import")
            self.assertEqual(actors["expert"].get(report["original_download_url"]).content, data)
            image_count = 0
            for row in rows:
                card = call("expert", "GET", f"/api/v1/versions/{vid}/items/{row['item_id']}")
                for image in card["images"]:
                    if image["download_url"]:
                        self.assertEqual(actors["expert"].get(image["download_url"]).status_code, 200)
                        image_count += 1
            self.assertEqual(image_count, 1)
            self.assertEqual(actors["expert_b"].get(f"/api/v1/tasks/{tid}").status_code, 404)
