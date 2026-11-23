"""Request correlation, safe exception telemetry and installed-module kill switch."""
import logging
import traceback
from hashlib import sha256

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone

from .events import audit, current_request
from .models import ErrorLog, ModuleRegistration
from .security import request_id

logger = logging.getLogger('customs.ops.collector')
SYSTEM_API = '/api/v1/ops/system/'
METHODS = {'GET', 'HEAD', 'OPTIONS', 'POST', 'PATCH', 'PUT', 'DELETE'}


def error_response(request, status, code, message, details=None):
    return JsonResponse({'error': {'code': code, 'message': message, 'details': details or {}},
                         'request_id': request_id(request)}, status=status)


def record_error(request, status_code, exc=None):
    match = request.resolver_match
    path = ('/' + str(match.route).lstrip('^/').rstrip('$'))[:180] if match else '/unresolved'
    method = request.method if request.method in METHODS else 'OTHER'
    exception_class = type(exc).__name__[:80] if exc else ''
    frames = [(f.name, f.lineno) for f in traceback.extract_tb(exc.__traceback__)] if exc else []
    fingerprint = sha256(repr((path, method, status_code, exception_class, frames)).encode()).hexdigest()
    now = timezone.now()
    try:
        with transaction.atomic():
            user = request.user
            row, created = ErrorLog.objects.get_or_create(stack_hash=fingerprint, resolved_at=None, defaults={
                'occurred_at': now, 'request_id': request_id(request), 'actor_id': user.pk if user.is_authenticated else None,
                'path': path, 'method': method, 'status_code': status_code, 'error_code': 'internal_error',
                'exception_class': exception_class, 'message_safe': '操作未完成，请使用请求 ID 核查受控日志。',
                'service_version': settings.OPS_ADMIN_SERVICE_VERSION, 'first_seen_at': now, 'last_seen_at': now,
            })
            if created:
                audit(request, 'ops.error.recorded', target_type='error_log', target_id=row.pk,
                      outcome='failure', summary_code='internal_error')
            else:
                ErrorLog.objects.filter(pk=row.pk, resolved_at__isnull=True).update(
                    repeat_count=F('repeat_count') + 1, last_seen_at=now, request_id=request_id(request))
    except DatabaseError:
        logger.error('ops.error.persist_failed', extra={'request_id': request_id(request), 'ops_ingest_failure_count': 1})


class OpsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = current_request.set(request)
        try:
            response = self.module_gate(request)
            if response is None:
                response = self.get_response(request)
            is_system = request.path_info.startswith(SYSTEM_API)
            is_ops = is_system or request.path_info.startswith('/ops/') or request.path_info.startswith('/api/v1/ops/')
            if response.status_code >= 500:
                record_error(request, response.status_code, getattr(request, 'ops_exception', None))
                if is_ops:
                    response = error_response(request, response.status_code, 'internal_error', '操作暂时不可用，请按请求 ID 联系运维')
            if is_ops:
                response['X-Request-ID'] = request_id(request)
                response['Cache-Control'] = 'no-store, private'
            return response
        finally:
            current_request.reset(token)

    def process_exception(self, request, exception):
        request.ops_exception = exception
        if request.path_info.startswith(SYSTEM_API):
            logger.error('ops.system.exception', extra={'request_id': request_id(request), 'exception_class': type(exception).__name__})
            return error_response(request, 500, 'internal_error', '操作暂时不可用，请按请求 ID 联系运维')
        return None

    @staticmethod
    def module_gate(request):
        path = request.path_info
        for key, manifest in apps.get_app_config('ops_admin').manifests.items():
            prefixes = (manifest.prefix, '/api/v1' + manifest.prefix) if manifest.api_router else (manifest.prefix,)
            if any(path == prefix.rstrip('/') or path.startswith(prefix) for prefix in prefixes):
                try:
                    enabled = ModuleRegistration.objects.filter(module_key=key, status='active').exists()
                except DatabaseError:
                    enabled = False
                if not enabled:
                    return error_response(request, 404, 'not_found', '对象不存在或不在可见范围')
        return None
