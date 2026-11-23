"""01 系统管理装载 02/03 真实模块后的挂载、门禁、导航与本地演示账号。

本文件覆盖“注册进应用”这一层：模块清单、URL/API 挂载、未激活 404、导航过滤、
事件名归属，以及 `seed_ops_demo` 的幂等效果。模块自身的业务行为由 02/03 的
隔离测试与 tests/ops_admin 负责。
"""
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import resolve

from apps.accounts.models import User
from apps.ops_admin.catalog import CONTRACTS
from apps.ops_admin.models import AuditRecord, ModuleRegistration
from apps.ops_admin.queries import nav_items

MODULES = ('reference_browser', 'agent_debug')
PAGES = {'reference_browser': '/ops/data/', 'agent_debug': '/ops/debug/'}
APIS = {'reference_browser': '/api/v1/ops/data/catalog', 'agent_debug': '/api/v1/ops/debug/capabilities'}


def module_permissions(key):
    return Permission.objects.filter(content_type__app_label=key, codename__in=tuple(CONTRACTS[key][3]))


def manifests():
    return apps.get_app_config('ops_admin').manifests


class ManifestTests(TestCase):
    def test_installed_apps_load_modules_before_the_console(self):
        order = list(settings.INSTALLED_APPS)
        self.assertLess(order.index('apps.reference_browser'), order.index('apps.ops_admin'))
        self.assertLess(order.index('apps.agent_debug'), order.index('apps.ops_admin'))

    def test_real_modules_declare_their_entry_points(self):
        declared = manifests()
        self.assertEqual(set(declared), set(MODULES))
        for key, manifest in declared.items():
            self.assertEqual(manifest.prefix, PAGES[key])
            self.assertEqual(manifest.nav_path, manifest.prefix)
            self.assertEqual(resolve(manifest.nav_path).url_name, 'index')
            self.assertTrue(manifest.api_router.startswith(f'apps.{key}.'))

    def test_event_types_belong_to_exactly_one_module(self):
        declared = manifests()
        events = [event for manifest in declared.values() for event in manifest.event_types]
        self.assertEqual(len(events), len(set(events)))
        for manifest in declared.values():
            for event in manifest.event_types:
                owner = next((key for key, spec in declared.items() if event in spec.event_types), None)
                self.assertEqual(owner, manifest.key)
        self.assertIn('debug.run.queued', declared['agent_debug'].event_types)
        self.assertIn('reference_browser.catalog.read', declared['reference_browser'].event_types)


@override_settings(OPS_DEBUG_ENABLED=True)
class ModuleGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='ops-viewer')
        self.user.user_permissions.add(*module_permissions('reference_browser'), *module_permissions('agent_debug'))
        self.client = Client()
        self.client.force_login(self.user)

    def activate(self, *keys):
        for key in keys:
            row, _ = ModuleRegistration.objects.get_or_create(module_key=key, defaults=manifests()[key].model_fields())
            if row.status != 'active':
                row.status = 'active'
                row.save(update_fields=('status', 'updated_at'))

    def test_pages_and_apis_are_hidden_until_the_console_activates_them(self):
        for key in MODULES:
            self.assertEqual(self.client.get(PAGES[key]).status_code, 404)
            self.assertEqual(self.client.get(APIS[key]).status_code, 404)
            self.assertNotIn(key, [item['module_key'] for item in nav_items(self.user)])

        self.activate(*MODULES)
        for key in MODULES:
            self.assertEqual(self.client.get(PAGES[key]).status_code, 200)
            self.assertIn(key, [item['module_key'] for item in nav_items(self.user)])

    def test_disabling_a_module_takes_the_entry_points_down_again(self):
        self.activate('reference_browser')
        self.assertEqual(self.client.get('/ops/data/').status_code, 200)
        ModuleRegistration.objects.filter(module_key='reference_browser').update(status='disabled')
        self.assertEqual(self.client.get('/ops/data/').status_code, 404)
        self.assertNotIn('reference_browser', [item['module_key'] for item in nav_items(self.user)])

    def test_navigation_needs_the_module_permission_not_only_activation(self):
        self.activate('agent_debug')
        User.objects.filter(pk=self.user.pk).first().user_permissions.remove(
            *module_permissions('agent_debug'))
        # 反向缓存：重新取一次用户，确认导航按库内直接授权判定。
        self.user = User.objects.get(pk=self.user.pk)
        self.assertNotIn('agent_debug', [item['module_key'] for item in nav_items(self.user)])


class SeedOpsDemoTests(TestCase):
    def test_command_creates_the_demo_account_and_activates_modules(self):
        call_command('seed_ops_demo')
        user = User.objects.get(username='ops_demo')
        self.assertIn(Group.objects.get(name='ops_system_admin'), user.groups.all())
        granted = {f'{p.content_type.app_label}.{p.codename}' for p in user.user_permissions.all()}
        self.assertLessEqual({f'{key}.{code}' for key in MODULES for code in CONTRACTS[key][3]}, granted)
        self.assertEqual(set(ModuleRegistration.objects.filter(status='active').values_list('module_key', flat=True)),
                         set(MODULES))
        self.assertEqual(AuditRecord.objects.filter(target_type='module').count(), 2)
        self.assertEqual(set(AuditRecord.objects.filter(target_type='module').values_list('event_type', flat=True)),
                         {'ops.module.changed'})

    def test_command_is_idempotent(self):
        call_command('seed_ops_demo')
        first = User.objects.get(username='ops_demo').password
        call_command('seed_ops_demo')
        self.assertEqual(User.objects.filter(username='ops_demo').count(), 1)
        self.assertEqual(User.objects.get(username='ops_demo').password, first)
        self.assertEqual(AuditRecord.objects.filter(target_type='module').count(), 2)
        self.assertEqual(ModuleRegistration.objects.filter(status='active').count(), 2)