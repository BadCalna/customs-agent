"""Persistence for pending accounts, one-time credentials, and durable rate limits."""
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone


class AccountLifecycle(models.Model):
    """Only ops-created/disabled accounts need a marker; ordinary users are supported."""

    class Status(models.TextChoices):
        PENDING_SECRET = "pending_secret", "待设密"
        ACTIVE = "active", "活跃"
        DISABLED = "disabled", "已禁用"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                primary_key=True, related_name="ops_lifecycle")
    status = models.CharField(max_length=14, choices=Status.choices)
    managed_creation = models.BooleanField(default=False, editable=False)
    updated_at = models.DateTimeField(auto_now=True)


class SecretTicket(models.Model):
    class Purpose(models.TextChoices):
        SET = "set_password", "首次设密"
        RESET = "reset_password", "重置密码"

    class Delivery(models.TextChoices):
        EMAIL = "trusted_email", "可信邮件"
        OFFLINE = "offline", "线下交付"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ops_secret_tickets")
    ticket_hash = models.CharField(max_length=64, unique=True, editable=False)
    account_fingerprint = models.CharField(max_length=64, editable=False)
    purpose = models.CharField(max_length=14, choices=Purpose.choices)
    issued_by = models.UUIDField(editable=False)
    delivery_mode = models.CharField(max_length=13, choices=Delivery.choices)
    email_message_id = models.CharField(max_length=160, blank=True)
    issued_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    idempotency_key_hash = models.CharField(max_length=64, editable=False)
    request_id = models.CharField(max_length=64, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("user", "purpose"),
                condition=models.Q(consumed_at__isnull=True, revoked_at__isnull=True, expired_at__isnull=True),
                name="ops_secret_one_open"),
            models.CheckConstraint(condition=(
                models.Q(consumed_at__isnull=True, revoked_at__isnull=True)
                | models.Q(consumed_at__isnull=True, expired_at__isnull=True)
                | models.Q(revoked_at__isnull=True, expired_at__isnull=True)
            ), name="ops_secret_one_terminal"),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F("issued_at")),
                                   name="ops_secret_valid_expiry"),
        ]


class LifecycleRateBucket(models.Model):
    """Privacy-preserving fixed windows; the key is a keyed hash, not an IP or a username."""

    key = models.CharField(max_length=64, primary_key=True, editable=False)
    window = models.DateTimeField(db_index=True)
    count = models.PositiveIntegerField(default=0)
