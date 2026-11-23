from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

USER = settings.AUTH_USER_MODEL


class RevisionState(models.TextChoices):
    DRAFT = "draft"
    TESTED = "tested"
    ACTIVE = "active"
    RETIRED = "retired"
    REVOKED = "revoked"


class State(models.TextChoices):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class BusinessState(models.TextChoices):
    CANDIDATE = "candidate"
    NEEDS_FACTS = "needs_facts"
    UNDETERMINED = "undetermined"
    NOT_EVALUATED = "not_evaluated"


class ConnectionRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alias = models.CharField(max_length=60)
    revision = models.PositiveIntegerField()
    endpoint_id = models.CharField(max_length=64)
    endpoint_fingerprint = models.CharField(max_length=64)
    model = models.CharField(max_length=160)
    auth_mode = models.CharField(max_length=16)
    secret_ref = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=RevisionState, default=RevisionState.DRAFT)
    last_probe = models.ForeignKey("DebugProbe", null=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()
        permissions = [("manage_connections", "Manage Agent Debug connections")]
        constraints = [
            models.UniqueConstraint(fields=["alias", "revision"], name="debug_alias_revision_uniq"),
            # All matching rows have the same status value; only one can exist.
            models.UniqueConstraint(fields=["status"], condition=Q(status="active"), name="debug_one_active_revision"),
        ]


class DebugDefault(models.Model):
    # Used as an instance-wide admission/claim mutex as well as default CAS.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    connection = models.OneToOneField(ConnectionRevision, null=True, on_delete=models.PROTECT, related_name="+")
    version = models.PositiveBigIntegerField(default=0)

    class Meta:
        default_permissions = ()
        constraints = [models.CheckConstraint(condition=Q(id=1), name="debug_default_singleton")]


class Work(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    idempotency_hash = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=State, default=State.QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        abstract = True
        default_permissions = ()


class DebugProbe(Work):
    connection = models.ForeignKey(ConnectionRevision, on_delete=models.PROTECT, related_name="probes")
    actor = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="+")
    dispatching_at = models.DateTimeField(null=True)
    http_status = models.PositiveSmallIntegerField(null=True)
    received = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["actor", "idempotency_hash"], name="debug_probe_idempotency")]
        indexes = [models.Index(fields=["status", "lease_until"])]


class DebugPreview(models.Model):
    ticket_hash = models.CharField(max_length=64, primary_key=True, editable=False)
    owner = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    parent_run = models.ForeignKey("DebugRun", null=True, on_delete=models.SET_NULL, related_name="+")
    sample_kind = models.CharField(max_length=16)
    sample_id = models.CharField(max_length=32, blank=True)
    mode = models.CharField(max_length=4, choices=[("mock", "mock"), ("live", "live")])
    input_ciphertext = models.TextField()
    input_digest = models.CharField(max_length=64)
    packet_digest = models.CharField(max_length=64)
    connection = models.ForeignKey(ConnectionRevision, null=True, on_delete=models.PROTECT, related_name="+")
    config_snapshot = models.JSONField(default=dict)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True)

    class Meta:
        default_permissions = ()


class DebugRun(Work):
    owner = models.ForeignKey(USER, null=True, on_delete=models.SET_NULL, related_name="+")
    parent_run = models.ForeignKey("self", null=True, on_delete=models.SET_NULL, related_name="+")
    preview_hash = models.CharField(max_length=64)
    mode = models.CharField(max_length=4, choices=[("mock", "mock"), ("live", "live")])
    sample_label = models.CharField(max_length=60, blank=True)
    input_ciphertext = models.TextField(blank=True)
    input_expires_at = models.DateTimeField(db_index=True)
    packet_digest = models.CharField(max_length=64)
    connection = models.ForeignKey(ConnectionRevision, null=True, on_delete=models.PROTECT, related_name="+")
    config_snapshot = models.JSONField(default=dict)
    business_status = models.CharField(max_length=16, choices=BusinessState, default=BusinessState.NOT_EVALUATED)
    result = models.JSONField(default=dict)  # codes, fixed messages and public identifiers only

    class Meta:
        default_permissions = ()
        permissions = [
            ("use_debug", "Use Agent Debug"),
            ("view_all_runs", "View summaries for all Agent Debug runs"),
            ("view_trace_payload", "View redacted Agent Debug payloads"),
        ]
        constraints = [models.UniqueConstraint(fields=["owner", "idempotency_hash"], name="debug_run_idempotency")]
        indexes = [models.Index(fields=["owner", "created_at", "id"]), models.Index(fields=["status", "lease_until"])]


class DebugStep(models.Model):
    run = models.ForeignKey(DebugRun, on_delete=models.CASCADE, related_name="steps")
    seq = models.PositiveSmallIntegerField()
    kind = models.CharField(max_length=20)
    status = models.CharField(max_length=18)
    attempt_no = models.PositiveSmallIntegerField(null=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    http_status = models.PositiveSmallIntegerField(null=True)
    received = models.BooleanField(default=False)
    provider_request_id = models.CharField(max_length=64, null=True)
    usage = models.JSONField(null=True)
    error_code = models.CharField(max_length=64, blank=True)
    truncated = models.BooleanField(default=False)

    class Meta:
        default_permissions = ()
        constraints = [models.UniqueConstraint(fields=["run", "seq"], name="debug_step_seq_uniq")]
        ordering = ["seq"]


class TracePayload(models.Model):
    step = models.OneToOneField(DebugStep, on_delete=models.CASCADE, related_name="trace")
    ciphertext = models.TextField(blank=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        default_permissions = ()


class DebugEvidence(models.Model):
    run = models.ForeignKey(DebugRun, on_delete=models.CASCADE, related_name="materials")
    step = models.ForeignKey(DebugStep, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=16)
    kind = models.CharField(max_length=8)
    source_key = models.CharField(max_length=64)  # digest of stable key, not a physical location
    status = models.CharField(max_length=20)
    ref = models.JSONField(null=True)  # EvidenceRef V1; no excerpt, locator, or model-generated URL

    class Meta:
        default_permissions = ()


class OutboundQuota(models.Model):
    """One reservation per probe/live acceptance, rolling hour; no inputs."""

    actor = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        default_permissions = ()
        indexes = [models.Index(fields=["actor", "created_at"])]


class EventOutbox(models.Model):
    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, db_index=True)
    envelope = models.JSONField()

    class Meta:
        default_permissions = ()
