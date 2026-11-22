"""Standalone URLConf; production can mount the router on its existing Ninja API."""
from ninja import NinjaAPI

from .api import router

api = NinjaAPI(title="基础数据中心 API", urls_namespace="reference_browser_api",
              docs_url=None, openapi_url=None)
api.add_router("/", router)
urlpatterns = api.urls[0]
