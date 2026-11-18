"""P09 业务员整批提交：不可变快照、幂等、提交后自动专家核查（R09、R13、D4 第 4 节）。

关键口径：
- 一次提交必须覆盖该版本**全部**商品行，且每行显式引用一条确认记录；
- 引用的确认必须仍是该商品当前有效确认，否则 409（旧确认已过期）；
- 快照落库后输入固定，后续编辑只影响下一次提交；
- 同事务内登记专家核查的待运行 ``Run``（queued），提交成功后才由执行器执行；
  inline 只是 M0/M1 的短时模拟，恢复与重试由 P13 接管；
- 同版本已有待审提交时，内容相同的重复请求重放原提交，内容不同返回 409。
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from apps.accounts.errors import ApiError
from apps.accounts.permissions import SUBMIT, get_visible_task, require_action
from apps.runs.models import Run

from ..models import AuditEvent, Submission, SubmissionItem, Task
from ..schemas import SubmissionLineOut, SubmissionOut, WithdrawalOut
from . import idempotency
from .confirmations import current_confirmation
from .reviewers import ensure_submission_expert

ACTION = "submission.create"


def _read_submission(submission_id: str) -> Submission:
    """首次读取只用于权限判断；写入前必须再走 ``lock_submission`` 拿锁后重读。"""
    try:
        submission = Submission.objects.select_related("task", "version").filter(pk=submission_id).first()
    except (ValidationError, ValueError, TypeError) as exc:
        # 畸形 UUID 抛 Django ValidationError；与不存在同样按 404 处理，不暴露 500。
        raise Http404("提交不存在或不在可见范围") from exc
    if submission is None:
        raise Http404("提交不存在或不在可见范围")
    return submission


def lock_submission(user, submission_id: str, action: str) -> tuple[Task, Submission]:
    """按 **Task → Submission** 的固定顺序加锁，并返回锁内重读的记录。

    审批（P10）与撤回（P09）都改任务状态，必须先拿同一把任务锁再改提交状态；
    否则会出现"批准发生在撤回读取与加锁之间"的交错：撤回看到的是旧状态，
    把已批准的任务改回待确认，而批准记录仍然存在。
    """
    submission = _read_submission(submission_id)
    require_action(user, action, submission)
    task = Task.objects.select_for_update().get(pk=submission.task_id)
    submission = Submission.objects.select_for_update().select_related("task", "version").get(pk=submission.pk)
    return task, submission


def _get_version(task, version_id):
    try:
        version = task.versions.filter(pk=version_id).first()
    except (ValidationError, ValueError) as exc:
        raise Http404("文件版本不存在或不在可见范围") from exc
    if version is None:
        raise Http404("文件版本不存在或不在可见范围")
    return version


def _line_fingerprint(pairs) -> str:
    return ";".join(sorted(f"{item}:{confirmation}" for item, confirmation in pairs))


def submission_to_out(submission, run, task, replay=False) -> SubmissionOut:
    lines = [
        SubmissionLineOut(
            item_id=str(line.item_id),
            confirmation_id=str(line.confirmation_id),
            submitted_hs=line.submitted_hs,
            item_revision=line.item_revision,
        )
        for line in submission.items.order_by("item__source_sheet", "item__source_row")
    ]
    return SubmissionOut(
        submission_id=str(submission.pk),
        task_id=str(submission.task_id),
        version_id=str(submission.version_id),
        revision=submission.revision,
        status=submission.status,
        items=lines,
        expert_run_id=str(run.pk) if run else None,
        expert_run_status=run.status if run else None,
        task_stage=task.stage,
        task_revision=task.revision,
        created_at=submission.created_at.isoformat(),
        idempotent_replay=replay,
    )


def _validate_lines(version, payload):
    """整批提交：行覆盖完整、逐行确认显式且仍为当前有效确认。"""
    items = {str(item.pk): item for item in version.items.all()}
    lines = payload.items
    if not lines:
        raise ApiError(422, "empty_submission", "提交必须包含至少一条商品的确认引用")
    seen = set()
    for line in lines:
        if line.item_id in seen:
            raise ApiError(422, "duplicate_item", "同一商品在一次提交中只能出现一次", {"item_id": line.item_id})
        seen.add(line.item_id)
        if line.item_id not in items:
            raise ApiError(
                422, "item_not_in_version",
                "提交行必须属于本次提交的文件版本，不能混入其它版本的商品",
                {"item_id": line.item_id},
            )
    missing = sorted(set(items) - seen)
    if missing:
        raise ApiError(
            422, "incomplete_items",
            "首期整批提交：该版本的每条商品都必须确认并纳入提交（R09）",
            {"missing_item_ids": missing},
        )
    resolved = []
    for line in lines:
        item = items[line.item_id]
        current = current_confirmation(item)
        if current is None:
            raise ApiError(
                422, "missing_confirmation",
                "该商品还没有业务员确认值，不能用 AI 建议或供应商原值代替",
                {"item_id": line.item_id},
            )
        if str(current.pk) != line.confirmation_id:
            raise ApiError(
                409, "stale_confirmation",
                "确认记录已被更新的确认取代，请刷新后重新提交",
                {"item_id": line.item_id, "current_confirmation_id": str(current.pk)},
            )
        resolved.append((item, current))
    return resolved


def _pending_replay(task, version, pairs, revision, *, user):
    """同版本已有待审提交：内容相同重放，不同则明确冲突，不创建第二个有效提交。"""
    pending = Submission.objects.filter(task=task, version=version, status=Submission.Status.PENDING).first()
    if pending is None:
        return None
    existing = _line_fingerprint(
        [(str(line.item_id), str(line.confirmation_id)) for line in pending.items.all()]
    )
    if existing != _line_fingerprint(pairs):
        raise ApiError(
            409, "submission_pending",
            "该版本已有一份待专家审核的提交，且内容不同；请先由专家处置或刷新后重试",
            {"submission_id": str(pending.pk)},
        )
    # 首次响应丢失后的重试可能带提交前的修订；只允许它或当前修订重放同一快照。
    if revision not in (pending.revision, task.revision):
        return None
    ensure_submission_expert(task, pending, actor=user, source="pending_replay")
    run = pending.runs.order_by("-created_at").first()
    return submission_to_out(pending, run, task, replay=True).model_dump(mode="json")


@transaction.atomic
def create_submission(user, task_id: str, payload, idempotency_key: str | None = None) -> dict:
    task = get_visible_task(user, task_id)
    require_action(user, SUBMIT, task)
    task = Task.objects.select_for_update().get(pk=task.pk)

    replay = idempotency.begin(user, ACTION, idempotency_key, payload, target_id=str(task.pk))
    if replay is not None:
        pending = Submission.objects.filter(task=task, pk=replay["submission_id"]).first()
        if pending:
            ensure_submission_expert(task, pending, actor=user, source="idempotent_replay")
        return replay

    version = _get_version(task, payload.version_id)
    latest = task.versions.order_by("-number").first()
    if latest is not None and version.pk != latest.pk:
        raise ApiError(
            409, "stale_version",
            "只能提交当前最新文件版本；旧版本的提交不会被专家审核",
            {"current_version_id": str(latest.pk)},
        )
    if task.stage == Task.Stage.APPROVED:
        raise ApiError(409, "task_closed", "任务已通过，如需变更请走补料重传")

    resolved = _validate_lines(version, payload)
    pairs = [(str(item.pk), str(confirmation.pk)) for item, confirmation in resolved]
    # 无幂等键的重复提交也要能安全重放：先按内容比对，再做修订校验。
    replayed = _pending_replay(task, version, pairs, payload.revision, user=user)
    if replayed is not None:
        idempotency.store(user, ACTION, idempotency_key, payload, replayed, target_id=str(task.pk))
        return replayed
    if payload.revision != task.revision:
        raise ApiError(
            409, "stale_revision",
            "任务内容已变化，请刷新后基于最新修订重新提交",
            {"current_revision": task.revision},
        )

    submission = Submission.objects.create(
        task=task, version=version, revision=task.revision, created_by=user,
    )
    for item, confirmation in resolved:
        SubmissionItem.objects.create(
            submission=submission, item=item, confirmation=confirmation,
            submitted_hs=confirmation.confirmed_hs, item_revision=confirmation.revision,
        )

    task.stage = Task.Stage.SUBMITTED
    task.revision += 1
    task.save(update_fields=["stage", "revision", "updated_at"])

    # 与提交同事务授权；默认专家不可用时整体回滚，不留下无人可审的 pending。
    ensure_submission_expert(task, submission, actor=user)

    # D4 第 4 节：同事务登记待运行记录；执行在提交成功后进行，进程崩溃由 P13 重扫。
    Run.objects.filter(version=version, kind=Run.Kind.EXPERT_CHECK, is_current=True).update(is_current=False)
    run = Run.objects.create(
        task=task, version=version, kind=Run.Kind.EXPERT_CHECK, submission=submission,
        status=Run.Status.QUEUED, is_current=True, triggered_by=None,
    )
    AuditEvent.objects.create(
        task=task, version=version, actor=user,
        action="submission.created", stage="business_submit",
        new_value=str(submission.pk),
        details={
            "submission_id": str(submission.pk), "version_number": version.number,
            "item_count": len(resolved), "expert_run_id": str(run.pk),
        },
    )
    response = submission_to_out(submission, run, task).model_dump(mode="json")
    idempotency.store(user, ACTION, idempotency_key, payload, response, target_id=str(task.pk))

    from apps.runs.services import schedule_run
    schedule_run(run)
    return response


WITHDRAW_ACTION = "submission.withdraw"


@transaction.atomic
def withdraw_submission(user, submission_id: str, idempotency_key: str | None = None) -> dict:
    """业务员撤回一份待审提交：提交置为 superseded，任务回到待确认，可再编辑与重新提交。

    - 只允许撤回 pending 提交；已通过/已退回/已撤回返回 409；
    - 撤回后该提交绑定的专家核查执行不再视为当前有效（结果原样保留）；
    - 只允许撤回最新文件版本的提交，旧版本提交由补料重传自然失效。
    """
    task, submission = lock_submission(user, submission_id, SUBMIT)
    replay = idempotency.begin(
        user, WITHDRAW_ACTION, idempotency_key, {"submission_id": str(submission.pk)},
        target_id=str(submission.pk),
    )
    if replay is not None:
        return replay

    if submission.status != Submission.Status.PENDING:
        raise ApiError(
            409, "submission_closed",
            "只能撤回待专家审核的提交；已通过/已退回/已撤回的提交原样保留",
            {"submission_id": str(submission.pk), "status": submission.status},
        )
    latest = task.versions.order_by("-number").first()
    if latest is not None and latest.pk != submission.version_id:
        raise ApiError(
            409, "stale_submission",
            "该提交对应的是旧文件版本，已有新版本；旧提交不可撤回，请在新版本上重新处理",
            {"current_version_id": str(latest.pk), "submission_version_id": str(submission.version_id)},
        )

    submission.status = Submission.Status.SUPERSEDED
    submission.save(update_fields=["status"])
    Run.objects.filter(submission=submission, kind=Run.Kind.EXPERT_CHECK, is_current=True).update(is_current=False)
    task.stage = Task.Stage.AWAITING_CONFIRMATION
    task.revision += 1
    task.save(update_fields=["stage", "revision", "updated_at"])
    AuditEvent.objects.create(
        task=task, version=submission.version, actor=user,
        action="submission.withdrawn", stage="business_submit",
        previous_value=str(submission.pk),
        details={"submission_id": str(submission.pk), "version_number": submission.version.number},
    )

    response = WithdrawalOut(
        submission_id=str(submission.pk), task_id=str(task.pk), version_id=str(submission.version_id),
        status=submission.status, task_stage=task.stage, task_revision=task.revision,
        idempotent_replay=False,
    ).model_dump(mode="json")
    idempotency.store(
        user, WITHDRAW_ACTION, idempotency_key, {"submission_id": str(submission.pk)}, response,
        target_id=str(submission.pk),
    )
    return response
