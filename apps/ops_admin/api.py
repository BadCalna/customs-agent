"""Same-origin, session-authenticated operations API."""
from typing import Literal
from uuid import UUID

from django.apps import apps
from django.contrib.auth import authenticate, login as django_login
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import Http404
from django.middleware.csrf import get_token
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import AuthenticationError, HttpError, ValidationError
from pydantic import ConfigDict, Field, SecretStr

from apps.accounts.auth import enforce_csrf, session_auth
from apps.accounts.errors import ApiError, AuthenticationRequired
from apps.accounts.models import User

from . import queries
from .catalog import whitelist
from .events import audit
from .exports import export_logs
from .lifecycle import services
from .middleware import SYSTEM_API
from .models import ErrorLog, ModuleRegistration
from .mutations import audit_rejection, mutate
from .security import explicit_permissions, fail, request_id, require

router = Router(tags=['系统管理'], auth=session_auth)


class Input(Schema):
    model_config = ConfigDict(extra='forbid')


Role = Literal['', 'supplier', 'operator', 'expert']
Delivery = Literal['offline', 'trusted_email']


class Login(Input):
    username: str = Field(min_length=1, max_length=150)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CreateUser(Input):
    username: str = Field(min_length=1, max_length=150)
    display_name: str = Field('', max_length=64)
    role: Role = ''
    groups: list[int] = Field(default_factory=list, max_length=50)
    email: str = Field('', max_length=254)
    issue_secret: bool = False
    delivery_mode: Delivery | None = None
    confirm: bool = False


class UpdateUser(Input):
    display_name: str | None = Field(None, max_length=64)
    role: Role | None = None
    groups: list[int] | None = Field(None, max_length=50)
    confirm: bool = False


class Reason(Input):
    reason: str = Field(min_length=1, max_length=300)


class DeleteUser(Reason):
    confirm: bool


class Reset(Input):
    delivery_mode: Delivery | None = None


class Consume(Input):
    ticket: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=1, max_length=1024)


class UpdateGroup(Input):
    permissions: list[str] = Field(max_length=100)
    confirm: bool = False


class ModuleStatus(Input):
    status: Literal['active', 'disabled']


class Export(Input):
    kind: Literal['audit', 'errors']
    filters: dict[str, str | int | bool] = Field(default_factory=dict, max_length=12)


def wrapper(request, permission, action, data, work, *, target=None, secret=False, rate=None):
    authorize = lambda: require(request.auth, permission)
    authorize()
    return mutate(request, action=action, body=data, work=work, authorize=authorize, target=target,
                  secret=secret, preflight=rate)


def input_query(request, allowed):
    unknown = set(request.GET.keys()) - allowed - {'page_size', 'cursor'}
    if unknown or any(len(values) != 1 for _, values in request.GET.lists()):
        fail(422, 'invalid_filter', '筛选字段无效或重复')
    return {name: request.GET[name] for name in allowed if name in request.GET and request.GET[name]}


def pagination(request):
    size, cursor = request.GET.get('page_size', '25'), request.GET.get('cursor', '')
    if not size.isascii() or not size.isdigit() or len(size) > 3 or len(cursor) > 512:
        fail(422, 'invalid_pagination', '分页参数无效')
    return int(size), cursor


@router.get('/dashboard')
def dashboard(request):
    require(request.auth, 'view_ops_dashboard')
    return queries.dashboard(request)


@router.get('/nav')
def nav(request):
    items = queries.nav_items(request.auth)
    return {'items': items, 'modules': items, 'next_cursor': None, 'request_id': request_id(request)}


@router.get('/modules')
def modules(request):
    require(request.auth, 'view_ops_dashboard')
    return {'items': queries.module_items(), 'next_cursor': None, 'request_id': request_id(request)}


