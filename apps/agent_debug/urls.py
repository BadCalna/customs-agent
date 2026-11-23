"""页面入口。由 01 的模块清单挂到 ``/ops/debug/``，因此这里只用相对路径。"""
from django.urls import path

from . import pages

app_name = "agent_debug"

urlpatterns = [
    path("", pages.index, name="index"),
    path("connections/", pages.connections, name="connections"),
    path("runs/", pages.runs, name="runs"),
    path("runs/<uuid:run_id>/", pages.run_detail, name="run-detail"),
]