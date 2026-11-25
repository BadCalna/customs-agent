"""Text-only expert debugging around P18, with no implicit sources or dispatch.

The preview records P18's actual model request using a neutral local recorder.
Execution uses the same frozen source responses and wraps ``complete`` with the
same packet decoration. A source-only check never needs a provider request.
"""
from __future__ import annotations

import re
from copy import deepcopy

from pydantic import ValidationError

from adapters.engines.expert_check import (
    ENGINE_VERSION, EXPERT_ASPECTS, PROMPT_VERSION, RULES_VERSION, SYSTEM_PROMPT,
    ExpertCheckEngine, ModelExpertJudgment, _strict_model_json,
)
from adapters.engines.mock import MockEngine
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput
from adapters.engines.skill_precheck import _safe_fact_text
from adapters.sources.hs_lookup import QueryBasis
from adapters.sources.regulatory_lookup import RegulatorySource
from adapters.sources.tax_lookup import SourceStoreNotConfigured
from apps.api.schemas import ExpertFinding, ExpertVerdict, IssueEntry, ResultMode

from .contracts import Basis, Facts, digest, json_bytes
from .errors import DebugError


_NEUTRAL = {
    "elements_semantic_conflict": False, "elements_conflict_reason": "",
    "image_verdict": "unknown", "image_reason": "本次未接入图片事实",
    "suggested_hs": None,
}
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_FIELDS = frozenset({"submitted_hs", "elements_text", "images", "supplier_tax_rate_raw",
                     "regulatory", "tax", "expert_check", "direction", "query_basis"})


def payload_for(snapshot, mode):
    """Build an expert-only DTO; debug IDs never become business identifiers."""
    try:
        facts = Facts.model_validate(snapshot["facts"]).model_dump()
        expert = snapshot["expert"]
        submitted_hs = expert["submitted_hs"]
        if not isinstance(submitted_hs, str) or not re.fullmatch(r"[0-9]{8}(?:[0-9]{2})?", submitted_hs):
            raise ValueError
        if expert.get("direction", "unknown") not in {"unknown", "import", "export"}:
            raise ValueError
        rate = expert.get("supplier_tax_rate_raw", "")
        rebate = expert.get("rebate_code", "")
        if not isinstance(rate, str) or len(rate) > 32 or not isinstance(rebate, str) or len(rebate) > 24:
            raise ValueError
        for value in (facts["name"], facts["elements_text"], *facts["attributes"].values(), rate, rebate):
            _safe_fact_text(value, ())
        Basis.model_validate(snapshot.get("query_basis", {}))
        return ExpertCheckInput(
            context=ExecutionContext(task_id="", version_id="", item_id="", run_id="", input_revision=1,
                                     result_mode=ResultMode(mode), mock_scenario=snapshot.get("mock_scenario")),
            submission_id="", submitted_hs=submitted_hs, supplier_tax_rate_raw=rate, images=[], **facts,
        )
    except (KeyError, ValueError, TypeError, RuntimeError, ValidationError):
        raise DebugError("INVALID_INPUT", 422) from None


class _UnavailableRegulatoryStore:
    def load(self):
        raise SourceStoreNotConfigured("调试快照未提供监管条件来源；不能判定无监管")


class _FrozenSource:
    """Keep query identity and source timestamps unchanged between preview/run."""
    def __init__(self, source, method):
        self.source, self.method, self.cache = source, method, {}

    def _query(self, query):
        key = json_bytes(query.model_dump(mode="json"))
        if key not in self.cache:
            self.cache[key] = getattr(self.source, self.method)(query.model_copy(deep=True))
        return deepcopy(self.cache[key])

    def check(self, query):
        return self._query(query)

    def lookup(self, query):
        return self._query(query)


def _packet(request, *, required):
    result = deepcopy(request)
    result.update(stage="expert_check", model_required=required)
    # P18 sends source status summaries, not source texts or locators. Those
    # sources appear only as postcheck evidence; never label them model-cited.
    result["reference_packet"] = {"evidence_index": [], "commentary": {"records": []}}
    return result


class _Recorder:
    def __init__(self):
        self.request = None

    def complete(self, request):
        if self.request is not None:
            raise DebugError("INTERNAL_ERROR", 500)
        self.request = _packet(request, required=True)
        return deepcopy(_NEUTRAL)


class _Forward:
    def __init__(self, model):
        self.model, self.error = model, None

    def complete(self, request):
        try:
            raw = self.model.complete(_packet(request, required=True))
            return _strict_model_json(raw).model_dump()
        except DebugError as exc:
            self.error = exc
        except (ValueError, TypeError, ValidationError):
            self.error = DebugError("OUTPUT_SCHEMA_INVALID", 502, received=True)
        except Exception:
            self.error = DebugError("INTERNAL_ERROR", 500)
        raise self.error from None


