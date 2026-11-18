"""六类比较与多异常优先级；元数据均为合成逻辑夹具。"""
from django.test import SimpleTestCase

from adapters.engines.hs_compare import compare_original_checked
from adapters.engines.mock import EngineTechnicalError, compare_original
from adapters.engines.protocols import CompareInput, ExecutionContext, RecommendOutput
from apps.api.schemas import EvidenceEntry, IssueEntry
from adapters.sources.hs_lookup import HsLookupSource, InMemoryTariffStore, QueryBasis
from tests.p14.helpers import tariff_store, tariff_code, official_tariff_dataset

HS = "8544422900"


def recommendation(**updates):
    values = dict(suggested_hs=HS, evidence_status="obtained", evidence=[EvidenceEntry(
        evidence_id="fixture", source_type="official_tariff", title="合成逻辑夹具",
        status="obtained", document="SYNTHETIC-TEST-ONLY", locator="条1",
    )])
    values.update(updates)
    return RecommendOutput(**values)


def compare_input(**updates):
    values = dict(context=ExecutionContext(
        task_id="test-task", version_id="test-version", item_id="test-item",
        run_id="test-run", input_revision=1,
    ), recommendation=recommendation(), supplier_hs_raw=HS)
    values.update(updates)
    return CompareInput(**values)


