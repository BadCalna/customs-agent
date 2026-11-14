"""P10 专家处置：整批通过 / 退回（R08、R09），拒绝失效提交，改码强制留痕。

- 通过必须逐行给出终值；终值与提交值不同即改码，改码原因必填；
- 退回必须给出对象（供应商/业务员）和逐商品问题，不产生逐行终值；
- 已处置过的提交、或已有更新文件版本的旧提交，都返回 409；
- AI 核查结果只是参考：不在这里被当作批准依据，专家决定本身才是结论。
"""
from django.db import transaction
from django.http import Http404

from apps.accounts.errors import ApiError
from apps.accounts.permissions import DECIDE, require_action

from ..models import AuditEvent, Decision, DecisionItem, Issue, Submission, Task
from ..schemas import DecisionLineOut, DecisionOut
from . import idempotency
from .confirmations import normalize_hs

ACTION = "decision.resolve"


def get_submission(user, submission_id):
    try:
        submission = Submission.objects.select_related("task", "version").filter(pk=submission_id).first()
    except (ValueError, TypeError) as exc:
        raise Http404("提交不存在或不在可见范围") from exc
    if submission is None:
        raise Http404("提交不存在或不在可见范围")
    return submission


def _require_active_submission(submission):
    task = submission.task
    if submission.status != Submission.Status.PENDING:
        raise ApiError(
            409, "submission_closed",
            "该提交已被处置，不能重复决定；历史结论原样保留",
            {"submission_id": str(submission.pk), "status": submission.status},
        )
    latest = task.versions.order_by("-number").first()
    if latest is not None and latest.pk != submission.version_id:
        # 旧版本的批准不能授予新版本（B08）：此时只允许回到新版本重新走流程。
        raise ApiError(
            409, "stale_submission",
            "该提交对应的是旧文件版本，已有新版本；请在新版本上重新提交与审核",
            {"current_version_id": str(latest.pk), "submission_version_id": str(submission.version_id)},
        )
    return task


def _approve(user, submission, task, payload, decision):
    lines = list(submission.items.select_related("item").all())
    by_item = {str(line.item_id): line for line in lines}
    provided = {}
    for line in payload.items:
        if line.item_id in provided:
            raise ApiError(422, "duplicate_item", "同一商品在一次决定中只能出现一次", {"item_id": line.item_id})
        if line.item_id not in by_item:
            raise ApiError(422, "item_not_in_submission", "逐行终值只能针对提交快照中的商品", {"item_id": line.item_id})
        provided[line.item_id] = line
    missing = sorted(set(by_item) - set(provided))
    if missing:
        raise ApiError(
            422, "incomplete_items",
            "首期不允许部分商品先通过（R09）：批准必须覆盖提交中的全部商品",
            {"missing_item_ids": missing},
        )

    rows = []
    for item_id, line in by_item.items():
        final_hs = normalize_hs(provided[item_id].final_hs)
        changed = final_hs != line.submitted_hs
        reason = provided[item_id].change_reason or ""
        if changed and not reason.strip():
            raise ApiError(
                422, "missing_change_reason",
                "专家改码必须填写改码原因（R08），前值与后值都要留痕",
                {"item_id": item_id},
            )
        rows.append(DecisionItem(
            decision=decision, item=line.item,
            previous_hs=line.submitted_hs, final_hs=final_hs, change_reason=reason,
        ))
    return rows


def _return(user, submission, payload, decision):
    if not payload.return_to:
        raise ApiError(422, "missing_return_to", "退回必须指定对象：supplier（供应商）或 operator（业务员）")
    if not payload.reason.strip():
        raise ApiError(422, "missing_reason", "退回必须说明原因")
    issues = [issue for issue in payload.issues if issue.message.strip()]
    if not issues:
        raise ApiError(422, "missing_issues", "退回必须至少给出一条具体问题，不能只退不写")
    return issues


@transaction.atomic
def decide(user, submission_id: str, payload, idempotency_key: str | None = None) -> dict:
    submission = get_submission(user, submission_id)
    task = require_action(user, DECIDE, submission)

    replay = idempotency.begin(user, ACTION, idempotency_key, payload)
    if replay is not None:
        return replay

    task = _require_active_submission(submission)
    if payload.action == Decision.Action.APPROVE:
        if not payload.items:
            raise ApiError(422, "empty_decision", "整批通过必须逐行给出终值")
    elif payload.action == Decision.Action.RETURN:
        if payload.items:
            raise ApiError(422, "return_has_final_values", "退回不产生专家终值；终值只能在整批通过时给出")
    else:
        raise ApiError(422, "invalid_action", "决定只能是 approve 或 return")

    decision = Decision(
        submission=submission, action=payload.action,
        return_to=payload.return_to if payload.action == Decision.Action.RETURN else "",
        reason=payload.reason or "", created_by=user,
    )
    decision.save()

    if payload.action == Decision.Action.APPROVE:
        rows = _approve(user, submission, task, payload, decision)
        for row in rows:
            row.save()
            if row.change_reason:
                AuditEvent.objects.create(
                    task=task, version=submission.version, item=row.item, actor=user,
                    action="decision.recoded", stage="expert_decision",
                    previous_value=row.previous_hs, new_value=row.final_hs, reason=row.change_reason,
                    details={"decision_id": str(decision.pk)},
                )
        submission.status = Submission.Status.APPROVED
        task.stage = Task.Stage.APPROVED
    else:
        issues = _return(user, submission, payload, decision)
        for issue in issues:
            Issue.objects.create(
                task=task, item_id=issue.item_id if issue.item_id else None,
                kind="expert_return", message=issue.message, created_by=user,
            )
        submission.status = Submission.Status.RETURNED
        task.stage = Task.Stage.RETURNED

    submission.save(update_fields=["status"])
    task.revision += 1
    task.save(update_fields=["stage", "revision", "updated_at"])

    AuditEvent.objects.create(
        task=task, version=submission.version, actor=user,
        action="decision.approved" if payload.action == Decision.Action.APPROVE else "decision.returned",
        stage="expert_decision", reason=decision.reason,
        details={
            "decision_id": str(decision.pk), "submission_id": str(submission.pk),
            "return_to": decision.return_to,
        },
    )

    response = decision_to_out(decision, submission, task).model_dump(mode="json")
    idempotency.store(user, ACTION, idempotency_key, payload, response)
    return response


def decision_to_out(decision, submission, task, replay=False) -> DecisionOut:
    rows = [
        DecisionLineOut(
            item_id=str(row.item_id), previous_hs=row.previous_hs,
            final_hs=row.final_hs, change_reason=row.change_reason,
        )
        for row in decision.items.select_related("item").all()
    ]
    return DecisionOut(
        decision_id=str(decision.pk),
        submission_id=str(submission.pk),
        task_id=str(task.pk),
        version_id=str(submission.version_id),
        action=decision.action,
        return_to=decision.return_to or None,
        reason=decision.reason,
        items=rows,
        submission_status=submission.status,
        task_stage=task.stage,
        task_revision=task.revision,
        created_at=decision.created_at.isoformat(),
        idempotent_replay=replay,
    )
