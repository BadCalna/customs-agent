import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Role
from apps.reviews.models import AuditEvent, Confirmation, FileVersion, ImportJob, Submission, Task
from apps.reviews.services import imports
from apps.runs.models import Run, RunAttempt, RunItem, RunWork
from apps.runs import services, dispatch
from apps.runs.jobs import perform_work
from tests.p01.factories import assign, make_item, make_task, make_user, make_version
from tests.p06.samples import sample


@override_settings(EXECUTOR_MODE="rq", CUSTOMS_RETRY_BACKOFF_SECONDS=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QueueTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier", Role.SUPPLIER)
        self.operator = make_user("operator", Role.OPERATOR)
        self.expert = make_user("expert", Role.EXPERT)
        self.other = make_user("other", Role.OPERATOR)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.items = [make_item(self.version, row=5+i, name=f"合成商品{i}") for i in range(2)]
        self.run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK, is_current=True)
        services.register_run(self.run)

    def drain(self, run=None):
        run = run or self.run
        for _ in range(4):
            perform_work(str(run.pk))
        run.refresh_from_db()
        return run

    def test_registration_is_durable_without_redis_or_worker(self):
        self.assertEqual(RunWork.objects.get(run=self.run).state, "pending")
        self.assertEqual(self.run.items.filter(status="queued").count(), 2)
        self.assertFalse(RunAttempt.objects.exists())

    def test_dispatch_failure_does_not_lose_pending_work(self):
        with patch.object(dispatch, "_enqueue", side_effect=ConnectionError("offline")):
            dispatch.dispatch_pending()
        work = RunWork.objects.get(run=self.run)
        self.assertEqual(work.state, "pending")
        self.assertIsNone(work.dispatched_at)
        self.drain()
        self.assertEqual(self.run.status, "succeeded")

    def test_duplicate_delivery_has_only_one_success_per_item(self):
        self.drain()
        self.assertEqual(RunAttempt.objects.filter(status="succeeded").count(), 2)
        self.assertEqual(self.run.items.count(), 2)

    def test_running_duplicate_cannot_call_engine_twice(self):
        original = services.evaluate_row
        def duplicate(run, item):
            perform_work(str(run.pk))
            return original(run, item)
        with patch.object(services, "evaluate_row", side_effect=duplicate) as mock:
            self.drain()
        self.assertEqual(mock.call_count, 2)

    def test_single_failure_is_bounded_and_does_not_block_other_rows(self):
        original = services.evaluate_row
        def fail(run, item):
            if item.pk == self.items[0].pk:
                raise TimeoutError("simulated model timeout")
            return original(run, item)
        with patch.object(services, "evaluate_row", side_effect=fail):
            self.drain()
        self.assertEqual(self.run.items.filter(status="succeeded").count(), 1)
        self.assertEqual(self.run.items.filter(status="failed").count(), 1)
        self.assertEqual(RunAttempt.objects.filter(row__item=self.items[0]).count(), 3)
        self.assertEqual(RunAttempt.objects.filter(row__item=self.items[1]).count(), 1)

    def test_retry_keeps_success_and_all_old_failures(self):
        original = services.evaluate_row
        with patch.object(services, "evaluate_row", side_effect=lambda r,i: (_ for _ in ()).throw(TimeoutError())
                          if i.pk == self.items[0].pk else original(r,i)):
            self.drain()
        old_success = self.run.items.get(item=self.items[1])
        old_result = old_success.result
        child = services.retry_run(self.operator, str(self.run.pk), [str(self.items[0].pk)], "retry-1")
        with patch.object(services, "evaluate_row", wraps=original) as mock:
            self.drain(Run.objects.get(pk=child["run_id"]))
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(RunAttempt.objects.filter(row__run=self.run, status="failed").count(), 3)
        old_success.refresh_from_db()
        self.assertEqual(old_success.result, old_result)
        self.assertEqual(Run.objects.get(pk=child["run_id"]).parent_run_id, self.run.pk)

    def test_expired_lease_recovery_fences_late_worker(self):
        original = services.evaluate_row
        def kill_and_recover(run, item):
            work = RunWork.objects.get(run=run)
            RunWork.objects.filter(run=run).update(lease_until=timezone.now()-timedelta(seconds=1))
            dispatch.recover_expired()
            self.assertNotEqual(RunWork.objects.get(run=run).token, work.token)
            return original(run, item)
        with patch.object(services, "evaluate_row", side_effect=kill_and_recover):
            perform_work(str(self.run.pk))
        self.assertFalse(self.run.items.filter(status="succeeded").exists())
        self.assertEqual(RunAttempt.objects.filter(status="interrupted").count(), 1)
        self.drain()
        self.assertEqual(self.run.status, "succeeded")

    def test_old_run_late_output_cannot_replace_new_run(self):
        original = services.evaluate_row
        newer = []
        def rerun(run, item):
            result = services.start_precheck(self.operator, str(self.version.pk), self.task.revision, "rerun-1")
            newer.append(result["run_id"])
            return original(run, item)
        with patch.object(services, "evaluate_row", side_effect=rerun):
            perform_work(str(self.run.pk))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "superseded")
        self.assertFalse(self.run.is_current)
        self.assertFalse(self.run.items.filter(status="succeeded").exists())
        self.assertEqual(Run.objects.filter(version=self.version, is_current=True).count(), 1)
        self.drain(Run.objects.get(pk=newer[0]))

    def test_v1_late_work_does_not_write_or_change_v2_stage(self):
        FileVersion.objects.create(task=self.task, number=2, uploaded_by=self.supplier, original_filename="v2.xlsx")
        self.task.stage = Task.Stage.UPLOADED
        self.task.save(update_fields=["stage"])
        self.drain()
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.UPLOADED)
        self.assertEqual(self.run.status, "superseded")
        self.assertFalse(RunAttempt.objects.exists())

    def test_business_unknown_is_success_not_technical_retry(self):
        self.items[0].name = ""
        self.items[0].save(update_fields=["name"])
        self.drain()
        self.assertEqual(self.run.items.filter(status="succeeded").count(), 2)
        self.assertEqual(RunAttempt.objects.count(), 2)

    def test_manual_rerun_is_idempotent_and_preserves_old_results(self):
        self.drain()
        before = list(self.run.items.values("id", "result"))
        first = services.start_precheck(self.operator, str(self.version.pk), self.task.revision, "again")
        second = services.start_precheck(self.operator, str(self.version.pk), self.task.revision, "again")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(before, list(self.run.items.values("id", "result")))

    def test_rerun_forbidden_after_submission(self):
        self.task.stage = Task.Stage.SUBMITTED
        self.task.save(update_fields=["stage"])
        with self.assertRaises(Exception) as ctx:
            services.start_precheck(self.operator, str(self.version.pk), self.task.revision, "blocked")
        self.assertEqual(ctx.exception.status, 409)

    def test_outsider_is_404_and_supplier_is_403_for_rerun(self):
        from django.http import Http404
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(Http404):
            services.start_precheck(self.other, str(self.version.pk), 1, "outside")
        with self.assertRaises(PermissionDenied):
            services.start_precheck(self.supplier, str(self.version.pk), 1, "supplier")

    def test_retry_refuses_successful_or_foreign_item(self):
        self.drain()
        with self.assertRaises(Exception) as ctx:
            services.retry_run(self.operator, str(self.run.pk), [str(self.items[0].pk)], "bad")
        self.assertEqual(ctx.exception.status, 422)

    def test_read_progress_never_executes(self):
        self.client.force_login(self.operator)
        response = self.client.get(f"/api/v1/runs/{self.run.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["progress"]["queued"], 2)
        self.assertFalse(RunAttempt.objects.exists())

    def test_api_enforces_session_csrf_and_explicit_retry_capability(self):
        self.client.force_login(self.operator)
        response = self.client.get(f"/api/v1/runs/{self.run.pk}")
        self.assertFalse(response.json()["retry_allowed"])
        self.client.force_login(self.supplier)
        response = self.client.post(f"/api/v1/versions/{self.version.pk}/prechecks",
            data=json.dumps({"revision": self.task.revision}), content_type="application/json", HTTP_IDEMPOTENCY_KEY="api")
        self.assertEqual(response.status_code, 403)
    def test_upload_returns_before_parsing_and_engine_worker_phase(self):
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            with patch.object(services, "evaluate_row", side_effect=AssertionError("request called engine")):
                result = imports.upload(self.supplier, filename="合成.xlsx", data=sample(), key="upload-new")
            self.assertEqual(result["executor_mode"], "rq")
            self.assertEqual(result["parse_status"], "staged")
            run = Run.objects.get(pk=result["precheck_run_id"])
            self.drain(run)
            self.assertEqual(run.status, "succeeded")
            self.assertEqual(run.items.count(), 3)

    def submit(self):
        from apps.reviews.schemas import SubmissionIn
        from apps.reviews.services.submissions import create_submission
        lines = []
        for item in self.items:
            confirmation = Confirmation.objects.create(item=item, revision=item.revision, confirmed_hs="8544421900",
                previous_hs="", reason="合成队列测试", created_by=self.operator)
            lines.append({"item_id": str(item.pk), "confirmation_id": str(confirmation.pk)})
        return create_submission(self.operator, str(self.task.pk), SubmissionIn(
            version_id=str(self.version.pk), revision=self.task.revision, items=lines), "submit")

    def test_submission_registers_work_in_transaction_and_uses_snapshot(self):
        result = self.submit()
        run = Run.objects.get(pk=result["expert_run_id"])
        self.assertTrue(RunWork.objects.filter(run=run, state="pending").exists())
        self.assertFalse(run.items.filter(status="succeeded").exists())
        self.drain(run)
        self.assertEqual(run.status, "succeeded")
        for row in run.items.all():
            self.assertEqual(row.result["target_hs"], "8544421900")

    def test_withdrawal_before_delivery_supersedes_expert_work(self):
        from apps.reviews.services.submissions import withdraw_submission
        result = self.submit()
        withdraw_submission(self.operator, result["submission_id"], "withdraw")
        run = Run.objects.get(pk=result["expert_run_id"])
        self.drain(run)
        self.assertEqual(run.status, "superseded")
        self.assertFalse(RunAttempt.objects.filter(row__run=run).exists())

    def test_successful_attempt_history_and_no_lease_token_in_public_response(self):
        self.drain()
        self.client.force_login(self.operator)
        data = self.client.get(f"/api/v1/runs/{self.run.pk}").json()
        self.assertEqual(len(data["items"][0]["attempt_history"]), 1)
        self.assertEqual(data["items"][0]["attempt_history"][0]["status"], "succeeded")
        self.assertNotIn("token", json.dumps(data))

    def test_late_result_after_lease_expired_is_recoverable(self):
        original = services.evaluate_row
        def expire(run, item):
            RunWork.objects.filter(run=run).update(lease_until=timezone.now()-timedelta(seconds=1))
            return original(run, item)
        with patch.object(services, "evaluate_row", side_effect=expire):
            perform_work(str(self.run.pk))
        self.assertEqual(dispatch.recover_expired(), 1)
        self.drain()
        self.assertEqual(self.run.status, "succeeded")

    def test_parse_failure_never_emits_success_or_calls_model(self):
        from apps.accounts.errors import ApiError
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            data = sample()
            result = imports.upload(self.supplier, filename="合成.xlsx", data=data, key="broken-parse")
            run = Run.objects.get(pk=result["precheck_run_id"])
            with patch.object(imports, "parse", side_effect=ApiError(422,"parse_blocked","模拟损坏")), \
                 patch.object(services, "evaluate_row") as engine:
                self.drain(run)
            self.assertFalse(AuditEvent.objects.filter(task_id=run.task_id, action="import.succeeded").exists())
            self.assertEqual(ImportJob.objects.get(precheck=run).status, "failed")
            self.assertEqual(RunWork.objects.get(run=run).state, "done")
            self.assertFalse(engine.called)
            replay = imports.upload(self.supplier, filename="合成.xlsx", data=data, key="broken-parse")
            self.assertEqual(replay["version_id"], result["version_id"])
            self.assertEqual(replay["parse_status"], "staged")
            self.drain(run)
            self.assertEqual(run.status, "succeeded")

    def test_csrf_required_for_real_retry_post(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.post(f"/api/v1/runs/{self.run.pk}/retries",
            data=json.dumps({"item_ids": [str(self.items[0].pk)]}), content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="csrf-test")
        self.assertEqual(response.status_code, 403)
