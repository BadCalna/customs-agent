"""Exercise the actual migration graph, always restore latest schema."""
from datetime import timedelta
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.runs.engine_config import current_engine_snapshot, run_result_mode
from apps.runs.models import Run


@override_settings(ENGINE_MODE="mock")
class EngineSnapshotMigrationTests(TransactionTestCase):
    def test_historical_rows_and_unfinished_work_fail_closed(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [("runs", "0008_import_run_attempts"), ("reviews", "0008_import_job_superseded")]
        try:
            executor.migrate(old_target)
            apps = executor.loader.project_state(old_target).apps
            User = apps.get_model("accounts", "User")
            Task = apps.get_model("reviews", "Task")
            Version = apps.get_model("reviews", "FileVersion")
            Item = apps.get_model("reviews", "Item")
            OldRun = apps.get_model("runs", "Run")
            Row = apps.get_model("runs", "RunItem")
            Work = apps.get_model("runs", "RunWork")
            Attempt = apps.get_model("runs", "RunAttempt")
            Import = apps.get_model("reviews", "ImportJob")
            user = User.objects.create(username="migration-supplier", role="supplier")
            task = Task.objects.create(supplier_id=user.pk, title="migration")
            version = Version.objects.create(task_id=task.pk, uploaded_by_id=user.pk, number=1,
                                            original_filename="history.xlsx")
            items = [Item.objects.create(version_id=version.pk, source_sheet="申报要素", source_row=5+i,
                                         source_cells=f"A{5+i}", name="history") for i in range(2)]
            live = {"result_mode": "live", "engine_version": "openai-compatible-1",
                    "prompt_version": "customs-review-1", "rules_version": "d4-0.1"}
            mock = {k: v for k, v in current_engine_snapshot().items()
                    if k in ("result_mode", "engine_version", "prompt_version", "rules_version")}
            homogeneous = OldRun.objects.create(task_id=task.pk, version_id=version.pk, kind="precheck", status="succeeded")
            mixed = OldRun.objects.create(task_id=task.pk, version_id=version.pk, kind="precheck", status="succeeded")
            for run, results in [(homogeneous, [live, live]), (mixed, [live, mock])]:
                for item, result in zip(items, results):
                    Row.objects.create(run_id=run.pk, item_id=item.pk, status="succeeded", result=result)
            pending = OldRun.objects.create(task_id=task.pk, version_id=version.pk, kind="precheck",
                                            status="running", is_current=True)
            token = uuid.uuid4()
            Work.objects.create(run_id=pending.pk, state="running", token=token,
                                lease_until=timezone.now()+timedelta(minutes=1))
            attempt = Attempt.objects.create(run_id=pending.pk, number=1, token=token, input_hash="old")
            job = Import.objects.create(version_id=version.pk, precheck_id=pending.pk,
                                        sha256="0"*64, stage_token=uuid.uuid4().hex,
                                        report={"result_mode": "live"})
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            # Migration-time deployment configuration was mock, but preserved
            # completed live history must still truthfully describe itself.
            migrated = Run.objects.get(pk=homogeneous.pk)
            self.assertEqual(migrated.engine_snapshot["result_mode"], "live")
            self.assertNotIn("config_hash", migrated.engine_snapshot)
            self.assertEqual(run_result_mode(migrated).value, "live")
            self.assertEqual(Run.objects.get(pk=mixed.pk).engine_snapshot, {})
            self.assertEqual(run_result_mode(Run.objects.get(pk=mixed.pk)).value, "mock")
            migrated = Run.objects.get(pk=pending.pk)
            self.assertEqual(migrated.engine_snapshot, {})
            self.assertEqual(migrated.status, "superseded")
            self.assertFalse(migrated.is_current)
            self.assertEqual(migrated.work.state, "done")
            self.assertIsNone(migrated.work.token)
            from apps.reviews.models import ImportJob
            from apps.runs.models import RunAttempt
            self.assertEqual(RunAttempt.objects.get(pk=attempt.pk).status, "superseded")
            migrated_job = ImportJob.objects.get(pk=job.pk)
            self.assertEqual(migrated_job.status, "failed")
            self.assertEqual(migrated_job.error["code"], "legacy_engine_configuration_unknown")
            self.assertEqual(migrated_job.stage_token, job.stage_token)
        finally:
            MigrationExecutor(connection).migrate(latest)
