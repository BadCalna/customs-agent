"""A strict collector for standard-library logger customs.ops.events."""
import logging
import re
import uuid
from contextvars import ContextVar

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger('customs.ops.collector')
current_request = ContextVar('ops_current_request', default=None)
CODE = re.compile(r'[a-z][a-z0-9_.-]{0,63}\Z')
REQUEST = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
BASE = {'schema_version', 'event_id', 'occurred_at', 'event_type', 'actor_id', 'request_id', 'target_type',
        'target_id', 'outcome', 'summary_code', 'run_id', 'payload'}
CHOICES = {
    'previous_role': {'', 'supplier', 'operator', 'expert'}, 'new_role': {'', 'supplier', 'operator', 'expert'},
    'delivery_mode': {'trusted_email', 'offline'},
    'previous_status': {'active', 'disabled', 'pending_secret', 'unregistered'},
    'new_status': {'active', 'disabled', 'pending_secret', 'unregistered'},
}
COUNT_KEYS = {'count', 'previous_count', 'new_count', 'limit', 'repeat_count'}


def _uuid(value):
    return uuid.UUID(str(value)) if value is not None else None


def _actor(request):
    actor = getattr(request, 'user', None)
    return actor.pk if actor is not None and actor.is_authenticated else None


def safe_payload(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_payload')
    result = {}
    for key, item in value.items():
        if key in COUNT_KEYS and type(item) is int and 0 <= item <= 1_000_000_000:
            result[key] = item
        elif key in CHOICES and isinstance(item, str) and item in CHOICES[key]:
            result[key] = item
        elif key == 'truncated' and type(item) is bool:
            result[key] = item
        elif key == 'filters' and isinstance(item, dict) and all(
            isinstance(k, str) and CODE.fullmatch(k) and isinstance(v, str) and re.fullmatch(r'ref_[0-9a-f]{32}', v)
            for k, v in item.items()
        ):
            result[key] = item
        else:
            raise ValueError('invalid_payload_field')
    return result


def audit(request, event_type, *, target_type='', target_id=None, outcome='success', summary_code='ok', payload=None):
    """Synchronous fail-closed append for security-sensitive local actions."""
    from .models import AuditRecord
    from .security import opaque, request_id

    return AuditRecord.objects.create(event_type=event_type, actor_id=_actor(request),
        request_id=request_id(request) if request is not None else uuid.uuid4().hex, target_type=target_type,
        target_id=opaque(target_id), outcome=outcome, summary_code=summary_code, source='ops_admin',
        payload=safe_payload(payload or {}))


def _normalize(envelope):
    from django.apps import apps

    from .security import opaque, request_id

    if not isinstance(envelope, dict) or type(envelope.get('schema_version')) is not int or envelope['schema_version'] != 1:
        raise ValueError('invalid_version')
    event_id = _uuid(envelope.get('event_id'))
    when = envelope.get('occurred_at')
    parsed = parse_datetime(when) if isinstance(when, str) else None
    if not event_id or not parsed or timezone.is_naive(parsed):
        raise ValueError('invalid_uuid_or_time')
    event_type = envelope.get('event_type')
    source = next((key for key, spec in apps.get_app_config('ops_admin').manifests.items()
                   if event_type in spec.event_types), None)
    summary, target, outcome = (envelope.get(k) for k in ('summary_code', 'target_type', 'outcome'))
    if isinstance(summary, str) and summary.isascii():
        summary = summary.lower() or 'ok'
    target_id = envelope['target_id']
    if (not source or not isinstance(summary, str) or not CODE.fullmatch(summary) or not isinstance(target, str)
            or len(target) > 48 or not CODE.fullmatch(target) or outcome not in ('success', 'failure', 'denied')
            or (target_id is not None and (not isinstance(target_id, str) or len(target_id) > 160))):
        raise ValueError('invalid_event_metadata')
    request = current_request.get()
    correlation = envelope.get('request_id') or (request_id(request) if request is not None else uuid.uuid4().hex)
    if not isinstance(correlation, str) or not REQUEST.fullmatch(correlation):
        raise ValueError('invalid_request_id')
    payload = safe_payload(envelope.get('payload', {}))
    extra = {k: v for k, v in envelope.items() if k not in BASE}
    if payload.keys() & extra.keys():
        raise ValueError('duplicate_payload_key')
    return event_id, dict(occurred_at=parsed, event_type=event_type, actor_id=_uuid(envelope.get('actor_id')) or _actor(request),
        request_id=correlation, target_type=target, target_id=opaque(target_id), outcome=outcome, summary_code=summary,
        run_id=_uuid(envelope.get('run_id')), payload=safe_payload({**payload, **extra}), source=source)


class OpsEventHandler(logging.Handler):
    def emit(self, record):
        from .models import AuditRecord

        if record.msg != 'ops.event':
            return
        envelope = getattr(record, 'ops_event', None)
        name = envelope.get('event_type') if isinstance(envelope, dict) else None
        from django.apps import apps

        debug = apps.get_app_config('ops_admin').manifests.get('agent_debug')
        critical = isinstance(name, str) and (name in settings.OPS_ADMIN_CRITICAL_EVENTS
                                              or (debug is not None and name in debug.event_types))
        try:
            event_id, values = _normalize(envelope)
            AuditRecord.objects.get_or_create(event_id=event_id, defaults=values)
        except (DatabaseError, KeyError, AttributeError, TypeError, ValueError, OverflowError):
            # Counter suitable for service-log alerting. Never output envelope or original exception content.
            logger.error('ops.collector.ingest_failed', extra={'ops_ingest_failure_count': 1, 'critical': critical})
            if critical:
                raise
