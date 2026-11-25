"""Expert debug HTTP/worker acceptance with isolated contract data, never real customs truth."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client

from apps.agent_debug.contracts import digest
from apps.agent_debug.errors import DebugError
from apps.agent_debug.models import DebugRun
from apps.agent_debug.transport import TransportResult
from apps.agent_debug.worker import work_once
from apps.reviews.models import Task
from tests.p16_real.test_reference_bundle import HS

from .fixture import make_published
from .test_acceptance import _connection
from .test_workflow import API, _client, _post

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def client(user):
    value = _client(user)
    assert value.get("/ops/debug/").status_code == 200
    return value


def published(settings, tmp_path, *, reviewed=True):
    path, _, maps = make_published(tmp_path, reviewed=reviewed)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(path.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(maps.parent)
    _connection()
    return path


def expert_input(*, mode="live", elements="用途:工业测试", basis=True):
    body = {
        "stage": "expert_check", "mode": mode,
        "facts": {"name": "核查测试商品", "attributes": {"material": "塑料"}, "elements_text": elements},
        "expert": {"submitted_hs": HS, "supplier_tax_rate_raw": "0%", "direction": "export"},
    }
    if basis:
        body["query_basis"] = {"declaration_date": "2026-11-18"}
    return body


def queue(client, body):
    preview = _post(client, "previews", body, 201)
    run_body = {"ticket": preview["ticket"], "confirm_external": False}
    key = str(uuid4())
    queued = _post(client, "runs", run_body, 202, key)
    return preview, queued, run_body, key


def detail(client, queued):
    response = client.get(API + f"runs/{queued['id']}")
    assert response.status_code == 200
    return response.json()


def neutral(**changes):
    return {"elements_semantic_conflict": False, "elements_conflict_reason": "",
            "image_verdict": "unknown", "image_reason": "图片未接入", "suggested_hs": None, **changes}


def test_expert_mock_is_four_aspects_zero_dispatch_and_no_business_write(client):
    before = Task.objects.count()
    preview, queued, body, key = queue(client, {"stage": "expert_check", "mode": "mock", "sample_id": "expert-demo"})
    assert preview["stage"] == "expert_check" and preview["model_required"] is False
    calls = []
    assert work_once(transport=lambda *args: calls.append(args))
    assert calls == []
    d = detail(client, queued)
    assert d["stage"] == "expert_check" and d["status"] == "succeeded"
    assert {f["aspect"] for f in d["findings"]} == {"elements", "images", "tax", "regulatory"}
    assert next(f for f in d["findings"] if f["aspect"] == "images")["verdict"] == "unknown"
    assert d["business_status"] == "undetermined"
    assert d["outbound_counts"] == {"attempts": 0, "responses": 0, "parsed": 0}
    steps = client.get(API + f"runs/{queued['id']}/steps").json()["items"]
    assert [row["kind"] for row in steps] == ["prepare", "mock", "postcheck"]
    assert steps[1]["finished_at"] <= steps[2]["started_at"]
    assert _post(client, "runs", body, 202, key)["id"] == queued["id"]
    assert not work_once(transport=lambda *args: calls.append(args))
    assert Task.objects.count() == before


def test_precheck_default_remains_compatible_and_stage_filter_keeps_legacy(client):
    preview, queued, _, _ = queue(client, {"mode": "mock", "sample_id": "demo"})
    assert "stage" not in preview  # Existing precheck preview contract and approval bytes are unchanged.
    assert work_once(transport=lambda *args: pytest.fail("mock dispatched"))
    d = detail(client, queued)
    assert d["stage"] == "precheck" and "candidates" in d and "findings" not in d
    run = DebugRun.objects.get(pk=queued["id"])
    legacy = dict(run.config_snapshot)
    legacy.pop("stage")
    DebugRun.objects.filter(pk=run.pk).update(config_snapshot=legacy)
    result = client.get(API + "runs?stage=precheck").json()
    assert queued["id"] in {row["id"] for row in result["items"]}
    assert not client.get(API + "runs?stage=expert_check").json()["items"]
    assert client.get(API + "runs?stage=invalid").status_code == 422


def test_live_expert_missing_elements_checks_sources_without_model(client, settings, tmp_path):
    published(settings, tmp_path)
    preview, queued, _, _ = queue(client, expert_input(elements=""))
    assert preview["model_required"] is False
    assert HS not in json.dumps(preview)
    assert "核查测试商品" not in json.dumps(preview, ensure_ascii=False)
    assert work_once(transport=lambda *args: pytest.fail("insufficient facts dispatched"))
    d = detail(client, queued)
    assert d["status"] == "succeeded" and d["business_status"] == "undetermined"
    assert d["target_hs"] == HS and len(d["findings"]) == 4
    assert d["outbound_counts"]["attempts"] == 0
    verdicts = {f["aspect"]: f["verdict"] for f in d["findings"]}
    assert verdicts["images"] == verdicts["regulatory"] == "unknown"
    assert verdicts["elements"] != "matched"
    steps = client.get(API + f"runs/{queued['id']}/steps").json()["items"]
    assert {s["kind"] for s in steps} == {"prepare", "postcheck"}


def test_live_expert_calls_exact_preview_once_and_redacts_provider_text(client, settings, tmp_path):
    published(settings, tmp_path)
    before = Task.objects.count()
    preview, queued, body, key = queue(client, expert_input())
    assert preview["model_required"] is True
    calls = []

    def transport(endpoint, wire, secret):
        calls.append(wire)
        assert endpoint.target == "local" and secret is None
        envelope = json.loads(wire)
        packet = json.loads(envelope["messages"][1]["content"])
        assert packet["stage"] == "expert_check" and packet["facts"]["submitted_hs"] == HS
        for private in ("submission_id", "task_id", "run_id", "source_cell", "reviewed_by"):
            assert private not in packet["facts"]
        return TransportResult(text=json.dumps(neutral(elements_semantic_conflict=True,
            elements_conflict_reason="PROVIDER-PRIVATE-FREE-TEXT", image_verdict="matched",
            image_reason="PROVIDER-PRIVATE-FREE-TEXT")), http_status=200, model=envelope["model"])

    assert work_once(transport=transport)
    assert len(calls) == 1 and digest(calls[0]) == preview["packet_digest"]
    assert len(calls[0]) == preview["request_bytes"]
    d = detail(client, queued)
    assert d["status"] == "succeeded" and d["target_hs"] == HS
    assert d["outbound_counts"] == {"attempts": 1, "responses": 1, "parsed": 1}
    assert {f["aspect"]: f["verdict"] for f in d["findings"]}["elements"] == "conflict"
    # Even a successful provider cannot manufacture an image check for an absent image.
    assert {f["aspect"]: f["verdict"] for f in d["findings"]}["images"] == "unknown"
    assert {m["role"] for m in d["materials"]} <= {"postcheck_only"}
    assert d["business_status"] == "undetermined"
    assert "PROVIDER-PRIVATE-FREE-TEXT" not in json.dumps(d)
    trace = client.get(API + f"runs/{queued['id']}/steps/2/payload").json()
    assert "PROVIDER-PRIVATE-FREE-TEXT" not in json.dumps(trace)
    assert "核查测试商品" not in json.dumps(trace, ensure_ascii=False)
    assert _post(client, "runs", body, 202, key)["id"] == queued["id"]
    assert not work_once(transport=transport) and len(calls) == 1
    assert Task.objects.count() == before


@pytest.mark.parametrize("failure,expected,state,received", [
    ("not-json", "OUTPUT_SCHEMA_INVALID", "failed", 1),
    ('{"elements_semantic_conflict": false, "unexpected": 1}', "OUTPUT_SCHEMA_INVALID", "failed", 1),
    ("timeout", "TIMEOUT", "interrupted", 0),
])
def test_expert_model_failure_is_technical_failure_without_replay(client, settings, tmp_path, failure, expected, state, received):
    published(settings, tmp_path)
    _, queued, _, _ = queue(client, expert_input())
    calls = []

    def transport(endpoint, wire, secret):
        calls.append(wire)
        if failure == "timeout":
            raise DebugError("TIMEOUT", 504)
        return TransportResult(text=failure, http_status=200, model="debug-model")

    assert work_once(transport=transport)
    d = detail(client, queued)
    assert d["status"] == state and d["error_code"] == expected
    assert d["business_status"] == "not_evaluated"
    assert d["outbound_counts"] == {"attempts": 1, "responses": received, "parsed": 0}
    assert not any(f["verdict"] == "matched" for f in d.get("findings", []))
    assert not work_once(transport=transport) and len(calls) == 1


@pytest.mark.parametrize("reviewed,basis", [(False, True), (True, False)])
def test_unreviewed_or_missing_basis_cannot_become_matched(client, settings, tmp_path, reviewed, basis):
    published(settings, tmp_path, reviewed=reviewed)
    preview, queued, _, _ = queue(client, expert_input(basis=basis))
    assert preview["model_required"] is False
    assert work_once(transport=lambda *args: pytest.fail("unverified source dispatched"))
    d = detail(client, queued)
    assert len(d["findings"]) == 4 and d["business_status"] == "undetermined"
    assert all(f["verdict"] == "unknown" for f in d["findings"])


def test_sample_and_parent_stage_are_immutable(client):
    _post(client, "previews", {"stage": "expert_check", "mode": "mock", "sample_id": "demo"}, 422)
    _post(client, "previews", {"mode": "mock", "sample_id": "expert-demo"}, 422)
    _post(client, "previews", {"stage": "expert_check", "mode": "mock", "sample_id": "expert-demo",
        "expert": {"submitted_hs": "9999999999"}}, 422)
    _, queued, _, _ = queue(client, {"stage": "expert_check", "mode": "mock", "sample_id": "expert-demo"})
    assert work_once(transport=lambda *args: pytest.fail("mock dispatched"))
    _post(client, "previews", {"stage": "precheck", "mode": "mock", "parent_run_id": queued["id"]}, 422)
    # Omitted stage keeps compatibility while inheriting the immutable parent input.
    preview = _post(client, "previews", {"mode": "mock", "parent_run_id": queued["id"]}, 201)
    assert preview["stage"] == "expert_check"
    new = _post(client, "runs", {"ticket": preview["ticket"], "parent_run_id": queued["id"]}, 202)
    assert work_once(transport=lambda *args: pytest.fail("mock rerun dispatched"))
    d = detail(client, new)
    assert d["stage"] == "expert_check" and d["target_hs"] == "8544422900"


def test_queued_expert_rechecks_pinned_release_before_dispatch(client, settings, tmp_path):
    release = published(settings, tmp_path)
    _, queued, _, _ = queue(client, expert_input())
    (release / "elements.json").write_text("{}", encoding="utf-8")
    assert work_once(transport=lambda *args: pytest.fail("tampered source dispatched"))
    d = detail(client, queued)
    assert d["status"] == "failed" and d["error_code"] == "REFERENCE_NOT_READY"
    assert d["business_status"] == "not_evaluated"
    assert d["outbound_counts"] == {"attempts": 0, "responses": 0, "parsed": 0}


def test_revoked_snapshot_permission_hides_expert_result_and_forbids_rerun(client, user, settings, tmp_path):
    published(settings, tmp_path)
    _, queued, _, _ = queue(client, expert_input(elements=""))
    assert work_once(transport=lambda *args: pytest.fail("source-only dispatched"))
    assert "findings" in detail(client, queued)
    user.user_permissions.remove(Permission.objects.get(content_type__app_label="reference_browser", codename="view_catalog"))
    d = detail(client, queued)
    assert d["content_visible"] is False and d["materials"] == []
    assert "findings" not in d and "target_hs" not in d
    _post(client, "previews", {"stage": "expert_check", "mode": "live", "parent_run_id": queued["id"]}, 403)


def test_expert_authorization_csrf_and_other_owner_are_enforced(client, user):
    anonymous = Client(enforce_csrf_checks=True)
    assert anonymous.get(API + "capabilities").status_code == 401
    assert client.post(API + "previews", json.dumps(expert_input(mode="mock")), content_type="application/json").status_code == 403
    _, queued, _, _ = queue(client, {"stage": "expert_check", "mode": "mock", "sample_id": "expert-demo"})
    other = get_user_model().objects.create_user(username="other-debug-user")
    other.user_permissions.add(Permission.objects.get(content_type__app_label="agent_debug", codename="use_debug"))
    second = _client(other)
    assert second.get("/ops/debug/").status_code == 200
    assert second.get(API + f"runs/{queued['id']}").status_code == 404
    _post(second, "previews", {"stage": "expert_check", "mode": "mock", "parent_run_id": queued["id"]}, 404)


@pytest.mark.parametrize("changes", [
    {"expert": {"submitted_hs": "０１０１２１００１０"}},
    {"expert": {"submitted_hs": "123"}},
    {"stage": "unsupported"},
    {"expert": {"submitted_hs": HS, "direction": "guess"}},
    {"expert": {"submitted_hs": HS, "supplier_tax_rate_raw": "password=secret"}},
])
def test_expert_bad_input_is_rejected_before_preview(client, changes):
    _post(client, "previews", {**expert_input(mode="mock"), **changes}, 422)


@pytest.mark.parametrize("raw", ["13％", "不予退税", "C-18", "未知"])
def test_supplier_rate_keeps_business_text_without_guessing(client, raw):
    body = expert_input(mode="mock")
    body["expert"]["supplier_tax_rate_raw"] = raw
    _post(client, "previews", body, 201)
