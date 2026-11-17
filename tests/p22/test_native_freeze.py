"""Real PG lock tests; the fake orchestration suite does not prove quiescence."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase

from adapters.files.backup_guard import file_guard
from scripts.operations.native_freeze import frozen_database


@unittest.skipUnless(connection.vendor == "postgresql", "真实 PostgreSQL 备份停写锁")
class NativeFreezeTests(TransactionTestCase):
    def setUp(self):
        import psycopg
        db = connection.settings_dict
        self.owner = psycopg.connect(dbname=db['NAME'], user=db['USER'], password=db['PASSWORD'],
                                     host=db['HOST'], port=db['PORT'], autocommit=True)
        self.writer = psycopg.connect(dbname=db['NAME'], user=db['USER'], password=db['PASSWORD'],
                                      host=db['HOST'], port=db['PORT'], autocommit=True)
        self.addCleanup(self.owner.close)
        self.addCleanup(self.writer.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_table_and_sequence_writes_blocked_and_resume_after_release(self):
        import psycopg
        self.writer.execute("SET lock_timeout='200ms'")
        sequence = self.owner.execute("SELECT sequencename FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename LIMIT 1").fetchone()[0]
        with frozen_database(self.owner, self.root) as evidence:
            self.assertTrue(evidence['snapshot'])
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.writer.execute("DELETE FROM auth_group WHERE false")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                self.writer.execute("SELECT nextval(%s::regclass)", [sequence])
        self.writer.execute("DELETE FROM auth_group WHERE false")
        self.assertIsInstance(self.writer.execute("SELECT nextval(%s::regclass)", [sequence]).fetchone()[0], int)

    def test_active_database_or_file_writer_prevents_freeze(self):
        import psycopg
        with self.writer.transaction():
            self.writer.execute("DELETE FROM auth_group WHERE false")
            with self.assertRaises(psycopg.errors.LockNotAvailable):
                with frozen_database(self.owner, self.root):
                    self.fail("existing DB writer must prevent freeze")
        with file_guard(self.root):
            with self.assertRaisesRegex(ValueError, "附件正在写入"):
                with frozen_database(self.owner, self.root):
                    self.fail("existing file writer must prevent freeze")

    def test_storage_writer_waits_until_backup_lock_is_released(self):
        code = ("import sys; from adapters.files.storage import _write_exclusive; "
                "_write_exclusive(sys.argv[1], 'proof/proof.txt', b'proof', '.txt')")
        process = None
        try:
            with frozen_database(self.owner, self.root):
                process = subprocess.Popen([sys.executable, '-c', code, str(self.root)], cwd=settings.BASE_DIR,
                                           env=dict(os.environ, TMPDIR='/tmp'))
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.4)
                self.assertFalse((self.root / 'proof/proof.txt').exists())
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertEqual((self.root / 'proof/proof.txt').read_bytes(), b'proof')
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
