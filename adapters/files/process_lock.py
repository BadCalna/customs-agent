"""Exclusive operating-system file locks, with an explicit contention deadline.

Callers own the descriptor, thread synchronization and reentrancy. Closing a
descriptor or terminating its process releases the operating-system lock.
"""
from __future__ import annotations

import errno
import math
import os
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_exclusive(fd: int, *, timeout: float | None = 30.0) -> None:
    """Lock byte zero on Windows, or the file on POSIX; never emulate a lock.

    ``None`` waits indefinitely; zero makes a single nonblocking attempt.
    Invalid descriptors and non-contention errors propagate immediately.
    """
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("lock timeout must be finite and nonnegative, or None")
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Timed out waiting for the exclusive file lock") from exc
            time.sleep(0.05 if remaining is None else min(0.05, remaining))


def release_exclusive(fd: int) -> None:
    """Release a lock acquired through :func:`acquire_exclusive`."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
