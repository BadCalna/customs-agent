#!/usr/bin/env python
"""P12：按依赖顺序导出 JSON fixture，并保留完整微秒精度。

为什么不用 `manage.py dumpdata`：Django 的 `DjangoJSONEncoder` 会把 datetime 截到
毫秒（`r[:23] + r[26:]`，见 django/core/serializers/json.py），用 dumpdata/loaddata
迁移会让 `created_at`/`updated_at` 这类字段丢掉微秒，且不会报错。P12 的迁移要求
数量与外键之外还要逐行内容一致，所以这里自己序列化：

- `serializers.serialize("python", ...)` 拿到的是原始 Python 值，不经过 JSON 编码；
- 再统一用 `isoformat()` 写出，`loaddata` 侧由 `parse_datetime` 按 6 位微秒解析。

    python scripts/database/dumpdata_full.py <输出文件> [app.Model ...]
"""
import datetime
import decimal
import json
import os
import sys
import uuid
from pathlib import Path

import django

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.apps import apps  # noqa: E402
from django.core import serializers  # noqa: E402

DEFAULT_MODELS = [
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


def encoder(value):
    if isinstance(value, datetime.datetime):
        # 关键差异：不做 [23] 截断，保留完整微秒。
        return value.isoformat()
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value)!r}")


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(sys.argv[1])
    labels = sys.argv[2:] or DEFAULT_MODELS
    objects = []
    for label in labels:
        model = apps.get_model(label)
        for obj in model._default_manager.all().order_by("pk"):
            objects.extend(serializers.serialize("python", [obj]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(objects, handle, ensure_ascii=False, default=encoder)
    print(f"已导出 {len(objects)} 个对象 → {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())