class CompareBoundaryTest(SimpleTestCase):
    def test_six_outcomes_and_priority(self):
        scenarios = [
            ({"association_ok": False, "supplier_hs_raw": ""}, "association_error"),
            ({"supplier_hs_raw": "", "supplier_hs_format_valid": False}, "supplier_missing"),
            ({"supplier_hs_raw": "bad", "recommendation": recommendation(suggested_hs=None)}, "supplier_invalid"),
            ({"recommendation": recommendation(gaps=[IssueEntry(code="GAP", message="待核")])}, "needs_confirmation"),
            ({}, "consistent"),
            ({"supplier_hs_raw": "8536901900"}, "inconsistent"),
        ]
        for kwargs, status in scenarios:
            with self.subTest(status=status):
                self.assertEqual(compare_original_checked(compare_input(**kwargs)).comparison_status, status)

    def test_retains_lower_priority_issues_and_does_not_mutate(self):
        payload = compare_input(association_ok=False, recommendation=recommendation(
            gaps=[IssueEntry(code="MISSING_FACTS", message="资料不足")]))
        before = payload.model_dump()
        result = compare_original_checked(payload)
        self.assertEqual(result.comparison_status, "association_error")
        self.assertIn("MISSING_FACTS", [i.code for i in result.issues])
        self.assertEqual(before, payload.model_dump())

    def test_malformed_input_is_not_washed_by_true_flag(self):
        for raw in ("123", "85444229000", "８５４４４２２９００", "٨٥٤٤٤٢٢٩٠٠", "8544 422900", "8.544e9"):
            with self.subTest(raw=raw):
                result = compare_original_checked(compare_input(supplier_hs_raw=raw))
                self.assertEqual(result.comparison_status, "supplier_invalid")

    def test_eight_digits_is_not_invalid(self):
        result = compare_original_checked(compare_input(supplier_hs_raw="85444229"))
        self.assertEqual(result.comparison_status, "needs_confirmation")
        self.assertIn("SUPPLIER_HS_SUBHEADING_ONLY", [i.code for i in result.issues])

    def test_whitespace_trim_is_comparison_copy_only(self):
        payload = compare_input(supplier_hs_raw=f"  {HS}\n")
        result = compare_original_checked(payload)
        self.assertEqual(result.comparison_status, "consistent")
        self.assertEqual(payload.supplier_hs_raw, f"  {HS}\n")

    def test_same_code_missing_evidence_never_consistent(self):
        for status in (None, "query_failed", "not_found", "version_unverified", "obtained"):
            with self.subTest(status=status):
                result = compare_original_checked(compare_input(recommendation=recommendation(
                    evidence_status=status, evidence=[])))
                self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_evidence_requires_location_and_obtained_state(self):
        for updates in ({"document": None}, {"locator": " "}, {"locator": "\u200b"}, {"status": "version_unverified"}):
            evidence = recommendation().evidence[0].model_copy(update=updates)
            result = compare_original_checked(compare_input(recommendation=recommendation(evidence=[evidence])))
            self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_bad_suggested_code_never_comparable(self):
        for hs in ("123", "85444229", "٨٥٤٤٤٢٢٩٠٠"):
            result = compare_original_checked(compare_input(recommendation=recommendation(suggested_hs=hs)))
            self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_source_verified_expired_or_merged_is_invalid(self):
        for state in ("expired", "merged"):
            source = HsLookupSource(tariff_store(codes=[tariff_code(
                state=state, **({"merged_to": "8536901900"} if state == "merged" else {}))]))
            result = compare_original_checked(compare_input(), hs_source=source,
                basis=QueryBasis(declaration_date="2026-05-20"))
            self.assertEqual(result.comparison_status, "supplier_invalid")

    def test_not_found_unreviewed_and_missing_date_are_pending_not_invalid(self):
        stores = [tariff_store(codes=[]), tariff_store(info=official_tariff_dataset(reviewed=False)), tariff_store()]
        for store in stores:
            result = compare_original_checked(compare_input(), hs_source=HsLookupSource(store))
            self.assertEqual(result.comparison_status, "needs_confirmation")
            self.assertIn("SUPPLIER_HS_VALIDITY_PENDING", [i.code for i in result.issues])

    def test_unreviewed_expired_not_asserted_invalid(self):
        store = tariff_store(codes=[tariff_code(state="expired")], info=official_tariff_dataset(reviewed=False))
        result = compare_original_checked(compare_input(), hs_source=HsLookupSource(store),
            basis=QueryBasis(declaration_date="2026-05-20"))
        self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_verified_active_original_can_compare(self):
        result = compare_original_checked(compare_input(), hs_source=HsLookupSource(tariff_store()),
            basis=QueryBasis(declaration_date="2026-05-20"))
        self.assertEqual(result.comparison_status, "consistent")

    def test_query_failed_is_pending(self):
        class BrokenStore:
            def load(self):
                raise OSError("private/path")
        result = compare_original_checked(compare_input(), hs_source=HsLookupSource(BrokenStore()))
        self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_unexpected_source_failure_has_sanitized_technical_error(self):
        class BrokenSource:
            def lookup(self, query):
                raise RuntimeError("private-path-and-token")
        with self.assertRaises(EngineTechnicalError) as caught:
            compare_original_checked(compare_input(), hs_source=BrokenSource())
        self.assertNotIn("private-path-and-token", str(caught.exception))

    def test_source_response_for_wrong_code_or_basis_is_rejected(self):
        result = HsLookupSource(tariff_store()).lookup({
            "hs_code_raw": HS, "basis": {"declaration_date": "2026-05-20"}})
        class WrongSource:
            def lookup(self, query):
                return result
        for updates, basis in (({"supplier_hs_raw": "8536901900"}, QueryBasis(declaration_date="2026-05-20")),
                               ({}, QueryBasis())):
            with self.assertRaises(EngineTechnicalError):
                compare_original_checked(compare_input(**updates), hs_source=WrongSource(), basis=basis)

    def test_explicit_non_official_live_evidence_is_pending(self):
        for source_type in ("synthetic", "enterprise_history", "third_party", "unknown"):
            payload = compare_input()
            payload.context.result_mode = "live"
            payload.recommendation.evidence[0].source_type = source_type
            self.assertEqual(compare_original_checked(payload).comparison_status, "needs_confirmation")

    def test_unspecified_real_origin_is_never_valid_or_invalid(self):
        for state in ("active", "expired"):
            info = official_tariff_dataset().model_copy(update={"synthetic": None})
            source = HsLookupSource(tariff_store(info=info, codes=[tariff_code(state=state)]))
            result = compare_original_checked(compare_input(), hs_source=source,
                basis=QueryBasis(declaration_date="2026-05-20"))
            self.assertEqual(result.comparison_status, "needs_confirmation")

    def test_wrong_record_cannot_be_laundered_by_correct_query(self):
        source = HsLookupSource(tariff_store(codes=[tariff_code(code="8536901900", state="expired")]))
        result = source.lookup({"hs_code_raw": "8536901900", "basis": {"declaration_date": "2026-05-20"}})
        result.query.hs_code_raw = HS
        class WrongSource:
            def lookup(self, query):
                return result
        with self.assertRaises(EngineTechnicalError):
            compare_original_checked(compare_input(), hs_source=WrongSource(),
                basis=QueryBasis(declaration_date="2026-05-20"))

    def test_low_priority_pending_and_invalid_preserved(self):
        for status in (None, "not_found", "query_failed", "version_unverified"):
            result = compare_original_checked(compare_input(association_ok=False, supplier_hs_raw="bad",
                recommendation=recommendation(evidence_status=status, evidence=[])))
            codes = {i.code for i in result.issues}
            self.assertEqual(result.comparison_status, "association_error")
            self.assertTrue({"EVIDENCE_PENDING", "SUPPLIER_HS_INVALID"} <= codes)

    def test_p02_priority_delegated_not_reimplemented(self):
        from unittest.mock import patch
        with patch("adapters.engines.hs_compare.compare_original", wraps=compare_original) as delegated:
            compare_original_checked(compare_input())
        delegated.assert_called_once()
