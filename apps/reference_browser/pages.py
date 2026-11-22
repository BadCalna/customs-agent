"""Permission-aware HTML shells for the snapshot-pinned reference API."""
from django.http import HttpResponseNotAllowed
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers, patch_vary_headers

from .permissions import VIEW_CATALOG, VIEW_HISTORY, VIEW_HISTORY_DETAIL, actor_can, authenticated


def _private(response):
    add_never_cache_headers(response)
    patch_vary_headers(response, ("Cookie",))
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _page(request, template):
    if request.method not in {"GET", "HEAD"}:
        return _private(HttpResponseNotAllowed(("GET", "HEAD")))
    if not authenticated(request.user):
        return _private(render(request, "reference_browser/access.html", {"state": "unauthenticated"}, status=401))

    can_catalog = actor_can(request.user, VIEW_CATALOG)
    can_history = actor_can(request.user, VIEW_HISTORY)
    can_history_detail = can_history and actor_can(request.user, VIEW_HISTORY_DETAIL)
    if not (can_catalog or can_history):
        return _private(render(request, "reference_browser/access.html", {"state": "forbidden"}, status=403))

    return _private(render(request, template, {
        "can_catalog": can_catalog,
        "can_history": can_history,
        "can_history_detail": can_history_detail,
        "operator": request.user.get_username(),
    }))


def browser(request):
    return _page(request, "reference_browser/index.html")


def evidence(request):
    # The evidence API re-authorizes the requested kind, snapshot and record.
    return _page(request, "reference_browser/evidence.html")
