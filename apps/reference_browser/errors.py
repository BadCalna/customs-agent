"""Fixed, non-enumerating error responses for HTTP adapters and service calls."""
from functools import partial
from uuid import uuid4

from django.http import JsonResponse


class ReferenceError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


UNAUTHORIZED = partial(ReferenceError, 401, "unauthorized", "未登录或会话已失效")
FORBIDDEN = partial(ReferenceError, 403, "forbidden", "无权查看该内容")
NOT_FOUND = partial(ReferenceError, 404, "not_found", "内容不存在或不在可见范围")
INVALID = partial(ReferenceError, 400, "invalid_query", "查询参数不合法")
SNAPSHOT_REQUIRED = partial(ReferenceError, 400, "snapshot_required", "请选择你有权限查看的快照")
QUERY_FAILED = partial(ReferenceError, 503, "query_failed", "查询失败，请凭 request_id 联系数据管理员")


def request_id(request):
    value = getattr(request, "ops_request_id", None)
    if not isinstance(value, str) or len(value) != 32 or not all(c in "0123456789abcdef" for c in value):
        value = request.ops_request_id = uuid4().hex
    return value


def json_response(request, payload, *, error=None):
    if error is not None:
        payload = {"error": {"code": error.code, "message": error.message, "details": {}},
                   "request_id": request_id(request)}
    response = JsonResponse(payload, status=error.status if error else 200)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Request-ID"] = request_id(request)
    return response
