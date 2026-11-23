"""Manage immutable connection revisions and execute fixed, synthetic probes."""
from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from .access import require
from .admission import check_concurrency, mutex, reserve_quota
from .contracts import ConnectionIn, digest
from .errors import MESSAGES, DebugError
from .maintenance import event
from .models import ConnectionRevision, DebugProbe, RevisionState, State
from .security import Endpoint, get_endpoint, load_secret, open_secret, persist_secret, seal_secret, serialize_request
from .transport import send

_PROBE_VALIDITY = timedelta(hours=24)
_PROBE_MESSAGES = (
    {"role": "system", "content": 'Connection health check. Respond only with the JSON object {"ok":true}.'},
    {"role": "user", "content": "Run the synthetic connection health check."},
)


def check_endpoint(connection: ConnectionRevision) -> Endpoint:
    endpoint = get_endpoint(connection.endpoint_id, connection.model, connection.auth_mode)
    if endpoint.fingerprint != connection.endpoint_fingerprint:
        raise DebugError("ENDPOINT_NOT_ALLOWED")
    return endpoint


def _locked_connection(connection_id) -> ConnectionRevision:
    try:
        return ConnectionRevision.objects.select_for_update().get(pk=connection_id)
    except ConnectionRevision.DoesNotExist:
        raise DebugError("not_found", 404) from None


def _bearer_key(value: str, *, code: str, status: int) -> str:
    # HTTP header values must not contain whitespace, controls, or non-ASCII text.
    if not isinstance(value, str) or not value or any(not ("!" <= c <= "~") for c in value):
        raise DebugError(code, status)
    return value


def key_for(connection: ConnectionRevision) -> str | None:
    """Open this revision's credential immediately before authorized dispatch."""
    if connection.auth_mode == "none":
        return None
    if connection.auth_mode != "bearer" or not connection.secret_ref:
        raise DebugError("SECRET_UNAVAILABLE")
    try:
        key = open_secret(load_secret(connection.secret_ref), str(connection.pk))
    except OSError:
        raise DebugError("SECRET_UNAVAILABLE") from None
    return _bearer_key(key, code="SECRET_UNAVAILABLE", status=409)


def _connection_event(action, user, request_id, connection):
    event(f"debug.connection.{action}", actor_id=user.pk, request_id=request_id,
          target_type="debug.connection", target_id=connection.pk)


def create(user, data: ConnectionIn, request_id) -> ConnectionRevision:
    require(user, "manage_connections")
    connection_id = uuid4()
    with transaction.atomic():
        mutex()
        endpoint = get_endpoint(data.endpoint_id, data.model, data.auth_mode)
        latest = ConnectionRevision.objects.filter(alias=data.alias).order_by("-revision").first()
        secret_ref = ""
        if data.auth_mode == "bearer":
            if data.copy_secret_from:
                try:
                    source = _locked_connection(data.copy_secret_from)
                except DebugError:
                    raise DebugError("SECRET_UNAVAILABLE") from None
                if source.status == RevisionState.REVOKED or source.auth_mode != "bearer":
                    raise DebugError("SECRET_UNAVAILABLE")
                # Copy the credential to a new UUID-bound ciphertext, never to a different target policy.
                if check_endpoint(source).fingerprint != endpoint.fingerprint:
                    raise DebugError("ENDPOINT_NOT_ALLOWED")
                key = key_for(source)
            else:
                key = _bearer_key(data.api_key, code="INVALID_INPUT", status=422)
            try:
                secret_ref = persist_secret(seal_secret(key, str(connection_id)))
            except OSError:
                raise DebugError("SECRET_UNAVAILABLE") from None
        connection = ConnectionRevision.objects.create(
            id=connection_id, alias=data.alias, revision=latest.revision + 1 if latest else 1,
            endpoint_id=endpoint.id, endpoint_fingerprint=endpoint.fingerprint, model=data.model,
            auth_mode=endpoint.auth_mode, secret_ref=secret_ref, created_by=user,
        )
        _connection_event("created", user, request_id, connection)
    return connection


