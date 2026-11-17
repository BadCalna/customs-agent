"""Database row counts + stable full-precision row fingerprints and file SHA256.

Uses database cursors, NOT dumpdata (which truncates datetime microseconds).
Includes introspected tables including django_migrations; excludes django_session.
Row hashes are sorted, so physical row order is irrelevant. Memory O(number of rows).
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
SKIP_TABLES = {"django_session"}
SKIP_DIRS = {"incoming"}


def value_token(value):
    """Type-tagged deterministic encoding; preserve all six datetime digits."""
    if value is None or isinstance(value, (bool, int, str)):
        return [type(value).__name__, value]
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, (date, time, Decimal, UUID)):
        return [type(value).__name__, str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, (bytes, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, dict):
        return ["dict", sorted((str(k), value_token(v)) for k, v in value.items())]
    if isinstance(value, (list, tuple)):
        return ["list", [value_token(v) for v in value]]
    raise TypeError(f"Unsupported database value: {type(value).__name__}")


def row_hash(row):
    return hashlib.sha256(json.dumps([value_token(v) for v in row], ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def database_inventory():
    import django
    django.setup()
    from django.db import connection
    counts, fingerprints = {}, {}
    with connection.cursor() as cursor:
        tables = sorted(set(connection.introspection.table_names(cursor)) - SKIP_TABLES)
        for table in tables:
            cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")
            columns = [column[0] for column in cursor.description]
            rows = []
            while True:
                batch = cursor.fetchmany(1000)
                if not batch:
                    break
                rows.extend(row_hash(row) for row in batch)
            counts[table] = len(rows)
            digest = hashlib.sha256(json.dumps(columns, separators=(",", ":")).encode())
            for hashed in sorted(rows):
                digest.update(hashed.encode("ascii"))
            fingerprints[table] = digest.hexdigest()
        oid = None
        if connection.vendor == "postgresql":
            cursor.execute("SELECT current_database(), oid FROM pg_database WHERE datname=current_database()")
            name, oid = cursor.fetchone()
        else:
            name = str(connection.settings_dict["NAME"])
    return counts, fingerprints, str(name), oid


def table_counts():
    return database_inventory()[0]


def file_inventory(root: Path):
    if not root.is_dir():
        raise ValueError(f"文件根目录不存在: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"不允许符号链接: {path}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append({"path": str(relative), "size": path.stat().st_size, "sha256": digest.hexdigest()})
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files-root")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    counts, fingerprints, db_name, oid = database_inventory()
    from django.conf import settings
    from django.db import connection
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "db_vendor": connection.vendor, "db_name": db_name, "database_oid": oid,
               "tables": counts, "table_sha256": fingerprints}
    if args.files_root:
        root = Path(args.files_root).resolve()
        if root != Path(settings.CUSTOMS_FILE_ROOT).resolve():
            raise ValueError("实际 Django 文件根与请求目标不一致")
        files = file_inventory(root)
        stat = root.stat()
        payload.update(file_root=str(root), file_identity={"path": str(root), "device": stat.st_dev, "inode": stat.st_ino},
                       files=files, file_count=len(files), file_bytes=sum(f["size"] for f in files))
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
