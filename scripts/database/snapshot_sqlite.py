#!/usr/bin/env python
"""Create a consistent, exclusive SQLite snapshot, including committed WAL rows."""
import sqlite3
import sys
from pathlib import Path


def snapshot(source: Path, target: Path):
    source, target = Path(source).resolve(strict=True), Path(target).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also prevents accidentally backing up over the source.
    with target.open("xb"):
        pass
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
    except BaseException:
        # Keep a failed snapshot for diagnosis; callers never consume it on failure.
        raise
    target.chmod(0o600)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("用法：snapshot_sqlite.py <源库> <新快照文件>")
    snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
