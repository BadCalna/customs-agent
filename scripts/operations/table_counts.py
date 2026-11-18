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
import re
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
        if connection.vendor == "postgresql":
            cursor.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.relname""")
            tables = [row[0] for row in cursor.fetchall() if row[0] not in SKIP_TABLES]
        else:
            tables = sorted(set(connection.introspection.table_names(cursor)) - SKIP_TABLES)
        for table in tables:
            qualified = connection.ops.quote_name(table)
            if connection.vendor == "postgresql":
                qualified = '"public".' + qualified
            cursor.execute(f"SELECT * FROM {qualified}")
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


def normalize_pg_definition(definition):
    """Normalize only PG dump's known varchar-literal ARRAY -> text rewrite.

    varchar WITHOUT typmod to text is lossless. Keep all casts elsewhere,
    including numeric casts, bounded varchar and business expressions.
    """
    quoted = r"'(?:[^']|'')*'"
    literal = quoted + r"::character varying"
    pattern = re.compile(rf"ARRAY\[(?P<items>{literal}(?:::\btext\b)?(?:, {literal}(?:::\btext\b)?)*)\](?P<outer>::text\[\])?")
    item_pattern = re.compile(rf"(?P<quoted>{quoted})::character varying(?P<text>::text)?")
    output, i = [], 0
    while i < len(definition):
        start = i
        char = definition[i]
        if definition.startswith("--", i):
            end = definition.find("\n", i + 2)
            i = len(definition) if end < 0 else end
        elif definition.startswith("/*", i):
            i, depth = i + 2, 1
            while i < len(definition) and depth:
                if definition.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif definition.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif char in ("'", '"'):
            escape = char == "'" and i > 0 and definition[i - 1] in "eE" and (i < 2 or not (definition[i - 2].isalnum() or definition[i - 2] in "_$"))
            i += 1
            while i < len(definition):
                if escape and definition[i] == "\\":
                    i += 2
                elif definition[i] == char:
                    if i + 1 < len(definition) and definition[i + 1] == char:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
        elif char == "$" and (tag := re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", definition[i:])):
            delimiter = tag.group(0)
            end = definition.find(delimiter, i + len(delimiter))
            i = len(definition) if end < 0 else end + len(delimiter)
        else:
            # Only a code-token ARRAY start can match. Never scan its literals
            # again, or rewrite cast-looking text inside those literals.
            match = pattern.match(definition, i) if (i == 0 or not (definition[i - 1].isalnum() or definition[i - 1] in "_$")) else None
            if match:
                items = list(item_pattern.finditer(match.group("items")))
                if match.group("outer") or any(item.group("text") for item in items):
                    output.append("ARRAY[" + ", ".join(item.group("quoted") + "::text" for item in items) + "]")
                else:
                    output.append(match.group(0))
                i = match.end()
                continue
            i += 1
        output.append(definition[start:i])
    return "".join(output)


def postgres_metadata(connection):
    """Portable logical definitions/state, never OIDs, owner or database name.

    Scope: user relations, columns/defaults/types/nullability/identity, constraints,
    indexes and sequence definitions/ownership/state. Not a role/ACL/extension or
    stored-procedure body audit. Session table schema is included (rows aren't).
    """
    schema = {}
    namespace = "n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast' AND n.nspname !~ '^pg_temp'"
    queries = {
        "relations": f"""SELECT n.nspname,c.relname,c.relkind::text,
            CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid,true) ELSE NULL END
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {namespace} AND c.relkind IN ('r','p','v','m','f') ORDER BY 1,2""",
        "columns": f"""SELECT n.nspname,c.relname,a.attnum,a.attname,
            format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity::text,a.attgenerated::text,
            pg_get_expr(d.adbin,d.adrelid),coll.collname
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
            WHERE {namespace} AND c.relkind IN ('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY 1,2,3""",
        "constraints": f"""SELECT n.nspname,c.relname,k.conname,k.contype::text,
            pg_get_constraintdef(k.oid,true),k.convalidated,k.condeferrable,k.condeferred
            FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {namespace} ORDER BY 1,2,3""",
        "indexes": f"""SELECT n.nspname,c.relname,ic.relname,pg_get_indexdef(i.indexrelid),
            i.indisvalid,i.indisready
            FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
            JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {namespace} ORDER BY 1,2,3""",
    }
    sequences = {}
    with connection.cursor() as cursor:
        cursor.execute(f"""SELECT n.nspname,c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {namespace} AND n.nspname <> 'public' AND c.relkind IN ('r','p','v','m','f','S')""")
        unsupported = cursor.fetchall()
        if unsupported:
            raise ValueError(f"暂不支持 public 以外的用户关系，拒绝不完整验收: {unsupported}")
        for category, sql in queries.items():
            cursor.execute(sql)
            schema[category] = [list(row) for row in cursor.fetchall()]
            if category == "columns":
                # Physical attnum gaps from dropped columns are not logical schema.
                schema[category] = [row[:2] + [position] + row[3:] for position, row in enumerate(schema[category])]
            elif category == "constraints":
                for row in schema[category]:
                    row[4] = normalize_pg_definition(row[4])
        cursor.execute(f"""SELECT n.nspname,c.relname,format_type(s.seqtypid,NULL),s.seqstart,
            s.seqincrement,s.seqmax,s.seqmin,s.seqcache,s.seqcycle,tn.nspname,t.relname,a.attname
            FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_depend dep ON dep.classid='pg_class'::regclass AND dep.objid=c.oid
              AND dep.refclassid='pg_class'::regclass AND dep.deptype IN ('a','i')
            LEFT JOIN pg_class t ON t.oid=dep.refobjid
            LEFT JOIN pg_namespace tn ON tn.oid=t.relnamespace
            LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=dep.refobjsubid
            WHERE {namespace} ORDER BY 1,2""")
        definitions = cursor.fetchall()
        for row in definitions:
            namespace_name, name, *definition = row
            qualified = connection.ops.quote_name(namespace_name) + "." + connection.ops.quote_name(name)
            cursor.execute(f"SELECT last_value,is_called FROM {qualified}")
            last_value, is_called = cursor.fetchone()
            sequences[qualified] = {"definition": list(definition), "last_value": last_value, "is_called": is_called}
    return schema, sequences


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


def validate_import_originals(files):
    """A COMPLETE backup must contain every registered import original."""
    from apps.reviews.models import ImportJob

    archived = {entry["path"].replace("\\", "/"): entry["sha256"] for entry in files}
    for job in ImportJob.objects.select_related("version").iterator():
        path = job.version.storage_path
        if not path or archived.get(path) != job.sha256:
            raise ValueError(
                f"导入原件未保全或哈希不符，拒绝标记完整备份: "
                f"import_id={job.pk}, version_id={job.version_id}, status={job.status}"
            )


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
    if connection.vendor == "postgresql":
        payload["database_schema"], payload["sequences"] = postgres_metadata(connection)
    if args.files_root:
        root = Path(args.files_root).resolve()
        if root != Path(settings.CUSTOMS_FILE_ROOT).resolve():
            raise ValueError("实际 Django 文件根与请求目标不一致")
        files = file_inventory(root)
        validate_import_originals(files)
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
