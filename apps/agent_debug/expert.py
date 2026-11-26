"""Text-only expert debugging around P18, with no implicit sources or dispatch.

The preview records P18's actual model request using a neutral local recorder.
Execution uses the same frozen source responses and wraps ``complete`` with the
same packet decoration. A source-only check never needs a provider request.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Literal

from ninja import Schema
from pydantic import Field, ValidationError

from adapters.engines.expert_check import (
    ENGINE_VERSION, EXPERT_ASPECTS, PROMPT_VERSION, RULES_VERSION, SYSTEM_PROMPT,
    ExpertCheckEngine, ModelExpertJudgment, _strict_model_json,
)
from adapters.engines.mock import MockEngine
from adapters.engines.protocols import ExecutionContext, ExpertCheckInput, ExpertCheckOutput
from adapters.engines.skill_precheck import _safe_fact_text
from adapters.sources.hs_lookup import (
    EffectiveStatus, QueryBasis, QueryStatus, classify_hs_format, resolve_version,
)
from adapters.sources.regulatory_lookup import Direction, RegulatoryOutcome, RegulatorySource
from adapters.sources.tax_lookup import (
    SourceStoreNotConfigured, TaxApplicability, TaxRateQuery, TaxRateResult,
    compare_rates, parse_original_rate,
)
from apps.api.schemas import EvidenceStatus, ExpertFinding, ExpertVerdict, IssueEntry, ResultMode

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
_POLICY_CODE = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")


class DebugTaxCheck(Schema):
    direction: Literal["unknown", "import", "export"] = "unknown"
    applicability: Literal["not_applicable", "direction_unknown", "pending", "applies"] = "pending"
    comparison: Literal["matched", "conflict", "unverified", "not_compared"] = "not_compared"
    resolved_version: str | None = None


class DebugRegulatoryMeasure(Schema):
    code: str
    category: Literal["licence", "inspection_quarantine", "dual_use", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list)


class DebugRegulatory(Schema):
    outcome: Literal["measures_required", "declared_none", "unknown"] = "unknown"
    verified: bool = False
    measures: list[DebugRegulatoryMeasure] = Field(default_factory=list)


class DebugExpertOutput(ExpertCheckOutput):
    """Debug-only structured policy facts; never added to the model response schema."""
    tax_check: DebugTaxCheck = Field(default_factory=DebugTaxCheck)
    regulatory: DebugRegulatory = Field(default_factory=DebugRegulatory)


def _policy_basis(basis):
    # P14 tariff versions and P17 policy versions belong to separate namespaces.
    # Without a declaration date neither policy source may infer applicability.
    return QueryBasis(declaration_date=basis.declaration_date)


def _initial_tax_summary(direction):
    value = direction.value if isinstance(direction, Direction) else direction
    return DebugTaxCheck(direction=value, applicability="not_applicable" if value == "import" else
                         "direction_unknown" if value == "unknown" else "pending")


class _DebugExpertEngine(ExpertCheckEngine):
    """Apply debug trade direction and independent P17 query bases around P18."""
    def __init__(self, *, direction, **kwargs):
        super().__init__(**kwargs)
        self.debug_direction = Direction(direction)
        self.tax_result = self.regulatory_result = None

    def _query_tax(self, payload, basis, rebate_code):
        policy_basis = _policy_basis(basis)
        if self.debug_direction is Direction.EXPORT:
            result = super()._query_tax(payload, policy_basis, rebate_code)
        else:
            # No query was issued. INVALID_INPUT + no evidence distinguishes this
            # direction gate from source failure/not-found and forbids comparison.
            reason = ("进口业务不适用出口退税核对，本项未比较" if self.debug_direction is Direction.IMPORT else
                      "贸易方向未确定，未执行出口退税比较")
            original = parse_original_rate(payload.supplier_tax_rate_raw)
            result = TaxRateResult(
                query=TaxRateQuery(hs_code_raw=payload.submitted_hs, rebate_code=rebate_code,
                                   basis=policy_basis, original_rate_raw=payload.supplier_tax_rate_raw),
                query_status=QueryStatus.INVALID_INPUT, evidence_status=None,
                format=classify_hs_format(payload.submitted_hs), version=resolve_version(policy_basis, ()),
                original_rate=original, rate_comparison=compare_rates(original, None),
                pending_reasons=[reason], reason=reason,
                issues=[IssueEntry(code="TAX_DIRECTION_NOT_APPLICABLE" if self.debug_direction is Direction.IMPORT else
                                   "TAX_DIRECTION_UNKNOWN", field="direction", message=reason, suggested_role="expert")],
            )
        self.tax_result = result.model_copy(deep=True)
        return result

    def _query_regulatory(self, payload, basis, direction):
        result = super()._query_regulatory(payload, _policy_basis(basis), direction)
        self.regulatory_result = result.model_copy(deep=True)
        return result

    def _tax_summary(self):
        summary = _initial_tax_summary(self.debug_direction)
        if self.debug_direction is not Direction.EXPORT:
            return summary
        result = self.tax_result
        if result is None:
            return summary
        if result.version.verified and _POLICY_CODE.fullmatch(result.version.version or ""):
            summary.resolved_version = result.version.version
        real = result.dataset is not None and result.dataset.synthetic is False
        if (real and result.authoritative and result.usable_as_basis and result.version.verified
                and result.applicability is TaxApplicability.APPLIES
                and result.effective_status is EffectiveStatus.EFFECTIVE):
            summary.applicability = "applies"
            summary.comparison = result.rate_comparison.status.value
        return summary

    def _regulatory_summary(self):
        result = self.regulatory_result
        if (result is None or result.dataset is None or result.dataset.synthetic is not False
                or not result.authoritative or not result.version.verified or not result.direction_matched
                or result.effective_status is not EffectiveStatus.EFFECTIVE or not result.usable_as_basis
                or result.expert_review_required or result.dual_use
                or result.outcome not in (RegulatoryOutcome.MEASURES_REQUIRED, RegulatoryOutcome.DECLARED_NONE)):
            return DebugRegulatory()
        measures = []
        for measure in result.measures:
            if not _POLICY_CODE.fullmatch(measure.code):
                return DebugRegulatory()
            matching = [e for e in result.evidence if e.document == measure.document and e.locator == measure.locator
                        and e.status is EvidenceStatus.OBTAINED and e.evidence_id.startswith("reg:")]
            if not matching:
                return DebugRegulatory()
            measures.append(DebugRegulatoryMeasure(code=measure.code, category=measure.category.value,
                                                   evidence_ids=[digest(e.evidence_id) for e in matching]))
        if result.outcome is RegulatoryOutcome.MEASURES_REQUIRED and not measures:
            return DebugRegulatory()
        return DebugRegulatory(outcome=result.outcome.value, verified=True, measures=measures)

    def expert_check(self, payload, **kwargs):
        self.tax_result = self.regulatory_result = None
        output = super().expert_check(payload, **kwargs)
        failed = any(i.code in {"EXPERT_CHECK_INTERNAL_ERROR", "EXPERT_CHECK_SELF_CHECK_FAILED"} for i in output.issues)
        return DebugExpertOutput(**output.model_dump(),
                                 tax_check=_initial_tax_summary(self.debug_direction) if failed else self._tax_summary(),
                                 regulatory=DebugRegulatory() if failed else self._regulatory_summary())


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
    engine_version = "debug-" + ENGINE_VERSION + "-2"
    prompt_version = PROMPT_VERSION
    rules_version = RULES_VERSION + "-debug-text-2"

    def __init__(self, snapshot, mode, materials=None):
        self.snapshot = deepcopy(snapshot)
        self.mode = mode
        self._preview = None
        if mode == "mock":
            self.engine = MockEngine()
            self.engine_version = "debug-expert-mock-2"
        elif mode == "live" and materials is not None:
            bundle = materials.bundle
            regulatory_source = getattr(bundle, "regulatory_source", None)
            # No environment fallback, default history, or code-ledger-as-law.
            self.engine = _DebugExpertEngine(
                direction=self.snapshot["expert"].get("direction", "unknown"),
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
        if not isinstance(self.engine, _DebugExpertEngine):
            raise DebugError("INTERNAL_ERROR", 500)
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
            direction = self.snapshot["expert"].get("direction", "unknown")
            return DebugExpertOutput(**output.model_dump(), tax_check=_initial_tax_summary(direction))
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
    tax_check = output.tax_check if isinstance(output, DebugExpertOutput) else DebugTaxCheck()
    regulatory = output.regulatory if isinstance(output, DebugExpertOutput) else DebugRegulatory()
    findings = []
    for aspect in EXPERT_ASPECTS:
        found = next((f for f in output.findings if f.aspect == aspect), None)
        verdict = found.verdict.value if found is not None and aspect != "images" else "unknown"
        reason = "图片未接入，本项待核实" if aspect == "images" else labels[aspect] + "：" + verdicts[verdict]
        if aspect == "tax" and tax_check.applicability != "applies":
            verdict = "unknown"
            reason = ("进口业务不适用出口退税核对，本项未比较" if tax_check.applicability == "not_applicable" else
                      "贸易方向未确定，未执行出口退税比较" if tax_check.applicability == "direction_unknown" else
                      "退税：资料或适用条件待核实，不等于无风险")
        if aspect == "regulatory":
            if regulatory.verified:
                reason = ("已核来源列明有监管要求，须核对办理，待人工复核" if regulatory.outcome == "measures_required" else
                          "已核来源明确声明无监管要求，仍需人工复核")
            else:
                verdict, reason = "unknown", "监管：资料或适用条件待核实，不等于无监管"
        findings.append({"aspect": aspect, "verdict": verdict,
                         "reason": reason,
                         "evidence_ids": [digest(eid) for eid in found.evidence_ids]
                         if found and aspect != "images" else []})
    gaps = []
    for item in output.issues:
        code = item.code if isinstance(item.code, str) and _CODE.fullmatch(item.code) else "EXPERT_CHECK_PENDING"
        field = item.field if item.field in _FIELDS else None
        message = ("进口业务不适用出口退税核对，本项未比较" if code == "TAX_DIRECTION_NOT_APPLICABLE" else
                   "贸易方向未确定，未执行出口退税比较" if code == "TAX_DIRECTION_UNKNOWN" else
                   "图片未接入，本项待核实" if code.startswith("IMAGE") else
                   "退税来源或适用条件待核实" if code.startswith("TAX") else
                   "监管来源或适用条件待核实" if code.startswith("REG") else
                   "核查存在待核事项，请核对分项结果和来源")
        value = {"code": code, "field": field, "message": message}
        if value not in gaps:
            gaps.append(value)
    return {"stage": "expert_check", "target_hs": output.target_hs,
            "suggested_hs": output.suggested_hs, "findings": findings, "gaps": gaps,
            "tax_check": tax_check.model_dump(mode="json"), "regulatory": regulatory.model_dump(mode="json"),
            "evidence_status": output.evidence_status.value if output.evidence_status else None}
