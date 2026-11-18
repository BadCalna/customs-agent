"""执行记录 → 契约 v1。

进度按该执行内各商品的当前行状态计数；业务待确认不计入技术 failed。
"""
from apps.api.schemas import EvidenceEntry, RunProgress
from apps.reviews.serializers import iso, source_location

from .schemas import RunDetail, RunItemEntry
from .engine_config import run_result_mode


def run_progress(run) -> RunProgress:
    counts = {key: 0 for key in ("queued", "running", "succeeded", "failed")}
    for row in run.items.all():
        if row.status in counts:
            counts[row.status] += 1
    return RunProgress(total=sum(counts.values()), **counts)


def _visible_result(result, user):
    payload = result or {}
    if getattr(user, "role", None) == "supplier":
        payload = dict(payload)
        payload.pop("history_reference", None)
        payload["evidence"] = [item for item in payload.get("evidence", []) if item.get("source_type") != "history"]
    return payload


def run_to_detail(run, user=None) -> RunDetail:
    items = []
    evidence: dict[str, EvidenceEntry] = {}
    for row in run.items.select_related("item").all():
        result = _visible_result(row.result, user)
        items.append(RunItemEntry(
            item_id=str(row.item_id),
            source_location=source_location(row.item),
            status=row.status,
            suggested_hs=row.suggested_hs,
            comparison_status=row.comparison_status or None,
            confidence=row.confidence,
            evidence_status=result.get("evidence_status"),
            issues=result.get("issues", []),
            error_message=row.error_message,
            attempts=row.attempt_records.count() if result.get("origin_run_id") else int(result.get("attempts", 0 if row.status == "queued" else 1)),
            origin_run_id=result.get("origin_run_id"),
            attempt_history=[{"number": a.number, "status": a.status, "input_hash": a.input_hash,
                "error_code": a.error_code, "started_at": iso(a.started_at), "finished_at": iso(a.finished_at)}
                for a in row.attempt_records.all()],
        ))
        for entry in result.get("evidence", []):
            parsed = EvidenceEntry(**entry)
            evidence[parsed.evidence_id] = parsed
    from .services import retry_capability
    retry_ids = retry_capability(user, run)
    return RunDetail(
        run_id=str(run.pk),
        task_id=str(run.task_id),
        version_id=str(run.version_id),
        submission_id=str(run.submission_id) if run.submission_id else None,
        kind=run.kind,
        status=run.status,
        is_current=run.is_current,
        parent_run_id=str(run.parent_run_id) if run.parent_run_id else None,
        retry_allowed=bool(retry_ids),
        retry_item_ids=retry_ids,
        result_mode=run_result_mode(run),
        progress=run_progress(run),
        items=items,
        evidence=list(evidence.values()),
        engine_version=run.engine_snapshot.get("engine_version", ""),
        prompt_version=run.engine_snapshot.get("prompt_version", ""),
        rules_version=run.engine_snapshot.get("rules_version", ""),
        created_at=iso(run.created_at),
        started_at=iso(run.started_at),
        finished_at=iso(run.finished_at),
    )
