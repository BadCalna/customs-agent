"""CSRF 失败时返回 JSON，而不是 Django 默认的 HTML 页面。

Ninja 写操作显式 ``csrf=True`` 后仍走 Django CSRF 中间件；页面与会话同源，
错误体与其它接口保持同一形状（D3 第 1 节）。
"""
import uuid

from django.http import JsonResponse


def csrf_failure(request, reason=""):
    return JsonResponse(
        {
            "error": {
                "code": "csrf_failed",
                "message": "CSRF 校验失败，请重新获取令牌后重试",
                "details": {"reason": reason},
            },
            "request_id": uuid.uuid4().hex,
        },
        status=403,
    )
