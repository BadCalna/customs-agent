"""修复既有当前 pending 提交缺专家授权；默认只预览，必须显式 --apply。"""
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.errors import ApiError
from apps.reviews.services.reviewers import repair_pending_expert


class Command(BaseCommand):
    help = "为显式任务的当前待审提交补默认专家授权；不修改提交或审核决定"

    def add_arguments(self, parser):
        parser.add_argument("task_id")
        parser.add_argument("--apply", action="store_true", help="实际补权；默认预览不写入")

    def handle(self, *args, **options):
        try:
            result = repair_pending_expert(options["task_id"], apply=options["apply"])
        except ApiError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
        state = "已有有效专家" if result["already_assigned"] else "已补授权" if result["created"] else "待补授权"
        self.stdout.write(f"{'执行' if result['applied'] else '预览'}：task={result['task_id']} "
                          f"submission={result['submission_id']} expert={result['expert_username']}；{state}")
