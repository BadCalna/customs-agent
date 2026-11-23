"""Authorized account/group mutations within the caller's OpsState transaction."""
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Role, User

from ..catalog import assignable_permissions, whitelist
from ..events import audit
from ..models import AuditRecord
from ..security import explicit_permissions, fail, opaque, require
from .models import AccountLifecycle, SecretTicket
from .sessions import live_sessions, revoke_sessions

ACTIVE = AccountLifecycle.Status.ACTIVE
DISABLED = AccountLifecycle.Status.DISABLED
PENDING = AccountLifecycle.Status.PENDING_SECRET
ADMIN_KEYS = {"manage_module_registry", "disable_user"}


def account_state(user):
    if user.is_active:
        return ACTIVE
    if not user.has_usable_password() and getattr(user, "ops_lifecycle", None) is not None:
        if user.ops_lifecycle.status == PENDING:
            return PENDING
    return DISABLED


def get_user(user_id, *, lock=False):
    users = User.objects.select_for_update() if lock else User.objects.all()
    try:
        return users.get(pk=user_id)
    except (User.DoesNotExist, ValidationError, ValueError):
        fail(404, "not_found", "对象不存在或不在可见范围")


def reserved_account(user):
    names = {name.casefold() for name in getattr(settings, "OPS_ADMIN_RESERVED_USERNAMES", ())}
    return user.is_staff or user.is_superuser or user.username.casefold() in names


def _target_guard(actor, target):
    if reserved_account(target):
        fail(403, "reserved_account", "保留账号须通过原生维护流程管理")
    # Read explicit grants even for inactive targets: re-enable/reset can restore their power.
    grants = _permission_names(Permission.objects.filter(Q(user=target) | Q(group__user=target)))
    _within_scope(actor, grants)


def _permission_names(permissions):
    return {f"{app}.{code}" for app, code in permissions.values_list("content_type__app_label", "codename").distinct()}


def _within_scope(actor, names):
    if names - explicit_permissions(actor):
        fail(403, "delegation_denied", "不能分配或更改超出自身权限范围的授权")


def _active_admin_capabilities():
    # Native superuser privileges are not counted as grants. Direct and grouped explicit grants both count.
    # Use a subquery for the group branch so a different member cannot satisfy the
    # negated password predicate from the member who satisfies is_active.
    active_users = User.objects.filter(is_active=True).exclude(password__startswith="!")
    return set(Permission.objects.filter(content_type__app_label="ops_admin", content_type__model="permissioncatalog",
        codename__in=ADMIN_KEYS).filter(Q(user__in=active_users) | Q(group__user__in=active_users))
        .values_list("codename", flat=True).distinct())


def _preserve_admins(before):
    if before - _active_admin_capabilities():
        fail(409, "last_active_admin", "须先向其他活跃管理员转移权限")


def _confirmed(value):
    if value is not True:
        fail(409, "confirmation_required", "此高风险操作需要二次确认")