def submit_probe(user, connection_id, idem, request_id) -> DebugProbe:
    """Queue a probe using the validated digest of the client's idempotency key."""
    require(user, "manage_connections")
    request_id = UUID(str(request_id))
    with transaction.atomic():
        mutex()
        connection = _locked_connection(connection_id)
        request_hash = digest(f"probe:{connection.pk}")
        replay = DebugProbe.objects.filter(actor=user, idempotency_hash=idem).first()
        if replay is not None:
            if replay.request_hash != request_hash:
                raise DebugError("IDEMPOTENCY_CONFLICT")
            return replay
        if connection.status == RevisionState.REVOKED:
            raise DebugError("CONNECTION_REVOKED")
        if connection.status == RevisionState.RETIRED:
            raise DebugError("CONNECTION_NOT_READY")
        check_endpoint(connection)
        check_concurrency(user)
        reserve_quota(user)
        probe = DebugProbe.objects.create(
            connection=connection, actor=user, idempotency_hash=idem,
            request_hash=request_hash, request_id=request_id,
        )
        connection.last_probe = probe
        if connection.status == RevisionState.TESTED:
            # A newer attempt supersedes the activation evidence, even if it never leaves the queue.
            connection.status = RevisionState.DRAFT
        connection.save(update_fields=["last_probe", "status"])
        event("debug.probe.queued", actor_id=user.pk, request_id=request_id,
              target_type="debug.probe", target_id=probe.pk)
        return probe


def activate(user, connection_id, expected_version, request_id) -> tuple[ConnectionRevision, int]:
    require(user, "manage_connections")
    with transaction.atomic():
        default = mutex()
        if default.version != expected_version:
            raise DebugError("DEFAULT_CHANGED")
        connection = _locked_connection(connection_id)
        if connection.status == RevisionState.REVOKED:
            raise DebugError("CONNECTION_REVOKED")
        if connection.status == RevisionState.ACTIVE and default.connection_id == connection.pk:
            return connection, default.version
        if connection.status != RevisionState.TESTED:
            raise DebugError("PROBE_REQUIRED")
        if not DebugProbe.objects.filter(
            pk=connection.last_probe_id, connection=connection, status=State.SUCCEEDED,
            finished_at__gte=timezone.now() - _PROBE_VALIDITY,
        ).exists():
            raise DebugError("PROBE_REQUIRED")
        check_endpoint(connection)
        key_for(connection)
        ConnectionRevision.objects.filter(status=RevisionState.ACTIVE).update(status=RevisionState.RETIRED)
        connection.status = RevisionState.ACTIVE
        connection.save(update_fields=["status"])
        default.connection = connection
        default.version += 1
        default.save(update_fields=["connection", "version"])
        _connection_event("activated", user, request_id, connection)
    return connection, default.version


def revoke(user, connection_id, expected_version, request_id) -> tuple[ConnectionRevision, int]:
    require(user, "manage_connections")
    with transaction.atomic():
        default = mutex()
        if default.version != expected_version:
            raise DebugError("DEFAULT_CHANGED")
        connection = _locked_connection(connection_id)
        was_default = default.connection_id == connection.pk
        if connection.status == RevisionState.REVOKED and not was_default:
            return connection, default.version
        connection.status = RevisionState.REVOKED
        connection.revoked_at = timezone.now()
        connection.save(update_fields=["status", "revoked_at"])
        if was_default:
            default.connection = None
            default.version += 1
            default.save(update_fields=["connection", "version"])
        _connection_event("revoked", user, request_id, connection)
    return connection, default.version


def _owns(current: DebugProbe, claimed: DebugProbe, now) -> bool:
    return (current.status == State.RUNNING and current.started_at is not None
            and current.started_at == claimed.started_at and current.lease_until is not None
            and current.lease_until == claimed.lease_until and current.lease_until > now)


