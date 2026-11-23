"""Operations-only persistence; business tables are not modified."""
from uuid import uuid4

from django.db import models
from django.utils import timezone


class OpsState(models.Model):
    """Singleton lock for cross-account operator mutations."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='ops_single_state')]


class MutationReceipt(models.Model):
    scope_hash = models.CharField(max_length=64, unique=True)
    body_hash = models.CharField(max_length=64)
    request_id = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField()
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class ModuleRegistration(models.Model):
    module_key = models.CharField(max_length=32, unique=True, editable=False)
    label = models.CharField(max_length=100)
    area = models.CharField(max_length=5, choices=[('debug', '调试'), ('data', '数据')])
    path = models.CharField(max_length=180)
    url_name = models.CharField(max_length=120, blank=True)
    install_version = models.CharField(max_length=40)
    permissions = models.JSONField(default=list)
    event_types = models.JSONField(default=list)
    status = models.CharField(max_length=8, choices=[('active', '启用'), ('disabled', '停用')], default='disabled')
    registered_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class PermissionCatalog(models.Model):
    app_label = models.CharField(max_length=40)
    codename = models.CharField(max_length=60)
    label = models.CharField(max_length=120)
    owner_module = models.CharField(max_length=40)
    assignable = models.BooleanField(default=True)
    risk_level = models.CharField(max_length=8, choices=[('standard', '标准'), ('high', '高风险')])
    source_version = models.CharField(max_length=40)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('app_label', 'codename'), name='ops_catalog_key')]
        permissions = [
            ('view_ops_dashboard', '查看系统管理'), ('view_user', '查看账号'), ('add_user', '新增账号'),
            ('change_user', '修改账号'), ('change_business_role', '变更业务角色'),
            ('disable_user', '启停账号与撤销会话'), ('reset_user_password', '签发一次性设密'),
            ('delete_user', '删除误建账号'), ('view_group', '查看组'), ('change_group', '维护组权限'),
            ('view_audit_event', '查看系统审计'), ('view_error_log', '查看异常日志'),
            ('resolve_error_log', '标记异常为已处理'), ('export_audit', '导出系统审计或异常'),
            ('manage_module_registry', '维护模块注册'),
        ]


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError('AuditRecord is append-only')

    def delete(self):
        raise TypeError('AuditRecord is append-only')

    def bulk_create(self, objs, **kwargs):
        if kwargs.get('update_conflicts'):
            raise TypeError('AuditRecord is append-only')
        return super().bulk_create(objs, **kwargs)


class AuditRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)
    event_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now)
    event_type = models.CharField(max_length=120)
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    request_id = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=48, blank=True)
    target_id = models.CharField(max_length=48, null=True, blank=True, db_index=True)
    outcome = models.CharField(max_length=8, choices=[('success', '成功'), ('failure', '失败'), ('denied', '拒绝')])
    summary_code = models.CharField(max_length=64)
    run_id = models.UUIDField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    source = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=('-occurred_at', '-event_id'), name='ops_audit_cursor')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError('AuditRecord is append-only')
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('AuditRecord is append-only')


class ErrorLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now)
    request_id = models.CharField(max_length=64, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True)
    path = models.CharField(max_length=180)
    method = models.CharField(max_length=12)
    status_code = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=64)
    exception_class = models.CharField(max_length=80, blank=True)
    message_safe = models.CharField(max_length=160)
    stack_hash = models.CharField(max_length=64, db_index=True)
    service_version = models.CharField(max_length=64)
    repeat_count = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=('-occurred_at', '-id'), name='ops_error_cursor')]
        constraints = [models.UniqueConstraint(fields=('stack_hash',), condition=models.Q(resolved_at__isnull=True),
                                               name='ops_error_unresolved')]


from .lifecycle.models import AccountLifecycle, LifecycleRateBucket, SecretTicket  # noqa: E402,F401
