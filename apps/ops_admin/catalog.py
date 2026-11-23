"""Installation and assignment are restricted to code-declared module contracts."""
import re
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Q
from django.urls import Resolver404, get_resolver, include, path
from django.utils.module_loading import import_string

from .models import ModuleRegistration, PermissionCatalog

CONTRACTS = {
    'agent_debug': ('debug', '系统调试', 'debug', {
        'use_debug': '使用调试台', 'manage_connections': '维护调试连接', 'view_all_runs': '查看所有运行摘要',
        'view_trace_payload': '查看脱敏报文',
    }),
    'reference_browser': ('data', '基础数据中心', 'reference', {
        'view_catalog': '查看参考目录', 'view_history': '查看历史案例', 'view_history_detail': '查看历史案例详情',
    }),
}
HIGH_RISK = frozenset({'add_user', 'change_user', 'change_business_role', 'disable_user', 'reset_user_password',
    'delete_user', 'change_group', 'manage_module_registry', 'export_audit', 'resolve_error_log',
    'manage_connections', 'view_all_runs', 'view_trace_payload', 'view_history_detail'})
EVENT_NAME = re.compile(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){2,4}\Z')


@dataclass(frozen=True)
class Manifest:
    key: str
    area: str
    label: str
    nav_path: str
    urlconf: str
    url_name: str
    install_version: str
    permissions: tuple
    event_types: tuple
    nav_permissions: tuple
    api_router: str

    @property
    def prefix(self):
        return f'/ops/{self.area}/'

    def model_fields(self):
        return dict(area=self.area, label=self.label, path=self.nav_path, url_name=self.url_name,
                    install_version=self.install_version, permissions=list(self.permissions), event_types=list(self.event_types))


def web_patterns(manifests):
    return [path(manifest.prefix.lstrip('/'), include(manifest.urlconf)) for manifest in manifests.values()]


def add_api_routers(api, manifests):
    for manifest in manifests.values():
        if manifest.api_router:
            api.add_router(manifest.prefix.strip('/'), import_string(manifest.api_router))


def load_manifests():
    result = {}
    configured = settings.OPS_ADMIN_MODULE_INSTALLERS
    if not isinstance(configured, (tuple, list)):
        raise ImproperlyConfigured('OPS_ADMIN_MODULE_INSTALLERS must be a list of installer paths')
    for installer in configured:
        try:
            data = import_string(installer)()
            key = data['module_key']
            area, _, event_prefix, codes = CONTRACTS[key]
            label, path, urls = (data[k] for k in ('label', 'nav_path', 'urlconf'))
            version = data.get('install_version', '1')
            url_name = data.get('url_name', '')
            router = data.get('api_router', '')
            permissions, events = data['permissions'], data['event_types']
            expected = {f'{key}.{code}' for code in codes}
            nav = data.get('nav_permissions', [f'{key}.{next(iter(codes))}'])
            if (key in result or not apps.is_installed(f'apps.{key}') or not 1 <= len(label.strip()) <= 100
                    or not path.startswith(f'/ops/{area}/') or len(path) > 180 or '//' in path
                    or not re.fullmatch(r'/[A-Za-z0-9_/-]+/', path)
                    or not urls.startswith(f'apps.{key}.')
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,39}', version)
                    or (url_name and not re.fullmatch(r'[A-Za-z0-9_:-]{1,120}', url_name))
                    or not isinstance(permissions, (list, tuple)) or len(permissions) != len(expected)
                    or set(permissions) != expected or not isinstance(nav, (list, tuple)) or not nav
                    or not set(nav).issubset(expected) or not isinstance(events, (list, tuple))
                    or not 1 <= len(events) <= 100 or len(events) != len(set(events))
                    or not all(len(e) <= 120 and EVENT_NAME.fullmatch(e) and e.split('.')[0] in {key, event_prefix, area}
                               for e in events)):
                raise ValueError('invalid module metadata')
            if router:
                from ninja import Router
                if not router.startswith(f'apps.{key}.') or not isinstance(import_string(router), Router):
                    raise ValueError('invalid router')
            # Resolve the actual entry inside the relative URLConf, not just its prefix.
            # This rejects stale/root-prefixed URLConfs before serving traffic.
            get_resolver(urls).resolve('/' + path[len(f'/ops/{area}/'):])
            result[key] = Manifest(key, area, label, path, urls, url_name, version, tuple(permissions),
                                   tuple(events), tuple(nav), router)
        except (ImportError, KeyError, AttributeError, ValueError, TypeError, Resolver404) as exc:
            raise ImproperlyConfigured('Invalid ops module install manifest') from exc
    return result


def catalog_rows(manifests):
    for code, label in PermissionCatalog._meta.permissions:
        yield 'ops_admin', code, str(label), '1'
    for key, manifest in manifests.items():
        for code, label in CONTRACTS[key][3].items():
            yield key, code, label, manifest.install_version


def sync_catalog(manifests):
    """Never activate modules or delete permissions during migration/sync."""
    with transaction.atomic():
        for app, code, label, version in catalog_rows(manifests):
            PermissionCatalog.objects.update_or_create(app_label=app, codename=code, defaults={
                'label': label, 'owner_module': app, 'source_version': version,
                'risk_level': 'high' if code in HIGH_RISK else 'standard',
            })
        for key, manifest in manifests.items():
            ModuleRegistration.objects.update_or_create(module_key=key, defaults=manifest.model_fields())


def assignable_permissions():
    pairs = {(app, code) for app, code, _, _ in catalog_rows(apps.get_app_config('ops_admin').manifests)}
    allowed = set(PermissionCatalog.objects.filter(assignable=True).values_list('app_label', 'codename')) & pairs
    query = Q(pk__in=[])
    for app, code in sorted(allowed):
        part = Q(content_type__app_label=app, codename=code)
        query |= part & Q(content_type__model='permissioncatalog') if app == 'ops_admin' else part
    result, duplicates = {}, set()
    for permission in Permission.objects.filter(query).select_related('content_type'):
        key = f'{permission.content_type.app_label}.{permission.codename}'
        if key in result:
            duplicates.add(key)
        result[key] = permission
    return {key: permission for key, permission in result.items() if key not in duplicates}


def whitelist():
    assignable = assignable_permissions()
    result = []
    for row in PermissionCatalog.objects.filter(assignable=True).order_by('app_label', 'codename').values(
        'app_label', 'codename', 'label', 'owner_module', 'assignable', 'risk_level', 'source_version'
    ):
        key = f"{row['app_label']}.{row['codename']}"
        if key in assignable:
            result.append({**row, 'key': key})
    return result
