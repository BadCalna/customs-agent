"""P10 业务员退回：补料/问题回供应商，走 Issue + 审计，不冒充专家决定。

- 业务员退回复用独立的 ``task.return`` 动作（D3 第 9 节，Jay 2026-11-15 确认单列）；
- 存在待专家审核的提交时不允许业务员直接退回，避免出现"提交在审、任务又被退回"的双轨；
- 退回不创建版本、不导出文件、不发通知（B05：导出不等于退回或已发送）。
"""
from django.db import transaction

from apps.accounts.errors import ApiError
from apps.accounts.permissions import RETURN_TASK, get_visible_task, require_action

from ..models import AuditEvent, Issue, Submission, Task
from ..schemas import TaskReturnOut
from . import idempotency
from .submissions import _get_version

ACTION = "task.return"


@transaction.atomic
def return_version(user, task_id: str, payload, idempotency_key: str | None = None) -> dict:
    task = get_visible_task(user, task_id)
    require_action(user, RETURN_TASK, task)
    task = Task.objects.select_for_update().get(pk=task.pk)

    replay = idempotency.begin(user, ACTION, idempotency_key, payload)
    if replay is not None:
        return replay

    if task.stage == Task.Stage.APPROVED:
        raise ApiError(409, "task_closed", "任务已通过，不能再退回；如需变更请走补料重传")

    version = _get_version(task, payload.version_id)
    latest = task.versions.order_by("-number").first()
    if latest is not None and latest.pk != version.pk:
        raise ApiError(
            409, "stale_version",
            "退回只能针对当前最新版本；旧版本只读回查",
            {"current_version_id": str(latest.pk)},
        )
    pending = Submission.objects.filter(task=task, version=version, status=Submission.Status.PENDING).first()
    if pending is not None:
        raise ApiError(
            409, "submission_pending",
            "该版本已提交专家审核，请先由专家处置（通过或退回），不要绕过审核直接退回",
            {"submission_id": str(pending.pk)},
        )
    if payload.revision != task.revision:
        raise ApiError(
            409, "stale_revision",
            "任务内容已变化，请刷新后基于最新修订退回",
            {"current_revision": task.revision},
        )
    issues = [issue for issue in payload.issues if issue.message.strip()]
    if not issues:
        raise ApiError(422, "missing_issues", "退回必须至少给出一条具体问题，不能只退不写")

    item_ids = {str(item.pk) for item in version.items.all()}
    rows = []
    for issue in issues:
        if issue.item_id and issue.item_id not in item_ids:
            raise ApiError(
                422, "item_not_in_version",
                "问题只能定位到本版本的商品，不能引用其它版本的商品行",
                {"item_id": issue.item_id},
            )
        rows.append(Issue.objects.create(
            task=task, item_id=issue.item_id or None,
            kind="operator_return", message=issue.message, created_by=user,
        ))

    task.stage = Task.Stage.RETURNED
    task.revision += 1
    task.save(update_fields=["stage", "revision", "updated_at"])
    AuditEvent.objects.create(
        task=task, version=version, actor=user,
        action="task.returned", stage="business_return",
        reason=payload.reason or "",
        details={"return_to": "supplier", "issue_ids": [str(row.pk) for row in rows]},
    )

    response = TaskReturnOut(
        task_id=str(task.pk), version_id=str(version.pk),
        stage=task.stage, task_revision=task.revision,
        issue_ids=[str(row.pk) for row in rows], idempotent_replay=False,
    ).model_dump(mode="json")
    idempotency.store(user, ACTION, idempotency_key, payload, response)
    return response
