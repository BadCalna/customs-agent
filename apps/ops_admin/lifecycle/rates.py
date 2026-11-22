"""Durable per-subject, per-actor and per-IP fixed-window request limits."""
from datetime import datetime, timezone as tz
from ipaddress import ip_address

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..security import digest, fail
from .models import LifecycleRateBucket

# seconds, subject max, actor max, IP max (None disables the respective dimension).
DEFAULT_LIMITS = {
    "login": (900, 20, None, 80),
    "issue": (900, 5, 30, 80),
    "consume": (900, 10, None, 30),
    "export": (900, None, 5, 20),
}


def _ip(request):
    # Trust only the application server's peer address. Deployment must configure its
    # trusted proxy to supply REMOTE_ADDR; callers cannot bypass limits with X-Forwarded-For.
    try:
        addr = ip_address(request.META.get("REMOTE_ADDR", ""))
        return str(getattr(addr, "ipv4_mapped", None) or addr)
    except ValueError:
        return "unknown"


def rate_limit(request, action, *, subject=None):
    """Call before the operation's savepoint so denied/failed attempts still spend budget."""
    configured = getattr(settings, "OPS_ADMIN_RATE_LIMITS", {})
    period, max_subject, max_actor, max_ip = configured.get(action, DEFAULT_LIMITS[action])
    if (type(period) is not int or not 1 <= period <= 86400
            or any(limit is not None and (type(limit) is not int or limit < 1)
                   for limit in (max_subject, max_actor, max_ip))):
        raise ValueError("Invalid ops rate limit")
    stamp = int(timezone.now().timestamp())
    start = datetime.fromtimestamp(stamp - stamp % period, tz=tz.utc)
    actor = getattr(request, "user", None)
    actor_id = actor.pk if actor is not None and actor.is_authenticated else None
    levels = {
        digest(f"rate.{action}.{kind}", str(value)[:1024]): limit
        for kind, value, limit in (("subject", subject, max_subject), ("actor", actor_id, max_actor),
                                   ("ip", _ip(request), max_ip))
        if value is not None and limit is not None
    }
    denied = False
    with transaction.atomic():
        # Deterministic lock order avoids a deadlock when requests share a subset of keys.
        for key, maximum in sorted(levels.items()):
            bucket, _ = LifecycleRateBucket.objects.select_for_update().get_or_create(
                key=key, defaults={"window": start})
            if bucket.window < start:
                LifecycleRateBucket.objects.filter(pk=key).update(count=1, window=start)
            elif bucket.count < maximum:
                LifecycleRateBucket.objects.filter(pk=key).update(count=F("count") + 1)
            else:
                denied = True
    if denied:
        fail(429, "rate_limited", "请求过于频繁，请稍后重试", {"retry_after_seconds": period - stamp % period})


def prune_rate_buckets():
    # Retain at least one full period for every configured bucket, including custom windows.
    cutoff = timezone.now() - timezone.timedelta(days=2)
    return LifecycleRateBucket.objects.filter(window__lt=cutoff).delete()[0]
