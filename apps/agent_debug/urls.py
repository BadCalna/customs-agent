from django.urls import path

from . import pages

app_name = "agent_debug"

urlpatterns = [
    path("ops/debug/", pages.index, name="index"),
    path("ops/debug/connections/", pages.connections, name="connections"),
    path("ops/debug/runs/", pages.runs, name="runs"),
    path("ops/debug/runs/<uuid:run_id>/", pages.run_detail, name="run-detail"),
]
