"""Reference permissions are independent of business roles and task rows."""
from django.conf import settings
from django.contrib.auth.models import Permission
from django.utils.module_loading import import_string

from .errors import FORBIDDEN, UNAUTHORIZED

VIEW_CATALOG = "view_catalog"
VIEW_HISTORY = "view_history"
VIEW_HISTORY_DETAIL = "view_history_detail"


def authenticated(user):
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_active", False))


def actor_can(user, codename):
    """Require an explicit direct/group grant; superuser/staff status is not enough."""
    if not authenticated(user):
        return False
    permissions = Permission.objects.filter(
        content_type__app_label="reference_browser",
        content_type__model="referenceaccess",
        codename=codename,
    )
    direct = user.user_permissions.filter(pk__in=permissions.values("pk")).exists()
    groups = user.groups.filter(permissions__in=permissions).exists()
    return direct or groups


def require_permission(user, codename):
    if not authenticated(user):
        raise UNAUTHORIZED()
    if not actor_can(user, codename):
        raise FORBIDDEN()


def scope_allows(user, *, kind, snapshot_id, dataset_key=None, record_id=None):
    """Site policy can only narrow Django permissions, including counts and locators.

    With no site policy only this app's packaged synthetic data is available. The
    policy is called at snapshot, dataset and row levels; errors fail closed.
    """
    policy = getattr(settings, "REFERENCE_BROWSER_SCOPE_POLICY", None)
    if policy is None:
        return True
    if isinstance(policy, str):
        policy = import_string(policy)
    return policy(user, kind=kind, snapshot_id=snapshot_id,
                  dataset_key=dataset_key, record_id=record_id) is True
