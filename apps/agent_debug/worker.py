"""One-shot debug execution. A committed dispatch marker is never replayed."""
from datetime import timedelta
from hmac import compare_digest

from django.db import connection as db, transaction
from django.utils import timezone

from .access import enabled, limit, live_enabled, require
from .admission import mutex
from .connections import check_endpoint, execute_probe, key_for
from .contracts import digest
from .engine import parse_response, prepare, result_summary, wire_bytes
from .errors import DebugError, MESSAGES
from .maintenance import event
from .materials import evidence_rows, live_materials
from .models import BusinessState, DebugEvidence, DebugProbe, DebugRun, DebugStep, RevisionState, State, TracePayload
from .security import seal_data
from .services import _read_input, check_external
from .transport import send


_PREPARE_SEQ, _CALL_SEQ, _PARSE_SEQ, _CHECK_SEQ = range(1, 5)


def _deadline(work):
    return work.started_at + timedelta(seconds=limit("OPS_DEBUG_RUN_SECONDS", 120))


def _claim():
    if not enabled():
        return None
    with transaction.atomic(durable=True):
        mutex()
        now = timezone.now()
        eligible = {"status": State.QUEUED, "created_at__gt": now - timedelta(minutes=5)}
        pending = []
        for model in (DebugProbe, DebugRun):
            q = model.objects.filter(**eligible).order_by("created_at", "pk")
            item = q.select_for_update(skip_locked=db.features.has_select_for_update_skip_locked).first()
            if item:
                pending.append(item)
        if not pending:
            return None
        work = min(pending, key=lambda row: (row.created_at, row.pk))
        work.status = State.RUNNING
        work.started_at = now
        work.lease_until = now + timedelta(seconds=180)
        work.save(update_fields=["status", "started_at", "lease_until"])
    return work


def _current(claim):
    return DebugRun.objects.select_for_update().filter(pk=claim.pk, status=State.RUNNING,
                                                       started_at=claim.started_at,
                                                       lease_until=claim.lease_until,
                                                       lease_until__gt=timezone.now()).first()


def _authorized(work):
    if work.owner is None:
        raise DebugError("unauthorized", 401)
    require(work.owner, "use_debug")
    if timezone.now() >= _deadline(work):
        raise DebugError("RUN_DEADLINE")
    if work.mode == "live" and not live_enabled():
        raise DebugError("ENDPOINT_NOT_ALLOWED", 403)


def _dispatch(claim, materials, protected, expected_digest):
    # Re-read the user's grants and the revision at the dispatch boundary; a
    # retired revision is pinned and legal, an emergency revocation is not.
    from .access import catalog_visible
    with transaction.atomic(durable=True):
        mutex()
        work = _current(claim)
        if work is None or work.steps.filter(kind="model").exists():
            raise DebugError("WORKER_INTERRUPTED")
        _authorized(work)
        if not catalog_visible(work.owner, materials.snapshot_id, materials.bundle.fingerprint):
            raise DebugError("forbidden", 403)
        connection = work.connection
        if not connection or connection.status not in (RevisionState.ACTIVE, RevisionState.RETIRED):
            raise DebugError("CONNECTION_REVOKED")
        endpoint = check_endpoint(connection)
        if endpoint.fingerprint != work.config_snapshot["endpoint_fingerprint"]:
            raise DebugError("PREVIEW_STALE")
        check_external(endpoint, protected["sample_kind"], protected["sample_id"], protected["input"], expected_digest)
        key = key_for(connection)
        now = timezone.now()
        if now >= _deadline(work):
            raise DebugError("RUN_DEADLINE")
        _, prepared = DebugStep.objects.get_or_create(run=work, seq=_PREPARE_SEQ, defaults={
            "kind": "prepare", "status": State.SUCCEEDED, "started_at": work.started_at, "finished_at": now,
        })
        if not prepared:
            raise DebugError("WORKER_INTERRUPTED")
        DebugStep.objects.create(run=work, seq=_CALL_SEQ, kind="model", status="dispatching", attempt_no=1, started_at=now)
    return endpoint, key


