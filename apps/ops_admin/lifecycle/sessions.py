"""Work with the shared Django DB session store; never expose session keys."""
from collections import Counter, defaultdict

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from ..security import fail


def _sessions():
    return Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=500)


def require_revocable_store():
    # A cookie-only or cache-backed session cannot be revoked by deleting its DB row.
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        fail(503, "session_store_unavailable", "当前会话存储不支持安全撤销")


def revoke_sessions(user_ids):
    require_revocable_store()
    ids = {str(pk) for pk in user_ids}
    if not ids:
        return 0
    keys = [row.session_key for row in _sessions() if row.get_decoded().get(SESSION_KEY) in ids]
    count = 0
    for start in range(0, len(keys), 500):
        count += Session.objects.filter(session_key__in=keys[start:start + 500]).delete()[0]
    return count


def live_sessions(users, *, include_details=False):
    """Return counts, and optionally expiry dates (device/IP are not stored by Django)."""
    if settings.SESSION_ENGINE != "django.contrib.sessions.backends.db":
        return None, None
    expected = {str(user.pk): user.get_session_auth_hash() for user in users if user.is_active}
    counts, details = Counter(), defaultdict(list)
    if not expected:
        return counts, details
    for row in _sessions():
        data = row.get_decoded()
        user_id = data.get(SESSION_KEY)
        if user_id not in expected or not constant_time_compare(data.get(HASH_SESSION_KEY, ""), expected[user_id]):
            continue
        counts[user_id] += 1
        if include_details and len(details[user_id]) < 25:
            details[user_id].append({"expires_at": row.expire_date.isoformat()})
    return counts, details
