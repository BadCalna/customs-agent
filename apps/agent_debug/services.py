"""Preview and admission: ciphertext inputs, immutable execution metadata."""
import json
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone
from pydantic import ValidationError

from .access import flag, limit, live_enabled, require, setting
from .admission import check_concurrency, mutex, reserve_quota
from .connections import check_endpoint
from .contracts import Basis, Facts, digest, input_hash
from .engine import prepare
from .errors import DebugError
from .maintenance import event
from .materials import live_materials
from .models import DebugDefault, DebugPreview, DebugRun, RevisionState
from .security import get_endpoint, open_data, seal_data

PREVIEW_TTL = timedelta(minutes=10)
_BUILT_IN = {
    "demo": ("合成样例 · 连接线", {"name": "演示连接线", "attributes": {"material": "铜", "usage": "低压传输"}}, "normal"),
    "missing-material": ("合成样例 · 缺用途", {"name": "演示连接线", "attributes": {"material": "铜"}}, "missing_facts"),
}
_SAMPLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")


def _json_setting(key, default):
    raw = setting(key, default)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return default


def _input(facts, basis=None, mock_scenario=None):
    return {"facts": Facts.model_validate(facts).model_dump(mode="json"),
            "query_basis": Basis.model_validate(basis or {}).model_dump(mode="json", exclude_none=True),
            "mock_scenario": mock_scenario}


def samples():
    found = {key: (label, _input(facts, mock_scenario=scenario), "mock_sample")
             for key, (label, facts, scenario) in _BUILT_IN.items()}
    approved = _json_setting("OPS_DEBUG_SAMPLES", [])
    if not isinstance(approved, list):
        return found
    registered = set()
    for row in approved:
        if not isinstance(row, dict) or row.keys() - {"id", "label", "facts", "query_basis"}:
            continue
        key, label = row.get("id"), row.get("label")
        if (not isinstance(key, str) or not _SAMPLE_ID.fullmatch(key) or key in _BUILT_IN
                or not isinstance(label, str) or not label.strip() or len(label) > 60 or not label.isprintable()):
            continue
        if key in registered:
            found.pop(key, None)
            continue
        registered.add(key)
        try:
            found[key] = (label, _input(row.get("facts"), row.get("query_basis")), "approved_sample")
        except ValidationError:
            continue
    return found


def _read_input(run):
    cutoff = run.created_at + timedelta(days=limit("OPS_DEBUG_INPUT_RETENTION_DAYS", 7))
    if not run.input_ciphertext or min(run.input_expires_at, cutoff) <= timezone.now():
        raise DebugError("DATA_UNAVAILABLE")
    return open_data(run.input_ciphertext, f"run:{run.pk}")


def _resolve(user, data):
    if data.parent_run_id:
        # Cross-account reruns would expose somebody else's input in the preview.
        try:
            parent = DebugRun.objects.get(pk=data.parent_run_id, owner=user)
        except DebugRun.DoesNotExist:
            raise DebugError("not_found", 404) from None
        if parent.mode == "live":
            from .access import catalog_visible
            cfg = parent.config_snapshot
            if not catalog_visible(user, cfg.get("snapshot_id"), cfg.get("bundle_fingerprint")):
                raise DebugError("forbidden", 403)
        source = _read_input(parent)
        return source["input"], source["sample_kind"], source["sample_id"], parent.sample_label
    if data.sample_id:
        try:
            label, snapshot, kind = samples()[data.sample_id]
        except KeyError:
            raise DebugError("INVALID_INPUT", 422) from None
        return snapshot, kind, data.sample_id, label
    return _input(data.facts, data.query_basis), "local_facts", "", "自填样例"


def _active(default):
    connection = default.connection if default and default.connection_id else None
    if not connection or connection.status != RevisionState.ACTIVE:
        raise DebugError("CONNECTION_NOT_READY")
    return connection, check_endpoint(connection)


def check_external(endpoint, kind, sample_id, source, body_digest):
    if endpoint.target == "local":
        if kind == "local_facts" and not flag("OPS_DEBUG_LOCAL_FREE_INPUT"):
            raise DebugError("EXTERNAL_NOT_APPROVED", 403)
        return
    approved = _json_setting("OPS_DEBUG_EXTERNAL_PACKET_DIGESTS", {})
    digests = approved.get(endpoint.id) if isinstance(approved, dict) else None
    current = samples().get(sample_id)
    if (kind != "approved_sample" or not current or current[1] != source
            or not isinstance(digests, list) or body_digest not in digests):
        raise DebugError("EXTERNAL_NOT_APPROVED", 403)


def external_packet(sample_id, endpoint_id, model):
    """Offline operations helper only: review these exact bytes before approving their digest."""
    endpoint = get_endpoint(endpoint_id, model)
    current = samples().get(sample_id)
    if endpoint.target != "external" or not current or current[2] != "approved_sample":
        raise DebugError("EXTERNAL_NOT_APPROVED", 403)
    return prepare(current[1], "live", model, live_materials(None, approval_review=True))[3]


def external_packet_digest(sample_id, endpoint_id, model):
    return digest(external_packet(sample_id, endpoint_id, model))


