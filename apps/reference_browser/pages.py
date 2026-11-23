"""Permission-aware HTML shells for the snapshot-pinned reference API."""
from django.http import HttpResponseNotAllowed
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers, patch_vary_headers

from . import services
from .audit import audited_call
from .permissions import VIEW_CATALOG, VIEW_HISTORY, VIEW_HISTORY_DETAIL, actor_can, authenticated


def _private(response):
    add_never_cache_headers(response)
    patch_vary_headers(response, ("Cookie",))
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _access(request, state, status):
    return _private(render(request, "reference_browser/access.html", {"state": state}, status=status))


def _context(user, can_catalog, can_history):
    return {"can_catalog": can_catalog, "can_history": can_history,
            "can_history_detail": can_history and actor_can(user, VIEW_HISTORY_DETAIL),
            "operator": user.get_username()}


def browser(request):
    if request.method not in {"GET", "HEAD"}:
        return _private(HttpResponseNotAllowed(("GET", "HEAD")))
    if not authenticated(request.user):
        return _access(request, "unauthenticated", 401)
    can_catalog = actor_can(request.user, VIEW_CATALOG)
    can_history = actor_can(request.user, VIEW_HISTORY)
    if not (can_catalog or can_history):
        return _access(request, "forbidden", 403)
    context = _context(request.user, can_catalog, can_history)
    return _private(render(request, "reference_browser/index.html", context))


def evidence(request):
    if request.method not in {"GET", "HEAD"}:
        return _private(HttpResponseNotAllowed(("GET", "HEAD")))
    result, error = audited_call(
        request, "evidence.read", "reference_evidence", services.evidence,
        request.user, services.Query(request.GET), detail=request.GET.get("kind") == "history",
    )
    if error:
        state = "unauthenticated" if error.status == 401 else error.code
        return _access(request, state, error.status)
    context = _context(request.user, actor_can(request.user, VIEW_CATALOG), actor_can(request.user, VIEW_HISTORY))
    context["evidence_payload"] = result
    return _private(render(request, "reference_browser/evidence.html", context))
