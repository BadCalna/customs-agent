"""Serialize submission, claim and state transitions per deployment."""
from datetime import timedelta

from django.utils import timezone

from .access import limit
from .errors import DebugError
from .models import DebugDefault, DebugProbe, DebugRun, OutboundQuota, State


OPEN = (State.QUEUED, State.RUNNING)


def mutex():
    """Caller holds an atomic transaction; worker never waits on network under this lock."""
    default, _ = DebugDefault.objects.select_for_update().get_or_create(pk=1)
    return default


def reserve_quota(user):
    count = OutboundQuota.objects.filter(actor=user, created_at__gt=timezone.now() - timedelta(hours=1)).count()
    if count >= limit("OPS_DEBUG_OUTBOUND_PER_HOUR", 20):
        raise DebugError("RATE_LIMITED", 429)
    OutboundQuota.objects.create(actor=user)


def check_concurrency(user):
    runs = DebugRun.objects.filter(status__in=OPEN)
    probes = DebugProbe.objects.filter(status__in=OPEN)
    if (runs.filter(owner=user).count() + probes.filter(actor=user).count() >= limit("OPS_DEBUG_USER_CONCURRENCY", 1)
            or runs.count() + probes.count() >= limit("OPS_DEBUG_INSTANCE_CONCURRENCY", 2)):
        raise DebugError("CONCURRENCY_LIMIT", 429)