def create_preview(user, data):
    source, kind, sample_id, label = _resolve(user, data)
    connection = endpoint = materials = None
    if data.mode == "live":
        if not live_enabled():
            raise DebugError("ENDPOINT_NOT_ALLOWED", 403)
        if kind == "mock_sample":
            raise DebugError("EXTERNAL_NOT_APPROVED", 403)
        default = DebugDefault.objects.select_related("connection").filter(pk=1).first()
        connection, endpoint = _active(default)
        if kind == "local_facts" and (endpoint.target != "local" or not flag("OPS_DEBUG_LOCAL_FREE_INPUT")):
            raise DebugError("EXTERNAL_NOT_APPROVED", 403)
        materials = live_materials(user)
    _, _, _, body, versions = prepare(source, data.mode, connection.model if connection else None, materials)
    packet_digest = digest(body)
    config = {"versions": versions, **versions, "target": "mock"}
    destination = {"target": "mock", "host": "本地模拟", "model": None}
    if endpoint:
        check_external(endpoint, kind, sample_id, source, packet_digest)
        destination = {"target": endpoint.target, "host": urlsplit(endpoint.url).hostname, "model": connection.model}
        config.update(target=endpoint.target, endpoint_fingerprint=endpoint.fingerprint,
                      default_version=default.version, model=connection.model, connection_alias=connection.alias,
                      connection_revision=connection.revision, connection_id=str(connection.pk), host=destination["host"])
    ticket = secrets.token_urlsafe(32)
    ticket_hash = digest(ticket)
    expires = timezone.now() + PREVIEW_TTL
    protected = {"input": source, "sample_kind": kind, "sample_id": sample_id, "label": label}
    DebugPreview.objects.create(ticket_hash=ticket_hash, owner=user, parent_run_id=data.parent_run_id,
                                sample_kind=kind, sample_id=sample_id, mode=data.mode,
                                input_ciphertext=seal_data(protected, f"preview:{ticket_hash}"), input_digest=input_hash(protected),
                                packet_digest=packet_digest, connection=connection, config_snapshot=config, expires_at=expires)
    # Browser response allowlist: do not include or deserialize the request body.
    return {"ticket": ticket, "expires_at": expires.isoformat(), "destination": destination,
            "request_bytes": len(body), "packet_digest": packet_digest, "versions": versions,
            "warnings": {"mock": ["合成模拟，不联网"], "local": ["仅发往登记的本机端点"],
                         "external": ["仅获批样例；确认后将出机"]}[destination["target"]]}


def _recheck_preview(user, preview, default, protected):
    config = preview.config_snapshot
    if not live_enabled():
        raise DebugError("ENDPOINT_NOT_ALLOWED", 403)
    if default.connection_id != preview.connection_id or default.version != config["default_version"]:
        raise DebugError("PREVIEW_STALE")
    connection, endpoint = _active(default)
    current = live_materials(user)
    _, _, _, body, versions = prepare(protected["input"], "live", connection.model, current)
    if (config["endpoint_fingerprint"] != endpoint.fingerprint or config["versions"] != versions
            or digest(body) != preview.packet_digest):
        raise DebugError("PREVIEW_STALE")
    check_external(endpoint, protected["sample_kind"], protected["sample_id"], protected["input"], preview.packet_digest)


def create_run(user, data, idem, request_id):
    ticket_hash = digest(data.ticket)
    fingerprint = input_hash(data.model_dump(mode="json"))
    # Resolve idempotency before checking consumed/expired tickets, but only after authorization.
    require(user, "use_debug")
    with transaction.atomic():
        default = mutex()
        existing = DebugRun.objects.filter(owner=user, idempotency_hash=idem).first()
        if existing:
            if existing.request_hash != fingerprint:
                raise DebugError("IDEMPOTENCY_CONFLICT")
            if existing.mode == "live":
                from .access import catalog_visible
                cfg = existing.config_snapshot
                if not catalog_visible(user, cfg.get("snapshot_id"), cfg.get("bundle_fingerprint")):
                    raise DebugError("forbidden", 403)
            return existing
        try:
            preview = DebugPreview.objects.get(pk=ticket_hash, owner=user)
        except DebugPreview.DoesNotExist:
            raise DebugError("PREVIEW_STALE") from None
        now = timezone.now()
        if preview.consumed_at or preview.expires_at <= now or preview.parent_run_id != data.parent_run_id:
            raise DebugError("PREVIEW_STALE")
        protected = open_data(preview.input_ciphertext, f"preview:{ticket_hash}")
        if input_hash(protected) != preview.input_digest:
            raise DebugError("PREVIEW_STALE")
        if preview.mode == "live":
            if preview.config_snapshot["target"] == "external" and not data.confirm_external:
                raise DebugError("EXTERNAL_CONFIRMATION_REQUIRED", 403)
            _recheck_preview(user, preview, default, protected)
        check_concurrency(user)
        if preview.mode == "live":
            reserve_quota(user)
        run_id = uuid.uuid4()
        run = DebugRun.objects.create(id=run_id, owner=user, request_id=request_id, parent_run=preview.parent_run,
                                      idempotency_hash=idem, request_hash=fingerprint, preview_hash=ticket_hash,
                                      mode=preview.mode, sample_label=protected["label"],
                                      input_ciphertext=seal_data(protected, f"run:{run_id}"),
                                      input_expires_at=now + timedelta(days=limit("OPS_DEBUG_INPUT_RETENTION_DAYS", 7)),
                                      packet_digest=preview.packet_digest, connection_id=preview.connection_id,
                                      config_snapshot=preview.config_snapshot)
        preview.consumed_at = now
        preview.input_ciphertext = ""
        preview.save(update_fields=["consumed_at", "input_ciphertext"])
        event("debug.run.queued", actor_id=user.pk, request_id=request_id, target_type="run", target_id=run.pk,
              summary_code="QUEUED", run_id=run.pk)
        return run


def work_once(*, transport=None):
    from .worker import work_once as _work_once
    return _work_once(transport=transport)
