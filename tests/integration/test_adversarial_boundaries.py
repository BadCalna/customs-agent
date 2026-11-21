"""P21 independent HTTP/storage/queue boundary regression (synthetic, never gold).

Every business setup starts with a real multipart upload and authenticated HTTP
session. Queue races replay the actual claim/begin/evaluate/publish protocol;
they are deterministic fencing checks, not evidence of multi-process delivery.
Only the model/transport fault boundary is substituted where explicitly stated.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role
from apps.notifications.models import Message
from apps.reviews.models import Confirmation, Decision, FileVersion, Item, ItemImage, Submission, Task
from apps.runs import services
from apps.runs.jobs import perform_work
from apps.runs.models import Run, RunAttempt
from tests.p01.factories import PASSWORD, make_user
from tests.p06.samples import sample


@override_settings(
    ENGINE_MODE="mock", EXECUTOR_MODE="inline", NOTIFICATION_MODE="mock",
    CUSTOMS_DEFAULT_EXPERT_USERNAME="p21-boundary-expert",
    CUSTOMS_HISTORY_REFERENCE_ENABLED=False,
    CUSTOMS_RETRY_BACKOFF_SECONDS=0,
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class AdversarialBoundaryTests(TestCase):
    """A05/A07/A08/A09/A15/A18 and T01/T02/T04/T07 boundaries."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p21-boundaries-")
        self.addCleanup(temporary.cleanup)
        self.file_root = Path(temporary.name) / "files"
        storage_settings = override_settings(CUSTOMS_FILE_ROOT=self.file_root)
        storage_settings.enable()
        self.addCleanup(storage_settings.disable)
        self.users = {
            "supplier": make_user("p21-boundary-supplier", Role.SUPPLIER),
            "operator": make_user("p21-boundary-operator", Role.OPERATOR),
            "expert": make_user("p21-boundary-expert", Role.EXPERT),
            "other_supplier": make_user("p21-boundary-outsider-supplier", Role.SUPPLIER),
            "other_operator": make_user("p21-boundary-outsider-operator", Role.OPERATOR),
            "other_expert": make_user("p21-boundary-outsider-expert", Role.EXPERT),
        }
        self.sessions = {role: self.login(user.username) for role, user in self.users.items()}
        self.source = sample()

    def login(self, username):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get("/api/v1/me").status_code, 401)
        response = client.post(
            "/api/v1/auth/login", data=json.dumps({"username": username, "password": PASSWORD}),
            content_type="application/json", HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(client.get("/api/v1/me").json()["username"], username)
        return client

    def request(self, role, method, path, payload=None, *, status=200, key=None):
        client = self.sessions[role]
        with self.captureOnCommitCallbacks(execute=settings.EXECUTOR_MODE == "inline"):
            response = client.generic(
                method, path, data=json.dumps(payload or {}), content_type="application/json",
                HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
                HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
            )
        self.assertEqual(response.status_code, status, (method, path, response.content[:1000]))
        return response

    def upload(self, *, source=None, key=None, task=None, previous=None, status=200):
        client = self.sessions["supplier"]
        body = {"file": SimpleUploadedFile("p21-boundary.xlsx", self.source if source is None else source)}
        path = "/api/v1/tasks"
        if task is not None:
            path = f"/api/v1/tasks/{task}/versions"
            body["expected_current_version_id"] = previous
        with self.captureOnCommitCallbacks(execute=settings.EXECUTOR_MODE == "inline"):
            response = client.post(path, body, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
                                   HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))
        self.assertEqual(response.status_code, status, response.content[:1000])
        return response.json()

    def claimed_upload(self):
        uploaded = self.upload()
        self.request("operator", "POST", f"/api/v1/tasks/{uploaded['task_id']}/claim")
        return uploaded

    def items(self, uploaded, role="operator"):
        response = self.sessions[role].get(f"/api/v1/versions/{uploaded['version_id']}/items")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["items"]

    def confirm(self, uploaded, item, *, hs="8544422900", expected=None, status=200):
        return self.request("operator", "PATCH",
                            f"/api/v1/versions/{uploaded['version_id']}/items/{item['item_id']}/confirmation",
                            {"confirmed_hs": hs, "reason": "P21 合成边界验收",
                             "expected_revision": item["revision"] if expected is None else expected},
                            status=status)

    def submission_payload(self, uploaded):
        lines = []
        for item in self.items(uploaded):
            confirmed = self.confirm(uploaded, item).json()
            lines.append({"item_id": item["item_id"], "confirmation_id": confirmed["confirmation_id"]})
        task = self.sessions["operator"].get(f"/api/v1/tasks/{uploaded['task_id']}").json()
        return {"version_id": uploaded["version_id"], "revision": task["revision"], "items": lines}

    def submit(self, uploaded, payload=None, *, key=None, status=200):
        return self.request("operator", "POST", f"/api/v1/tasks/{uploaded['task_id']}/submissions",
                            self.submission_payload(uploaded) if payload is None else payload,
                            key=key, status=status)

    def rerun(self, uploaded):
        task = self.sessions["operator"].get(f"/api/v1/tasks/{uploaded['task_id']}").json()
        created = self.request("operator", "POST", f"/api/v1/versions/{uploaded['version_id']}/prechecks",
                               {"revision": task["revision"]}).json()
        return Run.objects.get(pk=created["run_id"])

    def assert_error(self, response, code):
        self.assertEqual(response.json()["error"]["code"], code, response.content)

    def drain(self, run):
        for _ in range(4):
            perform_work(str(run.pk))
        run.refresh_from_db()

    def test_login_upload_and_logout_enforce_real_session_and_csrf(self):
        """A18: CSRF is checked at login, multipart upload and logout."""
        anonymous = Client(enforce_csrf_checks=True)
        denied = anonymous.post("/api/v1/auth/login", data=json.dumps({
            "username": self.users["supplier"].username, "password": PASSWORD,
        }), content_type="application/json")
        self.assertEqual(denied.status_code, 403)
        self.assert_error(denied, "csrf_failed")
        supplier = self.sessions["supplier"]
        denied = supplier.post("/api/v1/tasks", {"file": SimpleUploadedFile("x.xlsx", self.source)},
                               HTTP_IDEMPOTENCY_KEY="no-csrf")
        self.assertEqual(denied.status_code, 403)
        self.assert_error(denied, "csrf_failed")
        self.assertFalse(Task.objects.exists())
        uploaded = self.upload()
        self.assertEqual(supplier.get(uploaded["original_download_url"]).content, self.source)
        self.assertEqual(supplier.post("/api/v1/auth/logout").status_code, 403)
        self.request("supplier", "POST", "/api/v1/auth/logout")
        for path in (f"/api/v1/tasks/{uploaded['task_id']}", uploaded["original_download_url"],
                     f"/api/v1/runs/{uploaded['precheck_run_id']}", "/api/v1/messages"):
            self.assertEqual(supplier.get(path).status_code, 401, path)

    def test_object_scope_applies_to_task_version_run_original_image_and_messages(self):
        """A18/T02/T07: possession of UUIDs grants neither reads nor writes."""
        uploaded = self.claimed_upload()
        item = self.items(uploaded)[0]
        image = ItemImage.objects.get(item__version_id=uploaded["version_id"], status="available")
        paths = (
            f"/api/v1/tasks/{uploaded['task_id']}",
            f"/api/v1/tasks/{uploaded['task_id']}/history",
            f"/api/v1/versions/{uploaded['version_id']}/import",
            f"/api/v1/versions/{uploaded['version_id']}/items",
            f"/api/v1/versions/{uploaded['version_id']}/items/{item['item_id']}",
            f"/api/v1/runs/{uploaded['precheck_run_id']}",
            uploaded["original_download_url"], f"/api/v1/files/{image.pk}",
        )
        for role in ("other_supplier", "other_operator", "other_expert", "expert"):
            for path in paths:
                with self.subTest(role=role, path=path):
                    response = self.sessions[role].get(path)
                    self.assertEqual(response.status_code, 404, response.content[:500])
                    self.assertNotIn("p21-boundary.xlsx", response.content.decode())
        confirmation_path = f"/api/v1/versions/{uploaded['version_id']}/items/{item['item_id']}/confirmation"
        body = {"confirmed_hs": "8544422900", "expected_revision": item["revision"]}
        self.request("other_operator", "PATCH", confirmation_path, body, status=404)
        self.request("supplier", "PATCH", confirmation_path, body, status=403)
        submitted = self.submit(uploaded).json()
        # Submission grants the configured expert exactly this task, not every expert.
        for path in paths:
            self.assertEqual(self.sessions["expert"].get(path).status_code, 200, path)
            self.assertEqual(self.sessions["other_expert"].get(path).status_code, 404, path)
        own_messages = self.sessions["expert"].get("/api/v1/messages").json()
        self.assertTrue(own_messages["items"])
        own_id = own_messages["items"][0]["message_id"]
        for role in ("other_supplier", "other_operator", "other_expert"):
            self.assertEqual(self.sessions[role].get("/api/v1/messages").json()["items"], [])
            self.request(role, "PATCH", f"/api/v1/messages/{own_id}", {"is_read": True}, status=404)
        self.assertFalse(Message.objects.get(pk=own_id).is_read)
        self.request("expert", "PATCH", f"/api/v1/messages/{own_id}", {"is_read": True})
        self.assertTrue(Message.objects.get(pk=own_id).is_read)
        self.assertEqual(Submission.objects.get(pk=submitted["submission_id"]).status, "pending")
        self.assertFalse(Decision.objects.exists())

    def test_multipart_idempotency_replay_and_conflict_preserve_single_version_and_original(self):
        """A15/T01: retry is one upload, conflicting bytes are rejected."""
        uploaded = self.upload(key="p21-upload-replay")
        replay = self.upload(key="p21-upload-replay")
        self.assertTrue(replay["idempotent_replay"])
        for field in ("task_id", "version_id", "precheck_run_id"):
            self.assertEqual(replay[field], uploaded[field])
        conflict = self.upload(source=sample(order=(3, 2, 1)), key="p21-upload-replay", status=409)
        self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
        self.assertEqual((Task.objects.count(), FileVersion.objects.count(), Run.objects.count()), (1, 1, 1))
        original = self.sessions["supplier"].get(uploaded["original_download_url"])
        self.assertEqual(hashlib.sha256(original.content).hexdigest(), hashlib.sha256(self.source).hexdigest())
        self.assertEqual(Run.objects.get(pk=uploaded["precheck_run_id"]).items.count(), 3)

    def test_submission_replay_and_conflict_cannot_duplicate_snapshot_or_edit_pending_input(self):
        """A15/T02: a repeated HTTP submission has one immutable snapshot/run."""
        uploaded = self.claimed_upload()
        body = self.submission_payload(uploaded)
        first = self.submit(uploaded, body, key="p21-submit-replay").json()
        replay = self.submit(uploaded, body, key="p21-submit-replay").json()
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["submission_id"], first["submission_id"])
        self.assertEqual(replay["expert_run_id"], first["expert_run_id"])
        changed = deepcopy(body)
        changed["revision"] += 1
        self.assert_error(self.submit(uploaded, changed, key="p21-submit-replay", status=409),
                          "idempotency_conflict")
        item = self.items(uploaded)[0]
        self.assert_error(self.confirm(uploaded, item, hs="8536901900", status=409), "submission_pending")
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(Run.objects.filter(kind=Run.Kind.EXPERT_CHECK).count(), 1)
        snapshot = Submission.objects.get(pk=first["submission_id"])
        self.assertEqual(set(snapshot.items.values_list("submitted_hs", flat=True)), {"8544422900"})
        self.assertEqual({row.result["target_hs"] for row in
                          Run.objects.get(pk=first["expert_run_id"]).items.all()}, {"8544422900"})
        self.assertEqual(Confirmation.objects.count(), 3)
        self.assertFalse(Decision.objects.exists())

    def test_stale_revision_and_cross_version_identifiers_cannot_write_or_export(self):
        """A15: cross-version identities are rejected before touching results."""
        uploaded = self.claimed_upload()
        first_item = self.items(uploaded)[0]
        self.confirm(uploaded, first_item)
        self.assert_error(self.confirm(uploaded, first_item, hs="8536901900", status=409), "stale_revision")
        stale = self.submission_payload(uploaded)
        stale["revision"] = 999
        self.assert_error(self.submit(uploaded, stale, status=409), "stale_revision")
        newer = self.upload(source=sample(order=(3, 1, 2)), task=uploaded["task_id"], previous=uploaded["version_id"])
        current_item = self.items(newer)[0]
        self.request("operator", "PATCH",
                     f"/api/v1/versions/{newer['version_id']}/items/{first_item['item_id']}/confirmation",
                     {"confirmed_hs": "8544422900", "expected_revision": 1}, status=404)
        self.assert_error(self.confirm(uploaded, first_item, expected=2, hs="8536901900", status=409), "stale_version")
        for target, run_id in ((newer, uploaded["precheck_run_id"]), (uploaded, newer["precheck_run_id"])):
            response = self.request("operator", "POST", f"/api/v1/versions/{target['version_id']}/exports",
                                    {"kind": "issues", "run_id": run_id}, status=422)
            self.assert_error(response, "result_reference_mismatch")
        # An unrelated task is also rejected, rather than inferred from a valid Run UUID.
        foreign = self.upload(source=sample(blank_raw=True))
        response = self.request("operator", "POST", f"/api/v1/versions/{newer['version_id']}/exports",
                                {"kind": "issues", "run_id": foreign["precheck_run_id"]}, status=422)
        self.assert_error(response, "result_reference_mismatch")
        self.assertIsNone(self.sessions["operator"].get(
            f"/api/v1/versions/{newer['version_id']}/items/{current_item['item_id']}"
        ).json()["business_confirmed_hs"]["value"])
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(Confirmation.objects.count(), 3)

    def test_real_import_keeps_absent_broken_and_available_images_distinct_after_reorder(self):
        """A07/A16: reordered rows retain their own image state and storage."""
        uploaded = self.claimed_upload()
        old_images = dict(ItemImage.objects.filter(item__version_id=uploaded["version_id"])
                          .values_list("item__name", "status"))
        self.assertEqual(old_images, {"合成部件1": "available", "合成部件2": "absent", "合成部件3": "read_failed"})
        newer = self.upload(source=sample(order=(3, 2, 1)), task=uploaded["task_id"], previous=uploaded["version_id"])
        for item in self.items(newer):
            card = self.sessions["operator"].get(
                f"/api/v1/versions/{newer['version_id']}/items/{item['item_id']}"
            ).json()
            self.assertEqual(card["image_status"], old_images[item["name"]])
            self.assertEqual(len(card["images"]), 1)
            image = card["images"][0]
            if image["status"] == "available":
                response = self.sessions["operator"].get(image["download_url"])
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(b"\x89PNG"))
                stored = ItemImage.objects.get(pk=image["image_id"])
                self.assertIn(newer["version_id"], stored.storage_path)
                self.assertNotIn(uploaded["version_id"], stored.storage_path)
            else:
                self.assertIsNone(image["download_url"])
                if image["status"] == "read_failed":
                    self.assertTrue(image["error_message"])
        self.assertEqual(dict(ItemImage.objects.filter(item__version_id=uploaded["version_id"])
                              .values_list("item__name", "status")), old_images)

    @override_settings(ENGINE_MODE="openai", CUSTOMS_AI_API_BASE="https://p21-model.example.invalid/v1",
                       CUSTOMS_AI_API_KEY="synthetic-transport-only", CUSTOMS_AI_MODEL="p21-test-model")
    def test_model_transport_without_sources_cannot_manufacture_verified_evidence(self):
        """A05/A08: real adapter parsing with a scripted transport, no model call."""
        content = {"suggested_hs": "8544422900", "candidates": [{"hs": "8544422900", "reason": "合成候选"}],
                   "gaps": []}
        envelope = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode()
        with patch("adapters.engines.openai.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(envelope)) as transport:
            uploaded = self.claimed_upload()
        self.assertEqual(transport.call_count, 3)
        for item in self.items(uploaded):
            card = self.sessions["operator"].get(
                f"/api/v1/versions/{uploaded['version_id']}/items/{item['item_id']}"
            ).json()
            self.assertEqual(card["evidence_status"], "version_unverified")
            self.assertEqual(card["evidence"], [])
            self.assertIsNone(card["confidence"])
            self.assertEqual(card["comparison_status"], "needs_confirmation")
            self.assertIsNone(card["business_confirmed_hs"]["value"])
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Decision.objects.exists())

    def test_new_round_fences_an_already_evaluated_old_attempt(self):
        """A15/T04: a late old result cannot become the current same-version result."""
        uploaded = self.claimed_upload()
        baseline = deepcopy(list(Run.objects.get(pk=uploaded["precheck_run_id"]).items.values("id", "result")))
        with override_settings(EXECUTOR_MODE="rq"):
            old = self.rerun(uploaded)
            item = Item.objects.filter(version_id=uploaded["version_id"]).first()
            token = services.claim_work(str(old.pk))
            self.assertIsNotNone(token)
            attempt = services.begin_attempt(str(old.pk), str(item.pk), token)
            self.assertIsNotNone(attempt)
            delayed = services.evaluate_row(old, item)
            current = self.rerun(uploaded)
            self.assertFalse(services.finish_attempt(attempt.pk, token, output=delayed))
            old.refresh_from_db()
            attempt.refresh_from_db()
            self.assertEqual(old.status, "superseded")
            self.assertFalse(old.is_current)
            self.assertEqual(attempt.status, "superseded")
            self.assertFalse(current.items.exclude(status="queued").exists())
            self.drain(current)
            self.assertEqual(current.status, "succeeded")
            self.assertFalse(services.finish_attempt(attempt.pk, token, output=delayed))
            detail = self.sessions["operator"].get(f"/api/v1/tasks/{uploaded['task_id']}").json()
            self.assertEqual(detail["latest_run_id"], str(current.pk))
            self.assertEqual(Run.objects.filter(version_id=uploaded["version_id"], kind="precheck", is_current=True).count(), 1)
        self.assertEqual(list(Run.objects.get(pk=uploaded["precheck_run_id"]).items.values("id", "result")), baseline)

    def test_new_file_version_fences_older_worker_before_publication(self):
        """A12/A15/T04: a V1 worker cannot publish after a V2 upload."""
        uploaded = self.claimed_upload()
        with override_settings(EXECUTOR_MODE="rq"):
            old = self.rerun(uploaded)
            old_item = Item.objects.filter(version_id=uploaded["version_id"]).first()
            token = services.claim_work(str(old.pk))
            self.assertIsNotNone(token)
            attempt = services.begin_attempt(str(old.pk), str(old_item.pk), token)
            self.assertIsNotNone(attempt)
            delayed = services.evaluate_row(old, old_item)
            newer = self.upload(source=sample(order=(3, 1, 2)), task=uploaded["task_id"], previous=uploaded["version_id"])
            self.assertFalse(services.finish_attempt(attempt.pk, token, output=delayed))
            current = Run.objects.get(pk=newer["precheck_run_id"])
            self.drain(current)
            self.assertEqual(current.status, "succeeded")
            self.assertEqual(current.items.count(), 3)
            self.assertNotIn(old_item.pk, current.items.values_list("item_id", flat=True))
            self.assertEqual(set(current.items.values_list("status", flat=True)), {"succeeded"})
            for item in self.items(newer):
                self.assertEqual(item["latest_run_id"], str(current.pk))
                self.assertIsNone(item["business_confirmed_hs"])
            self.assertEqual(self.sessions["supplier"].get(uploaded["original_download_url"]).content, self.source)
            old.refresh_from_db()
            self.assertEqual(old.status, "superseded")

    def test_bounded_row_failure_retry_preserves_success_and_all_old_attempts(self):
        """A09/A15/T04: only explicit failures rerun; prior successes stay immutable."""
        uploaded = self.claimed_upload()
        with override_settings(EXECUTOR_MODE="rq"):
            parent = self.rerun(uploaded)
            failed_item = Item.objects.filter(version_id=uploaded["version_id"]).first()
            evaluate = services.evaluate_row

            def timeout_one(run, item):
                if item.pk == failed_item.pk:
                    raise TimeoutError("P21 injected model timeout")
                return evaluate(run, item)

            with patch.object(services, "evaluate_row", side_effect=timeout_one):
                self.drain(parent)
            self.assertEqual(parent.status, "failed")
            self.assertEqual(parent.items.filter(status="succeeded").count(), 2)
            self.assertEqual(RunAttempt.objects.filter(row__run=parent, row__item=failed_item, status="failed").count(), 3)
            old_success = list(parent.items.filter(status="succeeded").values("item_id", "result", "suggested_hs"))
            detail = self.sessions["operator"].get(f"/api/v1/runs/{parent.pk}").json()
            self.assertEqual(detail["retry_item_ids"], [str(failed_item.pk)])
            successful_item = parent.items.filter(status="succeeded").first().item_id
            denied = self.request("operator", "POST", f"/api/v1/runs/{parent.pk}/retries",
                                  {"item_ids": [str(successful_item)]}, status=422)
            self.assert_error(denied, "invalid_retry_items")
            body = {"item_ids": detail["retry_item_ids"]}
            first = self.request("operator", "POST", f"/api/v1/runs/{parent.pk}/retries", body, key="p21-retry").json()
            replay = self.request("operator", "POST", f"/api/v1/runs/{parent.pk}/retries", body, key="p21-retry").json()
            self.assertEqual(replay["run_id"], first["run_id"])
            child = Run.objects.get(pk=first["run_id"])
            self.drain(child)
            self.assertEqual(child.status, "succeeded")
            self.assertEqual(RunAttempt.objects.filter(row__run=child).count(), 1)
            self.assertEqual(child.parent_run_id, parent.pk)
            self.assertEqual(list(parent.items.filter(status="succeeded").values("item_id", "result", "suggested_hs")), old_success)
            self.assertEqual(RunAttempt.objects.filter(row__run=parent, row__item=failed_item, status="failed").count(), 3)
            for row in child.items.exclude(item=failed_item):
                self.assertEqual(row.result["origin_run_id"], str(parent.pk))
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(Decision.objects.exists())
