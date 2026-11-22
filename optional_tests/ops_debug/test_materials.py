from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission
from django.utils import timezone

from apps.agent_debug.contracts import PreviewIn, RunIn, digest
from apps.agent_debug.errors import DebugError
from apps.agent_debug.connections import revoke
from apps.agent_debug.maintenance import cleanup
from apps.agent_debug.materials import live_materials, reference_url
from apps.agent_debug.models import ConnectionRevision, DebugDefault, DebugRun, DebugStep, RevisionState, State
from apps.agent_debug.services import create_preview, create_run, external_packet_digest
from apps.agent_debug.transport import TransportResult
from apps.agent_debug.worker import work_once

from .fixture import make_published

pytestmark = pytest.mark.django_db(transaction=True)


def _catalog_user(user):
    ct, _ = ContentType.objects.get_or_create(app_label="reference_browser", model="catalog")
    perm, _ = Permission.objects.get_or_create(content_type=ct, codename="view_catalog", defaults={"name": "View catalog"})
    user.user_permissions.add(perm)
    return user


def _live_connection(settings, *, target="external"):
    from apps.agent_debug.security import get_endpoint
    endpoint_id = "external-test" if target == "external" else "loopback-test"
    ep = get_endpoint(endpoint_id, "debug-model")
    connection = ConnectionRevision.objects.create(alias=f"fixture-{target}", revision=1, endpoint_id=ep.id,
        endpoint_fingerprint=ep.fingerprint, model="debug-model", auth_mode=ep.auth_mode, status=RevisionState.ACTIVE)
    DebugDefault.objects.update_or_create(id=1, defaults={"connection": connection, "version": 1})
    return connection


def test_b06_real_bundle_map_anchor_and_fixed_label(tmp_path, settings, user):
    user = _catalog_user(user)
    published, release, map_root = make_published(tmp_path)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(map_root.parent)
    mats = live_materials(user)
    assert mats.snapshot_id == release["snapshot_id"]
    assert mats.map_digest == hashlib.sha256((map_root / "evidence-map.v1.jsonl").read_bytes()).hexdigest()
    ref = mats.index[("note", "0101#1")]
    assert ref["record_id"] == "note-ch84" and ref["label"] == "注释段落"
    assert reference_url(ref).startswith("/ops/data/evidence?")


def test_b06_missing_map_is_unverified_and_does_not_guess_latest(tmp_path, settings, user):
    user = _catalog_user(user)
    published, release, map_root = make_published(tmp_path)
    (map_root / "evidence-map.v1.jsonl").unlink()
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(map_root.parent)
    mats = live_materials(user)
    assert mats.index == {} and mats.map_digest == ""


def test_b07_external_exact_approval_uses_packet_digest_only(tmp_path, settings, user):
    user = _catalog_user(user)
    published, release, map_root = make_published(tmp_path)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(map_root.parent)
    _live_connection(settings, target="external")
    settings.OPS_DEBUG_EXTERNAL_PACKET_DIGESTS = {"external-test": []}
    with pytest.raises(DebugError) as exc:
        create_preview(user, PreviewIn.model_validate({"mode": "live", "sample_id": "fixture-demo"}))
    assert exc.value.code == "EXTERNAL_NOT_APPROVED"
    # A digest is only eligible if it is calculated from these exact bytes.
    expected = external_packet_digest("fixture-demo", "external-test", "debug-model")
    settings.OPS_DEBUG_EXTERNAL_PACKET_DIGESTS = {"external-test": [expected]}
    preview = create_preview(user, PreviewIn.model_validate({"mode": "live", "sample_id": "fixture-demo"}))
    assert preview["packet_digest"] == expected


