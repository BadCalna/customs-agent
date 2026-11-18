"""Explicit, PostgreSQL-only administrative snapshot import (no operational tables)."""
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from apps.history_reference.services import HistoryImportError, import_history_snapshot


class Command(BaseCommand):
    help = "导入 P15 历史参考快照；必须明确核对目标数据库名；默认保留 changed/missing"

    def add_arguments(self, parser):
        parser.add_argument("--records", required=True)
        parser.add_argument("--dataset-key", required=True)
        parser.add_argument("--expected-count", type=int)
        parser.add_argument("--require-real", action="store_true")
        parser.add_argument("--replace-changed", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--report", help="可选私有审计 JSON，0600 新建且拒绝覆盖已有文件")
        parser.add_argument("--expect-database", required=True, help="必须与当前连接的实际数据库名称完全一致")

    def handle(self, *args, **options):
        connection = connections["default"]
        if connection.vendor != "postgresql":
            raise CommandError("仅允许 PostgreSQL，未执行导入")
        # Do not trust an environment label: check the database actually connected.
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                actual_name = cursor.fetchone()[0]
        except Exception as exc:
            raise CommandError(f"无法核对目标数据库（{type(exc).__name__}），未执行导入") from exc
        if options["expect_database"] != actual_name:
            raise CommandError("--expect-database 与实际数据库不匹配，未执行导入")
        try:
            report = import_history_snapshot(
                options["records"], options["dataset_key"],
                replace_changed=options["replace_changed"], expected_count=options["expected_count"],
                require_real=options["require_real"], dry_run=options["dry_run"])
        except HistoryImportError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(f"导入失败并已整批回滚（{type(exc).__name__}）") from exc
        if options["report"]:
            try:
                fd = os.open(options["report"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2)
            except OSError as exc:
                raise CommandError(f"导入事务已完成，但私有报告未写入（{type(exc).__name__}）") from exc
        counts = {key: report[key] for key in (
            "status", "dry_run", "input_total", "total", "added", "replaced", "duplicate",
            "changed_kept", "missing", "conflicts", "needs_review", "revision")}
        self.stdout.write(json.dumps(counts, ensure_ascii=False, sort_keys=True))
        if report["status"] == "partial":
            raise CommandError("批次已原子提交（dry-run 则已回滚）；changed/conflict 保留旧记录，请审阅私有报告", returncode=2)
