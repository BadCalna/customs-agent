"""Standalone pages. Script APIs still authorize every action independently."""
from functools import wraps

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie

from .access import require
from .errors import DebugError, MESSAGES
from .views_data import visible_run


def page(permission):
    def decorate(view):
        @wraps(view)
        @ensure_csrf_cookie
        def dispatch(request, *args, **kwargs):
            try:
                require(request.user, permission)
                response = view(request, *args, **kwargs)
            except DebugError as exc:
                # Same response for unknown and out-of-scope object IDs.
                response = HttpResponse(MESSAGES[exc.code], status=exc.status, content_type="text/plain; charset=utf-8")
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        return dispatch
    return decorate


@page("use_debug")
def index(request):
    return render(request, "agent_debug/index.html")


@page("manage_connections")
def connections(request):
    return render(request, "agent_debug/connections.html")


@page("use_debug")
def runs(request):
    return render(request, "agent_debug/runs.html")


@page("use_debug")
def run_detail(request, run_id):
    visible_run(request.user, run_id)
    return render(request, "agent_debug/run_detail.html", {"run_id": str(run_id)})
