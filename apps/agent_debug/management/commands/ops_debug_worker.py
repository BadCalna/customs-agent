"""Run only Agent Debug jobs, without touching the production worker."""
import json
import signal
from threading import Event, current_thread, main_thread

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections


class Command(BaseCommand):
    help = "领取 Agent Debug 作业；默认清空当前队列，--watch 持续轮询。"

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--once", action="store_true", help="至多领取一个作业后退出。")
        mode.add_argument("--watch", action="store_true", help="持续轮询，直到收到 SIGTERM/SIGINT。")
        parser.add_argument("--interval", type=float, default=2, help="空闲轮询间隔（秒，默认 2）。")

    def handle(self, *args, **options):
        from apps.agent_debug.services import work_once

        interval = options["interval"]
        if not 0.1 <= interval <= 60:
            raise CommandError("interval 须在 0.1 到 60 秒之间")

        stopping = Event()
        previous_handlers = {}

        def stop(signum, frame):
            stopping.set()

        processed = 0
        try:
            if current_thread() is main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[signum] = signal.signal(signum, stop)
            while not stopping.is_set():
                if options["watch"]:
                    close_old_connections()
                claimed = work_once()
                processed += int(claimed)
                if options["once"] or not claimed and not options["watch"]:
                    break
                if not claimed:
                    stopping.wait(interval)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        self.stdout.write(json.dumps({"processed": processed}))
