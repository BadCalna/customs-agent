"""Real thread/process exclusion and OS release, on the host's native backend."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from adapters.files.process_lock import acquire_exclusive
from adapters.sources.reference_import import publish as publishing
from tests.p23.support import P23TestCase


def wait_for_file(path: Path, child: subprocess.Popen, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if child.poll() is not None:
            raise AssertionError(f"child exited before checkpoint: {child.communicate()[1]!r}")
        if time.monotonic() >= deadline:
            raise AssertionError("child did not reach checkpoint")
        time.sleep(0.01)


def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=5)


class PublishLockTests(P23TestCase):
    def start_holder(self, root: Path):
        root.mkdir(exist_ok=True)
        script = """
from pathlib import Path
import sys
from adapters.sources.reference_import.publish import _publish_lock
root = Path(sys.argv[1])
(root / 'started').write_text('started')
with _publish_lock(root, timeout=5):
    (root / 'acquired').write_text('acquired')
    sys.stdin.buffer.read()
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(root)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(stop_child, child)
        wait_for_file(root / "started", child)
        return child

    def test_same_thread_reenters_and_exception_releases(self):
        with self.assertRaisesRegex(ValueError, "body failed"):
            with publishing._publish_lock(self.root, timeout=0):
                with publishing._publish_lock(self.root, timeout=0):
                    raise ValueError("body failed")
        with publishing._publish_lock(self.root, timeout=0):
            pass
        self.assertNotIn((threading.get_ident(), self.root.resolve()), publishing._LOCK_DEPTH)

    def test_other_thread_times_out_without_entering(self):
        outcomes = []

        def contender():
            try:
                with publishing._publish_lock(self.root, timeout=0.1):
                    outcomes.append("entered")
            except publishing.PublishError:
                outcomes.append("timeout")

        with publishing._publish_lock(self.root):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ["timeout"])
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ["timeout", "entered"])

    def test_other_process_waits_until_owner_releases(self):
        with publishing._publish_lock(self.root):
            child = self.start_holder(self.root)
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            self.assertFalse((self.root / "acquired").exists())
        wait_for_file(self.root / "acquired", child)
        child.communicate(input=b"done", timeout=5)
        self.assertEqual(child.returncode, 0)

    def test_os_contention_timeout_does_not_leak_reentrant_depth(self):
        script = """
from pathlib import Path
import sys
from adapters.sources.reference_import.publish import _publish_lock, PublishError
for timeout in (0.1, 0):
    try:
        with _publish_lock(Path(sys.argv[1]), timeout=timeout):
            raise AssertionError('contender bypassed an existing OS lock')
    except PublishError:
        pass
"""
        with publishing._publish_lock(self.root):
            child = subprocess.run([sys.executable, "-c", script, str(self.root)],
                                   capture_output=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_killed_process_releases_os_lock(self):
        child = self.start_holder(self.root)
        wait_for_file(self.root / "acquired", child)
        child.kill()
        child.communicate(timeout=5)
        self.assertNotEqual(child.returncode, 0)
        with publishing._publish_lock(self.root, timeout=0.5):
            pass

    def test_acquire_error_closes_descriptor_and_clears_depth(self):
        with patch.object(publishing, "acquire_exclusive", side_effect=OSError("denied")):
            with self.assertRaises(publishing.PublishError):
                with publishing._publish_lock(self.root):
                    self.fail("failed lock must not enter its body")
        self.assertNotIn((threading.get_ident(), self.root.resolve()), publishing._LOCK_DEPTH)
        with publishing._publish_lock(self.root, timeout=0):
            pass

    def test_invalid_descriptor_is_not_retried_as_contention(self):
        fd = os.open(self.root / "closed.lock", os.O_RDWR | os.O_CREAT)
        os.close(fd)
        with self.assertRaises(OSError) as caught:
            acquire_exclusive(fd, timeout=1)
        self.assertNotIsInstance(caught.exception, TimeoutError)
