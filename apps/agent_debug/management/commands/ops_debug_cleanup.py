"""Expire Agent Debug jobs and apply the isolated retention policy."""
import json

from django.core.management.base import BaseCommand

from apps.agent_debug.maintenance import cleanup


class Command(BaseCommand):
    help = "清理过期 Agent Debug 作业、输入和报文，输出计数并记录审计。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写数据库或投递事件。")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self.stdout.write(json.dumps({"dry_run": dry_run, **cleanup(dry_run=dry_run)}, sort_keys=True))
