"""P13: durable registration, explicit reruns and guarded per-row execution.

Lock order is Task -> Run -> RunWork -> RunItem/RunAttempt. Model calls never
hold these locks. Every output is fenced again before publication.
"""
from copy import deepcopy
from datetime import timedelta
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from adapters.engines.protocols import CompareInput, ExpertCheckInput, get_engine
from apps.accounts.errors import ApiError
from apps.accounts.errors import AuthenticationRequired
from apps.accounts.permissions import CONFIRM, DECIDE, READ, require_action
from apps.reviews.models import AuditEvent, FileVersion, Item, Submission, Task
from apps.reviews.services import idempotency
from . import inline
from .models import Run, RunAttempt, RunItem, RunWork


def _eligible(run, task):
    latest_version = task.versions.order_by("-number").values_list("id", flat=True).first()
    latest_run = Run.objects.filter(version_id=run.version_id, kind=run.kind).order_by("-created_at", "-id").first()
    if latest_version != run.version_id or latest_run.pk != run.pk:
        return False
    if run.kind == Run.Kind.EXPERT_CHECK:
        return run.is_current and Submission.objects.filter(pk=run.submission_id, status="pending").exists()
    return task.stage not in (Task.Stage.SUBMITTED, Task.Stage.APPROVED, Task.Stage.RETURNED)


def register_run(run):
    """Called inside the business transaction; no network or engine calls here."""
    work, _ = RunWork.objects.get_or_create(run=run)
    items = Item.objects.filter(version_id=run.version_id)
    if run.submission_id:
        items = items.filter(submission_items__submission_id=run.submission_id)
    for item in items:
        RunItem.objects.get_or_create(run=run, item=item)
    return work


def schedule_run(run):
    if settings.EXECUTOR_MODE == "rq":
        register_run(run)
        from .dispatch import safe_dispatch
        transaction.on_commit(lambda: safe_dispatch(str(run.pk)))
    else:
        def execute():
            if run.kind == Run.Kind.PRECHECK:
                inline.execute_precheck(run.version, run=run)
            else:
                inline.execute_expert_check(run.submission, run=run)
        transaction.on_commit(execute)


