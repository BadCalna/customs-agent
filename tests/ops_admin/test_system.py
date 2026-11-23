import csv
import io
import json
import logging
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.db import DatabaseError
from django.test import Client, TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.ops_admin.catalog import Manifest, sync_catalog
from apps.ops_admin.lifecycle.models import AccountLifecycle, SecretTicket
from apps.ops_admin.middleware import record_error
from apps.ops_admin.models import AuditRecord, ErrorLog, ModuleRegistration, MutationReceipt

BASE = '/api/v1/ops/system/'
PASSWORD = token_urlsafe(24) + 'aA0!'


class SystemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = User.objects.create_user(username='ops-reviewer', password=PASSWORD)
        cls.operator.groups.add(Group.objects.get(name='ops_system_admin'))
        cls.ungranted = User.objects.create_superuser(username='native-only', password=PASSWORD)

    def browser(self, user=None):
        client = Client(enforce_csrf_checks=True)
        if user:
            client.force_login(user)
        self.assertEqual(client.get(BASE + 'session').status_code, 200)
        return client

    def write(self, client, path, body, *, key=None, method='post', csrf=True):
        headers = {'HTTP_IDEMPOTENCY_KEY': key or uuid4().hex}
        if csrf:
            headers['HTTP_X_CSRFTOKEN'] = client.cookies['csrftoken'].value
        return getattr(client, method)(BASE + path, body, content_type='application/json', **headers)

    def test_auth_csrf_and_native_admin_remain_separate(self):
        anonymous = self.browser()
        self.assertEqual(anonymous.get(BASE + 'nav').status_code, 401)
        self.assertEqual(anonymous.get('/ops/admin/').status_code, 401)
        self.assertEqual(self.write(anonymous, 'users', {'username': 'pending'}).status_code, 401)
        staff = self.browser(self.ungranted)
        self.assertEqual(staff.get(reverse('admin:accounts_user_changelist')).status_code, 200)
        self.assertEqual(staff.get('/ops/admin/').status_code, 403)
        self.assertEqual(staff.get(BASE + 'nav').json()['items'], [])
        for value in (self.operator.pk, uuid4()):
            self.assertEqual(staff.get(BASE + f'users/{value}').status_code, 404)
        ops = self.browser(self.operator)
        self.assertEqual(ops.get('/ops/admin/').status_code, 200)
        self.assertEqual(ops.get(reverse('admin:accounts_user_changelist')).status_code, 302)
        denied = self.write(ops, 'users', {'username': 'pending'}, csrf=False)
        self.assertEqual((denied.status_code, denied.json()['error']['code']), (403, 'csrf_failed'))
        self.assertEqual(denied['X-Request-ID'], denied.json()['request_id'])
        self.assertIn('no-store', denied['Cache-Control'])

    def test_login_roleless_and_does_not_grant_business_access(self):
        client = self.browser()
        before = client.cookies['csrftoken'].value
        result = self.write(client, 'login', {'username': self.operator.username, 'password': PASSWORD})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertNotEqual(client.cookies['csrftoken'].value, before)
        self.assertEqual(client.get(BASE + 'dashboard').status_code, 200)
        tasks = client.get('/api/v1/tasks')
        self.assertEqual((tasks.status_code, tasks.json()['items']), (200, []))
        self.assertEqual(client.get(f'/api/v1/tasks/{uuid4()}').status_code, 404)

    def test_ticket_replay_is_scrubbed_and_revokes_sessions(self):
        ops = self.browser(self.operator)
        key = uuid4().hex
        payload = {'username': 'new-ops', 'email': 'recipient@example.test', 'issue_secret': True, 'delivery_mode': 'offline'}
        first = self.write(ops, 'users', payload, key=key)
        self.assertEqual(first.status_code, 201, first.content)
        ticket = first.json()['ticket']
        replay = self.write(ops, 'users', payload, key=key)
        self.assertEqual((replay.status_code, replay.json()), (201, {'status': 'already_processed', 'request_id': first.json()['request_id']}))
        self.assertEqual(self.write(ops, 'users', {**payload, 'username': 'changed'}, key=key).status_code, 409)
        anonymous = self.browser()
        credentials = {'ticket': ticket, 'new_password': PASSWORD}
        self.assertEqual(self.write(anonymous, 'secret/consume', credentials, csrf=False).status_code, 403)
        consume_key = uuid4().hex
        result = self.write(anonymous, 'secret/consume', credentials, key=consume_key)
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(self.write(anonymous, 'secret/consume', credentials, key=consume_key).json()['status'], 'already_processed')
        self.assertEqual(anonymous.get(BASE + 'nav').status_code, 401)
        target = User.objects.get(username='new-ops')
        self.assertTrue(target.is_active)
        target_session = self.browser(target)
        self.assertEqual(target_session.get(BASE + 'nav').status_code, 200)
        disable_key = uuid4().hex
        self.assertEqual(self.write(ops, f'users/{target.pk}/disable', {'reason': 'test disable'}, key=disable_key).status_code, 200)
        same_key_other_target = self.write(ops, f'users/{uuid4()}/disable', {'reason': 'test disable'}, key=disable_key)
        self.assertEqual((same_key_other_target.status_code, same_key_other_target.json()['error']['code']),
                         (409, 'idempotency_conflict'))
        self.assertEqual(target_session.get(BASE + 'nav').status_code, 401)
        saved = json.dumps(list(MutationReceipt.objects.values_list('response', flat=True)))
        self.assertNotIn(ticket, saved)
        self.assertNotIn(PASSWORD, saved)
        self.assertNotIn('recipient@example.test', saved)

    def test_account_updates_require_scope_over_existing_target_grants(self):
        catalog = Permission.objects.filter(content_type__app_label='ops_admin', content_type__model='permissioncatalog')
        editor = User.objects.create_user(username='scoped-editor', password=PASSWORD)
        editor.user_permissions.add(catalog.get(codename='change_user'))
        higher_grant = catalog.get(codename='view_audit_event')
        privileged_group = Group.objects.create(name='audit-scope-fixture')
        privileged_group.permissions.add(higher_grant)
        ordinary_group = Group.objects.create(name='ordinary-scope-fixture')
        client, issuer = self.browser(editor), self.browser(self.operator)

        for source in ('direct', 'group'):
            target = User.objects.create_user(username=f'scope-target-{source}', password=PASSWORD, display_name='Original')
            if source == 'direct':
                target.user_permissions.add(higher_grant)
            else:
                target.groups.add(privileged_group)
            AccountLifecycle.objects.create(user=target, status=AccountLifecycle.Status.ACTIVE)
            target_session = self.browser(target).session.session_key
            path = f'users/{target.pk}'
            issued = self.write(issuer, f'{path}/secret/reset', {'delivery_mode': 'offline'})
            self.assertEqual(issued.status_code, 200)

            rows = (User.objects.filter(pk=target.pk), AccountLifecycle.objects.filter(user=target),
                    Session.objects.filter(pk=target_session), SecretTicket.objects.filter(user=target))
            before = [row.values().get() for row in rows]
            groups = set(target.groups.values_list('pk', flat=True))
            grants = set(target.user_permissions.values_list('pk', flat=True))
            for update in ({'display_name': 'Updated'}, {'groups': sorted(groups | {ordinary_group.pk})}):
                with self.subTest(source=source, field=next(iter(update))):
                    response = self.write(client, path, update, method='patch')
                    self.assertEqual((response.status_code, response.json()['error']['code']), (403, 'delegation_denied'))
                    self.assertEqual([row.values().get() for row in rows], before)
                    self.assertEqual(set(target.groups.values_list('pk', flat=True)), groups)
                    self.assertEqual(set(target.user_permissions.values_list('pk', flat=True)), grants)
                    self.assertFalse(AuditRecord.objects.filter(event_type='ops.user.changed').exists())

    def test_dashboard_partial_and_permission_fields(self):
        self.ungranted.user_permissions.add(Permission.objects.get(content_type__app_label='ops_admin', codename='view_ops_dashboard'))
        client = self.browser(self.ungranted)
        with patch('apps.ops_admin.queries.db_health', side_effect=DatabaseError('unavailable')):
            result = client.get(BASE + 'dashboard').json()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['metrics']['database'], {'state': 'unavailable', 'value': None, 'request_id': result['request_id']})
        self.assertNotIn('accounts', result['metrics'])
        self.assertNotIn('failures_24h', result['metrics'])

    def test_audit_cursor_export_and_detail_filtering(self):
        user = self.browser(self.operator)
        events = [AuditRecord.objects.create(event_type='ops.test.check', request_id='query-event',
                   outcome='success', source='ops_admin', payload={'count': 12}) for _ in range(3)]
        first = user.get(BASE + 'audit', {'page_size': 1, 'request_id': 'query-event'}).json()
        second = user.get(BASE + 'audit', {'page_size': 1, 'request_id': 'query-event', 'cursor': first['next_cursor']}).json()
        self.assertNotEqual(first['items'][0]['event_id'], second['items'][0]['event_id'])
        self.assertEqual(user.get(BASE + 'audit', {'cursor': first['next_cursor']}).status_code, 422)
        detail = user.get(BASE + f'audit/{events[0].event_id}').json()
        self.assertNotIn('details', detail)
        self.assertEqual(detail['event_request_id'], 'query-event')
        key = uuid4().hex
        body = {'kind': 'audit', 'filters': {'request_id': 'query-event'}}
        export = self.write(user, 'audit/export', body, key=key)
        self.assertEqual(export.status_code, 200, export.content)
        self.assertIn('no-store', export['Cache-Control'])
        rows = list(csv.reader(io.StringIO(export.content.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 6)
        self.assertIn('CONTROLLED', rows[0][0])
        self.assertNotIn('query-event', rows[1][1])
        self.assertEqual(self.write(user, 'audit/export', body, key=key).content, export.content)
        self.assertEqual(AuditRecord.objects.filter(event_type='ops.audit.exported').count(), 1)

    def test_safe_exception_summary_and_resolution(self):
        request = SimpleNamespace(user=self.operator, method='GET', resolver_match=resolve('/ops/admin/'))
        exception = RuntimeError('private payload and query')
        record_error(request, 500, exception)
        record_error(request, 500, exception)
        row = ErrorLog.objects.get()
        self.assertEqual(row.repeat_count, 2)
        ops = self.browser(self.operator)
        items = ops.get(BASE + 'errors', {'resolved': 'false'}).json()['items']
        self.assertEqual(len(items), 1)
        self.assertNotIn('private payload', json.dumps(items))
        result = self.write(ops, f'errors/{row.pk}/resolve', {'reason': 'reviewed'})
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(ops.get(BASE + 'errors', {'resolved': 'false'}).json()['items'], [])
        self.assertEqual(AuditRecord.objects.filter(event_type='ops.error.resolved').count(), 1)

    def test_declared_module_collection_and_registry_gates(self):
        source = 'agent_debug'
        event_type = 'debug.run.finished'
        spec = Manifest(source, 'debug', '调试', '/ops/debug/', 'unused', '', '1',
                        ('agent_debug.use_debug',), (event_type,), ('agent_debug.use_debug',), 'unused')
        envelope = {'schema_version': 1, 'event_id': str(uuid4()), 'occurred_at': timezone.now().isoformat(),
                    'event_type': event_type, 'actor_id': None, 'target_type': 'run',
                    'target_id': 'internal-target', 'outcome': 'success', 'summary_code': ''}
        logger = logging.getLogger('customs.ops.events')
        ops = self.browser(self.operator)
        self.assertEqual(self.write(ops, f'modules/{source}', {'status': 'active'}).status_code, 404)
        with patch.object(apps.get_app_config('ops_admin'), 'manifests', {source: spec}):
            sync_catalog({source: spec})
            gated = ops.get('/ops/debug/')
            self.assertEqual(gated.status_code, 404)
            self.assertEqual(gated.json()['error']['code'], 'not_found')
            self.assertEqual(ModuleRegistration.objects.get(module_key=source).status, 'disabled')
            self.assertEqual(self.write(ops, f'modules/{source}', {'status': 'active'}).status_code, 200)
            self.assertNotIn(source, {item['module_key'] for item in ops.get(BASE + 'nav').json()['items']})
            for _ in range(2):
                logger.info('ops.event', extra={'ops_event': envelope})
            row = AuditRecord.objects.get(event_id=envelope['event_id'])
            self.assertEqual((row.source, row.summary_code), (source, 'ok'))
            self.assertTrue(row.target_id.startswith('ref_'))
            invalid = {**envelope, 'event_id': str(uuid4()), 'password': PASSWORD}
            with self.assertLogs('customs.ops.collector', level='ERROR') as records, self.assertRaises(ValueError):
                logger.info('ops.event', extra={'ops_event': invalid})
            self.assertNotIn(PASSWORD, str(records.output))
            self.assertFalse(AuditRecord.objects.filter(event_id=invalid['event_id']).exists())
