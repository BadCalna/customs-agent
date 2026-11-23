"""Transactional audit handoff and expiry for Agent Debug's own tables."""
from __future__ import annotations

import logging
import re
from datetime import UTC, timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Q, Value
from django.db.models.functions import Least
from django.utils import timezone

from .access import limit
from .models import DebugDefault, DebugPreview, DebugProbe, DebugRun, DebugStep, EventOutbox, OutboundQuota, State, TracePayload


_logger = logging.getLogger("customs.ops.events")
_log = logging.getLogger(__name__)
_LABEL = re.compile(r"[a-z][a-z0-9_.-]{0,63}", re.ASCII)
_CODE = re.compile(r"[A-Za-z0-9_.-]{0,64}", re.ASCII)
_TERMINAL = (State.SUCCEEDED, State.FAILED, State.INTERRUPTED)
_QUEUE_TTL = timedelta(minutes=5)
_LEASE_TTL = timedelta(seconds=180)


def event(event_type, *, actor_id, request_id, target_type, target_id,
          outcome="success", summary_code="", run_id=None):
    """Record a whitelisted envelope. Call within the audited write's atomic block.

    IDs must be trusted system IDs, never input, endpoints, or secrets. In
    autocommit the event has its own transaction (e.g. a trace read audit).
    """
    if (not _LABEL.fullmatch(event_type) or not _LABEL.fullmatch(target_type)
            or not _CODE.fullmatch(summary_code) or outcome not in {"success", "failure", "denied"}):
        raise ValueError("Invalid Agent Debug event labels")
    event_id = uuid4()
    envelope = {
        "schema_version": 1,
        "event_id": str(event_id),
        "occurred_at": timezone.now().astimezone(UTC).isoformat(),
        "event_type": event_type,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "request_id": str(request_id),
        "target_type": target_type,
        "target_id": str(target_id) if target_id is not None else None,
        "outcome": outcome,
        "summary_code": summary_code,
    }
    if run_id is not None:
        envelope["run_id"] = str(run_id)
    with transaction.atomic():
        EventOutbox.objects.create(event_id=event_id, envelope=envelope)
        transaction.on_commit(lambda: _deliver(event_id))
    return event_id


def _has_event_sink():
    if not _logger.isEnabledFor(logging.INFO):
        return False
    logger = _logger
    while logger is not None:
        if any(h.level <= logging.INFO and not isinstance(h, logging.NullHandler) for h in logger.handlers):
            return True
        logger = logger.parent if logger.propagate else None
    return False


def _deliver(event_id):
    # Without an INFO sink there is no handoff. Keep the durable row for
    # ops_debug_cleanup to retry after the collector is installed/enabled.
    try:
        if not _has_event_sink():
            return False
        with transaction.atomic():
            row = EventOutbox.objects.select_for_update().filter(event_id=event_id, delivered_at__isnull=True).first()
            if row is None:
                return False
            _logger.info("ops.event", extra={"ops_event": row.envelope})
            row.delivered_at = timezone.now()
            row.save(update_fields=["delivered_at"])
        return True
    except Exception:
        # The feature transaction is already durable; telemetry failures must
        # not turn a successful action into a client-facing error or leak data.
        try:
            _log.warning("Agent Debug audit handoff failed; outbox entry retained")
        except Exception:
            pass
        return False


def flush_outbox(*, batch_size=1000):
    """Retry a bounded batch; logger consumers deduplicate by event_id."""
    if not _has_event_sink():
        return 0
    ids = list(EventOutbox.objects.filter(delivered_at__isnull=True)
               .order_by("occurred_at", "event_id").values_list("event_id", flat=True)[:batch_size])
    return sum(_deliver(event_id) for event_id in ids)


def _work_eligibility(now):
    queued = Q(status=State.QUEUED, created_at__lte=now - _QUEUE_TTL)
    fallback_cutoff = now - _LEASE_TTL
    stale = Q(status=State.RUNNING) & (
        Q(lease_until__lte=now)
        | (Q(lease_until__isnull=True) & (Q(started_at__lte=fallback_cutoff)
                                       | Q(started_at__isnull=True, created_at__lte=fallback_cutoff)))
    )
    return queued, stale


