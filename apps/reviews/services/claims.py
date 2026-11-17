"""P06 认领边界。候选范围与对象读权限分开，只有认领后才授予 TaskAssignment。"""
import sqlite3
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Exists, OuterRef, Subquery
from django.http import Http404
from django.utils.module_loading import import_string

from apps.accounts.models import Role, TaskAssignment, User
from apps.accounts.permissions import _require_authenticated
from apps.accounts.errors import ApiError
from apps.reviews.models import AuditEvent, FileVersion, ImportJob, Task, TaskClaim
from . import idempotency

CLAIM = "task.claim"
CLAIM_LIST = "task.claim.list"


def require_operator(user):
    _require_authenticated(user)
    if user.role != Role.OPERATOR:
        raise PermissionDenied("只有业务员可以查看待认领任务或认领")


def scope_tasks(user):
    """可插拔 QuerySet 策略。默认本试用空间全部在职业务员共享成功导入任务池。

    自定义策略接收 (user, queryset)，须返回同模型的收窄集合，列表和写入共用。
    无论策略如何都不允许扩大到未解析成功或已授权任务。
    """
    require_operator(user)
    current_status = FileVersion.objects.filter(task_id=OuterRef("pk")).order_by("-number").values("import_job__status")[:1]
    queryset = Task.objects.annotate(current_import_status=Subquery(current_status)).filter(current_import_status=ImportJob.Status.SUCCEEDED)
    policy = getattr(settings, "CUSTOMS_CLAIM_SCOPE_POLICY", "")
    if policy:
        queryset = queryset.filter(pk__in=import_string(policy)(user, queryset).values("pk"))
    return queryset


def claimable_tasks(user):
    assigned = TaskAssignment.objects.filter(task_id=OuterRef("pk"), role=Role.OPERATOR)
    return scope_tasks(user).annotate(has_operator=Exists(assigned)).filter(
        has_operator=False, claim__isnull=True,
    )


def validate_key(key):
    if not key or not key.strip() or len(key) > 128:
        raise ApiError(422, "invalid_idempotency_key", "请提供 1 至 128 字符的 Idempotency-Key")


def claim_task(user, task_id, key):
    require_operator(user)
    validate_key(key)
    # SQLite 的 read -> write 升级可能直接报 SQLITE_BUSY/LOCKED；必须离开失败事务后重试。
    for attempt in range(4):
        try:
            return _claim_once(user, task_id, key)
        except OperationalError as exc:
            cause = getattr(exc, "__cause__", None)
            code = getattr(cause, "sqlite_errorcode", 0) or 0
            locked = (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            if connection.vendor != "sqlite" or not locked:
                raise
        except IntegrityError:
            # 只重试已经出现认领凭据的竞争，其他完整性错误仍原样上报。
            if not TaskClaim.objects.filter(task_id=task_id).exists():
                raise
        if attempt < 3:
            time.sleep(0.025 * (attempt + 1))
    raise ApiError(409, "claim_busy", "任务正在被认领，请刷新或用同一幂等键重试", {"retryable": True})


def _claim_once(user, task_id, key):
    payload = {"task_id": str(task_id)}
    with transaction.atomic():
        # 同操作者的同 key 跨对象请求也串行，随后锁任务与现有人工操作共用。
        User.objects.select_for_update().get(pk=user.pk)
        try:
            task = Task.objects.select_for_update().get(pk=task_id)
        except (Task.DoesNotExist, ValidationError, ValueError) as exc:
            raise Http404("任务不存在或不在可认领范围") from exc
        owned = TaskAssignment.objects.filter(task=task, user=user, role=Role.OPERATOR).exists()
        if not owned and not scope_tasks(user).filter(pk=task.pk).exists():
            raise Http404("任务不存在或不在可认领范围")
        replay = idempotency.begin(user, CLAIM, key, payload, target_id=str(task.pk))
        if replay is not None:
            return replay
        claim = TaskClaim.objects.filter(task=task).first()
        if claim is not None and claim.operator_id == user.pk:
            response = _response(claim, replay=True)
        else:
            if claim is not None or TaskAssignment.objects.filter(task=task, role=Role.OPERATOR).exists():
                raise ApiError(409, "task_already_claimed", "任务已被业务员认领，请刷新待认领列表")
            claim = TaskClaim.objects.create(task=task, operator=user)
            TaskAssignment.objects.create(task=task, user=user, role=Role.OPERATOR)
            AuditEvent.objects.create(task=task, actor=user, action=CLAIM, details={
                "operator_id": str(user.pk), "claimed_at": claim.claimed_at.isoformat(),
            })
            response = _response(claim)
        idempotency.store(user, CLAIM, key, payload, response, target_id=str(task.pk))
        return response


def _response(claim, replay=False):
    return {"task_id": str(claim.task_id), "operator_id": str(claim.operator_id),
            "claimed_at": claim.claimed_at.isoformat(), "assignment_status": "claimed",
            "idempotent_replay": replay}
