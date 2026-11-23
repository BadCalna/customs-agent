"""URLConf used only by the isolated acceptance tests."""
from django.urls import include, path
from ninja import NinjaAPI

from apps.accounts.api import install_error_handlers
from apps.accounts.api import router as accounts_router
from apps.agent_debug.api import router as debug_router

api = NinjaAPI(title="Agent Debug Test API", urls_namespace="ops-debug-tests")
install_error_handlers(api)
api.add_router("/ops/debug/", debug_router)
api.add_router("", accounts_router)

urlpatterns = [
    path("api/v1/", api.urls),
    # 模块内的 URLConf 是相对路径，隔离环境在这里补回 /ops/debug/ 前缀。
    path("ops/debug/", include("apps.agent_debug.urls")),
]
