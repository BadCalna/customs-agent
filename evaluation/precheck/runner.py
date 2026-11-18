"""Deterministic comparison-wrapper runner. This does not call PrecheckEngine/model."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

from adapters.engines.hs_compare import RULES_VERSION, compare_original_checked
from adapters.engines.mock import RULES_VERSION as PRIORITY_RULES_VERSION
from adapters.engines.protocols import CompareInput, ExecutionContext, RecommendOutput
from adapters.sources.hs_lookup import (
    DatasetInfo, HsLookupSource, InMemoryTariffStore, QueryBasis, SCHEMA_VERSION as SOURCE_SCHEMA_VERSION,
)
from apps.api.schemas import CONTRACT_VERSION, ComparisonStatus, EvidenceEntry, IssueEntry

from .cases import SUITE_VERSION, SyntheticCase, fixed_cases

REPORT_SCHEMA_VERSION = "p16.technical-report.1"
FIXED_TIME = "2026-01-01T00:00:00+00:00"


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _source(case):
    if case.source == "none":
        return None, None
    # Metadata is honestly SYNTHETIC: cannot serve as official/current business basis.
    info = DatasetInfo(dataset_id="p16-synthetic-source", source_type="synthetic",
                       authority="synthetic", synthetic=True,
                       description="Fixed technical fixture; not a real tariff source")
    if case.source == "query_failed":
        class FailingMemoryStore:
            def load(self):
                raise OSError("synthetic controlled source failure")
        store = FailingMemoryStore()
    else:
        store = InMemoryTariffStore(codes=[], versions=[{
            "version": "synthetic-2026", "effective_from": "2026-01-01", "effective_to": "2026-12-31",
        }], dataset=info)
    basis = QueryBasis() if case.source == "version_unverified" else QueryBasis(
        declaration_date="2026-05-20", tariff_version="synthetic-2026",
        note="synthetic technical test constant; not a business declaration date")
    return HsLookupSource(store, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)), basis


def _payload(case):
    recommendation = case.recommendation
    return CompareInput(
        context=ExecutionContext(task_id="synthetic-task", version_id=SUITE_VERSION,
                                 item_id=case.id, run_id="synthetic-fixed-run", input_revision=1,
                                 engine_version="compare-wrapper-only", prompt_version="not-used-model-not-called",
                                 rules_version=RULES_VERSION, result_mode="mock"),
        supplier_hs_raw=case.supplier_hs_raw, association_ok=case.association_ok,
        supplier_hs_format_valid=case.supplier_hs_format_valid,
        recommendation=RecommendOutput(
            suggested_hs=recommendation.suggested_hs,
            evidence_status=recommendation.evidence_status,
            confidence=None, confidence_note="Synthetic fixture; no model called and no calibrated probability",
            evidence=[EvidenceEntry(
                evidence_id=f"synthetic-evidence-{index}", source_type="synthetic",
                title="SYNTHETIC technical evidence, not real law", status=entry.status,
                document=entry.document, locator=entry.locator, retrieved_at=FIXED_TIME,
                note="Only exercises post-recommendation comparison logic",
            ) for index, entry in enumerate(recommendation.evidence)],
            gaps=[IssueEntry(code=code, message="Synthetic technical gap; not a business finding")
                  for code in recommendation.gap_codes],
        ),
    )


def validate_cases(cases):
    """Reject empty/non-list, duplicate IDs, extras/types and missing six-class coverage."""
    if type(cases) is not list or not cases:
        raise ValueError("cases must be a nonempty list")
    # Revalidate instances too, preventing model_construct/model_copy from bypassing strict fixtures.
    parsed = [SyntheticCase.model_validate(case.model_dump(warnings=False) if isinstance(case, SyntheticCase) else case)
              for case in cases]
    ids = [case.id for case in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("case IDs must be distinct")
    coverage = {case.expected_status for case in parsed}
    if coverage != set(ComparisonStatus):
        raise ValueError("cases must cover all six comparison statuses")
    return parsed


def _source_diagnostics(case, output):
    return {
        "mode": case.source,
        "synthetic": True,
        "authority": "none-synthetic-not-business-basis",
        "original_validity": "not_verified" if case.source == "none" else "pending",
        "recommendation_evidence_status": (case.recommendation.evidence_status.value
                                           if case.recommendation.evidence_status else None),
        "recommendation_evidence_count": len(case.recommendation.evidence),
        "issue_codes": sorted({issue["code"] for issue in output.get("issues", [])}),
        "limitations": ["All evidence is synthetic; no real HS classification is evaluated",
                        "No-source comparison does not verify original code validity"],
    }


def build_report(cases=None):
    """Build a stable JSON-compatible technical report; requires settings + django.setup().

    Raises ValueError/ValidationError for invalid or incomplete case sets. Runtime
    comparison errors are sanitized per case and cause a failed technical result.
    Only expected statuses and exact issue-code sets are checked, plus no mutation.
    """
    parsed = validate_cases(fixed_cases() if cases is None else cases)
    results = []
    for case in parsed:
        payload = _payload(case)
        source, basis = _source(case)
        before = payload.model_dump(mode="json")
        normalized_input = {"payload": before, "source_mode": case.source,
                            "basis": basis.model_dump(mode="json") if basis else None}
        expected = {"comparison_status": case.expected_status.value,
                    "issue_codes": sorted(case.expected_issue_codes)}
        diffs = []
        error = None
        try:
            output = compare_original_checked(payload, hs_source=source, basis=basis).model_dump(mode="json")
            actual = {"comparison_status": output["comparison_status"],
                      "issue_codes": sorted({issue["code"] for issue in output["issues"]})}
            for key in expected:
                if expected[key] != actual[key]:
                    diffs.append({"field": key, "expected": expected[key], "actual": actual[key]})
            if before != payload.model_dump(mode="json"):
                diffs.append({"field": "input_unchanged", "expected": True, "actual": False})
        except Exception:
            # No raw exception/messages/paths/environment values enter the deliverable.
            error = "COMPARISON_TECHNICAL_ERROR"
            output = {"error": error}
            actual = {"comparison_status": None, "issue_codes": []}
            diffs.append({"field": "execution", "expected": "completed", "actual": error})
        results.append({
            "id": case.id, "description": case.description, "synthetic": True,
            "input": normalized_input, "input_sha256": sha256(normalized_input),
            "output": output, "output_sha256": sha256(output),
            "expected": expected, "actual": actual, "differences": diffs,
            "technical_passed": not diffs, "error": error,
            "source_issues": _source_diagnostics(case, output),
        })
    passed = sum(row["technical_passed"] for row in results)
    inputs = [{"id": row["id"], "input": row["input"]} for row in results]
    outputs = [{"id": row["id"], "output": row["output"]} for row in results]
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "synthetic": True, "technical_only": True, "model_not_called": True,
        "quality_gate_pending": True,
        "evaluation_scope": "post_recommendation_compare_wrapper_only",
        "versions": {"suite": SUITE_VERSION, "contract": CONTRACT_VERSION,
                     "engine": "compare_original_checked", "rules": RULES_VERSION,
                     "priority_rules": PRIORITY_RULES_VERSION, "source_schema": SOURCE_SCHEMA_VERSION,
                     "model": "not_called", "prompt": "not_used"},
        "input_sha256": sha256(inputs), "output_sha256": sha256(outputs),
        "cases_sha256": sha256([case.model_dump(mode="json") for case in parsed]),
        "hash_specification": "SHA-256 UTF-8 JSON; sorted keys; compact separators; ensure_ascii=false; no NaN",
        "summary": {"total": len(results), "technical_passed": passed,
                    "technical_failed": len(results) - passed,
                    "technical_success": passed == len(results),
                    "expected_status_counts": dict(sorted(Counter(c.expected_status.value for c in parsed).items())),
                    "actual_status_counts": dict(sorted(Counter(row["actual"]["comparison_status"] or "technical_error"
                                                                 for row in results).items()))},
        "limitations": [
            "No actual model was called; recommendation outputs are fixed synthetic fixtures.",
            "Technical pass counts are NOT business accuracy or model classification accuracy.",
            "Synthetic evidence never establishes real legal, tariff, validity or declaration support.",
            "Real model integration, reviewed de-identified business evaluation and quality gate remain pending.",
            "No network, database, real .env, business service or persistence is used by this runner.",
        ],
        "cases": results,
    }
