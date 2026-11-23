"""Explicit grants, including for Django superusers."""
import uuid

from django.contrib.auth.models import Permission
from django.db.models import Q
from django.utils.crypto import salted_hmac

from apps.accounts.errors import ApiError


def explicit_permissions(user):
    if not user or not user.is_authenticated or not user.is_active:
        return set()
    rows = Permission.objects.filter(Q(user=user) | Q(group__user=user)).exclude(
        Q(content_type__app_label='ops_admin') & ~Q(content_type__model='permissioncatalog')
    ).values_list('content_type__app_label', 'codename').distinct()
    return {f'{app}.{code}' for app, code in rows}


def require(user, codename, *, conceal=False):
    key = codename if '.' in codename else f'ops_admin.{codename}'
    if key not in explicit_permissions(user):
        if conceal:
            fail(404, 'not_found', '对象不存在或不在可见范围')
        fail(403, 'forbidden', '当前账号不具备此操作权限')


def fail(status, code, message, details=None):
    raise ApiError(status, code, message, details)


def request_id(request):
    if not hasattr(request, 'ops_request_id'):
        request.ops_request_id = uuid.uuid4().hex
    return request.ops_request_id


def digest(namespace, value):
    return salted_hmac(f'customs.ops.{namespace}', str(value), algorithm='sha256').hexdigest()


def opaque(value):
    return f'ref_{digest("reference", value)[:32]}' if value not in (None, '') else None
