"""Bounded synchronous exports; no temporary public files and no CSV formulas."""
import csv
import io
import json

from django.http import HttpResponse
from django.utils import timezone

from .events import audit
from .models import MutationReceipt
from .mutations import mutate
from .queries import audit_item, error_item, log_query
from .security import fail, opaque, request_id, require

LIMIT = 500
AUDIT_COLUMNS = ('occurred_at', 'event_id', 'event_type', 'actor_id', 'request_id', 'target_type', 'target_id',
                 'outcome', 'summary_code', 'module', 'run_id')
ERROR_COLUMNS = ('occurred_at', 'request_id', 'path', 'method', 'status_code', 'error_code', 'exception_class',
                 'message_safe', 'stack_hash', 'service_version', 'repeat_count', 'last_seen_at', 'resolved_at')


def _cell(value):
    if value is None:
        return ''
    value = str(value)
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def export_logs(request, data, preflight):
    kind = data['kind']
    needed = 'view_audit_event' if kind == 'audit' else 'view_error_log'

    def authorize():
        require(request.auth, 'export_audit')
        require(request.auth, needed)

    authorize()
    qs, filters = log_query(kind, data['filters'])
    # Do not echo secrets that an operator may accidentally paste in a search box.
    snapshot = {k: opaque(v) for k, v in filters.items()}

    def work():
        order = '-event_id' if kind == 'audit' else '-id'
        records = list(qs.order_by('-occurred_at', order)[:LIMIT + 1])
        if len(records) > LIMIT:
            fail(422, 'export_too_large', '导出结果超过 500 条，请缩小时间范围或增加筛选')
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['受控导出 / CONTROLLED', str(request.auth.pk), request_id(request), timezone.now().isoformat()])
        writer.writerow(['筛选快照（脱敏）', _cell(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))])
        columns = AUDIT_COLUMNS if kind == 'audit' else ERROR_COLUMNS
        writer.writerow(columns)
        for row in records:
            item = audit_item(row) if kind == 'audit' else error_item(row)
            writer.writerow([_cell(item.get(col)) for col in columns])
        audit(request, 'ops.audit.exported', target_type=kind, payload={'count': len(records), 'filters': snapshot})
        return {'csv': output.getvalue()}

    response = mutate(request, action='audit.export', body=data, work=work, authorize=authorize, preflight=preflight)
    if response.status_code >= 400:
        return response
    payload = json.loads(response.content)
    # Receipt is private and bounded; don't serve persisted snapshots beyond the short download window.
    created_at = MutationReceipt.objects.values_list('created_at', flat=True).get(request_id=payload['request_id'])
    if not payload.get('csv') or (timezone.now() - created_at).total_seconds() > 300:
        fail(409, 'export_expired', '导出已过期，请使用新的请求重新生成')
    result = HttpResponse('\ufeff' + payload['csv'], content_type='text/csv; charset=utf-8')
    result['Content-Disposition'] = f'attachment; filename="ops-{kind}-{payload["request_id"][:12]}.csv"'
    result['X-Content-Type-Options'] = 'nosniff'
    return result