class _SingleCall:
    def __init__(self, claim, transport, materials, protected):
        self.claim, self.transport, self.materials, self.protected = claim, transport, materials, protected
        self.error = self.response = self.answer = None
        self.parse_started = self.parse_finished = None
        self.cited = set()

    def complete(self, packet):
        try:
            wire = wire_bytes(self.claim.config_snapshot["model"], packet)
            actual = digest(wire)
            if not compare_digest(actual, self.claim.packet_digest):
                raise DebugError("PREVIEW_STALE")
            endpoint, key = _dispatch(self.claim, self.materials, self.protected, actual)
            self.response = self.transport(endpoint, wire, key)
            self.parse_started = timezone.now()
            http_status = self.response.http_status
            if type(http_status) is not int or not 200 <= http_status < 300:
                raise DebugError("PROVIDER_REJECTED", 502, received=True)
            if self.response.model != self.claim.config_snapshot["model"]:
                raise DebugError("OUTPUT_SCHEMA_INVALID", 502, received=True)
            if not isinstance(self.response.text, str):
                raise DebugError("OUTPUT_SCHEMA_INVALID", 502, received=True)
            if len(self.response.text.encode("utf-8")) > 65536:
                raise DebugError("RESPONSE_TOO_LARGE", 502, received=True, truncated=True)
            self.answer, self.cited = parse_response(self.response.text, packet)
            return self.response.text
        except DebugError as exc:
            self.error = exc
            raise
        except Exception:
            self.error = DebugError("INTERNAL_ERROR", 500)
            raise self.error from None
        finally:
            if self.parse_started is not None:
                self.parse_finished = timezone.now()


def _save_trace(work, step, packet, call, now):
    # Always redact by structure (not substring), including all free text,
    # identifiers from remote headers, excerpts, and system/prompt contents.
    protected = {"request_redacted": {"mode": work.mode, "packet_digest": work.packet_digest,
                                      "model": work.config_snapshot.get("model"),
                                      "fact_fields": sorted(packet["facts"]) if packet else [],
                                      "indexed_materials": len(packet["reference_packet"]["evidence_index"]) if packet else 0},
                 "response_redacted": ({"parsed": True, "candidate_count": len(call.answer.candidates),
                                        "missing_facts": call.answer.missing_facts}
                                       if call and call.answer else None),
                 "withheld_fields": ["authorization", "facts", "prompt", "source_text", "provider_text", "provider_headers"]}
    TracePayload.objects.create(step=step, ciphertext=seal_data(protected, f"trace:{work.pk}:{step.seq}"),
                                expires_at=min(work.input_expires_at, now + timedelta(days=7)))