def _expire(model, predicate, *, now, status, code, dry_run):
    rows = model.objects.filter(predicate)
    if dry_run:
        return rows.count()
    actor_field = "owner_id" if model is DebugRun else "actor_id"
    count = 0
    for work_id, actor_id, request_id in rows.order_by("pk").values_list("pk", actor_field, "request_id").iterator():
        if rows.filter(pk=work_id).update(status=status, error_code=code, finished_at=now, lease_until=None):
            is_run = model is DebugRun
            event("debug.run.finished" if is_run else "debug.probe.finished", actor_id=actor_id,
                  request_id=request_id, target_type="run" if is_run else "debug.probe",
                  target_id=work_id, outcome="failure", summary_code=code,
                  run_id=work_id if is_run else None)
            count += 1
    return count


def _redact(qs, data_field, expiry_field, *, now, dry_run):
    qs = qs.exclude(**{data_field: ""})
    if dry_run:
        return qs.count()
    return qs.update(**{data_field: "", expiry_field: Least(expiry_field, Value(now))})


def _delete(qs, *, dry_run):
    if dry_run:
        return qs.count()
    return qs.delete()[1].get(qs.model._meta.label, 0)


def cleanup(*, dry_run=False):
    """Apply retention under the same singleton mutex as claims/admission.

    Dry runs execute no writes or logger calls (even on an empty database).
    The returned counts are for top-level rows, not cascading deletions.
    """
    with transaction.atomic():
        if not dry_run:
            DebugDefault.objects.get_or_create(pk=1)
        DebugDefault.objects.select_for_update().filter(pk=1).first()
        now = timezone.now()
        input_cutoff = now - timedelta(days=limit("OPS_DEBUG_INPUT_RETENTION_DAYS", 7))
        metadata_cutoff = now - timedelta(days=limit("OPS_DEBUG_METADATA_RETENTION_DAYS", 90))
        queued, stale = _work_eligibility(now)
        terminal = Q(status__in=_TERMINAL) | queued | stale
        stats = {}

        for model, name in ((DebugRun, "runs"), (DebugProbe, "probes")):
            stats[f"{name}_queue_expired"] = _expire(model, queued, now=now, status=State.FAILED,
                                                    code="QUEUE_EXPIRED", dry_run=dry_run)
            stats[f"{name}_lease_expired"] = _expire(model, stale, now=now, status=State.INTERRUPTED,
                                                    code="WORKER_INTERRUPTED", dry_run=dry_run)

        # Dispatch commits are durable before transport. If cleanup finds the
        # run terminal (including one just expired above), retain attempt_no /
        # received flags but close any orphaned dispatch marker.
        steps = DebugStep.objects.filter(status="dispatching", run__in=DebugRun.objects.filter(terminal))
        stats["dispatching_steps_interrupted"] = (steps.count() if dry_run else steps.update(
            status=State.INTERRUPTED, error_code="WORKER_INTERRUPTED", finished_at=now))

        inputs = DebugRun.objects.filter(Q(input_expires_at__lte=now) | Q(created_at__lte=input_cutoff))
        traces = TracePayload.objects.filter(Q(expires_at__lte=now) | Q(step__run__created_at__lte=input_cutoff))
        stats["inputs_redacted"] = _redact(inputs, "input_ciphertext", "input_expires_at", now=now, dry_run=dry_run)
        stats["traces_redacted"] = _redact(traces, "ciphertext", "expires_at", now=now, dry_run=dry_run)
        stats["previews_deleted"] = _delete(DebugPreview.objects.filter(expires_at__lte=now), dry_run=dry_run)
        stats["quota_deleted"] = _delete(OutboundQuota.objects.filter(created_at__lte=now - timedelta(hours=1)), dry_run=dry_run)
        stats["runs_deleted"] = _delete(DebugRun.objects.filter(terminal, created_at__lte=metadata_cutoff), dry_run=dry_run)
        stats["probes_deleted"] = _delete(DebugProbe.objects.filter(terminal, created_at__lte=metadata_cutoff), dry_run=dry_run)
        delivered = EventOutbox.objects.filter(delivered_at__isnull=False, occurred_at__lte=metadata_cutoff)
        stats["outbox_deleted"] = _delete(delivered, dry_run=dry_run)
        if not dry_run:
            event("debug.cleanup.finished", actor_id=None, request_id=uuid4(), target_type="debug.retention",
                  target_id=None, summary_code="CLEANUP_COMPLETED")

    # Replay after releasing the claim/admission lock and after applying
    # retention. Undelivered events never expire; a failed handoff is retriable.
    stats["outbox_retried"] = 0 if dry_run else flush_outbox()
    stats["outbox_pending"] = EventOutbox.objects.filter(delivered_at__isnull=True).count()
    return stats
