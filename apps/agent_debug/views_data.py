"""Scoped run metadata, evidence navigation and redacted trace reads."""
from datetime import date, datetime, time, timedelta
from uuid import UUID

from django.core import signing
from django.db.models import Prefetch, Q
from django.utils import timezone

from .access import catalog_visible, has, limit
from .errors import DebugError, MESSAGES
from .maintenance import event
from .materials import reference_url
from .models import DebugRun, DebugStep, State
from .security import open_data


def _query(user):
    qs = DebugRun.objects.prefetch_related(Prefetch("steps", queryset=DebugStep.objects.select_related("trace")))
    return qs if has(user, "view_all_runs") else qs.filter(owner=user)


def content_visible(user, run):
    cfg = run.config_snapshot
    return run.mode != "live" or catalog_visible(user, cfg.get("snapshot_id"), cfg.get("bundle_fingerprint"))


def _allowed(user, run):
    return run.owner_id == user.pk or content_visible(user, run)


def visible_run(user, run_id):
    run = _query(user).filter(pk=run_id).first()
    if run is not None and _allowed(user, run):
        return run
    raise DebugError("not_found", 404)


def _payload_state(step):
    trace = getattr(step, "trace", None)
    cutoff = step.run.created_at + timedelta(days=limit("OPS_DEBUG_INPUT_RETENTION_DAYS", 7))
    if trace and min(trace.expires_at, cutoff) <= timezone.now():
        return "expired"
    return "available" if trace and trace.ciphertext else "unavailable"


def serialize_run(user, run, *, detail=False):
    visible = content_visible(user, run)
    steps = list(run.steps.all())
    attempts = [step for step in steps if step.attempt_no is not None]
    owner = run.owner_id == user.pk
    data = {"id": str(run.pk), "mode": run.mode,
            "stage": run.config_snapshot.get("stage", "precheck") if visible else None,
            "sample_label": run.sample_label if visible else "",
            "owner": str(run.owner_id) if run.owner_id else None,
            "model": run.config_snapshot.get("model") if visible else None,
            "status": run.status, "business_status": run.business_status if visible else "not_evaluated",
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
            "elapsed_ms": int((run.finished_at - run.started_at).total_seconds() * 1000) if run.finished_at and run.started_at else None,
            "error_code": run.error_code, "error_message": MESSAGES.get(run.error_code, ""),
            "outbound_counts": {"attempts": len(attempts), "responses": sum(s.received for s in attempts),
                                "parsed": sum(s.status == "parsed" for s in attempts)},
            "waiting": run.status == State.QUEUED, "content_visible": visible,
            "request_id": str(run.request_id), "can_rerun": owner and visible and bool(run.input_ciphertext)
            and min(run.input_expires_at, run.created_at + timedelta(days=limit("OPS_DEBUG_INPUT_RETENTION_DAYS", 7))) > timezone.now()}
    if detail:
        data.update(run.result if visible else {})
        data.update(snapshot=run.config_snapshot if visible else {},
                    parent_run_id=str(run.parent_run_id) if owner and visible and run.parent_run_id else None,
                    materials=[{"role": row.role, "kind": row.kind, "status": row.status,
                                **({"evidence_id": row.source_key} if run.config_snapshot.get("stage") == "expert_check" else {}),
                                "label": row.ref["label"] if row.ref else "不可定位",
                                "ref": row.ref, "url": reference_url(row.ref) if row.ref else None}
                               for row in run.materials.order_by("id")] if visible else [])
    return data


def serialize_steps(user, run):
    show_trace = content_visible(user, run) and has(user, "view_trace_payload")
    return [{"seq": s.seq, "kind": s.kind, "status": s.status, "attempt_no": s.attempt_no,
             "started_at": s.started_at, "finished_at": s.finished_at, "http_status": s.http_status,
             "received": s.received, "error_code": s.error_code, "error_message": MESSAGES.get(s.error_code, ""),
             "truncated": s.truncated, "usage": s.usage, "provider_request_id": s.provider_request_id,
             "payload_available": show_trace and _payload_state(s) != "unavailable"} for s in run.steps.all()]