@router.post('/modules/{key}', operation_id='opsRegisterModule')
@router.patch('/modules/{key}', operation_id='opsUpdateModule')
def change_module(request, key: str, payload: ModuleStatus):
    def work():
        spec = apps.get_app_config('ops_admin').manifests.get(key)
        if not spec:
            fail(404, 'not_found', '没有已安装的模块清单')
        row, created = ModuleRegistration.objects.get_or_create(module_key=key, defaults=spec.model_fields())
        previous = 'unregistered' if created else row.status
        row.status = payload.status
        row.save(update_fields=('status', 'updated_at'))
        audit(request, 'ops.module.registered' if created else 'ops.module.changed', target_type='module', target_id=key,
              payload={'previous_status': previous, 'new_status': row.status})
        return next(item for item in queries.module_items() if item['module_key'] == key)

    return wrapper(request, 'manage_module_registry', 'module.change', payload.model_dump(), work, target=key)


@router.get('/users')
def users(request):
    require(request.auth, 'view_user')
    filters = input_query(request, {'username', 'id', 'status', 'role', 'group'})
    data = User.objects.select_related('ops_lifecycle').prefetch_related(Prefetch('groups', queryset=Group.objects.order_by('name')))
    if any(len(v) > 150 for v in filters.values()):
        fail(422, 'invalid_filter', '筛选值过长')
    if 'username' in filters:
        data = data.filter(username__icontains=filters['username'])
    if 'id' in filters:
        try:
            data = data.filter(pk=UUID(filters['id']))
        except ValueError:
            fail(422, 'invalid_filter', '账号 UUID 格式无效')
    if 'role' in filters:
        role = filters['role']
        if role not in ('supplier', 'operator', 'expert', 'none'):
            fail(422, 'invalid_filter', '业务角色无效')
        data = data.filter(role='' if role == 'none' else role)
    if 'group' in filters:
        group = filters['group']
        data = data.filter(groups__id=int(group)) if group.isascii() and group.isdigit() else data.filter(groups__name=group)
    if 'status' in filters:
        status = filters['status']
        if status == 'active':
            data = data.filter(is_active=True)
        elif status == 'pending_secret':
            data = data.filter(is_active=False, ops_lifecycle__status='pending_secret', password__startswith='!')
        elif status == 'disabled':
            data = data.filter(is_active=False).exclude(ops_lifecycle__status='pending_secret', password__startswith='!')
        else:
            fail(422, 'invalid_filter', '账号状态无效')
    rows, cursor = queries.keyset(data, 'id', *pagination(request), 'users', filters, timestamp='date_joined')
    counts = services.session_counts(rows)
    return {'items': [services.user_summary(user, session_count=counts.get(str(user.pk), 0)) for user in rows],
            'next_cursor': cursor, 'request_id': request_id(request)}


@router.get('/users/{user_id}')
def user_detail(request, user_id: UUID):
    require(request.auth, 'view_user', conceal=True)
    user = User.objects.select_related('ops_lifecycle').filter(pk=user_id).first()
    if not user:
        fail(404, 'not_found', '对象不存在或不在可见范围')
    return {**services.user_summary(user, detail=True), 'request_id': request_id(request)}


@router.post('/users')
def add_user(request, payload: CreateUser):
    data = payload.model_dump()
    return wrapper(request, 'add_user', 'user.create', data, lambda: (201, services.create_user(request, data)),
                   secret=payload.issue_secret,
                   rate=(lambda: services.rate_limit(request, 'issue', subject=User.normalize_username(payload.username.strip()).casefold())) if payload.issue_secret else None)


@router.patch('/users/{user_id}')
def edit_user(request, user_id: UUID, payload: UpdateUser):
    data = payload.model_dump(exclude_unset=True)
    if any(data.get(key) is None for key in set(data) - {'confirm'}):
        fail(422, 'invalid_fields', '禁止将账号白名单字段设为 null')
    return wrapper(request, 'change_user', 'user.change', data, lambda: services.update_user(request, user_id, data), target=user_id)


@router.delete('/users/{user_id}')
def delete_user(request, user_id: UUID, payload: DeleteUser):
    data = payload.model_dump()
    return wrapper(request, 'delete_user', 'user.delete', data,
                   lambda: services.delete_user(request, user_id, **data), target=user_id)


