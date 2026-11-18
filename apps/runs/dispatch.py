"""Database outbox scanning: at-least-once delivery, fenced worker ownership."""
from datetime import timedelta
import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.reviews.models import Task
from .models import Run, RunAttempt, RunWork

logger = logging.getLogger(__name__)


def _enqueue(run_id):
    import django_rq
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    from .jobs import perform_work

    connection = Redis.from_url(settings.RQ_QUEUES[settings.CUSTOMS_QUEUE_NAME]["URL"],
        socket_connect_timeout=0.5, socket_timeout=0.5, retry=Retry(NoBackoff(), 0))
    try:
        django_rq.get_queue(settings.CUSTOMS_QUEUE_NAME, connection=connection).enqueue(
            perform_work, str(run_id), job_id=f"customs-{run_id}-{uuid.uuid4().hex}",
            job_timeout=settings.CUSTOMS_QUEUE_TIMEOUT, result_ttl=60, failure_ttl=86400)
    finally:
        connection.close()


def safe_dispatch(run_id):
    """An unavailable broker must not turn a committed submission into HTTP 500."""
    try:
        return dispatch_pending(run_id=run_id)
    except Exception as exc:
        logger.warning("P13 dispatch deferred for %s (%s)", run_id, type(exc).__name__)
        return 0


def dispatch_pending(*, run_id=None, limit=100):
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.CUSTOMS_QUEUE_REDELIVERY_SECONDS)
    sent = 0
    for _ in range(limit):
        with transaction.atomic():
            due = RunWork.objects.select_for_update(skip_locked=True).filter(
                state="pending", available_at__lte=now,
            ).filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lte=cutoff))
            if run_id:
                due = due.filter(run_id=run_id)
            work = due.order_by("available_at").first()
            if work is None:
                break
            dispatch_stamp = timezone.now()
            RunWork.objects.filter(pk=work.pk).update(dispatched_at=dispatch_stamp)
        try:
            _enqueue(work.run_id)
        except Exception as exc:
            logger.warning("P13 broker unavailable; durable work retained (%s)", type(exc).__name__)
            RunWork.objects.filter(pk=work.pk, state="pending", dispatched_at=dispatch_stamp).update(dispatched_at=None)
            break
        sent += 1
    return sent


def recover_expired(*, limit=100):
    ids = list(RunWork.objects.filter(state="running", lease_until__lte=timezone.now())
               .values_list("run_id", flat=True)[:limit])
    recovered = 0
    for run_id in ids:
        with transaction.atomic():
            initial = Run.objects.get(pk=run_id)
            Task.objects.select_for_update().get(pk=initial.task_id)
            run = Run.objects.select_for_update().get(pk=run_id)
            work = RunWork.objects.select_for_update().get(run=run)
            if work.state != "running" or work.lease_until > timezone.now():
                continue
            from .services import _engine_consistent
            if not _engine_consistent(run, work):
                continue
            for attempt in RunAttempt.objects.filter(Q(row__run=run) | Q(run=run), status="running"):
                attempt.status, attempt.error_code, attempt.finished_at = "interrupted", "worker_lease_expired", timezone.now()
                attempt.save(update_fields=["status", "error_code", "finished_at"])
                row = attempt.row
                if row is None:
                    continue
                row.status = "queued" if attempt.number < settings.CUSTOMS_ROW_MAX_ATTEMPTS else "failed"
                row.error_message = f"后台执行中断，已尝试 {attempt.number} 次"
                row.save(update_fields=["status", "error_message", "updated_at"])
            work.state, work.token, work.lease_until = "pending", None, None
            work.dispatched_at, work.available_at = None, timezone.now()
            work.last_error = "worker_lease_expired"
            work.save()
            run.status = "queued"
            run.save(update_fields=["status"])
            recovered += 1
    return recovered
