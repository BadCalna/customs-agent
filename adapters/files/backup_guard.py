"""Host-local reader/writer guard shared by storage writers and native backups.

Writers take a shared lock; backups take an exclusive nonblocking lock. Keep the
lock outside the file tree so archives and inventory hashes never contain it.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path


@contextmanager
def file_guard(root, *, exclusive=False):
    if os.name == "nt":
        if exclusive:
            raise ValueError("native 一致备份需要 POSIX flock")
        yield
        return
    import fcntl
    key = hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()
    # Login shells, workers and cron must share the same lock even if TMPDIR differs.
    directory = Path('/tmp') / f"customs-file-guards-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("附件锁目录必须由当前用户独占")
    descriptor = os.open(directory / (key + ".lock"),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        operation = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise ValueError("附件正在写入，不能建立一致备份；停止写入后重试") from exc
        yield
    finally:
        os.close(descriptor)


def guarded_writer(function):
    @wraps(function)
    def wrapped(root, *args, **kwargs):
        with file_guard(root):
            return function(root, *args, **kwargs)
    return wrapped