@router.post('/users/{user_id}/disable', operation_id='opsDisableUser')
def disable_user(request, user_id: UUID, payload: Reason):
    return wrapper(request, 'disable_user', 'user.disable', payload.model_dump(),
                   lambda: services.set_user_active(request, user_id, active=False, reason=payload.reason), target=user_id)


@router.post('/users/{user_id}/enable', operation_id='opsEnableUser')
def enable_user(request, user_id: UUID, payload: Reason):
    return wrapper(request, 'disable_user', 'user.enable', payload.model_dump(),
                   lambda: services.set_user_active(request, user_id, active=True, reason=payload.reason), target=user_id)


@router.post('/users/{user_id}/secret/reset')
def reset(request, user_id: UUID, payload: Reset):
    return wrapper(request, 'reset_user_password', 'secret.issue', payload.model_dump(),
                   lambda: services.issue_ticket(request, user_id, delivery_mode=payload.delivery_mode),
                   target=user_id, secret=True, rate=lambda: services.rate_limit(request, 'issue', subject=user_id))


@router.get('/groups')
def groups(request):
    require(request.auth, 'view_group')
    filters = input_query(request, {'name'})
    size, cursor = pagination(request)
    if not 1 <= size <= 100:
        fail(422, 'invalid_page_size', '每页数量必须在 1–100 之间')
    from django.core import signing

    data = Group.objects.order_by('pk')
    name = filters.get('name', '')
    if len(name) > 150:
        fail(422, 'invalid_filter', '筛选值过长')
    if name:
        data = data.filter(name__icontains=name)
    if cursor:
        try:
            pk, filter_name = signing.loads(cursor, salt='ops.groups', max_age=86400)
            if filter_name != name or not isinstance(pk, int):
                raise ValueError('wrong cursor')
        except (signing.BadSignature, ValueError, TypeError):
            fail(422, 'invalid_cursor', '分页游标无效')
        data = data.filter(pk__gt=pk)
    rows = list(data[:size + 1])
    return {'items': [services.group_summary(group) for group in rows[:size]],
            'next_cursor': signing.dumps([rows[size - 1].pk, name], salt='ops.groups') if len(rows) > size else None,
            'request_id': request_id(request)}


@router.patch('/groups/{group_id}')
def edit_group(request, group_id: int, payload: UpdateGroup):
    data = payload.model_dump()
    return wrapper(request, 'change_group', 'group.change', data,
                   lambda: services.update_group(request, group_id, **data), target=group_id)


@router.get('/permissions/whitelist')
def permission_whitelist(request):
    require(request.auth, 'view_group')
    return {'items': whitelist(), 'next_cursor': None, 'request_id': request_id(request)}


@router.get('/audit')
def audit_list(request):
    require(request.auth, 'view_audit_event')
    qs, filters = queries.log_query('audit', input_query(request, queries.AUDIT_FILTERS))
    rows, cursor = queries.keyset(qs, 'event_id', *pagination(request), 'audit', filters)
    return {'items': [queries.audit_item(row) for row in rows], 'next_cursor': cursor, 'request_id': request_id(request)}


@router.get('/audit/{uuid:event_id}')
def audit_detail(request, event_id: UUID):
    require(request.auth, 'view_audit_event', conceal=True)
    row = queries.AuditRecord.objects.filter(event_id=event_id).first()
    if row is None:
        fail(404, 'not_found', '对象不存在或不在可见范围')
    return {**queries.audit_item(row, detail='agent_debug.view_trace_payload' in explicit_permissions(request.auth)),
            'request_id': request_id(request), 'event_request_id': row.request_id}


@router.get('/errors')
def errors(request):
    require(request.auth, 'view_error_log')
    qs, filters = queries.log_query('errors', input_query(request, queries.ERROR_FILTERS))
    rows, cursor = queries.keyset(qs, 'id', *pagination(request), 'errors', filters)
    return {'items': [queries.error_item(row) for row in rows], 'next_cursor': cursor, 'request_id': request_id(request)}


