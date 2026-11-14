"""统一错误语义：401 未登录、403 动作不允许、404 对象不存在或不在可见范围（D3 第 1 节）。"""
import uuid


class AuthenticationRequired(Exception):
    """未登录或会话失效；API 层统一映射为 401 JSON，不返回页面重定向。"""


class ApiError(Exception):
    """业务端点可直接抛出的显式错误。"""

    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def error_payload(code: str, message: str, details=None) -> dict:
    return {
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": uuid.uuid4().hex,
    }