def read_trace(user, run_id, seq, request_id):
    run = visible_run(user, run_id)
    step = next((step for step in run.steps.all() if step.seq == seq), None)
    if step is None:
        raise DebugError("not_found", 404)
    kwargs = {"actor_id": user.pk, "request_id": request_id, "target_type": "debug.step",
              "target_id": step.pk, "run_id": run.pk}
    if not has(user, "view_trace_payload") or not content_visible(user, run):
        event("debug.trace.viewed", outcome="denied", summary_code="forbidden", **kwargs)
        raise DebugError("forbidden", 403)
    state = _payload_state(step)
    result = {"request_redacted": None, "response_redacted": None, "withheld_fields": [],
              "payload_state": state, "truncated": step.truncated}
    if state == "available":
        try:
            result.update(open_data(step.trace.ciphertext, f"trace:{run.pk}:{seq}"))
        except DebugError as exc:
            event("debug.trace.viewed", outcome="failure", summary_code=exc.code, **kwargs)
            raise
    event("debug.trace.viewed", **kwargs)
    return result


def run_page(user, query):
    filters = {name: query.get(name, "") for name in ("mode", "state", "date_from", "date_to", "id", "error_code", "stage")}
    qs = _query(user).order_by("-created_at", "-pk")
    try:
        page_size = int(query.get("page_size", "25"))
        if not 1 <= page_size <= 100:
            raise ValueError
        for name, field, allowed in (("mode", "mode", ("mock", "live")), ("state", "status", State.values)):
            if value := filters[name]:
                if value not in allowed:
                    raise ValueError
                qs = qs.filter(**{field: value})
        if value := filters["stage"]:
            if value not in ("precheck", "expert_check"):
                raise ValueError
            expert = Q(config_snapshot__stage="expert_check")
            qs = qs.filter(expert) if value == "expert_check" else qs.filter(
                Q(config_snapshot__stage="precheck") | Q(config_snapshot__stage__isnull=True))
        if value := filters["id"]:
            qs = qs.filter(pk=UUID(value))
        if not any(filters[k] for k in ("id", "date_from", "date_to")):
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=7))
        if value := filters["error_code"]:
            if value not in MESSAGES:
                raise ValueError
            qs = qs.filter(error_code=value)
        for name, lookup, offset in (("date_from", "created_at__gte", 0), ("date_to", "created_at__lt", 1)):
            if value := filters[name]:
                day = date.fromisoformat(value) + timedelta(days=offset)
                qs = qs.filter(**{lookup: timezone.make_aware(datetime.combine(day, time.min))})
        if value := query.get("cursor"):
            if len(value) > 2048:
                raise ValueError
            timestamp, uid, bound_user, bound_filters = signing.loads(value, salt="agent-debug.run-page", max_age=86400)
            if str(user.pk) != bound_user or filters != bound_filters:
                raise ValueError
            when = datetime.fromisoformat(timestamp)
            qs = qs.filter(Q(created_at__lt=when) | Q(created_at=when, pk__lt=UUID(uid)))
    except (OverflowError, ValueError, TypeError, signing.BadSignature):
        raise DebugError("INVALID_INPUT", 422) from None
    page_size = min(page_size, limit("OPS_DEBUG_PAGE_SIZE", 100))
    rows = []
    for row in qs.iterator(chunk_size=100):
        if _allowed(user, row):
            rows.append(row)
            if len(rows) > page_size:
                break
    page = rows[:page_size]
    next_cursor = None
    if len(rows) > page_size:
        tail = page[-1]
        next_cursor = signing.dumps([tail.created_at.isoformat(), str(tail.pk), str(user.pk), filters], salt="agent-debug.run-page")
    return {"items": [serialize_run(user, row) for row in page], "next_cursor": next_cursor}
