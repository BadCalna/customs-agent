"""P01 身份端点：登录、退出、当前会话（D3 第 2 节）。

业务读写端点由 P02 起在各自任务包里实现，统一复用本文件的错误语义和
``accounts.permissions`` 的对象级校验。

CSRF 说明：Django Ninja 1.8 的 API 视图在中间件层被豁免 CSRF，因此由
``apps.accounts.auth`` 的公共认证器统一校验带 session 的写请求；登录端点使用
``auth=None``，在本文件显式调用 ``enforce_csrf``。GET 只种下 csrftoken cookie，
不产生业务副作用。
"""
from django.contrib.auth import authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError as RequestValidationError

from .auth import enforce_csrf, session_auth
from .errors import ApiError, AuthenticationRequired, error_payload
from .models import Role

router = Router(tags=["身份"])


class LoginIn(Schema):
    username: str
    password: str


class SessionOut(Schema):
    id: str
    username: str
    display_name: str
    role: str
    role_label: str


class OkOut(Schema):
    status: str


def _session_payload(user) -> dict:
    return {
        "id": str(user.pk),
        "username": user.username,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "role_label": dict(Role.choices).get(user.role, ""),
    }


@router.post("/auth/login", auth=None, response=SessionOut)
def login(request, payload: LoginIn):
    """账号密码登录。写请求同样做 CSRF 校验；失败不区分“用户不存在”和“密码错误”。"""
    enforce_csrf(request)
    user = authenticate(request, username=payload.username, password=payload.password)
    if user is None or not user.is_active:
        raise ApiError(401, "invalid_credentials", "用户名或密码不正确")
    if not user.role:
        raise ApiError(403, "role_missing", "该账号未分配业务角色，不能登录业务接口")
    django_login(request, user)
    return _session_payload(user)


@router.post("/auth/logout", auth=session_auth, response=OkOut)
def logout(request):
    # CSRF 已由公共认证器统一校验（auth.py），视图不再重复。
    django_logout(request)
    return {"status": "ok"}


@router.get("/me", auth=session_auth, response=SessionOut)
def me(request):
    return _session_payload(request.auth)


def install_error_handlers(api):
    """把错误统一成 {error:{code,message,details}, request_id}，返回 JSON 而非页面跳转。"""

    @api.exception_handler(AuthenticationRequired)
    def _unauthenticated(request, exc):
        return JsonResponse(error_payload("unauthorized", "未登录或会话已失效"), status=401)

    @api.exception_handler(AuthenticationError)
    def _ninja_unauthenticated(request, exc):
        return JsonResponse(error_payload("unauthorized", "未登录或会话已失效"), status=401)

    @api.exception_handler(ApiError)
    def _api_error(request, exc):
        return JsonResponse(error_payload(exc.code, exc.message, exc.details), status=exc.status)

    @api.exception_handler(RequestValidationError)
    def _validation_error(request, exc):
        # Ninja 默认返回裸 detail 数组，前端取不到字段级错误；统一成 error 信封。
        return JsonResponse(
            error_payload("validation_error", "请求参数字段校验失败", {"errors": exc.errors}),
            status=422,
        )

    @api.exception_handler(PermissionDenied)
    def _forbidden(request, exc):
        return JsonResponse(error_payload("forbidden", str(exc) or "动作不允许"), status=403)

    @api.exception_handler(Http404)
    def _not_found(request, exc):
        return JsonResponse(error_payload("not_found", str(exc) or "对象不存在或不在可见范围"), status=404)

    @api.exception_handler(HttpError)
    def _http_error(request, exc):
        code = "csrf_failed" if exc.status_code == 403 else "error"
        return JsonResponse(error_payload(code, str(exc)), status=exc.status_code)


    return api
