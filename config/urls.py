from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI, Schema

from apps.accounts.api import install_error_handlers
from apps.accounts.api import router as accounts_router
from apps.api.demo import demo_asset, demo_fixture, demo_index
from apps.api.not_implemented import router as pending_router
from apps.reviews.api.read import router as reviews_read_router
from apps.runs.api import router as runs_router

api = NinjaAPI(title="关务审核 API", version="0.1.0")


class Health(Schema):
    status: str


@api.get("/health", response=Health, auth=None)
def health(request):
    """检查 Web 服务是否已启动。"""
    return {"status": "ok"}


api_v1 = NinjaAPI(title="关务审核 API v1", version="1.0.0", urls_namespace="api-v1")
install_error_handlers(api_v1)
api_v1.add_router("", accounts_router)
api_v1.add_router("", reviews_read_router)
api_v1.add_router("", runs_router)
api_v1.add_router("", pending_router)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api_v1.urls),
    path("api/", api.urls),
    # P02 组件演示壳：只读静态资源，不参与 API 契约。
    path("demo/", demo_index, name="demo-index"),
    path("demo/assets/<path:path>", demo_asset, name="demo-asset"),
    path("demo/fixtures/<path:path>", demo_fixture, name="demo-fixture"),
]
