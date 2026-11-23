"""Bounded, signed keyset queries and safe audit/error projections."""
import re
from datetime import datetime, time, timedelta
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.core import signing
from django.db import DatabaseError, connection
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.accounts.models import User

from .catalog import CONTRACTS
from .models import AuditRecord, ErrorLog, ModuleRegistration
from .security import digest, explicit_permissions, fail, opaque, request_id

AUDIT_FILTERS = frozenset({'date_from', 'date_to', 'event_type', 'actor_id', 'request_id', 'target_type',
                           'target_id', 'outcome', 'summary_code', 'module', 'run_id'})
ERROR_FILTERS = frozenset({'date_from', 'date_to', 'status_code', 'error_code', 'stack_hash', 'path', 'resolved'})


def keyset(qs, key, limit, cursor, scope, filters, timestamp='occurred_at'):
    if not 1 <= limit <= 100:
        fail(422, 'invalid_page_size', '每页数量必须在 1–100 之间')
    salt = 'customs.ops.cursor.' + scope
    binding = digest('cursor', sorted(filters.items()))
    if cursor:
        try:
            ts, pk, fingerprint = signing.loads(cursor, salt=salt, max_age=86400)
            if fingerprint != binding or not (parsed := parse_datetime(ts)) or timezone.is_naive(parsed):
                raise ValueError('invalid cursor')
            pk = qs.model._meta.get_field(key).to_python(pk)
        except (signing.BadSignature, TypeError, ValueError, AttributeError, ArithmeticError, ValidationError):
            fail(422, 'invalid_cursor', '分页游标已过期或与筛选不匹配')
        qs = qs.filter(Q(**{timestamp + '__lt': parsed}) | Q(**{timestamp: parsed, key + '__lt': pk}))
    rows = list(qs.order_by('-' + timestamp, '-' + key)[:limit + 1])
    if len(rows) <= limit:
        return rows, None
    last = rows[limit - 1]
    return rows[:limit], signing.dumps([getattr(last, timestamp).isoformat(), str(getattr(last, key)), binding], salt=salt)


# Model.to_python raises Django's ValidationError (rather than ValueError) for malformed UUIDs.
from django.core.exceptions import ValidationError  # noqa: E402


def date_filter(value, *, until=False):
    try:
        date = parse_date(value)
        if date is not None:
            return timezone.make_aware(datetime.combine(date + timedelta(days=int(until)), time.min))
        parsed = parse_datetime(value)
        if parsed is not None:
            return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
        raise ValueError('invalid date')
    except (ValueError, TypeError, OverflowError):
        fail(422, 'invalid_filter', '时间筛选必须为 ISO 日期或日期时间')


def log_query(kind, filters):
    allowed = AUDIT_FILTERS if kind == 'audit' else ERROR_FILTERS
    if filters.keys() - allowed or any(not isinstance(v, (str, bool, int)) or len(str(v)) > 180 for v in filters.values()):
        fail(422, 'invalid_filter', '筛选字段或值不受支持')
    filters = {k: str(v) for k, v in filters.items() if v != ''}
    qs = AuditRecord.objects.all() if kind == 'audit' else ErrorLog.objects.all()
    db_filters = {}
    for key, value in filters.items():
        if key == 'date_from':
            db_filters['occurred_at__gte'] = date_filter(value)
        elif key == 'date_to':
            db_filters['occurred_at__lt'] = date_filter(value, until=True)
        elif key in ('actor_id', 'run_id'):
            try:
                db_filters[key] = UUID(value)
            except ValueError:
                fail(422, 'invalid_filter', '账号或运行 ID 格式不正确')
        elif key == 'target_id':
            db_filters[key] = value if re.fullmatch(r'ref_[0-9a-f]{32}', value) else opaque(value)
        elif key == 'module':
            db_filters['source'] = value
        elif key == 'resolved':
            if value.lower() not in ('true', 'false', '1', '0'):
                fail(422, 'invalid_filter', '处理状态不受支持')
            db_filters['resolved_at__isnull'] = value.lower() in ('false', '0')
        elif key == 'status_code':
            if not re.fullmatch(r'[1-5]\d\d', value, re.ASCII):
                fail(422, 'invalid_filter', 'HTTP 状态码格式不正确')
            db_filters[key] = int(value)
        else:
            db_filters[key] = value
    if db_filters.get('occurred_at__gte') and db_filters.get('occurred_at__lt') and (
        db_filters['occurred_at__gte'] >= db_filters['occurred_at__lt']
    ):
        fail(422, 'invalid_filter', '起始时间必须早于结束时间')
    return qs.filter(**db_filters), filters


def audit_item(row, *, detail=False):
    return {
        'id': str(row.id), 'event_id': str(row.event_id), 'schema_version': row.schema_version,
        'occurred_at': row.occurred_at.isoformat(), 'event_type': row.event_type,
        'actor_id': str(row.actor_id) if row.actor_id else None, 'request_id': row.request_id,
        'target_type': row.target_type, 'target_id': row.target_id, 'outcome': row.outcome,
        'summary_code': row.summary_code, 'run_id': str(row.run_id) if row.run_id else None,
        'module': row.source, 'source': row.source,
        **({'details': row.payload} if detail else {}),
    }


