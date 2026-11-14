from django.contrib import admin
from django.urls import path
from ninja import NinjaAPI, Schema

api = NinjaAPI(title="关务审核 API", version="0.1.0")


class Health(Schema):
    status: str


@api.get("/health", response=Health, auth=None)
def health(request):
    """检查 Web 服务是否已启动。"""
    return {"status": "ok"}


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