def _finish(probe, connection, *, status, error_code="", http_status=None, received=False):
    # A failed re-test must not demote an active default or overwrite a newer probe's state.
    if (connection.last_probe_id == probe.pk
            and connection.status in (RevisionState.DRAFT, RevisionState.TESTED)):
        connection.status = RevisionState.TESTED if status == State.SUCCEEDED else RevisionState.DRAFT
        connection.save(update_fields=["status"])
    probe.status = status
    probe.finished_at = timezone.now()
    probe.lease_until = None
    probe.error_code = error_code
    probe.http_status = http_status
    probe.received = received
    probe.save(update_fields=["status", "finished_at", "lease_until", "error_code", "http_status", "received"])
    outcome = "success" if status == State.SUCCEEDED else (
        "denied" if error_code in {"forbidden", "unauthorized", "not_found", "CONNECTION_REVOKED"} else "failure"
    )
    event("debug.probe.finished", actor_id=probe.actor_id, request_id=probe.request_id,
          target_type="debug.probe", target_id=probe.pk, outcome=outcome, summary_code=error_code)
    return probe


def _strict_probe_response(text) -> bool:
    if not isinstance(text, str):
        return False
    try:
        # Using pairs preserves duplicate keys; only one actual boolean true is legal.
        obj = json.loads(text, object_pairs_hook=tuple)
    except (ValueError, RecursionError):
        return False
    return isinstance(obj, tuple) and obj == (("ok", True),) and obj[0][1] is True


def execute_probe(probe: DebugProbe, *, transport=send) -> DebugProbe | None:
    """Execute one already claimed probe. Never re-send a persisted dispatch marker."""
    # The marker must be committed before transport can run, even if called from an outer transaction.
    with transaction.atomic(durable=True):
        mutex()
        current = DebugProbe.objects.select_for_update().filter(pk=probe.pk).first()
        if current is None or not _owns(current, probe, timezone.now()) or current.dispatching_at is not None:
            return None
        connection = _locked_connection(current.connection_id)
        try:
            # Fetch actor afresh: never trust the request-time principal or its permission cache.
            actor = current.actor
            if actor is None:
                raise DebugError("unauthorized", 401)
            require(actor, "manage_connections")
            if connection.status == RevisionState.REVOKED:
                raise DebugError("CONNECTION_REVOKED")
            endpoint = check_endpoint(connection)
            key = key_for(connection)
            body = serialize_request(connection.model, {"messages": list(_PROBE_MESSAGES), "stream": False})
        except DebugError as exc:
            if _owns(current, probe, timezone.now()):
                return _finish(current, connection, status=State.FAILED, error_code=exc.code)
            return None
        dispatched_at = timezone.now()
        if not _owns(current, probe, dispatched_at):
            return None
        current.dispatching_at = dispatched_at
        current.save(update_fields=["dispatching_at"])

    status, error_code, http_status, received = State.SUCCEEDED, "", None, False
    try:
        result = transport(endpoint, body, key)
        http_status, received = result.http_status, True
        if not isinstance(http_status, int) or not 200 <= http_status < 300:
            raise DebugError("PROVIDER_REJECTED", 502)
        if getattr(result, "model", None) != connection.model or not _strict_probe_response(result.text):
            raise DebugError("OUTPUT_SCHEMA_INVALID", 502)
    except DebugError as exc:
        error_code = exc.code if exc.code in MESSAGES else "INTERNAL_ERROR"
        status = State.INTERRUPTED if error_code in {"TIMEOUT", "WORKER_INTERRUPTED"} else State.FAILED
        http_status = exc.http_status if exc.http_status is not None else http_status
        received = received or exc.received
    except Exception:
        status, error_code = State.FAILED, "INTERNAL_ERROR"
    if type(http_status) is not int or not 100 <= http_status < 600:
        http_status = None

    with transaction.atomic():
        mutex()
        current = DebugProbe.objects.select_for_update().filter(pk=probe.pk).first()
        if (current is None or not _owns(current, probe, timezone.now())
                or current.dispatching_at != dispatched_at):
            return None
        connection = _locked_connection(current.connection_id)
        if not _owns(current, probe, timezone.now()):
            return None
        # Revocation after the committed marker cannot retract the in-flight HTTP call.
        # Keep its outcome for audit but never lift the revision out of REVOKED.
        return _finish(current, connection, status=status, error_code=error_code,
                       http_status=http_status, received=bool(received))
