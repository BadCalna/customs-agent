"""Exercise the trusted installer contract without shipping 02/03 implementation code."""
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import clear_url_caches, include, path
from ninja import NinjaAPI, Router

from apps.accounts.models import User
from apps.ops_admin.catalog import CONTRACTS, add_api_routers, load_manifests, sync_catalog, web_patterns
from config.urls import urlpatterns as default_patterns

MODULES = ('agent_debug', 'reference_browser')


@override_settings(OPS_ADMIN_MODULE_INSTALLERS=tuple(f'apps.{key}.test_install.install' for key in MODULES))
class InstallTests(TestCase):
    def setUp(self):
        clear_url_caches()
        self.addCleanup(clear_url_caches)

    def module_fixtures(self, *, malformed=False):
        modules = {}
        for key in MODULES:
            area, label, _, codes = CONTRACTS[key]
            module = ModuleType(f'apps.{key}.test_install')

            def page(request, pk=None):
                return JsonResponse({'fixture': True, 'id': str(pk)})

            module.urlpatterns = [path(f'ops/{area}/' if malformed else '', page), path('records/<uuid:pk>/', page)]
            module.router = Router()

            @module.router.get('/installed')
            def installed(request):
                return {'fixture': True}

            manifest = {'module_key': key, 'label': label, 'nav_path': f'/ops/{area}/', 'urlconf': module.__name__,
                        'permissions': [f'{key}.{code}' for code in codes], 'event_types': [f'{key}.fixture.read'],
                        'api_router': f'{module.__name__}.router'}
            module.install = lambda manifest=manifest: manifest
            modules[module.__name__] = module
        return modules

    def test_mounted_module_deep_links_api_and_permission_filtered_nav(self):
        with patch.dict('sys.modules', self.module_fixtures()), patch.object(apps, 'is_installed', return_value=True):
            manifests = load_manifests()
            api = NinjaAPI(urls_namespace='ops-install-test')
            add_api_routers(api, manifests)
            fixture_urlconf = ModuleType('ops_install_fixture_urlconf')
            fixture_urlconf.urlpatterns = [*web_patterns(manifests), path('api/v1/', api.urls),
                                           path('', include(default_patterns))]
            sync_catalog(manifests)
            user = User.objects.create_user(username='ops-installer')
            user.groups.add(Group.objects.get(name='ops_system_admin'))
            client = Client()
            client.force_login(user)
            with patch.object(apps.get_app_config('ops_admin'), 'manifests', manifests), self.settings(ROOT_URLCONF=fixture_urlconf):
                for key, manifest in manifests.items():
                    identifier = uuid4()
                    deep_link = f'{manifest.prefix}records/{identifier}/'
                    api_link = '/api/v1' + manifest.prefix + 'installed'
                    status_link = f'/api/v1/ops/system/modules/{key}'
                    nav_link = '/api/v1/ops/system/nav'
                    self.assertEqual([client.get(url).status_code for url in (deep_link, api_link)], [404, 404])
                    changed = client.post(status_link, {'status': 'active'}, content_type='application/json',
                                          HTTP_IDEMPOTENCY_KEY=uuid4().hex)
                    self.assertEqual(changed.status_code, 200, changed.content)
                    self.assertEqual(client.get(deep_link).json()['id'], str(identifier))
                    self.assertTrue(client.get(api_link).json()['fixture'])
                    self.assertNotIn(key, [item['module_key'] for item in client.get(nav_link).json()['items']])
                    content_type = ContentType.objects.create(app_label=key, model='fixture')
                    permission = Permission.objects.create(content_type=content_type, name='Fixture navigation',
                                                           codename=manifest.nav_permissions[0].split('.')[1])
                    user.user_permissions.add(permission)
                    self.assertIn(key, [item['module_key'] for item in client.get(nav_link).json()['items']])
                    result = client.patch(status_link, {'status': 'disabled'}, content_type='application/json',
                                          HTTP_IDEMPOTENCY_KEY=uuid4().hex)
                    self.assertEqual(result.status_code, 200, result.content)
                    self.assertEqual([client.get(url).status_code for url in (deep_link, api_link)], [404, 404])

    def test_invalid_root_prefixed_installer_is_rejected(self):
        with patch.dict('sys.modules', self.module_fixtures(malformed=True)), patch.object(apps, 'is_installed', return_value=True):
            with self.assertRaisesMessage(ImproperlyConfigured, 'Invalid ops module install manifest'):
                load_manifests()
