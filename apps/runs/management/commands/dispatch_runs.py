import time
import logging
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from apps.runs.dispatch import dispatch_pending, recover_expired


class Command(BaseCommand):
    help = "重扫持久待运行记录并恢复过期 worker 租约；--watch 持续运行"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--interval", type=float, default=2)

    def handle(self, *args, **options):
        if settings.EXECUTOR_MODE != "rq":
            raise CommandError("此命令需要 CUSTOMS_EXECUTOR_MODE=rq 和 PostgreSQL")
        if not 0.2 <= options["interval"] <= 60:
            raise CommandError("扫描间隔必须在 0.2 到 60 秒之间")
        try:
            while True:
                recovered, sent = recover_expired(), dispatch_pending()
                # P19的持久outbox使用同一个受监管扫描进程；投递故障不停止执行任务恢复。
                try:
                    from apps.notifications.services import dispatch_due
                    dispatch_due()
                except Exception:
                    logging.getLogger(__name__).error("notification_scan_failed")
                if recovered or sent or not options["watch"]:
                    self.stdout.write(f"recovered={recovered} dispatched={sent}")
                    self.stdout.flush()
                if not options["watch"]:
                    break
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("dispatcher stopped")
