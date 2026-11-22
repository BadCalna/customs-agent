from __future__ import annotations

import base64

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission


def snapshot_access(user, snapshot_id, fingerprint):
    """Synthetic C-side snapshot gate used only by the isolated suite."""
    return bool(snapshot_id and fingerprint and snapshot_id.startswith("unit-contract-"))


@pytest.fixture(autouse=True)
def _test_keys(tmp_path, settings):
    settings.OPS_DEBUG_MASTER_KEY = base64.urlsafe_b64encode(b"ops-debug-test-master-key-32bytes!!"[:32]).decode()
    settings.OPS_DEBUG_DATA_KEY = base64.urlsafe_b64encode(b"ops-debug-test-data-key-32-bytes!!!"[:32]).decode()
    settings.OPS_DEBUG_SECRET_STORE = str(tmp_path / "secret-store")
    settings.OPS_DEBUG_ENABLED = True
    settings.OPS_DEBUG_LIVE_ENABLED = True
    settings.OPS_DEBUG_LOCAL_FREE_INPUT = True
    settings.OPS_DEBUG_ENDPOINTS = [
        {"id": "loopback-test", "url": "http://127.0.0.1:8765/v1/chat/completions", "models": ["debug-model"], "auth_mode": "none", "target": "local", "label": "测试本机"},
        {"id": "external-test", "url": "https://provider.invalid/v1/chat/completions", "models": ["debug-model"], "auth_mode": "bearer", "target": "external", "label": "测试外部"},
    ]
    settings.OPS_DEBUG_SAMPLES = [{"id": "fixture-demo", "label": "固定样例", "facts": {"name": "测试样例", "attributes": {"material": "塑料"}, "elements_text": "测试用途"}}]


@pytest.fixture
def user(db):
    value = get_user_model().objects.create_user(username="debug-user", password="pass", is_active=True)
    perms = list(Permission.objects.filter(content_type__app_label="agent_debug", codename__in=["use_debug", "manage_connections", "view_trace_payload"]))
    from django.contrib.contenttypes.models import ContentType
    ct, _ = ContentType.objects.get_or_create(app_label="reference_browser", model="catalog")
    catalog_perm, _ = Permission.objects.get_or_create(content_type=ct, codename="view_catalog", defaults={"name": "View catalog"})
    value.user_permissions.add(*perms, catalog_perm)
    return value
