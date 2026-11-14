"""执行记录 → 契约 v1。

进度按该执行内各商品的当前行状态计数；业务待确认不计入技术 failed。
"""
from apps.api.schemas import EvidenceEntry, RunProgress
from apps.reviews.serializers import iso, result_mode, source_location

from .schemas import RunDetail, RunItemEntry


def run_progress(run) -> RunProgress:
    counts = {key: 0 for key in ("queued", "running", "succeeded", "failed")}
    for row in run.items.all():
        if row.status in counts:
            counts[row.status] += 1
    return RunProgress(total=sum(counts.values()), **counts)


def run_to_detail(run) -> RunDetail:
    items = []
    evidence: dict[str, EvidenceEntry] = {}
    for row in run.items.select_related("item").all():
        result = row.result or {}
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
            attempts=int(result.get("attempts", 1)),
        ))
        for entry in result.get("evidence", []):
            parsed = EvidenceEntry(**entry)
            evidence[parsed.evidence_id] = parsed
    return RunDetail(
        run_id=str(run.pk),
        task_id=str(run.task_id),
        version_id=str(run.version_id),
        submission_id=str(run.submission_id) if run.submission_id else None,
        kind=run.kind,
        status=run.status,
        is_current=run.is_current,
        result_mode=result_mode(),
        progress=run_progress(run),
        items=items,
        evidence=list(evidence.values()),
        engine_version=run.items.first().result.get("engine_version", "") if run.items.exists() else "",
        prompt_version=run.items.first().result.get("prompt_version", "") if run.items.exists() else "",
        rules_version=run.items.first().result.get("rules_version", "") if run.items.exists() else "",
        created_at=iso(run.created_at),
        started_at=iso(run.started_at),
        finished_at=iso(run.finished_at),
    )