def error_item(row):
    result = {field: getattr(row, field) for field in ('request_id', 'path', 'method', 'status_code', 'error_code',
              'exception_class', 'message_safe', 'stack_hash', 'service_version', 'repeat_count')}
    for key in ('occurred_at', 'first_seen_at', 'last_seen_at', 'resolved_at'):
        dt = getattr(row, key)
        result[key] = dt.isoformat() if dt else None
    return {**result, 'id': str(row.pk), 'actor_id': str(row.actor_id) if row.actor_id else None}


def module_items():
    installed = apps.get_app_config('ops_admin').manifests
    rows = {r.module_key: r for r in ModuleRegistration.objects.all()}
    items = []
    for key, (area, label, _, _) in CONTRACTS.items():
        manifest, row = installed.get(key), rows.get(key)
        effective = row.status if row and manifest else 'unregistered'
        items.append({'module_key': key, 'label': manifest.label if manifest else label, 'area': area,
                      'path': manifest.nav_path if manifest else None, 'url_name': manifest.url_name if manifest else '',
                      'install_version': manifest.install_version if manifest else None, 'status': effective,
                      'state': 'available' if effective == 'active' else 'unavailable',
                      'permissions': list(manifest.permissions) if manifest else [],
                      'event_types': list(manifest.event_types) if manifest else [],
                      'registered_at': row.registered_at.isoformat() if row and manifest else None,
                      'updated_at': row.updated_at.isoformat() if row and manifest else None})
    return items


def nav_items(user):
    permissions = explicit_permissions(user)
    panels = {'view_ops_dashboard', 'view_user', 'add_user', 'view_group', 'view_audit_event', 'view_error_log'}
    entry = next((f'ops_admin.{code}' for code in sorted(panels) if f'ops_admin.{code}' in permissions), None)
    items = ([{'module_key': 'ops_admin', 'label': '系统管理', 'path': '/ops/admin/', 'permissions': [entry],
               'accessible': True}] if entry else [])
    enabled = set(ModuleRegistration.objects.filter(status='active').values_list('module_key', flat=True))
    for key, manifest in apps.get_app_config('ops_admin').manifests.items():
        if key in enabled and set(manifest.nav_permissions) <= permissions:
            items.append({'module_key': key, 'label': manifest.label, 'path': manifest.nav_path,
                          'permissions': list(manifest.nav_permissions), 'accessible': True})
    return items


def account_counts():
    rows = User.objects.aggregate(total=Count('id'), active=Count('id', filter=Q(is_active=True)),
        pending_secret=Count('id', filter=Q(is_active=False, ops_lifecycle__status='pending_secret', password__startswith='!')))
    rows['disabled'] = rows['total'] - rows['active'] - rows['pending_secret']
    return rows


def db_health():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return 'online'


def queue_health():
    if settings.EXECUTOR_MODE == 'inline':
        return 'inline'
    from redis import Redis
    from redis.exceptions import RedisError

    try:
        config = settings.RQ_QUEUES[settings.CUSTOMS_QUEUE_NAME]
        with Redis.from_url(config['URL'], socket_connect_timeout=0.5, socket_timeout=0.5) as client:
            return 'online' if client.ping() else None
    except (RedisError, OSError):
        return None


def dashboard(request):
    user = request.auth
    permissions = explicit_permissions(user)
    metrics = {}

    def metric(name, provider, allowed=True):
        if not allowed:
            return
        try:
            value = provider()
            state = 'available' if value is not None else 'degraded'
        except DatabaseError:
            value, state = None, 'unavailable'
        metrics[name] = {'state': state, 'value': value, 'request_id': request_id(request)}

    def collector_delay():
        row = AuditRecord.objects.exclude(source='ops_admin').order_by('-created_at').first()
        return round(max(0.0, (row.created_at - row.occurred_at).total_seconds()), 3) if row else None

    metric('accounts', account_counts, 'ops_admin.view_user' in permissions)
    metric('failures_24h', lambda: AuditRecord.objects.filter(outcome='failure', occurred_at__gte=timezone.now()-timedelta(days=1)).count(),
           'ops_admin.view_audit_event' in permissions)
    metric('collector_delay_seconds', collector_delay, 'ops_admin.view_audit_event' in permissions)
    metric('modules', lambda: {status: sum(row['status'] == status for row in module_items())
                               for status in ('active', 'disabled', 'unregistered')})
    metric('database', db_health)
    metric('queue', queue_health)
    metric('service_version', lambda: settings.OPS_ADMIN_SERVICE_VERSION)
    return {'operator': {'id': str(user.pk), 'display_name': user.display_name or user.username,
                         'groups': list(user.groups.order_by('name').values_list('name', flat=True)),
                         'permissions': sorted(p for p in permissions if p.split('.')[0] in ('ops_admin', *CONTRACTS))},
            'metrics': metrics, 'status': 'partial' if any(m['state'] != 'available' for m in metrics.values()) else 'ready',
            'request_id': request_id(request)}
