"""Serialized state transitions and scrubbed, actor-scoped idempotency receipts."""
import json
import re

from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare

from apps.accounts.errors import ApiError

from .events import audit
from .middleware import error_response
from .models import MutationReceipt, OpsState
from .security import digest, fail, request_id

KEY = re.compile(r'[!-~]{16,128}\Z')


def idempotency_key(request):
    key = request.headers.get('Idempotency-Key', '')
    if not KEY.fullmatch(key):
        fail(422, 'idempotency_key_required', '写请求需要 16–128 个 ASCII 可见字符的 Idempotency-Key')
    return key


def lock_ops_state():
    if connection.vendor == 'sqlite':
        # Obtain SQLite's write lock before reads; production uses row locking in PostgreSQL.
        OpsState.objects.filter(pk=1).update(id=F('id'))
    OpsState.objects.get_or_create(pk=1)
    return OpsState.objects.select_for_update().get(pk=1)


def _fresh_actor(request, actor):
    """Do not let an in-flight request reuse a revoked session behind the lock."""
    fresh = get_user_model().objects.filter(pk=actor.pk, is_active=True).first()
    session = type(request.session)(session_key=request.session.session_key).load()
    if (not fresh or str(session.get(SESSION_KEY)) != str(actor.pk)
            or not constant_time_compare(session.get(HASH_SESSION_KEY, ''), fresh.get_session_auth_hash())):
        fail(401, 'unauthorized', '未登录或会话已失效')
    request.user = request.auth = fresh


def mutate(request, *, action, body, work, authorize=None, target=None, secret=False, preflight=None):
    key = idempotency_key(request)
    actor = getattr(request, 'auth', None)
    principal = str(actor.pk) if actor and actor.is_authenticated else 'anonymous'
    scope_hash = digest('mutation-scope', json.dumps([principal, action, key]))
    body_hash = digest('mutation-body', json.dumps([str(target), body], cls=DjangoJSONEncoder, sort_keys=True, separators=(',', ':')))
    if preflight and not MutationReceipt.objects.filter(scope_hash=scope_hash).exists():
        preflight()
    receipt = None
    with transaction.atomic():
        lock_ops_state()
        if principal != 'anonymous':
            _fresh_actor(request, actor)
        elif action in {'login.attempt', 'secret.consume'} and request.session.session_key and request.user.is_authenticated:
            fail(403, 'anonymous_required', '请退出当前会话后再继续')
        if authorize:
            authorize()
        prior = MutationReceipt.objects.filter(scope_hash=scope_hash).first()
        if prior:
            if prior.body_hash != body_hash:
                fail(409, 'idempotency_conflict', '相同幂等键不能用于不同请求内容')
            request.ops_request_id = prior.request_id
            if secret:
                audit(request, f'ops.{action}.replayed', target_type='operation', target_id=target,
                      summary_code='already_processed')
            return JsonResponse(prior.response, status=prior.status_code)
        try:
            with transaction.atomic():
                output = work()
        except ApiError as exc:
            # A raised service error rolls back partial changes. A returned error tuple may
            # intentionally commit a defensive change (e.g. disable instead of unsafe delete).
            status, payload = exc.status, {'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}}
        else:
            status, payload = output if isinstance(output, tuple) else (200, output)
        if status >= 400:
            audit(request, f'ops.{action}.rejected', target_type='operation', target_id=target,
                  outcome='denied' if status in (403, 404, 429) else 'failure', summary_code=payload['error']['code'])
            request.ops_rejection_audited = True
        rid = request_id(request)
        if secret and status < 400:
            stored = {'status': 'already_processed', 'request_id': rid}
        else:
            stored = {**payload, 'request_id': rid}
        receipt = MutationReceipt.objects.create(scope_hash=scope_hash, body_hash=body_hash, request_id=rid,
                                                 response=stored, status_code=status)
    # on_commit may complete trusted-email delivery and populate an offline ticket on fallback.
    # `payload` is never passed to persistence for a successful secret operation.
    return JsonResponse({**payload, 'request_id': receipt.request_id}, status=status)


def audit_rejection(request, action, exc):
    if not getattr(request, 'ops_rejection_audited', False):
        audit(request, f'ops.{action}.rejected', target_type='operation',
              outcome='denied' if exc.status in (401, 403, 404, 429) else 'failure', summary_code=exc.code)
    return error_response(request, exc.status, exc.code, exc.message, exc.details)
