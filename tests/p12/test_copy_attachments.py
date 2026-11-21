"""Cross-deployment attachment copies preserve source bytes and refuse collisions."""
import hashlib
import os
from pathlib import Path
import tempfile

from django.conf import settings
from django.test import SimpleTestCase

from scripts.database.copy_attachments import copy_attachments, verify_manifest
from tests.p12.shell import run_database_script
from tests.platform_files import create_directory_link


class CopyAttachmentsTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "old deployment"
        self.target = self.base / "new deployment"
        self.source.mkdir()
        self.relative = "tasks/task/versions/version/original/book.xlsx"
        path = self.source / self.relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original attachment")
        self.manifest = [{"path": self.relative, "size": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]

    def test_separate_roots_copy_and_idempotent_reuse(self):
        result = copy_attachments(self.source, self.target, self.manifest)
        self.assertEqual(result["copied"], 1)
        self.assertEqual((self.source / self.relative).read_bytes(), b"original attachment")
        self.assertEqual((self.target / self.relative).read_bytes(), b"original attachment")
        self.assertEqual(copy_attachments(self.source, self.target, self.manifest)["reused"], 1)
        self.assertEqual(copy_attachments(self.source, self.source, self.manifest)["reused"], 1)

    def test_conflicting_target_is_never_overwritten(self):
        dest = self.target / self.relative
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"different deployment")
        with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
            copy_attachments(self.source, self.target, self.manifest)
        self.assertEqual(dest.read_bytes(), b"different deployment")

    def test_missing_or_changed_source_fails_before_target_creation(self):
        (self.source / self.relative).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "指纹不一致"):
            copy_attachments(self.source, self.target, self.manifest)
        self.assertFalse(self.target.exists())

    def test_target_symlink_and_nested_roots_are_rejected(self):
        self.target.mkdir()
        create_directory_link(self.target / "tasks", self.source / "tasks")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            copy_attachments(self.source, self.target, self.manifest)
        self.assertEqual((self.source / self.relative).read_bytes(), b"original attachment")
        with self.assertRaisesRegex(ValueError, "互相嵌套"):
            copy_attachments(self.source, self.source / "nested", self.manifest)

    def test_in_root_directory_redirect_is_rejected_before_publication(self):
        self.target.mkdir()
        actual = self.target / "actual-tasks"
        actual.mkdir()
        create_directory_link(self.target / "tasks", actual)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            copy_attachments(self.source, self.target, self.manifest)
        self.assertEqual(list(actual.iterdir()), [])
        self.assertEqual((self.source / self.relative).read_bytes(), b"original attachment")

    def test_manifest_verifies_both_roots_after_copy(self):
        copy_attachments(self.source, self.target, self.manifest)
        (self.target / self.relative).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "指纹不一致"):
            verify_manifest(self.target, self.manifest)
        verify_manifest(self.source, self.manifest)

    def test_arbitrary_source_requires_explicit_file_root(self):
        env = dict(os.environ)
        env.pop("CUSTOMS_SOURCE_FILE_ROOT", None)
        # This guard is before any PostgreSQL access; make that prerequisite
        # explicit rather than relying on a developer's local PG installation.
        env["CUSTOMS_PG_BIN"] = str(self.base / "no-bin")
        result = run_database_script(settings.BASE_DIR / "scripts/database/copy_sqlite_to_pg.sh",
                                     self.base / "some.sqlite3", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--source-files-root", result.stderr)
