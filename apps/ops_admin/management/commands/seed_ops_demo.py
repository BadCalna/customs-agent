"""建立本地演示用的运维账号，并激活 02/03 模块入口。

仅用于本地开发与演示录屏：真实环境的模块激活必须由持 ``ops_admin.manage_module_registry``
的账号在 01 系统管理页面完成。本命令仍写同一条系统审计，便于演示后追溯是谁、什么时候
把哪个模块从 disabled 改成 active。
"""
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.ops_admin.events import audit
from apps.ops_admin.models import ModuleRegistration

DEMO_USERNAME = 'ops_demo'
DEMO_DISPLAY_NAME = '运维演示管理员'
# 与 make seed-demo 的演示口令一致；只用于本地演示环境，不是生产凭据。
DEMO_PASSWORD = 'customs-demo-only'
OPS_GROUP = 'ops_system_admin'
MODULES = ('reference_browser', 'agent_debug')
MODULE_PERMISSIONS = {
    'reference_browser': ('view_catalog', 'view_history', 'view_history_detail'),
    'agent_debug': ('use_debug', 'manage_connections', 'view_all_runs', 'view_trace_payload'),
}


class Command(BaseCommand):
    help = '建立本地运维演示账号（ops_demo）并激活 02/03 模块；可重复执行，不重置已有密码。'

    def add_arguments(self, parser):
        parser.add_argument('--reset-password', action='store_true', help='重置 ops_demo 的演示密码（默认不动）。')
        parser.add_argument('--no-activate', action='store_true', help='只建账号与授权，不激活模块。')

    @transaction.atomic
    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME, defaults={'display_name': DEMO_DISPLAY_NAME, 'role': '', 'is_active': True})
        if created or options['reset_password']:
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=('password',))

        group = Group.objects.filter(name=OPS_GROUP).first()
        if group is None:
            self.stderr.write(f'缺少 {OPS_GROUP} 组：请先执行 manage.py migrate。')
            return
        user.groups.add(group)

        granted, missing = [], []
        for app_label, codes in MODULE_PERMISSIONS.items():
            for code in codes:
                permission = Permission.objects.filter(content_type__app_label=app_label, codename=code).first()
                if permission is None:
                    missing.append(f'{app_label}.{code}')
                    continue
                user.user_permissions.add(permission)
                granted.append(f'{app_label}.{code}')

        activated = []
        if not options['no_activate']:
            from django.apps import apps as django_apps

            manifests = django_apps.get_app_config('ops_admin').manifests
            for key in MODULES:
                spec = manifests.get(key)
                if spec is None:
                    self.stderr.write(f'模块 {key} 没有装载清单，跳过激活。')
                    continue
                row, just_created = ModuleRegistration.objects.get_or_create(
                    module_key=key, defaults=spec.model_fields())
                previous = 'unregistered' if just_created else row.status
                if previous != 'active':
                    row.status = 'active'
                    row.save(update_fields=('status', 'updated_at'))
                    audit(None, 'ops.module.registered' if just_created else 'ops.module.changed',
                          target_type='module', target_id=key,
                          payload={'previous_status': previous, 'new_status': 'active'})
                activated.append(f'{key}:{previous}→{row.status}')

        self.stdout.write(f'account={DEMO_USERNAME} created={created} permissions={len(granted)} missing={len(missing)}')
        for key in missing:
            self.stderr.write(f'缺少权限行 {key}：请先执行 manage.py migrate。')
        if activated:
            self.stdout.write('modules=' + ' '.join(activated))
        self.stdout.write(f'登录 {DEMO_USERNAME} 后可见入口：/ops/admin/ 、/ops/data/ 、/ops/debug/')