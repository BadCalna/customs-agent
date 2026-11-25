"""Expert debug contract tests; all sources are offline synthetic test fixtures."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from adapters.engines.expert_check import _strict_model_json
from adapters.engines.reference_bundle import ReferenceBundle, ReferenceBundleConfig, ReferenceBundleError
from apps.agent_debug.contracts import digest, json_bytes
from apps.agent_debug.errors import DebugError
from apps.agent_debug.expert import DebugExpertAdapter, payload_for, safe_result
from apps.agent_debug.materials import evidence_rows
from apps.api.schemas import EvidenceEntry, EvidenceStatus
from tests.p16_real.test_reference_bundle import HS, SOURCE, dataset, payloads, release_fixture


def snapshot(**expert):
    return {"stage": "expert_check", "facts": {"name": "测试商品", "attributes": {},
                                               "elements_text": "用途：试验用途"},
            "query_basis": {"declaration_date": "2026-11-18"},
            "expert": {"submitted_hs": HS, "supplier_tax_rate_raw": "0%",
                       "direction": "export", "rebate_code": "", **expert}}


def materials(tmp_path, reviewed=True):
    release, _ = release_fixture(tmp_path, reviewed=reviewed)
    return SimpleNamespace(bundle=ReferenceBundle.load(ReferenceBundleConfig(release_dir=release)))


def neutral(**kwargs):
    return {"elements_semantic_conflict": False, "elements_conflict_reason": "",
            "image_verdict": "unknown", "image_reason": "无图片", "suggested_hs": None, **kwargs}


def test_mock_has_exactly_four_aspects_no_model_and_no_image_match():
    source = snapshot()
    source["mock_scenario"] = "expert_tax_unknown"
    adapter = DebugExpertAdapter(source, "mock")
    model = Mock()
    payload = payload_for(source, "mock")
    assert payload.images == [] and payload.submission_id == ""
    assert not adapter.build_request(payload)["model_required"]
    output = adapter.execute(payload, model)
    model.complete.assert_not_called()
    result = safe_result(output)
    assert [f["aspect"] for f in result["findings"]] == ["elements", "images", "tax", "regulatory"]
    assert result["findings"][1]["verdict"] == "unknown"
    assert result["target_hs"] == HS


def test_live_unreviewed_does_not_call_model_or_claim_success(tmp_path):
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path, reviewed=False))
    payload = payload_for(source, "live")
    model = Mock()
    assert adapter.build_request(payload)["model_required"] is False
    result = safe_result(adapter.execute(payload, model))
    model.complete.assert_not_called()
    assert all(f["verdict"] != "matched" for f in result["findings"])
    assert result["findings"][3]["verdict"] == "unknown"


def test_live_preview_matches_exact_runtime_request_without_model_call(tmp_path):
    source = snapshot()
    before = deepcopy(source)
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path))
    payload = payload_for(source, "live")
    preview = adapter.build_request(payload)
    assert preview["model_required"] is True
    assert preview["stage"] == "expert_check"
    assert preview["facts"]["submitted_hs"] == HS
    assert "submission_id" not in preview["facts"]
    assert preview["reference_packet"]["evidence_index"] == []
    model = Mock()
    model.complete.return_value = neutral()
    result = safe_result(adapter.execute(payload, model))
    model.complete.assert_called_once()
    assert json_bytes(model.complete.call_args.args[0]) == json_bytes(preview)
    assert source == before
    assert result["target_hs"] == HS
    assert result["findings"][1]["verdict"] == "unknown"
    assert result["findings"][3]["verdict"] == "unknown"
    preview["facts"]["name"] = "tampered"
    assert adapter.build_request(payload)["facts"]["name"] == source["facts"]["name"]


def test_eight_digit_target_is_pending_not_automatically_padded(tmp_path):
    source = snapshot(submitted_hs=HS[:8])
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path))
    payload = payload_for(source, "live")
    result = safe_result(adapter.execute(payload))
    assert result["target_hs"] == HS[:8]
    assert result["suggested_hs"] is None
    assert all(f["verdict"] != "matched" for f in result["findings"])


@pytest.mark.parametrize("code", ["TIMEOUT", "PROVIDER_LIMIT", "AUTH_REJECTED"])
def test_model_technical_failure_is_not_swallowed_by_p18(tmp_path, code):
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path))
    payload = payload_for(source, "live")
    model = Mock()
    original = DebugError(code, 504)
    model.complete.side_effect = original
    with pytest.raises(DebugError) as failed:
        adapter.execute(payload, model)
    assert failed.value is original


@pytest.mark.parametrize("answer", [
    "not-json PRIVATE-PROVIDER-TEXT", neutral(extra="PRIVATE-PROVIDER-TEXT"),
    '{"elements_semantic_conflict":false,"elements_semantic_conflict":true}',
])
def test_bad_model_output_has_fixed_error_without_provider_text(tmp_path, answer):
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path))
    model = Mock()
    model.complete.return_value = answer
    with pytest.raises(DebugError) as failed:
        adapter.execute(payload_for(source, "live"), model)
    assert failed.value.code == "OUTPUT_SCHEMA_INVALID"
    assert "PRIVATE" not in str(failed.value)


def test_result_omits_model_and_source_free_text(tmp_path):
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", materials(tmp_path))
    model = Mock()
    model.complete.return_value = neutral(elements_semantic_conflict=True,
                                          elements_conflict_reason="PRIVATE-PROVIDER-TEXT")
    output = adapter.execute(payload_for(source, "live"), model)
    result = safe_result(output)
    assert result["findings"][0]["verdict"] == "conflict"
    assert b"PRIVATE" not in json_bytes(result)
    assert all(len(eid) == 64 for f in result["findings"] for eid in f["evidence_ids"])


def test_missing_regulatory_source_never_reads_environment(tmp_path):
    source = snapshot()
    prepared = materials(tmp_path, reviewed=False)
    with patch("adapters.sources.regulatory_lookup.EnvRegulatoryStore", side_effect=AssertionError("ENV")), \
         patch("adapters.sources.history_search.default_history_store", side_effect=AssertionError("FIXTURE")), \
         patch("socket.create_connection", side_effect=AssertionError("NETWORK")):
        adapter = DebugExpertAdapter(source, "live", prepared)
        result = safe_result(adapter.execute(payload_for(source, "live")))
    assert result["findings"][3]["verdict"] == "unknown"
    assert any(g["code"].startswith("REG_") for g in result["gaps"])


def test_source_exception_is_structured_unknown_without_source_diagnostics(tmp_path):
    source = snapshot()
    prepared = materials(tmp_path)
    prepared.bundle.elements_source.check = Mock(side_effect=RuntimeError("password=PRIVATE-SOURCE"))
    adapter = DebugExpertAdapter(source, "live", prepared)
    payload = payload_for(source, "live")
    model = Mock()
    assert not adapter.build_request(payload)["model_required"]
    result = safe_result(adapter.execute(payload, model))
    model.complete.assert_not_called()
    assert result["evidence_status"] == "query_failed"
    assert all(f["verdict"] == "unknown" for f in result["findings"])
    assert b"PRIVATE" not in json_bytes(result)


@pytest.mark.parametrize("value", ["123", "１２３４５６７８９０", "HS0101210010", "01012100100"])
def test_invalid_target_refused_before_source_access(value):
    with pytest.raises(DebugError) as failed:
        payload_for(snapshot(submitted_hs=value), "live")
    assert failed.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("field,value", [("name", "password=private"), ("elements_text", "C:\\private\\file")])
def test_sensitive_facts_refused(field, value):
    source = snapshot()
    source["facts"][field] = value
    with pytest.raises(DebugError) as failed:
        payload_for(source, "live")
    assert failed.value.code == "INVALID_INPUT"


def test_input_cannot_be_switched_after_preview():
    source = snapshot()
    adapter = DebugExpertAdapter(source, "mock")
    payload = payload_for(source, "mock")
    payload.submitted_hs = "9999999999"
    with pytest.raises(DebugError) as failed:
        adapter.execute(payload)
    assert failed.value.code == "PREVIEW_STALE"


def test_elements_with_tariff_label_and_same_locator_never_link_to_hs():
    """An official source label is not a document identity or a source scope."""
    ref = {"kind": "hs", "snapshot_id": "unit-snapshot", "record_id": "hs-record",
           "anchor": None, "label": "税则目录条目"}
    prepared = SimpleNamespace(bundle=SimpleNamespace(version="2026"), index={("hs", "same-cell"): ref})
    entry = EvidenceEntry(evidence_id=f"elements:2026:{HS}", source_type="official_tariff",
                          title="elements", status=EvidenceStatus.OBTAINED,
                          document="elements.xlsx", locator="same-cell")
    output = SimpleNamespace(target_hs=HS, evidence=[entry])
    rows = list(evidence_rows(prepared, {"stage": "expert_check"}, set(), output, sent=True))
    assert rows == [("postcheck_only", "elements", digest(entry.evidence_id), "unverified", None)]


@pytest.mark.parametrize("identity,source_type,located", [
    (f"hs:2026:{HS}", "official_tariff", True),
    (f"hs:2025:{HS}", "official_tariff", False),
    ("hs:2026:9999999999", "official_tariff", False),
    (f"hs:2026:{HS}", "third_party", False),
])
def test_hs_evidence_navigation_requires_exact_target_version_scope(identity, source_type, located):
    ref = {"kind": "hs", "snapshot_id": "unit-snapshot", "record_id": "hs-record",
           "anchor": None, "label": "税则目录条目"}
    prepared = SimpleNamespace(bundle=SimpleNamespace(version="2026"), index={("hs", "same-cell"): ref})
    entry = EvidenceEntry(evidence_id=identity, source_type=source_type, title="hs",
                          status=EvidenceStatus.OBTAINED, document="tariff.xlsx", locator="same-cell")
    output = SimpleNamespace(target_hs=HS, evidence=[entry])
    rows = list(evidence_rows(prepared, {"stage": "expert_check"}, set(), output, sent=True))
    assert len(rows) == 1
    role, kind, key, status, target = rows[0]
    assert role == "postcheck_only" and kind == "hs"
    assert status == ("located" if located else "unverified")
    assert target == (ref if located else None)


def regulatory_data(*, reviewed=True):
    """Official-shaped, real-labelled metadata simulates the gate, never real data."""
    data = payloads(reviewed=reviewed)
    data["p17_regulatory"] = {
        "schema_version": "p17.regulatory.1",
        "dataset": {**dataset("p17_regulatory", reviewed), "source_type": "official_regulatory"},
        "versions": deepcopy(data["p14_tariff"]["versions"]),
        "records": [{"hs_code": HS, "version": "2026", "direction": "export", "state": "in_force",
                     "effective_from": "2026-11-16", "effective_to": "2026-12-31",
                     "measures": [], "source": {**SOURCE, "document": "UNIT-REGULATORY-NOT-REAL",
                                                "locator": "reg-row-1"}}],
    }
    return data


def test_reviewed_regulatory_source_comes_from_the_same_validated_release(tmp_path):
    path, release = release_fixture(tmp_path, data=regulatory_data(), reviewed=True)
    bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
    assert bundle.regulatory_source is not None and bundle.regulatory_store is not None
    assert bundle.source_metadata["p17_regulatory"]["sha256"] == release["files"]["p17_regulatory"]["sha256"]
    assert bundle.source_metadata["p17_regulatory"]["authoritative"] is True
    assert bundle.source_metadata["p17_regulatory"]["versions"] == ["2026"]
    source = snapshot()
    model = Mock()
    model.complete.return_value = neutral()
    adapter = DebugExpertAdapter(source, "live", SimpleNamespace(bundle=bundle))
    result = safe_result(adapter.execute(payload_for(source, "live"), model))
    findings = {f["aspect"]: f for f in result["findings"]}
    assert findings["regulatory"]["verdict"] == "matched"
    assert findings["regulatory"]["evidence_ids"]


def test_regulatory_snapshot_stays_frozen_after_disk_changes(tmp_path):
    path, _ = release_fixture(tmp_path, data=regulatory_data(), reviewed=True)
    bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
    before = bundle.regulatory_store.load()
    (path / "regulatory.json").write_text("{}", encoding="utf-8")
    # The loaded release is pinned; returned dataclasses are deep copies too.
    assert bundle.regulatory_store.load() == before
    returned = bundle.regulatory_store.load()
    returned.records[0].note = "mutated"
    assert bundle.regulatory_store.load() == before
    with pytest.raises(ReferenceBundleError):
        ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))


def test_unreviewed_regulatory_conditions_remain_unknown(tmp_path):
    path, _ = release_fixture(tmp_path, data=regulatory_data(reviewed=False))
    bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
    assert bundle.regulatory_source is not None
    assert bundle.source_metadata["p17_regulatory"]["authoritative"] is False
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", SimpleNamespace(bundle=bundle))
    result = safe_result(adapter.execute(payload_for(source, "live")))
    assert next(f for f in result["findings"] if f["aspect"] == "regulatory")["verdict"] == "unknown"


def test_regulatory_code_ledger_is_not_a_regulatory_condition_source(tmp_path):
    data = payloads(reviewed=True)
    data["regulatory_code_ledger"] = {
        "schema_version": "p23.regulatory-codes.1",
        "dataset": {**dataset("regulatory_code_ledger", reviewed=True),
                    "source_type": "official_regulatory_codes_only"},
        "records": [{"hs_code": HS, "regulatory_codes": "A", "measures": []}],
    }
    path, _ = release_fixture(tmp_path, data=data, reviewed=True)
    bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
    assert bundle.regulatory_source is None and bundle.regulatory_store is None
    source = snapshot()
    adapter = DebugExpertAdapter(source, "live", SimpleNamespace(bundle=bundle))
    model = Mock()
    model.complete.return_value = neutral()
    result = safe_result(adapter.execute(payload_for(source, "live"), model))
    assert next(f for f in result["findings"] if f["aspect"] == "regulatory")["verdict"] == "unknown"


@pytest.mark.parametrize("problem", ["schema", "records", "direction", "version"])
def test_invalid_regulatory_snapshot_rejected_even_with_valid_release_hash(tmp_path, problem):
    data = regulatory_data()
    reg = data["p17_regulatory"]
    if problem == "schema":
        reg["schema_version"] = "unknown"
    elif problem == "records":
        del reg["records"]
    elif problem == "direction":
        reg["records"][0]["direction"] = "unknown"
    else:
        reg["records"][0]["version"] = "2025"
    path, _ = release_fixture(tmp_path, data=data, reviewed=True)
    with pytest.raises(ReferenceBundleError, match="p17_regulatory: snapshot contract invalid"):
        ReferenceBundle.load(ReferenceBundleConfig(release_dir=path))
