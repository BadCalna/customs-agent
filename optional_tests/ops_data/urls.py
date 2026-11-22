"""Independent URLConf for the reference-browser contract/security suite.

The production URLConf is deliberately not imported: 03 can be verified before
01 adds the app and its routes to the shared shell.
"""
from django.urls import include, path
from ninja import NinjaAPI

from apps.reference_browser.api import router

api_v1 = NinjaAPI(title="03 基础数据中心测试 API", urls_namespace="ops-data-test")
api_v1.add_router("/ops/data", router)

urlpatterns = [
    path("ops/data/", include("apps.reference_browser.urls")),
    path("api/v1/", api_v1.urls),
]
