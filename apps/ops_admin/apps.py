from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models.signals import post_migrate


class OpsAdminConfig(AppConfig):
    name = 'apps.ops_admin'
    verbose_name = '系统管理'

    def ready(self):
        from .catalog import load_manifests, sync_catalog

        required = {'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
                    'django.contrib.auth.middleware.AuthenticationMiddleware'}
        if (settings.AUTH_USER_MODEL != 'accounts.User' or not required.issubset(settings.MIDDLEWARE)
                or settings.SESSION_ENGINE != 'django.contrib.sessions.backends.db'):
            raise ImproperlyConfigured('ops_admin requires accounts.User, database sessions, auth and CSRF middleware')
        self.manifests = load_manifests()

        def after_migrate(sender, using, **kwargs):
            from django.db import connections

            if using == 'default' and 'ops_admin_permissioncatalog' in connections[using].introspection.table_names():
                sync_catalog(self.manifests)

        post_migrate.connect(after_migrate, sender=self, weak=False, dispatch_uid='ops_admin.sync_catalog')
