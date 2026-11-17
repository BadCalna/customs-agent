"""RQ entry point: only a server-generated run identifier enters the broker."""
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from apps.reviews.models import ImportJob, Task
from . import services
from .models import Run, RunWork


def perform_work(run_id):
    if not connection.in_atomic_block:
        close_old_connections()
    token = services.claim_work(run_id)
    if token is None:
        return
    try:
        run = Run.objects.select_related("version", "submission").get(pk=run_id)
        job = ImportJob.objects.filter(precheck=run).first()
        if job and job.status != "succeeded":
            from apps.reviews.services.imports import finish_import
            with transaction.atomic():
                Task.objects.select_for_update().get(pk=run.task_id)
                work = RunWork.objects.select_for_update().get(run=run)
                exhausted = work.import_attempts >= settings.CUSTOMS_ROW_MAX_ATTEMPTS
                if not exhausted:
                    work.import_attempts += 1
                    work.save(update_fields=["import_attempts", "updated_at"])
            if exhausted:
                ImportJob.objects.filter(pk=job.pk).update(status="failed", error={"code": "import_attempts_exhausted",
                    "message": "文件解析中断次数达到上限，请重试原请求或联系维护人员"})
                Run.objects.filter(pk=run.pk).update(status="failed", is_current=False, finished_at=timezone.now())
                _import_failed(run, token, "import_attempts_exhausted")
                return
            try:
                finish_import(str(job.pk), defer_precheck=True)
            except Exception as exc:
                _import_failed(run, token, type(exc).__name__)
                return
        with transaction.atomic():
            Task.objects.select_for_update().get(pk=run.task_id)
            services.register_run(run)
        for item_id in run.items.order_by("item__source_row").values_list("item_id", flat=True):
            attempt = services.begin_attempt(run_id, item_id, token)
            if attempt is None:
                continue
            try:
                output = services.evaluate_row(run, attempt.row.item)
            except Exception as exc:
                from rq.timeouts import JobTimeoutException
                if isinstance(exc, JobTimeoutException):
                    raise
                services.finish_attempt(attempt.pk, token, error=exc)
            else:
                services.finish_attempt(attempt.pk, token, output=output)
        services.finish_pass(run_id, token)
    finally:
        if not connection.in_atomic_block:
            close_old_connections()


@transaction.atomic
def _import_failed(run, token, error_code):
    Task.objects.select_for_update().get(pk=run.task_id)
    work = RunWork.objects.select_for_update().get(run=run)
    if work.state != "running" or str(work.token) != token:
        return
    work.state = "pending" if work.import_attempts < settings.CUSTOMS_ROW_MAX_ATTEMPTS else "done"
    work.token, work.lease_until, work.dispatched_at = None, None, None
    work.available_at = timezone.now() + timedelta(seconds=settings.CUSTOMS_RETRY_BACKOFF_SECONDS)
    work.last_error = error_code[:200]
    work.save()
