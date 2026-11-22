"""Install this router under /api/v1/ops/debug/. No dependency on A or C models."""
from __future__ import annotations

import json
import uuid
from functools import wraps

from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from ninja import Router
from pydantic import ValidationError

from apps.accounts.auth import session_auth
from apps.accounts.errors import ApiError

from . import connections as revisions
from .access import live_enabled, require
from .contracts import ConnectionIn, DefaultIn, PreviewIn, RunIn, idempotency_hash
from .errors import DebugError, MESSAGES
from .maintenance import event
from .materials import live_materials
from .models import ConnectionRevision, DebugDefault, DebugProbe, RevisionState
from .security import endpoints
from .services import create_preview, create_run, samples
from .views_data import read_trace, run_page, serialize_run, serialize_steps, visible_run

router = Router(tags=["Agent 调试台"], auth=None)


def _json(request, payload, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "private, no-store"
    response["X-Request-ID"] = request.debug_request_id
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _guard(permission):
    def wrap(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            request.debug_request_id = str(uuid.uuid4())
            user = None
            try:
                # Reuse project session/CSRF behavior, but keep B errors and
                # their request IDs together instead of leaking schema details.
                user = session_auth(request)
                if user is None or not user.is_active:
                    raise DebugError("unauthorized", 401)
                require(user, permission)
                return view(request, user, *args, **kwargs)
            except (DebugError, ApiError) as exc:
                code, status = exc.code, exc.status
            except Exception:
                code, status = "INTERNAL_ERROR", 500
            if user is not None and request.method == "POST" and status in (401, 403, 409, 422, 429):
                try:
                    event("debug.request.rejected", actor_id=user.pk, request_id=request.debug_request_id,
                          target_type="debug.request", target_id=request.debug_request_id,
                          outcome="denied" if status in (401, 403) else "failure", summary_code=code)
                except Exception:
                    # Do not reveal database failures when refusing a request.
                    pass
            return _json(request, {"error": {"code": code, "message": MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"]),
                                             "details": {}}, "request_id": request.debug_request_id}, status)
        # Ninja must not interpret our injected user as an API argument.
        from inspect import signature
        sig = signature(view)
        guarded.__signature__ = sig.replace(parameters=[p for p in sig.parameters.values() if p.name != "user"])
        return guarded
    return wrap


def _input(request, cls):
    def unique_pairs(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > 32768 or len(request.body) > 32768:
            raise ValueError
        return cls.model_validate(json.loads(request.body or b"{}", object_pairs_hook=unique_pairs))
    except (ValidationError, ValueError, TypeError, RecursionError, RequestDataTooBig):
        raise DebugError("INVALID_INPUT", 422) from None


def _id(value):
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        raise DebugError("not_found", 404) from None


def _idem(request):
    return idempotency_hash(request.headers.get("Idempotency-Key", ""))


def _probe_row(probe):
    return {"id": str(probe.pk), "status": probe.status, "error_code": probe.error_code,
            "error_message": MESSAGES.get(probe.error_code, ""), "http_status": probe.http_status,
            "received": probe.received, "started_at": probe.started_at, "finished_at": probe.finished_at,
            "actor": str(probe.actor_id) if probe.actor_id else None, "waiting": probe.status == "queued"}


def _connection_row(connection, default_id=None):
    probe = connection.last_probe
    return {"id": str(connection.pk), "alias": connection.alias, "revision": connection.revision,
            "endpoint_id": connection.endpoint_id, "model": connection.model, "auth_mode": connection.auth_mode,
            "state": connection.status, "has_key": bool(connection.secret_ref), "is_default": default_id == connection.pk,
            "last_probe": _probe_row(probe) if probe else None}


@router.get("capabilities")
@_guard("use_debug")
def capabilities(request, user):
    snapshot = active = None
    if live_enabled():
        try:
            snapshot = live_materials(user).snapshot_id
        except DebugError:
            pass
        default = DebugDefault.objects.select_related("connection").filter(pk=1).first()
        if snapshot and default and default.connection and default.connection.status == RevisionState.ACTIVE:
            try:
                endpoint = revisions.check_endpoint(default.connection)
                active = {"alias": default.connection.alias, "model": default.connection.model, "target": endpoint.target}
            except DebugError:
                pass
    return _json(request, {"modes": ["mock"] + (["live"] if snapshot else []), "connection": active,
                           "samples": [{"id": key, "label": label, "mode": "mock" if kind == "mock_sample" else None}
                                       for key, (label, _, kind) in samples().items() if snapshot or kind == "mock_sample"],
                           "snapshot_ready": bool(snapshot), "snapshot": snapshot, "external_approval_required": True})


@router.get("connections")
@_guard("manage_connections")
def connection_list(request, user):
    default = DebugDefault.objects.filter(pk=1).first()
    rows = ConnectionRevision.objects.select_related("last_probe").order_by("alias", "-revision")
    return _json(request, {"items": [_connection_row(c, default.connection_id if default else None) for c in rows],
                           "default_version": default.version if default else 0,
                           "endpoints": [{"id": e.id, "label": e.label, "models": list(e.models), "auth_mode": e.auth_mode,
                                          "target": e.target} for e in endpoints().values()]})


@router.post("connections")
@_guard("manage_connections")
def create_connection(request, user):
    obj = revisions.create(user, _input(request, ConnectionIn), request.debug_request_id)
    return _json(request, {"id": str(obj.pk), "revision": obj.revision, "state": obj.status, "has_key": bool(obj.secret_ref)}, 201)


@router.post("connections/{connection_id}/test")
@_guard("manage_connections")
def probe(request, user, connection_id: str):
    work = revisions.submit_probe(user, _id(connection_id), _idem(request), request.debug_request_id)
    return _json(request, {"probe_id": str(work.pk), "status": work.status}, 202)


@router.get("probes/{probe_id}")
@_guard("manage_connections")
def probe_detail(request, user, probe_id: str):
    work = DebugProbe.objects.filter(pk=_id(probe_id)).first()
    if work is None:
        raise DebugError("not_found", 404)
    return _json(request, _probe_row(work))


@router.post("connections/{connection_id}/activate")
@_guard("manage_connections")
def activate(request, user, connection_id: str):
    d = _input(request, DefaultIn)
    obj, version = revisions.activate(user, _id(connection_id), d.expected_default_version, request.debug_request_id)
    return _json(request, {"connection": _connection_row(obj, obj.pk), "default_version": version, "version": version})


@router.post("connections/{connection_id}/revoke")
@_guard("manage_connections")
def revoke(request, user, connection_id: str):
    d = _input(request, DefaultIn)
    obj, version = revisions.revoke(user, _id(connection_id), d.expected_default_version, request.debug_request_id)
    return _json(request, {"connection": _connection_row(obj), "default_version": version, "version": version})


@router.post("previews")
@_guard("use_debug")
def preview(request, user):
    return _json(request, create_preview(user, _input(request, PreviewIn)), 201)


@router.post("runs")
@_guard("use_debug")
def run_create(request, user):
    run = create_run(user, _input(request, RunIn), _idem(request), request.debug_request_id)
    # The replay has its own HTTP correlation ID; run_request_id points at
    # the original queue/finish pair without re-emitting a queued event.
    return _json(request, {"id": str(run.pk), "status": run.status, "request_id": request.debug_request_id,
                           "run_request_id": str(run.request_id)}, 202)


@router.get("runs")
@_guard("use_debug")
def run_list(request, user):
    return _json(request, run_page(user, request.GET))


@router.get("runs/{run_id}")
@_guard("use_debug")
def run_detail(request, user, run_id: str):
    return _json(request, serialize_run(user, visible_run(user, _id(run_id)), detail=True))


@router.get("runs/{run_id}/steps")
@_guard("use_debug")
def steps(request, user, run_id: str):
    return _json(request, {"items": serialize_steps(user, visible_run(user, _id(run_id)))})


@router.get("runs/{run_id}/steps/{seq}/payload")
@_guard("use_debug")
def payload(request, user, run_id: str, seq: str):
    if not seq.isascii() or not seq.isdecimal() or len(seq) > 5:
        raise DebugError("not_found", 404)
    return _json(request, read_trace(user, _id(run_id), int(seq), request.debug_request_id))
