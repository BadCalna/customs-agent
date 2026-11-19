import json
import time
from django.core.management.base import BaseCommand, CommandError
from apps.notifications.services import dispatch_due


class Command(BaseCommand):
    help = "恢复持久 mock DWS outbox；本命令不会发送真实通知。"

    def add_arguments(self, parser):
        parser.add_argument("--watch", action="store_true")
        parser.add_argument("--interval", type=float, default=5)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000 or not 1 <= options["interval"] <= 60:
            raise CommandError("limit须在1到1000之间，interval须在1到60秒之间")
        try:
            while True:
                self.stdout.write(json.dumps({"mode": "mock_or_disabled", "processed": dispatch_due(limit=options["limit"])}))
                if not options["watch"]:
                    break
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            return
