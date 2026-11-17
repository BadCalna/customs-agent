"""按表统计行数、按文件统计 sha256，供备份/恢复比对（P22）。

为什么不用 ``dumpdata``：P12 实测 Django 的 JSON 序列化会把 datetime 截到毫秒，
用它做“对比依据”会掩盖真实差异；这里只取**行数**与**文件哈希**，不搬业务字段。

用法（在容器里或本机 venv 里都能跑）::

    # 只统计行数
    python scripts/operations/table_counts.py

    # 行数 + 文件清单（备份/恢复校验用；大目录会慢，看的是完整性）
    python scripts/operations/table_counts.py --files-root /srv/customs/files --out counts.json

输出是 JSON，写到 --out 或 stdout。列出所有表（含 django_migrations），
但跳过 django_session：会话表随时变化，把它算进“必须一致”只会制造假告警。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 让脚本能从仓库根直接运行（compose 里 cwd 就是 /app）。
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

#: 不参与一致性比对的表：内容是易变的会话数据。
SKIP_TABLES = {"django_session"}
#: 备份时被排除的目录（P05 的暂存区，事务未提交的半成品）。
SKIP_DIRS = {"incoming"}


def table_counts() -> dict[str, int]:
    import django

    django.setup()
    from django.apps import apps

    counts: dict[str, int] = {}
    for model in apps.get_models():
        table = model._meta.db_table
        if table in SKIP_TABLES:
            continue
        counts[table] = model.objects.count()
    return counts


def file_inventory(root: Path) -> list[dict[str, object]]:
    """列出受控存储里的每个文件：相对路径、字节数、sha256。"""
    entries: list[dict[str, object]] = []
    if not root.exists():
        return entries
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(
            {"path": str(relative), "size": path.stat().st_size, "sha256": digest.hexdigest()}
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统计表行数与受控存储文件清单（P22 备份/恢复用）")
    parser.add_argument("--files-root", default=None, help="受控存储根目录；给了才统计文件")
    parser.add_argument("--out", default=None, help="输出 JSON 路径；不给则写 stdout")
    args = parser.parse_args(argv)

    from django.db import connection

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_vendor": connection.vendor,
        "db_name": connection.settings_dict.get("NAME"),
        "tables": table_counts(),
    }

    if args.files_root:
        root = Path(args.files_root)
        files = file_inventory(root)
        payload["file_root"] = str(root)
        payload["file_count"] = len(files)
        payload["file_bytes"] = sum(int(item["size"]) for item in files)
        payload["files"] = files

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {args.out}（{len(payload.get('tables', {}))} 张表"
              + (f"，{payload.get('file_count')} 个文件" if args.files_root else "") + "）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