@router.post('/errors/{error_id}/resolve')
def resolve(request, error_id: UUID, payload: Reason):
    def work():
        require(request.auth, 'view_error_log')
        row = ErrorLog.objects.select_for_update().filter(pk=error_id).first()
        if not row:
            fail(404, 'not_found', '对象不存在或不在可见范围')
        if not payload.reason.strip():
            fail(422, 'reason_required', '请填写处理原因')
        if not row.resolved_at:
            row.resolved_at = timezone.now()
            row.save(update_fields=('resolved_at',))
            audit(request, 'ops.error.resolved', target_type='error_log', target_id=error_id)
        return {'status': 'resolved', 'item': queries.error_item(row)}

    return wrapper(request, 'resolve_error_log', 'error.resolve', payload.model_dump(), work, target=error_id)


@router.post('/audit/export')
def export(request, payload: Export):
    return export_logs(request, payload.model_dump(), lambda: services.rate_limit(request, 'export', subject=request.auth.pk))


@router.get('/session', auth=None)
def session(request):
    get_token(request)
    return {'authenticated': request.user.is_authenticated, 'request_id': request_id(request)}


@router.post('/login', auth=None)
def login(request, payload: Login):
    enforce_csrf(request)
    if request.user.is_authenticated:
        fail(409, 'already_authenticated', '请退出当前会话后再登录')
    username = User.normalize_username(payload.username.strip())
    subject = username.casefold()
    password = payload.password.get_secret_value()

    def work():
        user = authenticate(request, username=username, password=password)
        if not user or not any(p.startswith('ops_admin.') for p in explicit_permissions(user)):
            audit(request, 'ops.login.failed', target_type='login', target_id=subject,
                  outcome='denied', summary_code='invalid_credentials')
            return 401, {'error': {'code': 'invalid_credentials', 'message': '登录失败，请检查账号凭据或联系运维', 'details': {}}}
        django_login(request, user)
        audit(request, 'ops.login.succeeded', target_type='user', target_id=user.pk)
        return {'status': 'authenticated'}

    return mutate(request, action='login.attempt', body={'username': username, 'password': password}, work=work,
                  target=subject, secret=True, preflight=lambda: services.rate_limit(request, 'login', subject=subject))


@router.post('/secret/consume', auth=None)
def consume(request, payload: Consume):
    enforce_csrf(request)
    if request.user.is_authenticated:
        fail(403, 'anonymous_required', '请退出当前会话后再使用一次性凭据')
    ticket, password = payload.ticket.get_secret_value(), payload.new_password.get_secret_value()
    return mutate(request, action='secret.consume', body={'ticket': ticket, 'new_password': password}, secret=True,
                  work=lambda: services.consume_ticket(request, ticket=ticket, new_password=password),
                  preflight=lambda: services.rate_limit(request, 'consume', subject=ticket))


def install_ops_error_handlers(api):
    """Keep existing API handlers unchanged outside the system namespace."""
    errors = {
        ApiError: lambda exc: exc,
        AuthenticationRequired: lambda exc: ApiError(401, 'unauthorized', '未登录或会话已失效'),
        AuthenticationError: lambda exc: ApiError(401, 'unauthorized', '未登录或会话已失效'),
        ValidationError: lambda exc: ApiError(422, 'validation_error', '请求参数字段校验失败'),
        PermissionDenied: lambda exc: ApiError(403, 'forbidden', '动作不允许'),
        Http404: lambda exc: ApiError(404, 'not_found', '对象不存在或不在可见范围'),
        HttpError: lambda exc: ApiError(exc.status_code, 'request_error', '请求未完成'),
    }

    def wrap(previous, normalize):
        def handler(request, exc):
            if not request.path_info.startswith(SYSTEM_API):
                return previous(request, exc)
            # Never copy Pydantic input, validation context or exception text into the response/audit.
            action = 'request.' + ('read' if request.method in ('GET', 'HEAD') else 'write')
            return audit_rejection(request, action, normalize(exc))

        return handler

    for error_type, normalize in errors.items():
        api.add_exception_handler(error_type, wrap(api._exception_handlers[error_type], normalize))