def _reason(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        fail(422, "reason_required", "请填写操作原因（1–500 字符）")


def _field_values(data, *, create=False):
    allowed = {"display_name", "role", "groups", "confirm"}
    if create:
        allowed.update({"username", "email", "issue_secret", "delivery_mode"})
    if data.keys() - allowed:
        fail(422, "invalid_user_fields", "请求含不可修改的账号字段")
    result = {}
    if "display_name" in data:
        name = data["display_name"]
        if not isinstance(name, str) or len(name) > 64:
            fail(422, "invalid_display_name", "显示名最长 64 字符")
        result["display_name"] = name.strip()
    if "role" in data:
        role = data["role"]
        if not isinstance(role, str) or role not in {"", *Role.values}:
            fail(422, "invalid_role", "业务角色不在允许范围内")
        result["role"] = role
    if create:
        username = data.get("username")
        if not isinstance(username, str):
            fail(422, "invalid_username", "用户名无效")
        username = User.normalize_username(username.strip())
        try:
            User._meta.get_field("username").clean(username, None)
        except ValidationError:
            fail(422, "invalid_username", "用户名无效")
        result["username"] = username
        email = data.get("email", "")
        if not isinstance(email, str) or len(email) > 254:
            fail(422, "invalid_email", "邮箱无效")
        if email:
            try:
                validate_email(email)
            except ValidationError:
                fail(422, "invalid_email", "邮箱无效")
            email = User.objects.normalize_email(email)
        result["email"] = email
    return result


def _selection(values, item_type):
    if (not isinstance(values, (list, tuple)) or len(values) > 100
            or any(type(item) is not item_type for item in values) or len(set(values)) != len(values)):
        fail(422, "invalid_selection", "请选择不重复的白名单项目")
    return set(values)


def _groups(user, requested, actor, confirm):
    ids = _selection(requested, int)
    # Guard ORM integer-range overflows and reject non-existing group's implicit idempotence.
    if any(pk <= 0 or pk > 2**63 - 1 for pk in ids):
        fail(422, "invalid_group", "所选系统组不存在")
    current = set(user.groups.values_list("pk", flat=True))
    groups = list(Group.objects.filter(pk__in=ids))
    if len(groups) != len(ids):
        fail(422, "invalid_group", "所选系统组不存在")
    changed = current ^ ids
    if not changed:
        return None
    added = Permission.objects.filter(group__pk__in=ids - current)
    reserved_groups = getattr(settings, "OPS_ADMIN_RESERVED_GROUPS", ())
    adding_reserved = any(group.pk in ids - current and group.name in reserved_groups for group in groups)
    if adding_reserved:
        require(actor, "manage_module_registry")
        if actor.pk == user.pk:
            fail(403, "self_escalation", "不能将自己加入保留组")
        _confirmed(confirm)
    if set(added.values_list("pk", flat=True)) - {perm.pk for perm in assignable_permissions().values()}:
        fail(422, "group_not_assignable", "所选组含白名单外权限")
    _within_scope(actor, _permission_names(Permission.objects.filter(group__pk__in=changed)))
    high = {row["key"] for row in whitelist() if row["risk_level"] == "high"}
    if high & _permission_names(Permission.objects.filter(group__pk__in=changed)):
        if actor.pk == user.pk and high & _permission_names(added):
            fail(403, "self_escalation", "不能为自己增加高风险授权")
        _confirmed(confirm)
    return groups


@transaction.atomic
def create_user(request, data):
    require(request.user, "add_user")
    if "issue_secret" in data and type(data["issue_secret"]) is not bool:
        fail(422, "invalid_issue_secret", "是否签发一次性设密必须为布尔值")
    fields = _field_values(data, create=True)
    if fields.get("role"):
        require(request.user, "change_business_role")
        _confirmed(data.get("confirm"))
    user = User(**fields, is_active=False)
    if reserved_account(user):
        fail(403, "reserved_account", "保留账号须通过原生维护流程管理")
    if User.objects.filter(username=user.username).exists():
        fail(409, "username_exists", "用户名已存在")
    user.set_unusable_password()
    try:
        with transaction.atomic():
            user.save(force_insert=True)
    except IntegrityError:
        if User.objects.filter(username=user.username).exists():
            fail(409, "username_exists", "用户名已存在")
        raise
    if (groups := _groups(user, data.get("groups", []), request.user, data.get("confirm"))) is not None:
        user.groups.set(groups)
    AccountLifecycle.objects.create(user=user, status=PENDING, managed_creation=True)
    audit(request, "ops.user.created", target_type="user", target_id=user.pk,
          payload={"new_role": user.role, "new_status": PENDING})
    result = user_summary(user, session_count=0)
    if data.get("issue_secret"):
        from .tickets import issue_ticket

        issue_ticket(request, user.pk, delivery_mode=data.get("delivery_mode"), reply=result)
    elif data.get("delivery_mode"):
        fail(422, "invalid_delivery_mode", "仅签发一次性设密时可以选择交付方式")
    return result


@transaction.atomic
def update_user(request, user_id, data):
    require(request.user, "change_user")
    target = get_user(user_id, lock=True)
    fields = _field_values(data)
    if data:
        _target_guard(request.user, target)
    elif reserved_account(target):
        fail(403, "reserved_account", "保留账号须通过原生维护流程管理")
    previous_role = target.role
    role_changed = fields.get("role", target.role) != target.role
    if role_changed:
        require(request.user, "change_business_role")
        if request.user.pk == target.pk:
            fail(403, "self_escalation", "不能为自己变更业务角色")
        _confirmed(data.get("confirm"))
    groups = _groups(target, data["groups"], request.user, data.get("confirm")) if "groups" in data else None
    before = _active_admin_capabilities() if groups is not None else set()
    for key, val in fields.items():
        setattr(target, key, val)
    if fields:
        target.save(update_fields=list(fields))
    if groups is not None:
        target.groups.set(groups)
        _preserve_admins(before)
    if role_changed or groups is not None:
        from .tickets import revoke_tickets

        # Previously issued credentials cannot be reused after an authorization change.
        revoke_tickets(request, target.pk)
        revoke_sessions([target.pk])
    audit(request, "ops.user.changed", target_type="user", target_id=target.pk,
          payload={"previous_role": previous_role, "new_role": target.role} if role_changed else None)
    return user_summary(target)


def _disable(request, target):
    from .tickets import revoke_tickets

    if request.user.pk == target.pk:
        fail(403, "self_disable", "不能停用当前登录账号")
    before = _active_admin_capabilities()
    previous = account_state(target)
    target.is_active = False
    target.save(update_fields=["is_active"])
    AccountLifecycle.objects.update_or_create(user=target, defaults={"status": DISABLED})
    _preserve_admins(before)
    revoke_tickets(request, target.pk)
    count = revoke_sessions([target.pk])
    audit(request, "ops.user.disabled", target_type="user", target_id=target.pk,
          payload={"previous_status": previous, "new_status": DISABLED, "count": count})


@transaction.atomic
def set_user_active(request, user_id, *, active, reason):
    require(request.user, "disable_user")
    _reason(reason)
    target = get_user(user_id, lock=True)
    _target_guard(request.user, target)
    if not active:
        _disable(request, target)
    else:
        previous = account_state(target)
        if previous == PENDING:
            fail(409, "secret_required", "待设密账号须先完成一次性设密")
        status = ACTIVE if target.has_usable_password() else PENDING
        if previous != ACTIVE:
            # Clear sessions that raced the previous deactivation before login can resume.
            revoke_sessions([target.pk])
        target.is_active = status == ACTIVE
        target.save(update_fields=["is_active"])
        AccountLifecycle.objects.update_or_create(user=target, defaults={"status": status})
        audit(request, "ops.user.enabled", target_type="user", target_id=target.pk,
              payload={"previous_status": previous, "new_status": status})
    return user_summary(target)


def _has_business_relations(user):
    # Check every directly linked record, including PROTECT, SET_NULL, and user-as-actor references.
    # Only ops lifecycle records and native admin logs are known non-business exceptions; Django
    # memberships/direct grants are many-to-many join rows, not a business-object relationship.
    for rel in user._meta.get_fields(include_hidden=True):
        if not rel.auto_created or rel.concrete or not (rel.one_to_many or rel.one_to_one):
            continue
        if rel.related_model in (AccountLifecycle, SecretTicket) or rel.related_model._meta.label_lower == "admin.logentry":
            continue
        # Skip only Django auth's automatically generated membership/grant join rows.
        if rel.related_model._meta.auto_created is User and rel.related_model in (User.groups.through, User.user_permissions.through):
            continue
        if rel.related_model._base_manager.filter(**{rel.field.name: user.pk}).exists():
            return True
    return False


@transaction.atomic
def delete_user(request, user_id, *, reason, confirm):
    require(request.user, "delete_user")
    _reason(reason)
    _confirmed(confirm)
    target = get_user(user_id, lock=True)
    if request.user.pk == target.pk:
        fail(403, "self_delete", "不能删除当前登录账号")
    _target_guard(request.user, target)
    established = (target.is_active or target.has_usable_password() or target.last_login is not None
                   or not AccountLifecycle.objects.filter(user=target, managed_creation=True).exists()
                   or SecretTicket.objects.filter(user=target, consumed_at__isnull=False).exists())
    if established or _has_business_relations(target):
        # This expected 409 commits the disable, so the generic wrapper must not raise it.
        require(request.user, "disable_user")
        _disable(request, target)
        audit(request, "ops.user.deleted", target_type="user", target_id=target.pk, outcome="denied",
              summary_code="account_not_deletable")
        return 409, {"error": {"code": "account_not_deletable", "message": "该账号不能物理删除，已停用",
                               "details": {"status": DISABLED}}}
    from .tickets import revoke_tickets

    revoke_tickets(request, target.pk)
    revoke_sessions([target.pk])
    pk = target.pk
    target.delete()
    audit(request, "ops.user.deleted", target_type="user", target_id=pk)
    return {"status": "deleted"}


@transaction.atomic
def update_group(request, group_id, *, permissions, confirm=False):
    require(request.user, "change_group")
    try:
        group = Group.objects.select_for_update().get(pk=group_id)
    except (Group.DoesNotExist, ValueError, OverflowError):
        fail(404, "not_found", "对象不存在或不在可见范围")
    keys = _selection(permissions, str)
    if group.name in getattr(settings, "OPS_ADMIN_RESERVED_GROUPS", ()):
        require(request.user, "manage_module_registry")
    allowed = assignable_permissions()
    if keys - allowed.keys():
        fail(422, "permission_not_assignable", "所选权限不在允许分配的白名单")
    current = set(group.permissions.values_list("pk", flat=True))
    managed = {key for key, permission in allowed.items() if permission.pk in current}
    _within_scope(request.user, keys ^ managed)
    high = {row["key"] for row in whitelist() if row["risk_level"] == "high"}
    if request.user.groups.filter(pk=group.pk).exists() and (keys - managed) & high:
        fail(403, "self_escalation", "不能向自己的组增加高风险授权")
    if (keys ^ managed) & high:
        _confirmed(confirm)
    count = len(managed)
    if keys != managed:
        before = _active_admin_capabilities()
        # Preserve out-of-domain grants: absence from the ops UI is not an instruction to remove them.
        group.permissions.remove(*(allowed[key] for key in managed - keys))
        group.permissions.add(*(allowed[key] for key in keys - managed))
        _preserve_admins(before)
        from .tickets import revoke_tickets

        member_ids = list(group.user_set.values_list("pk", flat=True))
        for member_id in member_ids:
            revoke_tickets(request, member_id)
        revoke_sessions(member_ids)
    audit(request, "ops.group.changed", target_type="group", target_id=group.pk,
          payload={"previous_count": count, "new_count": len(keys)})
    return group_summary(group)


def _masked_email(email):
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain[:1]}***" if local and domain else None