def test_b04_pinned_bundle_changes_make_run_stale(tmp_path, settings, user):
    user = _catalog_user(user)
    published, release, map_root = make_published(tmp_path)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(map_root.parent)
    _live_connection(settings, target="external")
    expected = external_packet_digest("fixture-demo", "external-test", "debug-model")
    settings.OPS_DEBUG_EXTERNAL_PACKET_DIGESTS = {"external-test": [expected]}
    preview = create_preview(user, PreviewIn.model_validate({"mode": "live", "sample_id": "fixture-demo"}))
    # Altering the map after preview changes the frozen map digest; consuming must fail stale.
    map_file = map_root / "evidence-map.v1.jsonl"
    map_file.write_bytes(map_file.read_bytes() + b"\n")
    with pytest.raises(DebugError) as exc:
        create_run(user, RunIn.model_validate({"ticket": preview["ticket"], "confirm_external": True}), digest("stale-live-idem"), __import__("uuid").uuid4())
    assert exc.value.code == "PREVIEW_STALE"


@pytest.fixture
def local_live(tmp_path, settings, user):
    published, _, maps = make_published(tmp_path)
    settings.OPS_DEBUG_PUBLISHED_DIR = str(published.parent.parent)
    settings.OPS_DEBUG_EVIDENCE_MAP_ROOT = str(maps.parent)
    return _live_connection(settings, target="local")


def _enqueue(user):
    preview = create_preview(user, PreviewIn(mode="live", sample_id="fixture-demo"))
    return create_run(user, RunIn(ticket=preview["ticket"], confirm_external=False), digest(str(uuid4())), uuid4())


@pytest.mark.parametrize(("failure", "code", "received", "terminal"), [
    ("not-json", "OUTPUT_SCHEMA_INVALID", True, State.FAILED),
    ("fake-citation", "OUTPUT_SCHEMA_INVALID", True, State.FAILED),
    ("timeout", "TIMEOUT", False, State.INTERRUPTED),
    ("oversize", "RESPONSE_TOO_LARGE", True, State.FAILED),
])
def test_b03_live_failure_never_resends(user, local_live, failure, code, received, terminal):
    calls = []

    def transport(*args):
        calls.append(args)
        if failure == "timeout":
            raise DebugError("TIMEOUT", 504)
        text = {
            "not-json": "TEST_ONLY_SENTINEL",
            "fake-citation": json.dumps({"candidates": [{"hs": "0101210010", "evidence_ids": ["forged"]}],
                                         "suggested_hs": None, "missing_facts": []}),
            "oversize": "X" * 65537,
        }[failure]
        return TransportResult(text=text, http_status=200, model="debug-model")

    run = _enqueue(user)
    assert work_once(transport=transport) and not work_once(transport=transport)
    run.refresh_from_db()
    step = run.steps.get(kind="model")
    assert len(calls) == 1 and step.attempt_no == 1 and step.received is received
    assert (run.status, run.error_code) == (terminal, code)
    assert not run.materials.filter(role="cited_by_model").exists()
    assert "TEST_ONLY_SENTINEL" not in json.dumps(run.result)


def test_b04_revoked_and_expired_claims_never_send(user, local_live):
    run = _enqueue(user)
    revoke(user, local_live.pk, 1, uuid4())
    calls = []
    assert work_once(transport=lambda *args: calls.append(args))
    run.refresh_from_db()
    assert run.error_code == "CONNECTION_REVOKED" and calls == []

    # Simulate a worker disappearing after persisting the one allowed dispatch.
    pending = create_preview(user, PreviewIn(mode="mock", sample_id="demo"))
    abandoned = create_run(user, RunIn(ticket=pending["ticket"], confirm_external=False), digest(str(uuid4())), uuid4())
    now = timezone.now()
    DebugRun.objects.filter(pk=abandoned.pk).update(status=State.RUNNING, started_at=now - timedelta(minutes=4),
                                                   lease_until=now - timedelta(seconds=1))
    DebugStep.objects.create(run=abandoned, seq=2, kind="model", status="dispatching", attempt_no=1,
                             started_at=now - timedelta(minutes=4))
    assert cleanup(dry_run=True)["runs_lease_expired"] == 1
    abandoned.refresh_from_db()
    assert abandoned.status == State.RUNNING
    assert cleanup()["runs_lease_expired"] == 1
    abandoned.refresh_from_db()
    assert abandoned.status == State.INTERRUPTED
    step = abandoned.steps.get(seq=2)
    assert step.attempt_no == 1 and not step.received and step.error_code == "WORKER_INTERRUPTED"
    assert not work_once(transport=lambda *args: calls.append(args)) and calls == []
