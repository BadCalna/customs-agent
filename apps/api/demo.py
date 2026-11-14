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
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404

WEB_ROOT = Path(settings.BASE_DIR) / "web"
FIXTURE_ROOT = Path(settings.BASE_DIR) / "contracts" / "fixtures"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _served_file(root: Path, relative: str) -> FileResponse:
    """把仓内相对路径变成响应，越界或后缀不合法一律 404。"""
    if not settings.DEBUG:
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


def demo_asset(request, path: str):
    """演示壳的 js / css / svg。"""
    return _served_file(WEB_ROOT, path)


def demo_fixture(request, path: str):
    """fixture 模式读取的契约样例（DEMO-SET.json 等）。"""
    return _served_file(FIXTURE_ROOT, path)
