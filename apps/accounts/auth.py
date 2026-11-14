"""公共 session 认证：统一校验写请求的 CSRF，并为响应种下 csrftoken cookie。

设计（D3 第 6.2 节，P01 修复轮）：
- 认证层是唯一入口：任何声明 ``auth=session_auth`` 的写端点自动获得 CSRF 校验，
  后续 Agent 不需要、也不应该在每个视图里自行调用 check_csrf。
- 先判身份再判 CSRF：匿名写请求返回 401（未登录），已登录但缺令牌才返回
  403 ``csrf_failed``；避免把“未登录”误报成“CSRF 失败”。
- 登录端点使用 ``auth=None``，由视图显式调用 ``enforce_csrf``；
  未登录 GET 仍会种下初始 csrftoken cookie，供后续登录使用。
"""
from django.middleware.csrf import get_token
from ninja.utils import check_csrf

from .errors import ApiError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def enforce_csrf(request):
    """写请求必须携带有效 CSRF 令牌；失败抛 403 ``csrf_failed``（统一错误体）。"""
    if check_csrf(request):
        raise ApiError(403, "csrf_failed", "CSRF 校验失败，请重新获取令牌后重试")


class SessionWithCsrfCookie:
    def __call__(self, request):
        get_token(request)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if request.method not in SAFE_METHODS:
            enforce_csrf(request)
        return user


session_auth = SessionWithCsrfCookie()
