"""Regression: configuration changes must never resume a partial mixed Run."""
from copy import deepcopy
from datetime import timedelta
import importlib
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.apps import apps
from django.db.migrations.state import ProjectState
from django.utils import timezone

from adapters.engines.mock import MockEngine
from apps.accounts.errors import ApiError
from apps.accounts.models import Role
from apps.reviews.models import ImportJob, Task
from apps.reviews.serializers import task_to_entry
from apps.reviews.services import imports
from apps.runs import dispatch, inline, services
from apps.runs.engine_config import current_engine_snapshot, result_metadata
from apps.runs.jobs import perform_work
from apps.runs.models import Run, RunAttempt, RunItem, RunWork
from apps.runs.serializers import run_to_detail
from tests.p01.factories import assign, make_item, make_task, make_user, make_version
from tests.p06.samples import sample


@override_settings(ENGINE_MODE="mock", EXECUTOR_MODE="rq", CUSTOMS_RETRY_BACKOFF_SECONDS=0)
class EngineIdentityTests(TestCase):
    def setUp(self):
        self.supplier = make_user("identity-supplier", Role.SUPPLIER)
        self.operator = make_user("identity-operator", Role.OPERATOR)
        self.task = make_task(self.supplier, stage=Task.Stage.PRECHECKING)
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = make_version(self.task, self.supplier)
        self.items = [make_item(self.version, row=5+i) for i in range(2)]
        self.run = Run.objects.create(task=self.task, version=self.version, kind="precheck", is_current=True)
        services.register_run(self.run)

    def partial(self):
        token = services.claim_work(str(self.run.pk))
        attempt = services.begin_attempt(str(self.run.pk), str(self.items[0].pk), token)
        output = services.evaluate_row(self.run, self.items[0])
        self.assertTrue(services.finish_attempt(attempt.pk, token, output=output))
        self.success = deepcopy(self.run.items.get(item=self.items[0]).result)
        return token

    def assert_invalidated(self):
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "superseded")
        self.assertFalse(self.run.is_current)
        self.assertEqual(self.run.work.state, "done")
        self.assertIsNone(self.run.work.token)
        self.assertEqual(self.run.items.get(item=self.items[0]).result, self.success)

    def test_registration_has_fixed_metadata_and_no_network(self):
        snapshot = deepcopy(self.run.engine_snapshot)
        self.assertEqual(snapshot, current_engine_snapshot())
        with override_settings(ENGINE_MODE="openai"), patch("adapters.engines.openai.request.urlopen") as network:
            services.register_run(self.run)
        network.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.engine_snapshot, snapshot)
        for row in self.run.items.all():
            self.assertEqual(row.result, result_metadata(self.run))

    def test_model_instance_queryset_and_bulk_updates_cannot_rewrite_identity(self):
        self.run.engine_snapshot = {}
        with self.assertRaises(ValidationError):
            self.run.save()
        with self.assertRaises(ValidationError):
            Run.objects.filter(pk=self.run.pk).update(engine_snapshot={})
        with self.assertRaises(ValidationError):
            Run.objects.bulk_update([self.run], ["engine_snapshot"])

    def test_payload_and_input_hash_remain_fixed_when_settings_change(self):
        before = services._payload(self.run, self.items[0])
        with override_settings(ENGINE_MODE="openai"):
            after = services._payload(self.run, self.items[0])
        self.assertEqual(before.model_dump(), after.model_dump())

    def test_claim_after_mode_change_retains_success_and_never_calls_engine(self):
        token = self.partial()
        services.finish_pass(str(self.run.pk), token)
        with override_settings(ENGINE_MODE="openai"), patch.object(services, "evaluate_row") as evaluate:
            perform_work(str(self.run.pk))
        evaluate.assert_not_called()
        self.assert_invalidated()
        self.assertEqual(RunAttempt.objects.count(), 1)

    def test_recovery_after_mode_change_invalidates_whole_run(self):
        token = self.partial()
        attempt = services.begin_attempt(str(self.run.pk), str(self.items[1].pk), token)
        RunWork.objects.filter(run=self.run).update(lease_until=timezone.now()-timedelta(seconds=1))
        with override_settings(ENGINE_MODE="openai"):
            dispatch.recover_expired()
        self.assert_invalidated()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "superseded")
        with patch.object(services, "evaluate_row") as evaluate:
            perform_work(str(self.run.pk))
        evaluate.assert_not_called()

    def test_begin_after_mode_change_does_not_relabel_queued_row(self):
        token = self.partial()
        before = self.run.items.get(item=self.items[1]).result
        with override_settings(ENGINE_MODE="openai"):
            self.assertIsNone(services.begin_attempt(str(self.run.pk), str(self.items[1].pk), token))
        self.assert_invalidated()
        self.assertEqual(self.run.items.get(item=self.items[1]).result, before)

    def test_late_publish_after_mode_change_is_fenced(self):
        token = self.partial()
        attempt = services.begin_attempt(str(self.run.pk), str(self.items[1].pk), token)
        output = services.evaluate_row(self.run, self.items[1])
        with override_settings(ENGINE_MODE="openai"):
            self.assertFalse(services.finish_attempt(attempt.pk, token, output=output))
        self.assert_invalidated()
        self.assertEqual(self.run.items.filter(status="succeeded").count(), 1)

    def test_mismatched_output_identity_is_not_published(self):
        token = self.partial()
        attempt = services.begin_attempt(str(self.run.pk), str(self.items[1].pk), token)
        output = services.evaluate_row(self.run, self.items[1])
        output["result"]["result_mode"] = "live"
        self.assertFalse(services.finish_attempt(attempt.pk, token, output=output))
        self.assert_invalidated()

    def test_finish_pass_checks_all_rows_not_only_first(self):
        token = self.partial()
        row = self.run.items.get(item=self.items[1])
        row.status, row.result = "succeeded", {**result_metadata(self.run), "result_mode": "live"}
        row.save()
        services.finish_pass(str(self.run.pk), token)
        self.assert_invalidated()
        self.assertEqual(run_to_detail(self.run).result_mode.value, "mock")
        self.assertEqual(task_to_entry(self.operator, self.task).result_mode.value, "mock")

    def test_version_change_blocks_automatic_and_manual_retry(self):
        token = self.partial()
        row = self.run.items.get(item=self.items[1])
        row.status = "failed"
        row.save()
        services.finish_pass(str(self.run.pk), token)
        self.run.refresh_from_db()
        self.assertTrue(services.retry_capability(self.operator, self.run))
        with patch.object(MockEngine, "prompt_version", "new-prompt"):
            self.assertEqual(services.retry_capability(self.operator, self.run), [])
            with self.assertRaises(ApiError) as failure:
                services.retry_run(self.operator, str(self.run.pk), [str(self.items[1].pk)], "changed-version")
        self.assertEqual(failure.exception.code, "engine_mode_changed")

    def test_version_change_blocks_worker_before_model_call(self):
        with patch.object(MockEngine, "rules_version", "new-rules"), patch.object(services, "evaluate_row") as evaluate:
            perform_work(str(self.run.pk))
        evaluate.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "superseded")

    def test_full_new_round_does_not_reuse_success(self):
        token = self.partial()
        services.finish_pass(str(self.run.pk), token)
        with override_settings(ENGINE_MODE="openai"):
            result = services.start_precheck(self.operator, str(self.version.pk), self.task.revision, "new-engine")
        child = Run.objects.get(pk=result["run_id"])
        self.assertIsNone(child.parent_run_id)
        self.assertEqual(child.engine_snapshot["result_mode"], "live")
        self.assertEqual(child.items.filter(status="queued").count(), 2)
        self.assertTrue(all("origin_run_id" not in row.result for row in child.items.all()))
        self.assertEqual(self.run.items.get(item=self.items[0]).result, self.success)

    def test_empty_and_unknown_history_never_inherit_current_live_mode(self):
        unknown = Run.objects.create(task=self.task, version=self.version, kind="precheck", engine_snapshot={})
        with override_settings(ENGINE_MODE="openai"):
            self.assertEqual(run_to_detail(unknown).result_mode.value, "mock")
            self.assertEqual(task_to_entry(self.operator, self.task).result_mode.value, "mock")
            empty_live = Run.objects.create(task=self.task, version=self.version, kind="precheck")
            self.assertEqual(run_to_detail(empty_live).result_mode.value, "mock")

    @override_settings(ENGINE_MODE="openai", CUSTOMS_AI_API_BASE="https://example.invalid/v1", CUSTOMS_AI_MODEL="one")
    def test_live_config_model_and_endpoint_changes_but_not_key_rotation_invalidate(self):
        run = Run.objects.create(task=self.task, version=self.version, kind="precheck")
        from apps.runs.engine_config import engine_matches
        with override_settings(CUSTOMS_AI_API_KEY="rotated"):
            self.assertTrue(engine_matches(run))
        with override_settings(CUSTOMS_AI_MODEL="two"):
            self.assertFalse(engine_matches(run))
        with override_settings(CUSTOMS_AI_API_BASE="https://other.invalid/v1"):
            self.assertFalse(engine_matches(run))

    @override_settings(ENGINE_MODE="openai")
    def test_homogeneous_live_and_mixed_live_first_aggregate_safely(self):
        self.run.is_current = False
        self.run.save(update_fields=["is_current"])
        run = Run.objects.create(task=self.task, version=self.version, kind="precheck", is_current=True, status="succeeded")
        services.register_run(run)
        for row in run.items.all():
            row.status = "succeeded"
            row.save()
        self.assertEqual(run_to_detail(run).result_mode.value, "live")
        self.assertEqual(task_to_entry(self.operator, self.task).result_mode.value, "live")
        # Keep first row live: selecting only .first() used to hide this mix.
        row = run.items.last()
        row.result = {**row.result, "result_mode": "mock"}
        row.save()
        self.assertEqual(run_to_detail(run).result_mode.value, "mock")
        self.assertEqual(task_to_entry(self.operator, self.task).result_mode.value, "mock")

    def test_retry_model_rejects_different_engine_snapshot(self):
        with override_settings(ENGINE_MODE="openai"), self.assertRaises(ValidationError):
            Run.objects.create(task=self.task, version=self.version, kind="precheck", parent_run=self.run)

    def test_inline_preregistered_run_also_rejects_mode_change(self):
        # No queue rows exist in normal inline scheduling.
        run = Run.objects.create(task=self.task, version=self.version, kind="precheck")
        with override_settings(ENGINE_MODE="openai"), patch("adapters.engines.openai.OpenAICompatibleEngine.recommend") as call:
            inline.execute_precheck(self.version, run=run)
        call.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "superseded")

    def test_inline_import_recovery_unknown_or_changed_version_preserves_original(self):
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            for unknown in (False, True):
                result = imports.upload(self.supplier, filename="recovery.xlsx", data=sample(), key=f"recover-{unknown}")
                run = Run.objects.get(pk=result["precheck_run_id"])
                if unknown:
                    # Simulate a migration-era row using an unguarded historical
                    # model; production managers cannot rewrite fixed identity.
                    historical = ProjectState.from_apps(apps).apps.get_model("runs", "Run")
                    historical.objects.filter(pk=run.pk).update(engine_snapshot={})
                with override_settings(EXECUTOR_MODE="inline"), patch.object(MockEngine, "prompt_version", "changed"), \
                     patch.object(imports, "parse") as parse, self.assertRaises(ApiError) as failure:
                    imports.finish_import(result["import_id"])
                parse.assert_not_called()
                self.assertEqual(failure.exception.details["cause"]["code"], "engine_mode_changed")
                self.assertIn("重新上传", str(failure.exception))
                job = ImportJob.objects.get(pk=result["import_id"])
                self.assertEqual(job.status, "failed")
                self.assertTrue(Path(root, "incoming", f"{job.stage_token}.xlsx").is_file())
                self.assertFalse(job.version.items.exists())
                self.assertFalse(job.version.task.audit_events.filter(action="import.succeeded").exists())

    def test_inline_import_does_not_publish_success_when_executor_supersedes(self):
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            result = imports.upload(self.supplier, filename="late.xlsx", data=sample(), key="late-config")
            def invalidate(version, **kwargs):
                run = kwargs["run"]
                run.status, run.is_current = "superseded", False
                run.save(update_fields=["status", "is_current"])
                return run
            with override_settings(EXECUTOR_MODE="inline"), patch.object(imports, "execute_precheck", side_effect=invalidate), \
                 self.assertRaises(ApiError) as failure:
                imports.finish_import(result["import_id"])
            self.assertEqual(failure.exception.details["cause"]["code"], "engine_mode_changed")
            job = ImportJob.objects.get(pk=result["import_id"])
            self.assertEqual(job.status, "failed")
            # Publication precedes DB rollback: incoming bytes have moved, but
            # the durable receipt still binds this token to the intact original.
            from apps.reviews.services import files
            self.assertEqual(files.read_staged_version_original(job.stage_token), sample())
            self.assertFalse(job.version.items.exists())
            # A new upload must also be able to preserve the failed predecessor
            # through that same receipt, not merely leave readable orphan bytes.
            new = imports.upload(self.supplier, filename="replacement.xlsx", data=sample(order=(3, 2, 1)),
                key="late-config-replacement", task_id=result["task_id"],
                expected_current_version_id=result["version_id"])
            perform_work(new["precheck_run_id"])
            job.refresh_from_db()
            self.assertEqual(job.status, "superseded")
            self.assertTrue(job.version.storage_path)
            self.assertEqual(files.read_version_original(self.supplier, str(job.version_id)).content, sample())

    def test_completed_import_replay_is_not_rejected_after_configuration_change(self):
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            result = imports.upload(self.supplier, filename="done.xlsx", data=sample(), key="done-config")
            run = Run.objects.get(pk=result["precheck_run_id"])
            perform_work(str(run.pk))
            with patch.object(MockEngine, "prompt_version", "changed"):
                replay = imports.queue_import(result["import_id"], replay=True)
                inline_replay = imports.finish_import(result["import_id"], replay=True)
            self.assertEqual(replay["parse_status"], "succeeded")
            self.assertEqual(inline_replay["parse_status"], "succeeded")

    def test_import_mode_change_unblocks_new_upload_without_losing_original(self):
        with tempfile.TemporaryDirectory() as root, override_settings(CUSTOMS_FILE_ROOT=Path(root)):
            result = imports.upload(self.supplier, filename="identity.xlsx", data=sample(), key="old-upload")
            old = Run.objects.get(pk=result["precheck_run_id"])
            with override_settings(ENGINE_MODE="openai"):
                perform_work(str(old.pk))
                old.refresh_from_db()
                job = ImportJob.objects.get(precheck=old)
                self.assertEqual(job.status, "failed")
                self.assertEqual(job.error["code"], "engine_configuration_changed")
                self.assertTrue(Path(root, "incoming", f"{job.stage_token}.xlsx").is_file())
                # Replaying the old import cannot retrofit the saved identity.
                with self.assertRaises(ApiError) as refused:
                    imports.queue_import(result["import_id"], replay=True)
                self.assertEqual(refused.exception.code, "engine_mode_changed")
                with patch.object(services, "evaluate_row") as evaluate:
                    perform_work(str(old.pk))
                evaluate.assert_not_called()
                old.refresh_from_db()
                self.assertEqual(old.status, "superseded")
                self.assertEqual(old.engine_snapshot["result_mode"], "mock")
                new = imports.upload(self.supplier, filename="new.xlsx", data=sample(order=(3, 2, 1)), key="new-upload",
                    task_id=result["task_id"], expected_current_version_id=result["version_id"])
            self.assertNotEqual(new["precheck_run_id"], result["precheck_run_id"])
            self.assertEqual(new["version_number"], 2)
