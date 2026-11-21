from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI, Schema

from apps.accounts.api import install_error_handlers
from apps.accounts.api import router as accounts_router
from apps.api.demo import (
    demo_asset,
    demo_fixture,
    demo_index,
    production_asset,
    production_index,
    production_login,
    tasks_index,
    upload_index,
    workbench_asset,
    workbench_index,
)
from apps.api.not_implemented import router as pending_router
from apps.reviews.api.confirmations import router as confirmations_router
from apps.reviews.api.decisions import router as decisions_router
from apps.reviews.api.exports import router as exports_router
from apps.reviews.api.history import router as history_router
from apps.reviews.api.history_reference import router as history_reference_router
from apps.reviews.api.imports import router as imports_router
from apps.reviews.api.read import router as reviews_read_router
from apps.reviews.api.submissions import router as submissions_router
from apps.runs.api import router as runs_router
from apps.notifications.api import router as notifications_router

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
api_v1.add_router("", imports_router)
api_v1.add_router("", reviews_read_router)
api_v1.add_router("", confirmations_router)
api_v1.add_router("", submissions_router)
api_v1.add_router("", decisions_router)
api_v1.add_router("", history_router)
api_v1.add_router("", history_reference_router)
api_v1.add_router("", runs_router)
api_v1.add_router("", exports_router)
api_v1.add_router("", notifications_router)
api_v1.add_router("", pending_router)

urlpatterns = [
    path("", production_index, name="production-index"),
    path("login/", production_login, name="production-login"),
    path("app/assets/<path:path>", production_asset, name="production-asset"),
    path("admin/", admin.site.urls),
    path("api/v1/", api_v1.urls),
    path("api/", api.urls),
    # P02 组件演示壳：只读静态资源，不参与 API 契约。
    path("uploads/", upload_index, name="p06-upload"),
    path("demo/", demo_index, name="demo-index"),
    path("demo/assets/<path:path>", demo_asset, name="demo-asset"),
    path("demo/fixtures/<path:path>", demo_fixture, name="demo-fixture"),
    # 正式工作台默认真实会话/API；样例仍只在 DEBUG 下显式访问。
    path("workbench/assets/<path:path>", workbench_asset, name="workbench-asset"),
    path("workbench/", workbench_index, name="workbench-index"),
    # P08 独立只读任务列表与运行进度页面入口（仅在 DEBUG=True 开发模式可用；正式工作台为 /workbench/）。
    path("tasks/", tasks_index, name="p08-tasks"),
]
