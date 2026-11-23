import os

from django.conf import settings
from django.utils.module_loading import import_string

from .errors import DebugError


def setting(name, default=None):
    return getattr(settings, name, os.environ.get(name, default))


def flag(name):
    return setting(name) in (True, "1", "true", "True")


def enabled():
    return flag("OPS_DEBUG_ENABLED")


def live_enabled():
    return enabled() and flag("OPS_DEBUG_LIVE_ENABLED")


def has(user, permission):
    return bool(user.is_authenticated and user.is_active and user.has_perm(f"agent_debug.{permission}"))


def require(user, permission):
    if not enabled():
        raise DebugError("not_found", 404)
    if not user.is_authenticated or not user.is_active:
        raise DebugError("unauthorized", 401)
    if not has(user, permission):
        raise DebugError("forbidden", 403)


def catalog_visible(user, snapshot_id, fingerprint):
    """C owns row/snapshot eligibility; a Django permission alone is insufficient."""
    checker = setting("OPS_DEBUG_SNAPSHOT_ACCESS")
    if not (checker and snapshot_id and fingerprint and user.is_active
            and user.has_perm("reference_browser.view_catalog")):
        return False
    try:
        checker = import_string(checker) if isinstance(checker, str) else checker
        return checker(user, snapshot_id, fingerprint) is True
    except Exception:
        # A missing C deployment or a failed checker is not an authorization grant.
        return False


def limit(name, maximum):
    """Deployment may tighten, never raise, the documented upper bounds."""
    try:
        value = int(setting(name, maximum))
    except (ValueError, TypeError):
        return maximum
    return min(maximum, max(1, value))
