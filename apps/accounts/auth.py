"""session 身份认证：同时对响应种下 csrftoken cookie，供后续写请求携带。

GET 查询本身无副作用（D3 第 1 节）；种 cookie 是 Django 的标准 CSRF 准备动作，
不创建业务对象，也不推进任何执行状态。
"""
from django.middleware.csrf import get_token


class SessionWithCsrfCookie:
    def __call__(self, request):
        get_token(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return user
        return None


session_auth = SessionWithCsrfCookie()
