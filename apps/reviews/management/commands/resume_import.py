"""显式恢复 P06 持久导入凭据；不提供真实后台队列。"""
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from apps.accounts.errors import ApiError
from apps.reviews.models import ImportJob
from apps.reviews.services.imports import finish_import, queue_import


class Command(BaseCommand):
    help = "从已提交的 P06 import_id 继续发布/解析；复用原版本和预审执行"

    def add_arguments(self, parser):
        parser.add_argument("import_id")

    def handle(self, *args, **options):
        try:
            result = (queue_import if settings.EXECUTOR_MODE == "rq" else finish_import)(options["import_id"], replay=True)
        except (ImportJob.DoesNotExist, ApiError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{result['parse_status']} V{result['version_number']} items={result['item_count']} mock={result['precheck_status']}")
