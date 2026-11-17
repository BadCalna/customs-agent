"""提交与专家显式授权在同一事务内完成；不放宽专家角色的全局可见范围。"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.errors import ApiError
from apps.accounts.models import Role, TaskAssignment, User
from ..models import AuditEvent, Submission, Task


def _select_expert(task):
    assigned = (TaskAssignment.objects.filter(task=task, role=Role.EXPERT,
                                             user__role=Role.EXPERT, user__is_active=True)
                .select_related("user").order_by("created_at", "pk").first())
    if assigned:
        return assigned.user, True
    username = settings.CUSTOMS_DEFAULT_EXPERT_USERNAME
    expert = User.objects.filter(username=username, role=Role.EXPERT, is_active=True).first() if username else None
    if expert is None:
        raise ApiError(409, "default_expert_unavailable",
                       "当前任务没有可用审核专家，请管理员配置有效的默认专家账号后重试提交")
    existing = TaskAssignment.objects.filter(task=task, user=expert).first()
    if existing and existing.role != Role.EXPERT:
        raise ApiError(409, "expert_assignment_conflict", "默认专家在该任务中的授权角色冲突，请管理员核对")
    return expert, False


def ensure_submission_expert(task, submission, *, actor, source="submission"):
    """调用方必须持有 Task 行锁；旧版或已关闭提交的重放不补回被移除的权限。"""
    latest = task.versions.order_by("-number").first()
    if (submission.task_id != task.pk or submission.status != Submission.Status.PENDING
            or task.stage != Task.Stage.SUBMITTED or latest is None or latest.pk != submission.version_id):
        return None, False
    expert, assigned = _select_expert(task)
    if assigned:
        return expert, False
    grant, created = TaskAssignment.objects.get_or_create(task=task, user=expert, defaults={"role": Role.EXPERT})
    if grant.role != Role.EXPERT:
        raise ApiError(409, "expert_assignment_conflict", "默认专家在该任务中的授权角色冲突，请管理员核对")
    if created:
        AuditEvent.objects.create(
            task=task, version=submission.version, actor=actor, action="task.expert_assigned", stage="business_submit",
            new_value=str(expert.pk), reason="当前待审任务没有有效专家，按默认专家配置建立任务授权",
            details={"expert_id": str(expert.pk), "submission_id": str(submission.pk), "source": source},
        )
    return expert, created


@transaction.atomic
def repair_pending_expert(task_id, *, apply=False):
    """显式单任务修复入口；预览不写入，也不改动提交内容、阶段或 AI 结果。"""
    try:
        task = Task.objects.select_for_update().get(pk=task_id)
    except (Task.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ApiError(404, "task_not_found", "任务不存在") from exc
    latest = task.versions.order_by("-number").first()
    submission = (Submission.objects.filter(task=task, version=latest, status=Submission.Status.PENDING)
                  .order_by("-created_at").first()) if latest else None
    if task.stage != Task.Stage.SUBMITTED or submission is None:
        raise ApiError(409, "no_pending_submission", "该任务当前版本没有待专家审核的提交，不补授权")
    expert, assigned = _select_expert(task)
    created = False
    if apply:
        expert, created = ensure_submission_expert(task, submission, actor=None, source="pending_repair")
    return {"task_id": str(task.pk), "submission_id": str(submission.pk), "expert_id": str(expert.pk),
            "expert_username": expert.username, "already_assigned": assigned, "created": created, "applied": apply}
