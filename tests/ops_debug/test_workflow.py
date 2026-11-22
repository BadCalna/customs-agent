"""HTTP + worker acceptance across the A/C boundary using offline fixtures."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client
from django.utils import timezone

from apps.agent_debug.contracts import digest
from apps.agent_debug.maintenance import cleanup
from apps.agent_debug.models import EventOutbox, TracePayload
from apps.agent_debug.transport import TransportResult
from apps.agent_debug.worker import work_once

from .fixture import make_published

API = "/api/v1/ops/debug/"


def _client(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    return client


def _post(client, path, body, expected, key=None):
    response = client.post(API + path, body, content_type="application/json",
                           headers={"X-CSRFToken": client.cookies["csrftoken"].value,
                                    "Idempotency-Key": key or str(uuid4())})
    assert response.status_code == expected, response.json()
    assert "no-store" in response["Cache-Control"]
    data = response.json()
    assert response["X-Request-ID"]
    if "request_id" in data:
        assert response["X-Request-ID"] == data["request_id"]
    return data


@pytest.mark.django_db(transaction=True)
def test_admin_probe_to_user_live_result_and_revoked_visibility(settings, tmp_path, user):
    published, _, map_root = make_published(tmp_path)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(map_root.parent)
    manager = get_user_model().objects.create_user(username="connection-manager")
    manage = Permission.objects.get(content_type__app_label="agent_debug", codename="manage_connections")
    manager.user_permissions.add(manage)
    user.user_permissions.remove(manage)
    admin, operator = _client(manager), _client(user)
    assert admin.get("/ops/debug/connections/").status_code == 200
    assert operator.get("/ops/debug/").status_code == 200
    assert operator.get(API + "connections").status_code == 403

    calls = []

    def transport(endpoint, wire, key):
        assert endpoint.target == "local" and key is None
        request = json.loads(wire)
        calls.append(wire)
        if len(calls) == 1:
            assert "测试样例" not in wire.decode()
            text = '{"ok":true}'
        else:
            packet = json.loads(request["messages"][1]["content"])
            assert packet["facts"]["name"] == "测试样例"
            text = '{"candidates":[],"suggested_hs":null,"missing_facts":["material"]}'
        return TransportResult(text=text, http_status=200, model=request["model"])

    connection = _post(admin, "connections", {"alias": "本机测试", "endpoint_id": "loopback-test",
                       "model": "debug-model", "auth_mode": "none"}, 201)
    connection_path = f"connections/{connection['id']}"
    assert calls == []
    probe = _post(admin, f"{connection_path}/test", {}, 202)
    assert work_once(transport=transport)
    assert admin.get(API + f"probes/{probe['probe_id']}").json()["status"] == "succeeded"
    _post(admin, f"{connection_path}/activate", {"expected_default_version": 0}, 200)

    for mode, sample in (("mock", "demo"), ("live", "fixture-demo")):
        before = len(calls)
        preview = _post(operator, "previews", {"mode": mode, "sample_id": sample}, 201)
        assert len(calls) == before
        body = {"ticket": preview["ticket"], "confirm_external": False}
        key = str(uuid4())
        queued = _post(operator, "runs", body, 202, key)
        run_path = f"runs/{queued['id']}"
        assert operator.get(API + run_path).json()["waiting"]
        assert work_once(transport=transport)
        assert _post(operator, "runs", body, 202, key)["id"] == queued["id"]
        assert not work_once(transport=transport)
        d = operator.get(API + run_path).json()
        assert d["status"] == "succeeded"
        count = int(mode == "live")
        assert len(calls) == before + count
        assert d["outbound_counts"] == {"attempts": count, "responses": count, "parsed": count}
        if mode == "live":
            # Displayed packet is the exact object that was serialized and sent.
            assert digest(calls[-1]) == preview["packet_digest"]
            assert json.loads(calls[-1]) == preview["redacted_packet"]
            assert d["business_status"] == "needs_facts"

    assert admin.get(API + run_path).status_code == 403
    steps = operator.get(API + f"{run_path}/steps").json()["items"]
    assert [s["kind"] for s in steps] == ["prepare", "model", "parse", "postcheck"]
    payload_path = API + f"{run_path}/steps/2/payload"
    payload = operator.get(payload_path)
    assert payload.status_code == 200 and payload.json()["payload_state"] == "available"
    assert "测试样例".encode() not in payload.content
    assert b"debug-model" in payload.content
    assert EventOutbox.objects.filter(envelope__request_id=payload["X-Request-ID"],
                                      envelope__event_type="debug.trace.viewed").exists()
    trace = TracePayload.objects.get(step__run_id=queued["id"])
    assert "测试样例" not in trace.ciphertext
    TracePayload.objects.filter(pk=trace.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert cleanup(dry_run=True)["traces_redacted"] == 1
    assert cleanup()["traces_redacted"] == 1
    trace.refresh_from_db()
    assert not trace.ciphertext
    assert operator.get(payload_path).json()["payload_state"] == "expired"

    user.user_permissions.remove(Permission.objects.get(content_type__app_label="reference_browser", codename="view_catalog"))
    assert operator.get(payload_path).status_code == 403
    stub = operator.get(API + run_path).json()
    assert not stub["content_visible"] and not stub["materials"] and "candidates" not in stub