def _lookup(model, pk):
    try:
        return model.objects.get(pk=pk)
    except (model.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise Http404("对象不存在或不在可见范围") from exc


def _response(run, replay=False):
    return {"run_id": str(run.pk), "task_id": str(run.task_id), "version_id": str(run.version_id),
            "status": run.status, "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            "idempotent_replay": replay}


def precheck_capability(user, task):
    if settings.EXECUTOR_MODE != "rq":
        return False
    try:
        require_action(user, CONFIRM, task)
    except (PermissionDenied, Http404, AuthenticationRequired):
        return False
    if task.stage in (Task.Stage.SUBMITTED, Task.Stage.APPROVED, Task.Stage.RETURNED):
        return False
    version = task.versions.order_by("-number").first()
    return bool(version and version.items.exists() and
                (not hasattr(version, "import_job") or version.import_job.status == "succeeded"))


@transaction.atomic
def start_precheck(user, version_id, revision, key=None):
    version = _lookup(FileVersion, version_id)
    require_action(user, CONFIRM, version)
    if settings.EXECUTOR_MODE != "rq":
        raise ApiError(409, "queue_required", "重新预审需要启用后台队列")
    task = Task.objects.select_for_update().get(pk=version.task_id)
    require_action(user, CONFIRM, task)
    fingerprint = {"version_id": str(version.pk), "revision": revision}
    replay = idempotency.begin(user, "precheck.create", key, fingerprint)
    if replay is not None:
        return replay
    if task.versions.order_by("-number").first().pk != version.pk:
        raise ApiError(409, "stale_version", "只能重新预审当前版本")
    if task.stage in (Task.Stage.SUBMITTED, Task.Stage.APPROVED, Task.Stage.RETURNED):
        raise ApiError(409, "task_not_editable", "任务已提交、已通过或已退回，请先完成对应处置")
    if revision != task.revision:
        raise ApiError(409, "stale_revision", "任务已变化，请刷新后重新预审", {"current_revision": task.revision})
    if not version.items.exists() or (hasattr(version, "import_job") and version.import_job.status != "succeeded"):
        raise ApiError(409, "import_incomplete", "请等待文件解析完成后再执行预审")
    Run.objects.filter(version=version, kind=Run.Kind.PRECHECK, is_current=True).update(is_current=False)
    run = Run.objects.create(task=task, version=version, kind=Run.Kind.PRECHECK, is_current=True, triggered_by=user)
    task.stage = Task.Stage.PRECHECKING
    task.revision += 1
    task.save(update_fields=["stage", "revision", "updated_at"])
    AuditEvent.objects.create(task=task, version=version, actor=user, action="precheck.created", details={"run_id": str(run.pk)})
    schedule_run(run)
    result = _response(run)
    idempotency.store(user, "precheck.create", key, fingerprint, result)
    return result


def retry_capability(user, run):
    if settings.EXECUTOR_MODE != "rq":
        return []
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return []
    try:
        require_action(user, CONFIRM if run.kind == Run.Kind.PRECHECK else DECIDE, run)
    except (PermissionDenied, Http404, AuthenticationRequired):
        return []
    if run.status not in (Run.Status.SUCCEEDED, Run.Status.FAILED) or not run.is_current or not _eligible(run, run.task):
        return []
    return [str(v) for v in run.items.filter(status="failed").values_list("item_id", flat=True)]


@transaction.atomic
def retry_run(user, run_id, item_ids, key=None):
    parent = _lookup(Run, run_id)
    require_action(user, CONFIRM if parent.kind == Run.Kind.PRECHECK else DECIDE, parent)
    if settings.EXECUTOR_MODE != "rq":
        raise ApiError(409, "queue_required", "失败行重试需要启用后台队列")
    task = Task.objects.select_for_update().get(pk=parent.task_id)
    parent = Run.objects.select_for_update().get(pk=parent.pk)
    require_action(user, CONFIRM if parent.kind == Run.Kind.PRECHECK else DECIDE, parent)
    fingerprint = {"run_id": str(parent.pk), "item_ids": sorted(item_ids)}
    replay = idempotency.begin(user, "run.retry", key, fingerprint)
    if replay is not None:
        return replay
    failed = set(str(v) for v in parent.items.filter(status="failed").values_list("item_id", flat=True))
    if not item_ids or len(set(item_ids)) != len(item_ids) or not set(item_ids).issubset(failed):
        raise ApiError(422, "invalid_retry_items", "只能重试本轮执行中明确失败的商品行")
    if not retry_capability(user, parent):
        raise ApiError(409, "run_not_retryable", "执行尚未结束、不是当前轮次或提交已结束，请刷新")
    parent.is_current = False
    parent.save(update_fields=["is_current"])
    run = Run.objects.create(task=task, version=parent.version, kind=parent.kind, submission=parent.submission,
                             is_current=True, triggered_by=user, parent_run=parent)
    for old in parent.items.all():
        copied = deepcopy(old.result)
        copied["origin_run_id"] = str(parent.pk)
        RunItem.objects.create(run=run, item=old.item,
            status=RunItem.Status.QUEUED if str(old.item_id) in item_ids else old.status,
            suggested_hs=None if str(old.item_id) in item_ids else old.suggested_hs,
            comparison_status="" if str(old.item_id) in item_ids else old.comparison_status,
            confidence=None if str(old.item_id) in item_ids else old.confidence,
            result={} if str(old.item_id) in item_ids else copied,
            error_message="" if str(old.item_id) in item_ids else old.error_message)
    task.revision += 1
    if run.kind == Run.Kind.PRECHECK:
        task.stage = Task.Stage.PRECHECKING
    task.save(update_fields=["revision", "stage", "updated_at"])
    AuditEvent.objects.create(task=task, version=run.version, actor=user, action="run.retried",
        details={"run_id": str(run.pk), "parent_run_id": str(parent.pk), "item_ids": item_ids})
    schedule_run(run)
    result = _response(run)
    idempotency.store(user, "run.retry", key, fingerprint, result)
    return result


def _supersede(run, work):
    run.status, run.is_current, run.finished_at = Run.Status.SUPERSEDED, False, timezone.now()
    run.save(update_fields=["status", "is_current", "finished_at"])
    work.state, work.token, work.lease_until = "done", None, None
    work.save()
    RunAttempt.objects.filter(row__run=run, status="running").update(status="superseded", finished_at=timezone.now())


@transaction.atomic
def claim_work(run_id):
    initial = _lookup(Run, run_id)
    task = Task.objects.select_for_update().get(pk=initial.task_id)
    run = Run.objects.select_for_update(of=("self",)).select_related("version", "submission").get(pk=run_id)
    work = RunWork.objects.select_for_update().get(run=run)
    if work.state != "pending" or work.available_at > timezone.now():
        return None
    if not _eligible(run, task):
        _supersede(run, work)
        return None
    work.state, work.token = "running", uuid.uuid4()
    work.lease_until = timezone.now() + timedelta(seconds=settings.CUSTOMS_QUEUE_LEASE_SECONDS)
    work.dispatched_at = None
    work.save()
    return str(work.token)


@transaction.atomic
def begin_attempt(run_id, item_id, token):
    initial = Run.objects.get(pk=run_id)
    task = Task.objects.select_for_update().get(pk=initial.task_id)
    run = Run.objects.select_for_update().get(pk=run_id)
    work = RunWork.objects.select_for_update().get(run=run)
    if work.state != "running" or str(work.token) != token or work.lease_until <= timezone.now():
        return None
    if not _eligible(run, task):
        _supersede(run, work)
        return None
    row = RunItem.objects.select_for_update().select_related("item").get(run=run, item_id=item_id)
    if row.status != "queued":
        return None
    count = row.attempt_records.count()
    if count >= settings.CUSTOMS_ROW_MAX_ATTEMPTS:
        row.status = "failed"
        row.save(update_fields=["status"])
        return None
    attempt = RunAttempt.objects.create(row=row, number=count+1, token=token,
                                       input_hash=_payload(run, row.item).context.input_hash)
    row.status = "running"
    row.result = {"attempts": count+1, "issues": [], "evidence": []}
    row.save(update_fields=["status", "result", "updated_at"])
    run.status, run.is_current = "running", True
    run.started_at = run.started_at or timezone.now()
    run.save(update_fields=["status", "is_current", "started_at"])
    return attempt


def _payload(run, item):
    if run.kind == Run.Kind.PRECHECK:
        return inline._build_recommend_input(run, item, "normal")
    line = run.submission.items.get(item=item)
    context = inline._context(run, item, "normal")
    context.input_revision = line.item_revision
    payload = ExpertCheckInput(context=context, submission_id=str(run.submission_id), submitted_hs=line.submitted_hs,
        name=item.name, attributes={str(k): str(v) for k,v in (item.attributes or {}).items()},
        elements_text=item.supplier_elements_raw, supplier_tax_rate_raw=item.supplier_tax_rate_raw,
        images=inline._image_facts(item))
    context.input_hash = inline._input_hash(payload)
    return payload


def evaluate_row(run, item):
    """Pure engine boundary: call only outside Task/Run DB locks."""
    engine, payload = get_engine(), _payload(run, item)
    metadata = {k: getattr(payload.context, k) for k in ("engine_version", "prompt_version", "rules_version", "input_hash")}
    if run.kind == Run.Kind.PRECHECK:
        recommendation = engine.recommend(payload)
        comparison = engine.compare(CompareInput(context=payload.context, recommendation=recommendation,
                                                  supplier_hs_raw=item.supplier_hs_raw))
        result = {"candidates": [v.model_dump(mode="json") for v in recommendation.candidates],
            "comparison_reason": comparison.reason, "confidence_note": recommendation.confidence_note,
            "evidence_status": recommendation.evidence_status.value if recommendation.evidence_status else None,
            "evidence": [v.model_dump(mode="json") for v in recommendation.evidence],
            "issues": inline._merge_issues(recommendation.gaps, comparison.issues), **metadata}
        return {"suggested_hs": recommendation.suggested_hs, "comparison_status": comparison.comparison_status.value,
                "confidence": recommendation.confidence, "result": result}
    output = engine.expert_check(payload)
    return {"suggested_hs": output.suggested_hs, "comparison_status": "", "confidence": None,
            "result": {"target_hs": output.target_hs, "findings": [v.model_dump(mode="json") for v in output.findings],
                "evidence_status": output.evidence_status.value if output.evidence_status else None,
                "evidence": [v.model_dump(mode="json") for v in output.evidence],
                "issues": [v.model_dump(mode="json") for v in output.issues], **metadata}}


@transaction.atomic
def finish_attempt(attempt_id, token, output=None, error=None):
    initial = RunAttempt.objects.select_related("row__run").get(pk=attempt_id)
    task = Task.objects.select_for_update().get(pk=initial.row.run.task_id)
    run = Run.objects.select_for_update().get(pk=initial.row.run_id)
    work = RunWork.objects.select_for_update().get(run=run)
    attempt = RunAttempt.objects.select_for_update().get(pk=attempt_id)
    if attempt.status != "running":
        return False
    if work.state != "running" or str(work.token) != token or work.lease_until <= timezone.now():
        return False
    if not _eligible(run, task):
        _supersede(run, work)
        return False
    row = RunItem.objects.select_for_update().get(pk=attempt.row_id)
    if attempt.input_hash != _payload(run, row.item).context.input_hash:
        _supersede(run, work)
        return False
    attempt.finished_at = timezone.now()
    if error is not None:
        attempt.status, attempt.error_code = "failed", type(error).__name__[:100]
        row.status = "queued" if attempt.number < settings.CUSTOMS_ROW_MAX_ATTEMPTS else "failed"
        row.error_message = f"技术执行失败（{attempt.error_code}），已尝试 {attempt.number} 次"
        row.result = {"attempts": attempt.number, "issues": [], "evidence": []}
    else:
        attempt.status, attempt.result = "succeeded", deepcopy(output)
        row.status, row.error_message = "succeeded", ""
        for field, value in output.items():
            setattr(row, field, value)
        row.result["attempts"] = attempt.number
    attempt.save()
    row.save()
    return True


@transaction.atomic
def finish_pass(run_id, token):
    initial = Run.objects.get(pk=run_id)
    task = Task.objects.select_for_update().get(pk=initial.task_id)
    run = Run.objects.select_for_update().get(pk=run_id)
    work = RunWork.objects.select_for_update().get(run=run)
    if work.state != "running" or str(work.token) != token or work.lease_until <= timezone.now():
        return
    if not _eligible(run, task):
        _supersede(run, work)
        return
    if run.items.filter(status__in=["queued", "running"]).exists():
        work.state = "pending"
        work.available_at = timezone.now() + timedelta(seconds=settings.CUSTOMS_RETRY_BACKOFF_SECONDS)
    else:
        work.state = "done"
        run.status = "failed" if run.items.exists() and not run.items.filter(status="succeeded").exists() else "succeeded"
        run.finished_at = timezone.now()
        if run.kind == Run.Kind.PRECHECK and task.stage in (Task.Stage.UPLOADED, Task.Stage.PRECHECKING):
            task.stage = Task.Stage.AWAITING_CONFIRMATION
            task.save(update_fields=["stage", "updated_at"])
    work.token, work.lease_until = None, None
    work.save()
    run.save(update_fields=["status", "finished_at"])
