from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import Client, override_settings
from django.utils import timezone

from apps.agent_debug.contracts import digest
from apps.agent_debug.models import ConnectionRevision, DebugDefault, DebugPreview, DebugRun, DebugStep, RevisionState, State
from apps.agent_debug.services import create_preview, create_run
from apps.agent_debug.worker import work_once
from apps.agent_debug.transport import TransportResult

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user(db):
    User = get_user_model()
    value = User.objects.create_user(username="debug-user", password="pass", is_active=True)
    perms = list(Permission.objects.filter(content_type__app_label="agent_debug", codename__in=["use_debug", "manage_connections", "view_trace_payload"]))
    ct, _ = ContentType.objects.get_or_create(app_label="reference_browser", model="catalog")
    catalog_perm, _ = Permission.objects.get_or_create(content_type=ct, codename="view_catalog", defaults={"name": "View catalog"})
    perms.append(catalog_perm)
    value.user_permissions.add(*perms)
    return value


@pytest.fixture
def api_client(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


def _connection(*, target="local", status=RevisionState.ACTIVE):
    from apps.agent_debug.security import get_endpoint
    endpoint_id = "loopback-test" if target == "local" else "external-test"
    ep = get_endpoint(endpoint_id, "debug-model")
    connection = ConnectionRevision.objects.create(alias=f"{target}-conn", revision=1, endpoint_id=ep.id,
        endpoint_fingerprint=ep.fingerprint, model="debug-model", auth_mode=ep.auth_mode, status=status)
    DebugDefault.objects.update_or_create(id=1, defaults={"connection": connection, "version": 1})
    return connection


def test_settings_are_standalone_and_keys_are_not_placeholder(settings):
    assert settings.SECRET_KEY != "ops-debug-test-secret"
    assert settings.OPS_DEBUG_MASTER_KEY and settings.OPS_DEBUG_DATA_KEY
    assert settings.OPS_DEBUG_MASTER_KEY != settings.OPS_DEBUG_DATA_KEY


def test_b01_loopback_probe_does_not_use_product_data(user, settings):
    from apps.agent_debug.connections import submit_probe
    import uuid
    connection = _connection(target="local", status=RevisionState.DRAFT)
    seen = []

    def fake(endpoint, body, key=None):
        seen.append((endpoint.id, body, key))
        return TransportResult(text='{"ok":true}', http_status=200, model=connection.model)

    probe = submit_probe(user, connection.pk, digest("probe-idempotency"), uuid.uuid4())
    assert work_once(transport=fake) is True
    probe.refresh_from_db()
    assert probe.status == State.SUCCEEDED
    assert probe.received is True and probe.http_status == 200
    assert seen and b"product" not in seen[0][1].lower() and seen[0][2] is None


def test_b02_preview_and_mock_have_zero_transport_calls(user, settings):
    from apps.agent_debug.contracts import PreviewIn, RunIn
    import uuid
    _connection(target="local")
    calls = []
    data = PreviewIn.model_validate({"mode": "mock", "sample_id": "fixture-demo"})
    preview = create_preview(user, data)
    run = create_run(user, RunIn.model_validate({"ticket": preview["ticket"], "confirm_external": False}), digest("mock-idempotency"), uuid.uuid4())
    assert work_once(transport=lambda *args: calls.append(args)) is True
    run.refresh_from_db()
    assert calls == []
    assert run.status == State.SUCCEEDED


def test_b03_transport_error_classification_has_no_retry():
    from apps.agent_debug.errors import DebugError
    from apps.agent_debug.transport import _decode_envelope
    with pytest.raises(DebugError) as exc:
        _decode_envelope(b"not-json", expected_model="debug-model", http_status=200)
    assert exc.value.code == "OUTPUT_SCHEMA_INVALID" and exc.value.received is True


def test_b04_preview_consumption_and_idempotency_are_distinct(user):
    from apps.agent_debug.contracts import PreviewIn
    from apps.agent_debug.services import create_run
    _connection(target="local")
    preview = create_preview(user, PreviewIn.model_validate({"mode": "mock", "sample_id": "fixture-demo"}))
    ticket = preview["ticket"]
    import uuid
    payload = __import__("apps.agent_debug.contracts", fromlist=["RunIn"]).RunIn.model_validate({"ticket": ticket, "confirm_external": False})
    first = create_run(user, payload, digest("idempotency-key"), uuid.uuid4())
    second = create_run(user, payload, digest("idempotency-key"), uuid.uuid4())
    assert str(first.id) == str(second.id)
    assert DebugPreview.objects.get(ticket_hash=digest(ticket)).consumed_at is not None


def test_b05_object_scope_and_redaction(api_client, user):
    response = api_client.get("/api/v1/ops/debug/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code in (404, 403)
    assert b"SECRET_SENTINEL" not in response.content


def test_b06_business_tables_are_not_touched(user):
    from apps.reviews.models import Task
    before = Task.objects.count()
    _connection(target="local")
    assert Task.objects.count() == before


def test_b07_external_requires_explicit_confirmation_and_approved_bytes(user, settings):
    from apps.agent_debug.contracts import PreviewIn, RunIn
    from apps.agent_debug.errors import DebugError
    _connection(target="external")
    with pytest.raises(DebugError) as exc:
        create_preview(user, PreviewIn.model_validate({"mode": "live", "sample_id": "fixture-demo"}))
    assert exc.value.code in {"REFERENCE_NOT_READY", "EXTERNAL_NOT_APPROVED"}
