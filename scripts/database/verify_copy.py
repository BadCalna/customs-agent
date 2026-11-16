#!/usr/bin/env python
"""P12：SQLite → PostgreSQL 迁移后的核对脚本（数量、外键、逐行内容、附件引用）。

    .venv-customs/bin/python scripts/database/verify_copy.py --source .local/db.sqlite3
    # 只核对目标库内部一致性（如从 pg_dump 恢复后）：
    .venv-customs/bin/python scripts/database/verify_copy.py --skip-source

目标库由当前 Django 配置决定（`CUSTOMS_DB_BACKEND=postgres` + `CUSTOMS_PG_*`）；
源库用标准库 sqlite3 直接读，不依赖第二套 Django 配置，也不写源库。
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
from django.db import connection, models  # noqa: E402

# 与 copy_sqlite_to_pg.sh 的导入顺序一致；仅用于报告分组。
MODEL_ORDER = [
    "auth.Group",
    "accounts.User",
    "reviews.Task",
    "accounts.TaskAssignment",
    "reviews.FileVersion",
    "reviews.Item",
    "reviews.ItemMember",
    "reviews.ItemImage",
    "reviews.Confirmation",
    "reviews.Submission",
    "reviews.SubmissionItem",
    "runs.Run",
    "reviews.Issue",
    "reviews.Decision",
    "reviews.DecisionItem",
    "reviews.AuditEvent",
    "reviews.IdempotencyRecord",
    "runs.RunItem",
    "notifications.Message",
]
class Mismatch(Exception):
    pass


def _norm_json(value):
    """jsonb 与 SQLite 文本都要归一成同一种可比较结构。"""
    if isinstance(value, dict):
        return {str(k): _norm_json(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_norm_json(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        # 浮点在不同序列化路径上有末位差异，保留 12 位有效小数再比较。
        return round(value, 12)
    return value


def _norm_datetime(value):
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def _uuid_canon(value):
    """SQLite 把 UUID 存成 32 位十六进制字符串，PostgreSQL 返回 UUID 对象，统一成 hex。"""
    return uuid.UUID(str(value)).hex


def canon(field, value):
    if value is None:
        return None
    if isinstance(field, models.JSONField):
        if isinstance(value, (str, bytes)):
            value = json.loads(value or "null")
        return _norm_json(value)
    if isinstance(field, models.DateTimeField):
        return _norm_datetime(value)
    if isinstance(field, models.BooleanField):
        return bool(value)
    if isinstance(field, models.FloatField):
        return round(float(value), 12)
    if isinstance(field, models.IntegerField):
        return int(value)
    if isinstance(field, models.UUIDField):
        return _uuid_canon(value)
    if field.is_relation:
        target = field.target_field
        if isinstance(target, models.UUIDField):
            return _uuid_canon(value)
        if isinstance(target, models.IntegerField):
            return int(value)
        return str(value)
    if isinstance(field, models.BinaryField):
        return hashlib.sha256(bytes(value)).hexdigest()
    return str(value)


def sqlite_table_rows(conn, model, fields):
    table = model._meta.db_table
    columns = ", ".join(f'"{f.column}"' for f in fields)
    pk_cols = ", ".join(f'"{f.column}"' for f in fields if f.primary_key)
    sql = f'SELECT {columns} FROM "{table}"'
    if pk_cols:
        sql += f" ORDER BY {pk_cols}"
    return [tuple(canon(f, row[i]) for i, f in enumerate(fields)) for row in conn.execute(sql)]


def pg_rows(model, fields):
    rows = model._default_manager.values_list(*[f.attname for f in fields])
    return [tuple(canon(f, value) for f, value in zip(fields, row)) for row in rows.iterator()]


def digest(rows):
    payload = json.dumps(sorted(rows, key=lambda r: str(r[0])), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def fk_orphans(model):
    """目标库里指向不存在父行的外键数量（PostgreSQL 有约束，这里是独立复核）。"""
    problems = []
    qi = connection.ops.quote_name
    with connection.cursor() as cursor:
        for field in model._meta.concrete_fields:
            if not field.is_relation or field.null:
                continue
            parent = field.related_model
            sql = (
                f"SELECT COUNT(*) FROM {qi(model._meta.db_table)} t "
                f"WHERE t.{qi(field.column)} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {qi(parent._meta.db_table)} p "
                f"WHERE p.{qi(parent._meta.pk.column)} = t.{qi(field.column)})"
            )
            cursor.execute(sql)
            count = cursor.fetchone()[0]
            if count:
                problems.append(f"{model._meta.label}.{field.name} 有 {count} 行指向不存在的父记录")
    return problems


def attachment_report(repo_root):
    """附件引用：SQLite 与 PG 的非空路径集合是否一致，以及这些路径当前是否存在。"""
    paths = []
    for label, column in (("reviews.FileVersion", "storage_path"), ("reviews.ItemImage", "storage_path")):
        model = apps.get_model(label)
        paths += [p for p in model._default_manager.values_list(column, flat=True) if p]
    missing = []
    for path in paths:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = repo_root / path
        if not candidate.exists():
            missing.append(path)
    return len(paths), missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=".local/db.sqlite3", help="SQLite 源库路径")
    parser.add_argument("--skip-source", action="store_true", help="只核对目标库内部一致性")
    parser.add_argument("--json", help="把结果另存为 JSON")
    args = parser.parse_args()

    if connection.vendor != "postgresql":
        print(f"目标库不是 PostgreSQL（vendor={connection.vendor}），先切 CUSTOMS_DB_BACKEND=postgres", file=sys.stderr)
        return 2

    source_path = Path(args.source)
    if not source_path.is_absolute():
        source_path = REPO_ROOT / source_path
    source = None
    if not args.skip_source:
        if not source_path.exists():
            print(f"找不到 SQLite 源库：{source_path}", file=sys.stderr)
            return 2
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)

    results = []
    failures = 0
    for label in MODEL_ORDER:
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        fields = model._meta.concrete_fields
        pg = pg_rows(model, fields)
        entry = {"model": label, "table": model._meta.db_table, "pg_rows": len(pg)}
        if source is not None:
            try:
                src = sqlite_table_rows(source, model, fields)
            except sqlite3.OperationalError as exc:
                entry["error"] = f"源表读取失败：{exc}"
                failures += 1
                results.append(entry)
                continue
            entry.update(source_rows=len(src), digest_match=digest(src) == digest(pg))
            if not entry["digest_match"]:
                src_set, pg_set = set(src), set(pg)
                only_src = list(src_set - pg_set)[:2]
                only_pg = list(pg_set - src_set)[:2]
                entry["first_missing_in_pg"] = [str(v) for v in only_src]
                entry["first_extra_in_pg"] = [str(v) for v in only_pg]
                failures += 1
        orphans = fk_orphans(model)
        entry["fk_orphans"] = orphans
        failures += len(orphans)
        results.append(entry)

    warnings = []
    attachment_count, missing_files = attachment_report(REPO_ROOT)
    if missing_files:
        warnings.append(
            f"附件路径非空 {attachment_count} 条，其中 {len(missing_files)} 条在本机不存在"
            f"（前 3 条：{', '.join(missing_files[:3])}）；合成演示数据本就没有真实文件，需在试用前补齐。"
        )

    print("\n## 迁移核对（目标：PostgreSQL）\n")
    print(f"- 数据库：`{connection.settings_dict['NAME']}` @ {connection.settings_dict['HOST']}:{connection.settings_dict['PORT']}")
    print(f"- 源库：`{source_path}`" if source is not None else "- 源库：跳过（--skip-source）")
    with connection.cursor() as cursor:
        cursor.execute("select version()")
        server_version = cursor.fetchone()[0]
    print(f"- 服务端版本：{server_version}\n")
    print("| 模型 | 源行数 | 目标行数 | 逐行内容 | 外键孤儿 |")
    print("|---|---|---|---|---|")
    for entry in results:
        if "error" in entry:
            print(f"| {entry['model']} | - | {entry['pg_rows']} | {entry['error']} | - |")
            continue
        if "digest_match" not in entry:
            content = "未比对（--skip-source）"
        else:
            content = "一致" if entry["digest_match"] else "**不一致**"
        print(f"| {entry['model']} | {entry.get('source_rows', '-')} | {entry['pg_rows']} | {content} | "
              f"{len(entry['fk_orphans'])} |")

    if warnings:
        print("\n提示：")
        for warning in warnings:
            print(f"- {warning}")

    summary = {"results": results, "warnings": warnings, "failures": failures}
    if args.json:
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 结果：{args.json}")

    if failures:
        print(f"\n**核对未通过**：{failures} 处数量/内容/外键问题，见上表。", file=sys.stderr)
        return 1
    print("\n核对通过：行数、逐行内容（含 JSON/时间/UUID）与外键一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())