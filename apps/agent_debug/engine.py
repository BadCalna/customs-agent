"""The debug worker drives the existing precheck adapter, not a second prompt."""
from adapters.engines.mock import MockEngine
from adapters.engines.precheck import _strict_json
from adapters.engines.protocols import ExecutionContext, RecommendInput
from adapters.engines.skill_precheck import SkillPrecheckAdapter, _FACT_CODE, _safe_fact_text
from apps.api.schemas import ResultMode

from .contracts import digest, json_bytes
from .errors import DebugError
from .security import serialize_request


def payload_for(input_snapshot, mode):
    facts = input_snapshot["facts"]
    try:
        for text in (facts["name"], facts["elements_text"], *facts["attributes"].values()):
            if _FACT_CODE.search(_safe_fact_text(text, ())):
                raise ValueError
    except (RuntimeError, ValueError):
        raise DebugError("INVALID_INPUT", 422) from None
    return RecommendInput(
        context=ExecutionContext(task_id="", version_id="", item_id="", run_id="", input_revision=1,
                                 result_mode=ResultMode(mode), mock_scenario=input_snapshot.get("mock_scenario")),
        **facts, query_basis=input_snapshot["query_basis"],
    )


class PreviewOnly:
    def complete(self, request):
        raise RuntimeError("preview cannot dispatch")


def prepare(input_snapshot, mode, model=None, materials=None):
    payload = payload_for(input_snapshot, mode)
    if mode == "mock":
        adapter = MockEngine()
        body = json_bytes({"mode": "mock", "input": input_snapshot})
        return payload, adapter, None, body, versions(adapter)
    try:
        adapter = SkillPrecheckAdapter(model=PreviewOnly(), reference_bundle=materials.bundle)
        packet = adapter.build_request(payload)
    except RuntimeError:
        raise DebugError("REFERENCE_NOT_READY") from None
    return payload, adapter, packet, wire_bytes(model, packet), versions(adapter, materials, packet)


def wire_bytes(model, packet):
    # Strict serialization of the same packet presented to complete() by P16.
    body = {"messages": [{"role": "system", "content": packet["system"]},
                         {"role": "user", "content": json_bytes({k: v for k, v in packet.items()
                                                                  if k != "system"}).decode("utf-8")}],
            "temperature": 0}
    return serialize_request(model, body)


def versions(adapter, materials=None, packet=None):
    result = {name: getattr(adapter, name) for name in ("engine_version", "prompt_version", "rules_version")}
    if materials:
        result.update(bundle_id=materials.bundle.bundle_id, snapshot_id=materials.snapshot_id,
                      bundle_fingerprint=materials.bundle.fingerprint, map_digest=materials.map_digest,
                      rules_digest=packet["skill"]["selected_rules_sha256"], prompt_digest=digest(packet["system"]))
    return result


def parse_response(raw, packet):
    try:
        answer = _strict_json(raw)
    except RuntimeError:
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, received=True) from None
    known = {e["evidence_id"] for e in packet["reference_packet"]["evidence_index"]}
    cited = {ref for candidate in answer.candidates for ref in candidate.evidence_ids}
    if not cited <= known:
        raise DebugError("OUTPUT_SCHEMA_INVALID", 502, received=True)
    return answer, cited


def result_summary(output):
    """No provider text, supplier values, score, or source body enters result JSON."""
    return {"suggested_hs": output.suggested_hs,
            "candidates": [{"hs": c.hs, "reason": c.reason} for c in output.candidates],
            "gaps": [{"code": g.code, "field": g.field, "message": g.message} for g in output.gaps],
            "evidence_status": output.evidence_status.value if output.evidence_status else None}
