"""Windows 存储锁必须在独立进程间互斥，持有进程退出后可恢复。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from adapters.files import storage


@unittest.skipUnless(os.name == "nt", "Windows 专项")
class WindowsTokenLockTests(unittest.TestCase):
    def test_other_process_waits_and_releases_lock_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "incoming").mkdir()
            acquired = root / "acquired"
            script = (
                "import pathlib,sys; from adapters.files.storage import _token_lock; "
                "root=pathlib.Path(sys.argv[1]); "
                "lock=_token_lock(root,'test-token'); lock.__enter__(); "
                "(root/'acquired').write_text('locked')"
            )
            with storage._token_lock(root, "test-token"):
                child = subprocess.Popen([sys.executable, "-c", script, str(root)])
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.3)
                    self.assertFalse(acquired.exists())
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
            try:
                self.assertEqual(child.wait(timeout=15), 0)
                self.assertEqual(acquired.read_text(), "locked")
                with storage._token_lock(root, "test-token"):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
