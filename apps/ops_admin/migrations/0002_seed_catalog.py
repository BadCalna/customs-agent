from django.db import migrations


GROUPS = {
    'ops_system_admin': None,
    'ops_account_manager': ('view_ops_dashboard', 'view_user', 'add_user', 'change_user',
                            'disable_user', 'reset_user_password', 'view_group'),
    'ops_audit_reader': ('view_ops_dashboard', 'view_audit_event', 'view_error_log'),
    # 02 permissions are assigned only after code and auth.Permission rows are installed.
    'ops_debug_operator': (),
}
STANDARD = frozenset({'view_ops_dashboard', 'view_user', 'view_group', 'view_audit_event', 'view_error_log'})


def seed(apps, schema_editor):
    using = schema_editor.connection.alias
    catalog = apps.get_model('ops_admin', 'PermissionCatalog')
    permissions = apps.get_model('auth', 'Permission').objects.using(using)
    groups = apps.get_model('auth', 'Group').objects.using(using)
    content_type, _ = apps.get_model('contenttypes', 'ContentType').objects.using(using).get_or_create(
        app_label='ops_admin', model='permissioncatalog')
    apps.get_model('ops_admin', 'OpsState').objects.using(using).get_or_create(pk=1)
    known = {}
    for code, label in catalog._meta.permissions:
        catalog.objects.using(using).get_or_create(app_label='ops_admin', codename=code, defaults={
            'label': label, 'owner_module': 'ops_admin', 'source_version': '1',
            'risk_level': 'standard' if code in STANDARD else 'high',
        })
        known[code], _ = permissions.get_or_create(content_type=content_type, codename=code, defaults={'name': label})
    for name, codes in GROUPS.items():
        group, created = groups.get_or_create(name=name)
        # Do not modify preexisting groups on a deployment with local configuration.
        if created:
            group.permissions.add(*(known[code] for code in (known if codes is None else codes)))


class Migration(migrations.Migration):
    dependencies = [('ops_admin', '0001_initial'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