def session_counts(users):
    counts, _ = live_sessions(users)
    return counts


def user_summary(user, *, detail=False, session_count=None):
    groups = [{"id": group.pk, "name": group.name} for group in user.groups.all()]
    risk_flags = (["reserved_account"] if reserved_account(user) else [])
    if detail or session_count is None:
        counts, sessions = live_sessions([user], include_details=detail)
        session_count = counts[str(user.pk)] if counts is not None else None
    else:
        sessions = None
    result = {
        "id": str(user.pk), "username": user.username, "display_name": user.display_name, "role": user.role,
        "groups": groups, "status": account_state(user), "is_active": user.is_active, "session_count": session_count,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "created_at": user.date_joined.isoformat(), "risk_flags": risk_flags,
    }
    if detail:
        rows = AuditRecord.objects.filter(target_type="user", target_id=opaque(user.pk)).order_by("-occurred_at", "-event_id")[:10]
        failures = AuditRecord.objects.filter(event_type="ops.login.failed", target_id=opaque(user.username.casefold()),
                                               occurred_at__gte=timezone.now() - timezone.timedelta(hours=24)).count()
        result.update(email_masked=_masked_email(user.email), sessions=sessions[str(user.pk)] if sessions is not None else None,
            recent_failed_logins=failures, groups=[group_summary(group) for group in user.groups.all()],
            recent_operations=[{"event_type": row.event_type, "summary_code": row.summary_code, "outcome": row.outcome,
                                "occurred_at": row.occurred_at.isoformat(), "request_id": row.request_id} for row in rows])
    return result


def group_summary(group):
    allowed = assignable_permissions()
    ids = set(group.permissions.values_list("pk", flat=True))
    last = AuditRecord.objects.filter(event_type="ops.group.changed", target_id=opaque(group.pk),
                                      target_type="group", outcome="success").order_by("-occurred_at", "-event_id").first()
    return {"id": group.pk, "name": group.name, "description": "",
            "permissions": sorted(key for key, permission in allowed.items() if permission.pk in ids),
            "member_count": group.user_set.count(), "reserved": group.name in getattr(settings, "OPS_ADMIN_RESERVED_GROUPS", ()),
            "last_changed_at": last.occurred_at.isoformat() if last else None,
            "last_changed_by": opaque(last.actor_id) if last else None}