class DebugExpertAdapter:
    engine_version = "debug-" + ENGINE_VERSION
    prompt_version = PROMPT_VERSION
    rules_version = RULES_VERSION + "-debug-text-1"

    def __init__(self, snapshot, mode, materials=None):
        self.snapshot = deepcopy(snapshot)
        self.mode = mode
        self._preview = None
        if mode == "mock":
            self.engine = MockEngine()
            self.engine_version = "debug-expert-mock-1"
        elif mode == "live" and materials is not None:
            bundle = materials.bundle
            regulatory_source = getattr(bundle, "regulatory_source", None)
            # No environment fallback, default history, or code-ledger-as-law.
            self.engine = ExpertCheckEngine(
                elements_source=_FrozenSource(bundle.elements_source, "check"),
                hs_source=_FrozenSource(bundle.hs_source, "lookup"),
                tax_source=_FrozenSource(bundle.tax_source, "lookup"),
                regulatory_source=_FrozenSource(
                    regulatory_source if regulatory_source is not None else
                    RegulatorySource(_UnavailableRegulatoryStore()), "lookup"),
                history_source=None,
            )
        else:
            raise DebugError("REFERENCE_NOT_READY")

    def _check(self, payload, model):
        self.engine.model = model
        return self.engine.expert_check(
            payload.model_copy(deep=True),
            basis=QueryBasis(**self.snapshot.get("query_basis", {})),
            direction=self.snapshot["expert"].get("direction", "unknown"),
            rebate_code=self.snapshot["expert"].get("rebate_code", ""),
        )

    def build_request(self, payload):
        if self._preview is None:
            recorder = _Recorder()
            if self.mode == "live":
                self._check(payload, recorder)
            self._preview = recorder.request or _packet({
                "system": SYSTEM_PROMPT, "facts": {}, "source_summary": {},
                "history_reference": [], "response_schema": ModelExpertJudgment.model_json_schema(),
            }, required=False)
        return deepcopy(self._preview)

    def execute(self, payload, model=None):
        # Revalidate even when called without the Web input schema.
        expected = payload_for(self.snapshot, self.mode)
        if payload.model_dump() != expected.model_dump():
            raise DebugError("PREVIEW_STALE")
        if self.mode == "mock":
            output = self.engine.expert_check(payload.model_copy(deep=True))
            # Mock scenarios may manufacture image matches; this module has no
            # image input, so every scenario must preserve image UNKNOWN.
            output.findings = [f if f.aspect != "images" else ExpertFinding(
                aspect="images", verdict=ExpertVerdict.UNKNOWN,
                reason="本次未接入图片事实，保持待核实", evidence_ids=[],
            ) for f in output.findings]
            output.issues.append(IssueEntry(code="IMAGES_NOT_CONNECTED", field="images", message="图片未接入"))
            return output
        packet = self.build_request(payload)
        if packet["model_required"] and model is None:
            raise DebugError("CONNECTION_NOT_READY")
        forward = _Forward(model) if model is not None else None
        output = self._check(payload, forward)
        # P18 deliberately degrades model errors into UNKNOWN. The debug worker
        # must additionally retain the actual technical error/dispatch outcome.
        if forward is not None and forward.error is not None:
            raise forward.error
        return output


def safe_result(output):
    """Only fixed messages, validated codes and opaque evidence handles persist."""
    labels = {"elements": "申报要素", "images": "图片", "tax": "退税", "regulatory": "监管"}
    verdicts = {"matched": "来源核对相符，仍需人工复核", "conflict": "发现冲突，请人工核对",
                "unknown": "资料或适用条件待核实，不等于无风险"}
    findings = []
    for aspect in EXPERT_ASPECTS:
        found = next((f for f in output.findings if f.aspect == aspect), None)
        verdict = found.verdict.value if found is not None and aspect != "images" else "unknown"
        findings.append({"aspect": aspect, "verdict": verdict,
                         "reason": "图片未接入，本项待核实" if aspect == "images" else labels[aspect] + "：" + verdicts[verdict],
                         "evidence_ids": [digest(eid) for eid in found.evidence_ids]
                         if found and aspect != "images" else []})
    gaps = []
    for item in output.issues:
        code = item.code if isinstance(item.code, str) and _CODE.fullmatch(item.code) else "EXPERT_CHECK_PENDING"
        field = item.field if item.field in _FIELDS else None
        message = ("图片未接入，本项待核实" if code.startswith("IMAGE") else
                   "退税来源或适用条件待核实" if code.startswith("TAX") else
                   "监管来源或适用条件待核实" if code.startswith("REG") else
                   "核查存在待核事项，请核对分项结果和来源")
        value = {"code": code, "field": field, "message": message}
        if value not in gaps:
            gaps.append(value)
    return {"stage": "expert_check", "target_hs": output.target_hs,
            "suggested_hs": output.suggested_hs, "findings": findings, "gaps": gaps,
            "evidence_status": output.evidence_status.value if output.evidence_status else None}
