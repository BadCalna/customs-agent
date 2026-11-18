#!/usr/bin/env python
"""Strict SQLite -> PostgreSQL row, M2M, foreign key and attachment verification.

Source must be the upgraded, consistent snapshot produced by copy_sqlite_to_pg.sh.
--skip-source only checks target integrity; it is not evidence of source equality.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import django

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import connection, models  # noqa: E402
from adapters.files.storage import StorageError, file_sha256, resolve_path  # noqa: E402
from scripts.database.model_inventory import (  # noqa: E402
    validate_generated_metadata, validate_source_schema, validate_target_metadata, verification_models,
)
from scripts.database.copy_attachments import checked_manifest, verify_manifest  # noqa: E402
from scripts.database.import_guard import require_completed_imports  # noqa: E402


def _norm_json(value):
    if isinstance(value, dict):
        return {str(k): _norm_json(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_norm_json(v) for v in value]
    return value


def _norm_datetime(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _uuid_canon(value):
    return uuid.UUID(str(value)).hex


def canon(field, value, *, sqlite_value=False):
    if value is None:
        return None
    if isinstance(field, models.JSONField):
        # PostgreSQL ORM has already decoded JSON; a JSON scalar can itself be a string.
        if sqlite_value and isinstance(value, (str, bytes)):
            value = json.loads(value)
        return _norm_json(value)
    if isinstance(field, models.DateTimeField):
        return _norm_datetime(value)
    if isinstance(field, models.BooleanField):
        return bool(value)
    if isinstance(field, models.FloatField):
        return float(value)  # JSON repr retains the full round-trippable IEEE-754 value.
    if isinstance(field, models.IntegerField):
        return int(value)
    if isinstance(field, models.UUIDField):
        return _uuid_canon(value)
    if field.is_relation:
        return canon(field.target_field, value, sqlite_value=sqlite_value)
    if isinstance(field, models.BinaryField):
        return hashlib.sha256(bytes(value)).hexdigest()
    return str(value)


def metadata_reference(field, value, source=None):
    """Generated ContentType/Permission IDs may differ: compare their natural keys."""
    if value is None or not field.is_relation:
        return None
    label = field.related_model._meta.label_lower
    if label not in {"contenttypes.contenttype", "auth.permission"}:
        return None
    if source is not None:
        if label == "contenttypes.contenttype":
            row = source.execute('SELECT app_label, model FROM django_content_type WHERE id=?', (value,)).fetchone()
        else:
            row = source.execute('SELECT p.codename,c.app_label,c.model FROM auth_permission p '
                                 'JOIN django_content_type c ON c.id=p.content_type_id WHERE p.id=?', (value,)).fetchone()
        if row is None:
            raise ValueError(f"源库元数据外键不存在：{label}:{value}")
        return tuple(row)
    obj = field.related_model._default_manager.get(pk=value)
    return tuple(obj.natural_key())


def sqlite_table_rows(conn, model, fields):
    columns = ", ".join('"' + f.column.replace('"', '""') + '"' for f in fields)
    table = model._meta.db_table.replace('"', '""')
    return [tuple(metadata_reference(f, row[i], conn) or canon(f, row[i], sqlite_value=True)
                  for i, f in enumerate(fields))
            for row in conn.execute(f'SELECT {columns} FROM "{table}"')]


def pg_rows(model, fields):
    rows = model._default_manager.values_list(*[f.attname for f in fields])
    return [tuple(metadata_reference(f, value) or canon(f, value) for f, value in zip(fields, row))
            for row in rows.iterator()]


def digest(rows):
    # Nested JSON is unhashable; canonical JSON rows also handle duplicate/M2M tuples.
    encoded = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in rows)
    return hashlib.sha256(json.dumps(encoded, ensure_ascii=False).encode()).hexdigest()


def fk_orphans(model):
    problems = []
    qi = connection.ops.quote_name
    with connection.cursor() as cursor:
        for field in model._meta.concrete_fields:
            if not field.is_relation:
                continue
            parent = field.related_model
            cursor.execute(
                f"SELECT COUNT(*) FROM {qi(model._meta.db_table)} t "
                f"WHERE t.{qi(field.column)} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {qi(parent._meta.db_table)} p "
                f"WHERE p.{qi(field.target_field.column)} = t.{qi(field.column)})")
            count = cursor.fetchone()[0]
            if count:
                problems.append(f"{model._meta.label}.{field.name} 有 {count} 行指向不存在的父记录")
    return problems


def attachment_paths(file_root=None):
    require_completed_imports(file_root)
    paths = []
    for label in ("reviews.FileVersion", "reviews.ItemImage"):
        model = apps.get_model(label)
        paths.extend(p for p in model._default_manager.values_list("storage_path", flat=True) if p)
    return paths


def attachment_report(file_root):
    paths, missing = attachment_paths(file_root), []
    for path in paths:
        try:
            candidate = resolve_path(file_root, path)
            with candidate.open("rb"):
                pass
        except (StorageError, OSError):
            missing.append(path)
    return len(paths), missing


def attachment_manifest(file_root):
    """Capture source bytes before import, and recapture them after verification."""
    _, missing = attachment_report(file_root)
    if missing:
        raise ValueError(f"附件实体缺失或路径越界：{missing}")
    return [{"path": path, "size": resolve_path(file_root, path).stat().st_size,
             "sha256": file_sha256(file_root, path)} for path in sorted(set(attachment_paths(file_root)))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=".local/db.sqlite3")
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument("--json")
    parser.add_argument("--files-manifest", help="迁移前 attachment_manifest 生成的文件指纹 JSON")
    parser.add_argument("--source-files-root", help="原 SQLite 部署的附件根，完整源目标核对必填")
    parser.add_argument("--target-files-root", default=str(settings.CUSTOMS_FILE_ROOT))
    args = parser.parse_args()
    if not args.skip_source and (not args.files_manifest or not args.source_files_root):
        parser.error("源/目标完整核对必须提供 --files-manifest 迁移前指纹及 --source-files-root")
    target_root = Path(args.target_files_root).expanduser()
    if not target_root.is_absolute():
        target_root = REPO_ROOT / target_root
    metadata_errors = []
    try:
        validate_target_metadata(connection)
    except ValueError as exc:
        metadata_errors.append(str(exc))
    if connection.vendor != "postgresql":
        print(f"目标库不是 PostgreSQL（vendor={connection.vendor}）", file=sys.stderr)
        return 2
    source_path = Path(args.source)
    if not source_path.is_absolute():
        source_path = REPO_ROOT / source_path
    source = None
    if not args.skip_source:
        if not source_path.is_file():
            print(f"找不到 SQLite 源库：{source_path}", file=sys.stderr)
            return 2
        source = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    results, failures = [], 0
    try:
        if source is not None:
            validate_source_schema(source)
            validate_generated_metadata(source)
        for model in verification_models():
            # loaddata preserves M2M relationships; surrogate through IDs are not business keys.
            fields = [f for f in model._meta.concrete_fields
                      if not (model._meta.auto_created and f.primary_key)]
            pg = pg_rows(model, fields)
            entry = {"model": model._meta.label, "table": model._meta.db_table, "pg_rows": len(pg)}
            if source is not None:
                src = sqlite_table_rows(source, model, fields)
                entry.update(source_rows=len(src), source_digest=digest(src), pg_digest=digest(pg),
                             digest_match=digest(src) == digest(pg))
                failures += int(not entry["digest_match"])
            orphans = fk_orphans(model)
            entry["fk_orphans"] = orphans
            failures += len(orphans)
            results.append(entry)
    finally:
        if source is not None:
            source.close()
    count, missing = attachment_report(target_root)
    failures += len(missing) + len(metadata_errors)
    files = []
    for path in sorted(set(attachment_paths(target_root)) - set(missing)):
        candidate = resolve_path(target_root, path)
        files.append({"path": path, "size": candidate.stat().st_size,
                      "sha256": file_sha256(target_root, path)})
    files_match, source_files_match = None, None
    source_files_error = None
    if args.files_manifest:
        baseline = checked_manifest(json.loads(Path(args.files_manifest).read_text(encoding="utf-8")))
        files_match = baseline == files
        failures += int(not files_match)
        if args.source_files_root:
            source_root = Path(args.source_files_root).expanduser()
            if not source_root.is_absolute():
                source_root = REPO_ROOT / source_root
            try:
                verify_manifest(source_root, baseline)
                source_files_match = True
            except (ValueError, StorageError, OSError) as exc:
                source_files_match, source_files_error = False, str(exc)
                failures += 1
    summary = {"database": connection.settings_dict["NAME"], "source": None if args.skip_source else str(source_path),
               "results": results, "attachment_references": count, "files": files,
               "files_match": files_match, "files_manifest": args.files_manifest,
               "source_files_root": args.source_files_root, "target_files_root": str(target_root.resolve()),
               "source_files_match": source_files_match, "source_files_error": source_files_error,
               "metadata_match": not metadata_errors, "metadata_errors": metadata_errors,
               "missing_files": missing, "warnings": [], "failures": failures}
    print(f"\n迁移核对：{connection.settings_dict['NAME']} @ {connection.settings_dict['HOST']}:{connection.settings_dict['PORT']}")
    for entry in results:
        match = entry.get("digest_match")
        verdict = "一致" if match else ("未比对源库" if match is None else "不一致")
        print(f"{entry['model']}: 源 {entry.get('source_rows', '-')} / PG {entry['pg_rows']}，{verdict}，外键孤儿 {len(entry['fk_orphans'])}")
    print(f"附件：{count} 条引用，{len(files)} 个实体，缺失/越界 {len(missing)}；迁移前后指纹一致={files_match}")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        print(f"核对未通过：{failures} 处内容/外键/附件问题", file=sys.stderr)
        return 1
    print("核对通过：" + ("目标内部完整性" if args.skip_source else "全模型、M2M、逐行内容、外键与附件实体"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
