"""Mount with `api_v1.add_router('/ops/data', router)`; no write routes."""
import logging
from uuid import uuid4

from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from ninja import Router

from . import services
from .errors import QUERY_FAILED, ReferenceError, json_response, request_id
from .permissions import authenticated
from .store import SnapshotUnreadable

router = Router(tags=["基础数据中心"])
logger = logging.getLogger("customs.ops.events")


def _emit(request, operation, target_type, result, error, *, detail):
    user = getattr(request, "user", None)
    target_id = None
    if error is None and (row := result.get("record")):
        target_id = "ref_" + salted_hmac(
            "reference_browser.target", f"{result['snapshot_id']}:{row['record_id']}"
        ).hexdigest()[:32]
    if error:
        outcome = "denied" if error.status in {401, 403, 404} else "failure"
        summary = error.code
    else:
        outcome = "success"
        summary = result.get("outcome", "ok")
        if detail:
            # The service validated this enum before success; no free-form reasons enter audit.
            summary = request.GET.get("reason", "reference_review").strip()
    logger.info("ops.event", extra={"ops_event": {
        "schema_version": 1, "event_id": uuid4().hex, "occurred_at": timezone.now().isoformat(),
        "event_type": f"reference_browser.{operation}",
        "actor_id": str(user.pk) if authenticated(user) else None, "request_id": request_id(request),
        "target_type": target_type, "target_id": target_id, "outcome": outcome, "summary_code": summary,
    }})


def _run(request, operation, target_type, action, *args, detail=False) -> JsonResponse:
    query = services.Query(request.GET)
    error = None
    result = {}
    try:
        result = action(getattr(request, "user", None), query, *args)
    except ReferenceError as exc:
        error = exc
    except (SnapshotUnreadable, TimeoutError):
        error = QUERY_FAILED()
    except Exception:
        # A site row policy or unreadable source must fail closed; no unsafe exception
        # message/traceback is returned or captured in an event.
        error = QUERY_FAILED()
    _emit(request, operation, target_type, result, error, detail=detail)
    return json_response(request, result, error=error)


@router.get("/snapshots", auth=None)
def snapshots(request):
    return _run(request, "snapshots.read", "reference_snapshot", services.snapshots)


@router.get("/catalog", auth=None)
def catalog(request):
    return _run(request, "catalog.read", "reference_catalog", services.catalog)


@router.get("/catalog/{kind}/{record_id}", auth=None)
def catalog_detail(request, kind: str, record_id: str):
    return _run(request, "catalog.read", "reference_catalog", services.catalog_detail, kind, record_id)


@router.get("/hs/{hs_code}", auth=None)
def hs_detail(request, hs_code: str):
    return _run(request, "catalog.read", "reference_catalog", services.hs_detail, hs_code)


@router.get("/history", auth=None)
def history(request):
    return _run(request, "history.search", "reference_history", services.history)


@router.get("/history/export", auth=None, include_in_schema=False)
def history_export(request):
    """Explicit deny (and audit) for historical export; no export operation exists."""
    return _run(request, "history.export", "reference_history", services.history_export)


@router.get("/history/{record_id}", auth=None)
def history_detail(request, record_id: str):
    return _run(request, "history.detail", "reference_history", services.history_detail, record_id, detail=True)


@router.get("/evidence", auth=None)
def evidence(request):
    return _run(request, "evidence.read", "reference_evidence", services.evidence, detail=request.GET.get("kind") == "history")
