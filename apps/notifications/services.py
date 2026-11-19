"""本人消息与事务 outbox。不得在业务事务内发送通知。"""
import logging
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.permissions import get_visible_message, visible_tasks
from .models import DeliveryAttempt, Message, NotificationDelivery

logger = logging.getLogger(__name__)


def message_out(message, *, can_open=True):
    target = None
    if can_open and message.task_id and message.version_id:
        target = "/?" + urlencode({"task": message.task_id, "version": message.version_id})
    return dict(message_id=str(message.pk), task_id=str(message.task_id) if message.task_id else None,
                version_id=str(message.version_id) if message.version_id else None,
                submission_id=str(message.submission_id) if message.submission_id else None,
                decision_id=str(message.decision_id) if message.decision_id else None,
                kind=message.kind, title=message.title, body=message.body, is_read=message.is_read,
                read_at=message.read_at.isoformat() if message.read_at else None,
                created_at=message.created_at.isoformat(), target_url=target)


@transaction.atomic
def mark_read(user, message_id):
    message = get_visible_message(user, message_id)
    Message.objects.filter(pk=message.pk, is_read=False).update(is_read=True, read_at=timezone.now())
    message.refresh_from_db()
    return message_out(message, can_open=visible_tasks(user).filter(pk=message.task_id).exists())


@transaction.atomic
def record_message(*, recipient, task, version, kind, event_key, title, body="", submission=None, decision=None):
    if recipient is None or not recipient.is_active:
        return None
    message, created = Message.objects.get_or_create(recipient=recipient, event_key=event_key, defaults={
        "task": task, "version": version, "submission": submission, "decision": decision,
        "kind": kind, "title": title, "body": body,
    })
    if created and recipient.role in (Role.OPERATOR, Role.EXPERT):
        delivery = NotificationDelivery.objects.create(message=message)
        transaction.on_commit(lambda: deliver_one(str(delivery.pk)), robust=True)
    return message


def notify_submission(submission, expert):
    return record_message(recipient=expert, task=submission.task, version=submission.version,
                          submission=submission, kind="submission.created", event_key=f"submission:{submission.pk}",
                          title="有新的关务审核提交", body="业务员已提交本版本，请打开对应版本审核。")


def notify_decision(decision):
    submission = decision.submission
    task = submission.task
    approved = decision.action == "approve"
    recipients = [submission.created_by, task.supplier] if approved else [
        task.supplier if decision.return_to == "supplier" else submission.created_by]
    for recipient in recipients:
        record_message(recipient=recipient, task=task, version=submission.version, submission=submission,
                       decision=decision, kind="decision.approved" if approved else "decision.returned",
                       event_key=f"decision:{decision.pk}", title="关务审核已通过" if approved else "关务审核已退回",
                       body="专家已对本次提交作出决定，请打开对应版本查看。")


def notify_return(event):
    return record_message(recipient=event.task.supplier, task=event.task, version=event.version,
                          kind="task.returned", event_key=f"return:{event.pk}", title="单证已退回补充",
                          body="业务员已退回本版本，请查看问题后补充材料。")


def _claim(delivery_id, now):
    with transaction.atomic():
        row = NotificationDelivery.objects.select_for_update().filter(pk=delivery_id).first()
        if row is None or row.status not in ("pending", "processing"):
            return None
        if row.status == "processing" and row.lease_until and row.lease_until > now:
            return None
        if row.next_attempt_at and row.next_attempt_at > now:
            return None
        # 旧领取者不再能发布；保留其历史尝试，mock可用同一幂等令牌重放。
        row.history.filter(status="started").update(status="interrupted", error_code="lease_expired", finished_at=now)
        if row.attempts >= 3:
            row.status = "exhausted"
            row.last_error_code = "attempts_exhausted"
            row.lease_token = row.lease_until = None
            row.save()
            return None
        token = uuid.uuid4()
        # CAS同时保护无select_for_update的SQLite；竞争失败不占尝试次数。
        count = NotificationDelivery.objects.filter(pk=row.pk, status=row.status, attempts=row.attempts,
                                                    lease_token=row.lease_token).update(
            status="processing", attempts=row.attempts + 1, lease_token=token,
            lease_until=now + timedelta(seconds=60), next_attempt_at=None)
        if not count:
            return None
        attempt = DeliveryAttempt.objects.create(delivery=row, number=row.attempts + 1, token=token)
        row.refresh_from_db()
        return row, attempt, token


def deliver_one(delivery_id, *, transport=None, now=None):
    """单次领取；外部边界在锁外执行。失败只存固定错误码，不存异常/URL/业务正文。"""
    now = now or timezone.now()
    claimed = _claim(delivery_id, now)
    if not claimed:
        return False
    row, attempt, token = claimed
    message = row.message
    mode = settings.NOTIFICATION_MODE
    status, receipt, error = "mocked", "", ""
    if mode == "disabled":
        status = "suppressed"
    elif mode != "mock":
        status, error = "exhausted", "unsupported_mode"
    elif not message.recipient.is_active or not visible_tasks(message.recipient).filter(pk=message.task_id).exists():
        status, error = "suppressed", "recipient_unavailable"
    else:
        try:
            from adapters.notifications.dws import MockDwsTransport, MockReceipt
            result = (transport or MockDwsTransport()).send(
                delivery_id=str(row.pk), recipient_id=str(message.recipient_id), title=message.title,
                body=message.body, target_url=message_out(message)["target_url"])
            if not isinstance(result, MockReceipt) or result.mode != "mock" or result.delivery_id != str(row.pk):
                raise ValueError("invalid mock receipt")
            receipt = f"mock:{row.pk}"
        except Exception:
            status = "pending" if row.attempts < 3 else "exhausted"
            error = "transport_failed"
    finished = timezone.now()
    with transaction.atomic():
        owned = NotificationDelivery.objects.filter(pk=row.pk, status="processing", lease_token=token,
                                                    lease_until__gt=finished)
        changed = owned.update(status=status, receipt=receipt, last_error_code=error,
                               lease_token=None, lease_until=None,
                               next_attempt_at=now + timedelta(seconds=30 * row.attempts) if status == "pending" else None,
                               updated_at=finished)
        if changed:
            DeliveryAttempt.objects.filter(pk=attempt.pk, token=token, status="started").update(
                status="failed" if status == "pending" or error == "transport_failed" else status,
                error_code=error, finished_at=finished)
    return bool(changed)


def dispatch_due(*, limit=100, now=None, transport=None):
    now = now or timezone.now()
    ids = list(NotificationDelivery.objects.filter(
        Q(status="pending", next_attempt_at__isnull=True) | Q(status="pending", next_attempt_at__lte=now)
        | Q(status="processing", lease_until__lte=now) | Q(status="processing", lease_until__isnull=True)
        ).order_by("created_at").values_list("pk", flat=True)[:limit])
    completed = 0
    for delivery_id in ids:
        try:
            completed += bool(deliver_one(delivery_id, transport=transport, now=now))
        except Exception:
            logger.error("notification_dispatch_failed delivery_id=%s", delivery_id)
    return completed
