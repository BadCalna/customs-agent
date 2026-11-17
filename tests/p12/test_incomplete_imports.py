"""Incomplete P06 imports must not produce migration/backup attachment manifests."""
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.accounts.errors import ApiError
from apps.accounts.models import Role
from apps.reviews.models import FileVersion, IdempotencyRecord, ImportJob, Item, Task
from apps.reviews.services import files, imports
from apps.runs.models import Run, RunItem
from scripts.database import verify_copy
from scripts.database.import_guard import require_completed_imports
from scripts.operations import table_counts
from tests.p01.factories import make_user
from tests.p06.samples import sample


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IncompleteImportTests(TransactionTestCase):
    # Real commits distinguish the durable reservation in transaction A from
    # publication/row writes rolled back by transaction B.
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "files"
        override = override_settings(CUSTOMS_FILE_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.supplier = make_user("p12-incomplete-supplier", Role.SUPPLIER)
        self.data = sample()
        self.key = "p12-incomplete-upload"

    def upload(self):
        return imports.upload(
            self.supplier, filename="synthetic.xlsx", data=self.data, key=self.key,
        )

    def failed_upload(self, side_effect=None):
        if side_effect is None:
            side_effect = ApiError(500, "storage_unavailable", "simulated publication failure")
        with patch.object(files, "publish_staged_version_original", side_effect=side_effect):
            with self.assertLogs(imports.logger, level="ERROR"):
                with self.assertRaises(ApiError) as failure:
                    self.upload()
        self.assertEqual(failure.exception.code, "import_failed")
        job = ImportJob.objects.get()
        self.assertEqual(job.status, ImportJob.Status.FAILED)
        self.assertEqual(job.version.storage_path, "")
        self.assertEqual(job.precheck.status, Run.Status.FAILED)
        self.assertFalse(job.precheck.is_current)
        self.assertFalse(Item.objects.exists())
        self.assertFalse(RunItem.objects.exists())
        self.assertEqual(IdempotencyRecord.objects.get(key=self.key).response, {"import_id": str(job.pk)})
        return job

    def file_snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def assert_manifest_rejected_without_changes(self, job):
        before_files = self.file_snapshot()
        before_job = ImportJob.objects.values().get(pk=job.pk)
        before_replay = list(IdempotencyRecord.objects.values())
        checks = (
            ("shared guard", require_completed_imports),
            ("attachment paths", verify_copy.attachment_paths),
            ("attachment manifest", lambda: verify_copy.attachment_manifest(self.root)),
        )
        for name, check in checks:
            with self.subTest(entry_point=name):
                with self.assertRaisesRegex(ValueError, "未完成导入"):
                    check()
                self.assertEqual(self.file_snapshot(), before_files)
                self.assertEqual(ImportJob.objects.values().get(pk=job.pk), before_job)
                self.assertEqual(list(IdempotencyRecord.objects.values()), before_replay)

    def assert_original_key_recovers(self, job):
        version_id, run_id, token = job.version_id, job.precheck_id, job.stage_token
        result = self.upload()
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(result["import_id"], str(job.pk))
        self.assertEqual(result["version_id"], str(version_id))
        self.assertEqual(result["precheck_run_id"], str(run_id))
        self.assertEqual(result["parse_status"], ImportJob.Status.SUCCEEDED)
        self.assertEqual((result["item_count"], result["member_count"]), (3, 3))
        self.assertEqual(
            (Task.objects.count(), FileVersion.objects.count(), ImportJob.objects.count(), Run.objects.count()),
            (1, 1, 1, 1),
        )
        self.assertEqual(Item.objects.filter(version_id=version_id).count(), 3)
        self.assertEqual(RunItem.objects.filter(run_id=run_id).count(), 3)
        job.refresh_from_db()
        self.assertEqual(job.status, ImportJob.Status.SUCCEEDED)
        self.assertEqual(job.stage_token, token)
        self.assertEqual(Run.objects.get(pk=run_id).status, Run.Status.SUCCEEDED)
        version = FileVersion.objects.get(pk=version_id)
        self.assertTrue(version.storage_path)
        self.assertEqual((self.root / version.storage_path).read_bytes(), self.data)
        self.assertIsNone(require_completed_imports())
        manifest = verify_copy.attachment_manifest(self.root)
        expected_paths = {version.storage_path}
        expected_paths.update(
            version.items.values_list("images__storage_path", flat=True).exclude(images__storage_path="")
        )
        self.assertEqual({entry["path"] for entry in manifest}, expected_paths)
        self.assertEqual(len(manifest), 2)  # Original plus the sample's one available image.
        for entry in manifest:
            content = (self.root / entry["path"]).read_bytes()
            self.assertEqual(entry["size"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        original = next(entry for entry in manifest if entry["path"] == version.storage_path)
        self.assertEqual(original["sha256"], hashlib.sha256(self.data).hexdigest())
        self.assertFalse((self.root / "incoming" / f"{token}.xlsx").exists())
        self.assertFalse((self.root / "incoming" / f"{token}.json").exists())
        return manifest

    def reserved_job(self, status=ImportJob.Status.STAGED):
        task = Task.objects.create(supplier=self.supplier)
        version = FileVersion.objects.create(
            task=task, number=1, uploaded_by=self.supplier, original_filename="synthetic.xlsx",
        )
        run = Run.objects.create(task=task, version=version, kind=Run.Kind.PRECHECK)
        return ImportJob.objects.create(
            version=version, precheck=run, sha256=hashlib.sha256(self.data).hexdigest(),
            stage_token="p12-reserved-without-files", status=status,
        )

    def test_failed_publish_preserves_stage_and_original_key_retry_restores_complete_manifest(self):
        self.assertFalse(connection.in_atomic_block)
        job = self.failed_upload()
        self.assertFalse(connection.in_atomic_block)
        # A survived B's rollback even though no permanent path can be enumerated.
        self.assertEqual(
            (Task.objects.count(), FileVersion.objects.count(), ImportJob.objects.count(), Run.objects.count()),
            (1, 1, 1, 1),
        )
        staged = self.root / "incoming" / f"{job.stage_token}.xlsx"
        receipt = self.root / "incoming" / f"{job.stage_token}.json"
        self.assertEqual(staged.read_bytes(), self.data)
        self.assertFalse(receipt.exists())
        self.assert_manifest_rejected_without_changes(job)
        manifest = self.assert_original_key_recovers(job)
        out = self.directory / "complete-counts.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(table_counts.main([
                "--require-complete-imports", "--files-root", str(self.root), "--out", str(out),
            ]), 0)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["files"], manifest)
        self.assertEqual(payload["tables"][Item._meta.db_table], 3)

    def test_published_receipt_survives_manifest_rejection_and_database_rollback_retry(self):
        publish = files.publish_staged_version_original

        def fail_after_publish(*args, **kwargs):
            publish(*args, **kwargs)
            raise ApiError(500, "storage_unavailable", "simulated interruption after publication")

        job = self.failed_upload(side_effect=fail_after_publish)
        receipt = self.root / "incoming" / f"{job.stage_token}.json"
        published = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "incoming" / f"{job.stage_token}.xlsx").exists())
        self.assertEqual((self.root / published["relative_path"]).read_bytes(), self.data)
        self.assert_manifest_rejected_without_changes(job)
        self.assert_original_key_recovers(job)

    def test_every_non_success_status_rejects_even_without_any_files(self):
        job = self.reserved_job()
        # Unknown/blank values model historical or future database states, so
        # deliberately bypass the model's choices with a direct stored update.
        statuses = [status for status in ImportJob.Status.values if status != ImportJob.Status.SUCCEEDED]
        for status in [*statuses, "unknown", "", "SUCCEEDED"]:
            with self.subTest(status=status):
                ImportJob.objects.filter(pk=job.pk).update(status=status)
                self.assertFalse(self.root.exists())
                self.assert_manifest_rejected_without_changes(job)
                self.assertFalse(self.root.exists())

    def test_incomplete_job_blocks_manifest_even_when_another_import_succeeded(self):
        self.upload()
        self.assertIsNone(require_completed_imports())
        self.assertEqual(len(verify_copy.attachment_manifest(self.root)), 2)
        pending = self.reserved_job(status=ImportJob.Status.FAILED)
        self.assert_manifest_rejected_without_changes(pending)

    def test_empty_database_has_no_incomplete_imports_or_attachment_references(self):
        self.assertFalse(ImportJob.objects.exists())
        self.assertIsNone(require_completed_imports())
        self.assertEqual(verify_copy.attachment_paths(), [])
        self.assertEqual(verify_copy.attachment_manifest(self.root), [])

    def test_strict_counts_reject_before_stdout_or_output_creation_or_overwrite(self):
        job = self.failed_upload()
        before_files = self.file_snapshot()
        for existing in (False, True):
            with self.subTest(existing_report=existing):
                out = self.directory / f"counts-{existing}.json"
                if existing:
                    out.write_bytes(b"previous accepted report\n")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with self.assertRaisesRegex(ValueError, "未完成导入"):
                        table_counts.main([
                            "--require-complete-imports", "--files-root", str(self.root), "--out", str(out),
                        ])
                self.assertEqual(stdout.getvalue(), "")
                if existing:
                    self.assertEqual(out.read_bytes(), b"previous accepted report\n")
                else:
                    self.assertFalse(out.exists())
                self.assertEqual(self.file_snapshot(), before_files)
        self.assertEqual(ImportJob.objects.get(pk=job.pk).status, ImportJob.Status.FAILED)

    def test_counts_without_strict_flag_still_exports_incomplete_import_diagnostics(self):
        job = self.failed_upload()
        before_files = self.file_snapshot()
        out = self.directory / "diagnostic-counts.json"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(table_counts.main([
                "--files-root", str(self.root), "--out", str(out),
            ]), 0)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["tables"][ImportJob._meta.db_table], 1)
        self.assertEqual(payload["tables"][FileVersion._meta.db_table], 1)
        self.assertEqual(payload["tables"][Item._meta.db_table], 0)
        self.assertEqual(payload["files"], [])
        self.assertEqual(payload["file_count"], 0)
        self.assertEqual(self.file_snapshot(), before_files)
        self.assertEqual(ImportJob.objects.get(pk=job.pk).status, ImportJob.Status.FAILED)
        with self.assertRaisesRegex(ValueError, "未完成导入"):
            require_completed_imports()