def _finish(claim, materials, packet, call, output, error):
    with transaction.atomic():
        mutex()
        work = _current(claim)
        if work is None:
            return
        now = timezone.now()
        if not error and now >= _deadline(work):
            error = DebugError("RUN_DEADLINE")
        code = (error.code if error.code in MESSAGES else "INTERNAL_ERROR") if error else ""
        status = (State.INTERRUPTED if code in {"TIMEOUT", "WORKER_INTERRUPTED"} else State.FAILED) if error else State.SUCCEEDED
        step = work.steps.filter(seq=_CALL_SEQ).first()
        parsed = bool(call and call.answer)
        if step is None and work.mode == "mock" and output is not None:
            prepared = work.steps.get(seq=_PREPARE_SEQ)
            step = DebugStep(run=work, seq=_CALL_SEQ, kind="mock", started_at=prepared.finished_at)
        if step is not None:
            if call and call.response:
                response = call.response
                step.received = True
                step.http_status = response.http_status if type(response.http_status) is int and 100 <= response.http_status < 600 else None
                if isinstance(response.usage, dict):
                    step.usage = {field: value for field, value in response.usage.items()
                                  if field in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                  and type(value) is int and 0 <= value <= 10**12} or None
            if error:
                step.received = step.received or bool(error.received)
                step.truncated = bool(error.truncated)
                if type(error.http_status) is int and 100 <= error.http_status < 600:
                    step.http_status = error.http_status
            step.status = "parsed" if parsed else status
            step.error_code = code
            step.finished_at = (call.parse_started if call else None) or now
            step.save()
            if code not in {"DATA_UNAVAILABLE", "SECRET_UNAVAILABLE"}:
                _save_trace(work, step, packet, call, now)
        if call and call.parse_started:
            DebugStep.objects.create(run=work, seq=_PARSE_SEQ, kind="parse", started_at=call.parse_started,
                                     finished_at=call.parse_finished, status=State.SUCCEEDED if parsed else State.FAILED,
                                     error_code="" if parsed else code)
        postcheck_step = None
        if parsed:
            postcheck_step = DebugStep.objects.create(run=work, seq=_CHECK_SEQ, kind="postcheck", started_at=call.parse_finished,
                                                      finished_at=now, status=status, error_code=code)
        prepare_step = work.steps.filter(seq=_PREPARE_SEQ).first()
        if prepare_step is None:
            prepare_step = DebugStep.objects.create(run=work, seq=_PREPARE_SEQ, kind="prepare", started_at=work.started_at,
                                                    finished_at=now, status=status, error_code=code)
        if packet:
            DebugEvidence.objects.bulk_create([
                DebugEvidence(run=work, step=(postcheck_step or prepare_step) if role == "postcheck_only" else
                              prepare_step if role == "retrieved" or step is None else step,
                              role=role, kind=kind, source_key=key, status=located, ref=ref)
                for role, kind, key, located, ref in evidence_rows(
                    materials, packet, call.cited if call else set(), output, sent=bool(step and step.attempt_no))
            ])
        if not error:
            work.result = result_summary(output)
            missing = {"MODEL_FACTS_PENDING", "MISSING_USAGE"}
            work.business_status = (BusinessState.NEEDS_FACTS if any(g.code in missing for g in output.gaps)
                                    else BusinessState.CANDIDATE if output.candidates else BusinessState.UNDETERMINED)
        work.status, work.error_code, work.finished_at, work.lease_until = status, code, now, None
        work.save(update_fields=["status", "error_code", "finished_at", "lease_until", "business_status", "result"])
        event("debug.run.finished", actor_id=work.owner_id, request_id=work.request_id,
              target_type="run", target_id=work.pk, run_id=work.pk,
              outcome="denied" if error and error.status in (401, 403) else "failure" if error else "success", summary_code=code)


def _execute_run(claim, transport):
    materials = packet = call = output = error = None
    try:
        _authorized(claim)
        protected = _read_input(claim)
        cfg = claim.config_snapshot
        if claim.mode == "live":
            materials = live_materials(claim.owner, snapshot=cfg)
        payload, adapter, packet, body, versions = prepare(protected["input"], claim.mode, cfg.get("model"), materials)
        if not compare_digest(digest(body), claim.packet_digest) or cfg["versions"] != versions:
            raise DebugError("PREVIEW_STALE")
        if claim.mode == "live":
            call = _SingleCall(claim, transport, materials, protected)
            adapter.model = call
        else:
            DebugStep.objects.create(run=claim, seq=_PREPARE_SEQ, kind="prepare", status=State.SUCCEEDED,
                                     started_at=claim.started_at, finished_at=timezone.now())
        output = adapter.recommend(payload)
    except DebugError as exc:
        error = exc
    except Exception:
        error = (call.error if call else None) or DebugError("INTERNAL_ERROR", 500)
    _finish(claim, materials, packet, call, output, error)


def work_once(*, transport=None):
    """Claim only fresh queued work; never re-claim an interrupted request."""
    claim = _claim()
    if claim is None:
        return False
    if isinstance(claim, DebugProbe):
        execute_probe(claim, transport=transport or send)
    else:
        _execute_run(claim, transport or send)
    return True
