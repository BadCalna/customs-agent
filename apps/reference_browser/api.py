"""Mount with `api_v1.add_router('/ops/data', router)`; no write routes."""
from django.http import JsonResponse
from ninja import Router

from . import services
from .audit import audited_call
from .errors import json_response

router = Router(tags=["基础数据中心"])


def _run(request, operation, target_type, action, *args, detail=False) -> JsonResponse:
    result, error = audited_call(
        request, operation, target_type, action, getattr(request, "user", None),
        services.Query(request.GET), *args, detail=detail,
    )
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
