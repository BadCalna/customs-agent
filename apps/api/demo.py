"""P02 组件演示壳的只读静态入口。

Django 开发服务器只会自动服务 app 内部的 ``static/`` 目录，而演示壳的 ``web/``
放在仓库根，因此这里用三个受控视图把 ``web/`` 与 ``contracts/fixtures/`` 暴露给
浏览器：

- ``/demo/``                     → ``web/index.html``
- ``/demo/assets/<path>``        → ``web/<path>``
- ``/demo/fixtures/<path>``      → ``contracts/fixtures/<path>``

三者都只读、只接受白名单后缀，并且把请求路径 resolve 之后校验仍位于允许的根目录
内，避免 ``../`` 穿越读到仓库其他文件。演示壳不写数据库、不校验业务权限，只用于
查看组件形状；接口是否可用以 live 端点的响应为准。

正式 /workbench/ 与 /uploads/ 以及 /workbench/assets/ 白名单不依赖 DEBUG；
页面不包含账号或业务资料，所有数据和写动作仍由 session API 校验。
原型/fixture 入口继续只在开发模式提供。
"""
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

WEB_ROOT = Path(settings.BASE_DIR) / "web"
FIXTURE_ROOT = Path(settings.BASE_DIR) / "contracts" / "fixtures"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _served_file(root: Path, relative: str, *, formal: bool = False) -> FileResponse:
    """把仓内相对路径变成响应，越界或后缀不合法一律 404。"""
    if not settings.DEBUG and not formal:
        # 演示壳含演示账号与本地样例，只在开发模式可用；生产由正式页面替代。
        raise Http404("演示壳只在开发模式可用")
    relative = relative or ""
    suffix = Path(relative).suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise Http404("不支持的演示资源类型")

    base = root.resolve()
    candidate = (base / relative).resolve()
    # resolve 之后再比较，符号链接与 .. 都会落在同一个真实路径上。
    if not candidate.is_relative_to(base) or not candidate.is_file():
        raise Http404("演示资源不存在")

    response = FileResponse(candidate.open("rb"), content_type=CONTENT_TYPES[suffix])
    # 演示壳还在迭代，禁止浏览器缓存，改完文件刷新即生效。
    response["Cache-Control"] = "no-store"
    return response


def demo_index(request):
    """演示壳首页。"""
    return _served_file(WEB_ROOT, "index.html")


def workbench_index(request):
    """正式入口只用真实会话/API；显式样例模式保留在 DEBUG 环境。"""
    if request.GET.get("mode") in ("demo", "fixture"):
        return _served_file(WEB_ROOT, "workbench.html")
    return _served_file(WEB_ROOT, "workbench-live.html", formal=True)


# Public static resources contain no data or demo accounts. APIs still enforce session/object scope.
WORKBENCH_ASSETS = frozenset((
    "components/message-list.js", "styles/messages.css",
    "api.js", "state.js", "workbench-context.js", "workbench-live.js", "styles.css",
    "components/task-list.js", "components/run-progress.js", "components/export-actions.js",
    "components/commodity-list.js", "components/commodity-card.js", "components/hs-comparison.js",
    "components/review-actions.js", "styles/commodity.css", "styles/review-actions.css",
    "pages/p06.js", "styles/p06.css",
    "styles/tasks.css", "styles/workbench-live.css",
))


def workbench_asset(request, path: str):
    if path not in WORKBENCH_ASSETS:
        raise Http404("工作台资源不存在")
    return _served_file(WEB_ROOT, path, formal=True)


def demo_asset(request, path: str):
    """演示壳的 js / css / svg。"""
    return _served_file(WEB_ROOT, path)


def demo_fixture(request, path: str):
    """fixture 模式读取的契约样例（DEMO-SET.json 等）。"""
    return _served_file(FIXTURE_ROOT, path)


def upload_index(request):
    """P06 真实上传与认领入口，仅 live API（正式入口，不依赖 DEBUG）。"""
    return _served_file(WEB_ROOT, "pages/p06.html", formal=True)


def tasks_index(request):
    """P08 独立只读任务列表与运行进度开发验收入口（DEBUG=True 时可用）。"""
    return _served_file(WEB_ROOT, "pages/p08.html")


def _entry_target(request, path):
    """Only carry review coordinates between the two fixed same-site pages."""
    query = urlencode([(key, request.GET[key]) for key in ("task", "version") if request.GET.get(key)])
    return f"{path}?{query}" if query else path


def _has_business_session(request):
    # A session opened in /admin/ without a business role is not a desk login.
    from apps.accounts.models import Role

    return request.user.is_authenticated and request.user.role in Role.values


@never_cache
def production_index(request):
    if not _has_business_session(request):
        return HttpResponseRedirect(_entry_target(request, "/login/"))
    return _production_file("production.html")


@never_cache
@ensure_csrf_cookie
def production_login(request):
    if _has_business_session(request):
        return HttpResponseRedirect(_entry_target(request, "/"))
    return _production_file("login.html")


PRODUCTION_ASSETS = frozenset({
    "production.js", "production.css", "appearance.js", "login.js", "api.js",
    "components/message-list.js", "styles/messages.css", "review-insights.js", "insights.css",
})


def production_asset(request, path: str):
    if path not in PRODUCTION_ASSETS:
        raise Http404("资源不存在")
    return _production_file(path)


def _production_file(relative: str):
    candidate = (WEB_ROOT / relative).resolve()
    if not candidate.is_relative_to(WEB_ROOT.resolve()) or not candidate.is_file():
        raise Http404("资源不存在")
    response = FileResponse(candidate.open("rb"), content_type=CONTENT_TYPES[candidate.suffix])
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
