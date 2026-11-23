"""Read-only HTML shells; the system API remains the write and authorization boundary."""

from django.http import HttpResponseNotAllowed
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers, patch_vary_headers

from .security import explicit_permissions


_CAPABILITIES = {
    "dashboard": "view_ops_dashboard",
    "users": "view_user",
    "add_user": "add_user",
    "change_user": "change_user",
    "change_role": "change_business_role",
    "disable_user": "disable_user",
    "reset_secret": "reset_user_password",
    "delete_user": "delete_user",
    "groups": "view_group",
    "change_group": "change_group",
    "audit": "view_audit_event",
    "errors": "view_error_log",
    "resolve_error": "resolve_error_log",
    "export": "export_audit",
    "modules": "manage_module_registry",
}


def _private(response):
    add_never_cache_headers(response)
    patch_vary_headers(response, ("Cookie",))
    response["Referrer-Policy"] = "no-referrer"
    return response


def dashboard(request):
    if request.method not in {"GET", "HEAD"}:
        return _private(HttpResponseNotAllowed(("GET", "HEAD")))
    get_token(request)
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return _private(render(request, "ops_admin/access.html", {"state": "unauthenticated"}, status=401))

    permissions = explicit_permissions(user)
    caps = {key: f"ops_admin.{name}" in permissions for key, name in _CAPABILITIES.items()}
    caps["accounts"] = caps["users"] or caps["add_user"]
    caps["native_users"] = user.is_staff and caps["users"] and (
        user.has_perm("accounts.view_user") or user.has_perm("accounts.change_user")
    )
    caps["native_groups"] = user.is_staff and caps["groups"] and (
        user.has_perm("auth.view_group") or user.has_perm("auth.change_group")
    )
    if not any(caps[key] for key in ("dashboard", "accounts", "groups", "audit", "errors")):
        return _private(render(request, "ops_admin/access.html", {"state": "forbidden"}, status=403))
    return _private(render(request, "ops_admin/dashboard.html", {
        "caps": caps,
        "operator": user.display_name or user.get_username(),
        "operator_id": str(user.pk),
    }))


def secret(request):
    if request.method not in {"GET", "HEAD"}:
        return _private(HttpResponseNotAllowed(("GET", "HEAD")))
    get_token(request)
    # A ticket must not be read from a query string or interpolated into HTML.
    return _private(render(request, "ops_admin/secret.html"))
