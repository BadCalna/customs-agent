"""P16 synthetic offline contract/security tests, NOT model/business evaluation.

P14 'official' metadata below SIMULATES a reviewed source solely to exercise the
trust gates. It is not real official data and must never be installed as such.
"""
import copy
import json
import sys
import types
import warnings
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from adapters.engines.mock import EngineTechnicalError
from adapters.engines.precheck import (
    ClassificationNote, ModelRecommendation, PrecheckEngine,
)
from adapters.engines.protocols import (
    CompareInput, CompareOutput, ExecutionContext, ImageFact, RecommendInput,
)
from adapters.sources.declaration_elements import DeclarationElementsSource
from adapters.sources.history_search import HistorySearchSource
from adapters.sources.hs_lookup import HsLookupSource, HsQuery, QueryBasis, TariffStoreError
from apps.api.schemas import ComparisonStatus, EvidenceEntry, EvidenceStatus, ResultMode
from tests.p14.helpers import (
    FIXED_CLOCK, element_store, official_tariff_dataset, official_elements_dataset,
    synthetic_dataset, tariff_store,
)

HS = "8544422900"  # synthetic format sample, NOT an evaluated classification
BASIS = {"declaration_date": "2026-05-20", "tariff_version": "2026"}


def payload(**overrides):
    params = dict(
        context=ExecutionContext(task_id="SYN-TASK", version_id="SYN-VERSION", item_id="SYN-ITEM",
                                 run_id="SYN-RUN", input_revision=1, result_mode=ResultMode.LIVE),
        name="合成测试商品", attributes={"material": "合成材质"},
        elements_text="品牌类型:无品牌;用途:工业设备连接;型号:X-100;品牌:无",
        query_basis=BASIS.copy(),
    )
    params.update(overrides)
    return RecommendInput(**params)


def answer(*, refs=None, suggested=HS, missing=None):
    return {"candidates": [{"hs": HS, "evidence_ids": refs or []}],
            "suggested_hs": suggested, "missing_facts": missing or []}


def note(**changes):
    basis = QueryBasis(**BASIS)
    data = dict(
        evidence=EvidenceEntry(
            evidence_id="notes:SYN-1", source_type="official_tariff", status=EvidenceStatus.OBTAINED,
            title="SIMULATED reviewed note, not real", document="SYNTHETIC-UNIT-DOCUMENT",
            locator="SYNTHETIC paragraph 1", excerpt="合成归类支持，仅验证门槛",
            retrieved_at="2026-05-01T00:00:00+08:00", effective_from="2026-01-01", effective_to="2026-12-31",
            query={"hs_codes": [HS], "tariff_version": "2026", "basis": basis.model_dump()},
        ),
        hs_codes=[HS], tariff_version="2026", basis=basis,
        authoritative=True, verified=True, synthetic=False,
    )
    data.update(changes)
    return ClassificationNote(**data)


class FakeModel:
    def __init__(self, result=None):
        self.result = answer() if result is None else result
        self.requests = []

    def complete(self, request):
        self.requests.append(copy.deepcopy(request))
        return copy.deepcopy(self.result)


class FakeNotes:
    def __init__(self, notes):
        self.notes = notes
        self.calls = []

    def search(self, facts, basis):
        self.calls.append((copy.deepcopy(facts), basis.model_dump()))
        return self.notes


class PrecheckTests(SimpleTestCase):
    def engine(self, *, result=None, notes=None, hs_source=None, elements_source=None, history_source=None):
        return PrecheckEngine(
            model=FakeModel(result),
            hs_source=hs_source or HsLookupSource(tariff_store(), clock=FIXED_CLOCK),
            elements_source=elements_source or DeclarationElementsSource(element_store(), clock=FIXED_CLOCK),
            notes_source=FakeNotes(notes) if notes is not None else None,
            history_source=history_source,
        )

    def codes(self, output):
        return {gap.code for gap in output.gaps}

    def test_complete_p14_without_classification_support_still_has_gap(self):
        output = self.engine().recommend(payload())
        self.assertEqual(output.suggested_hs, HS)
        self.assertIn("CLASSIFICATION_SUPPORT_PENDING", self.codes(output))
        self.assertIsNone(output.confidence)
        self.assertTrue(all(candidate.confidence is None for candidate in output.candidates))
        self.assertIn("未经", output.confidence_note)
        self.assertIn("锚定风险", output.confidence_note)

    def test_simulated_reviewed_support_can_exercise_gap_free_gate(self):
        output = self.engine(result=answer(refs=["notes:SYN-1"]), notes=[note()]).recommend(payload())
        self.assertEqual(output.gaps, [])
        self.assertEqual(output.evidence_status, EvidenceStatus.OBTAINED)
        self.assertEqual(len(output.evidence), 3)
        self.assertIsNone(output.confidence)

    def test_all_model_schema_keys_required_and_extra_forbidden(self):
        schema = ModelRecommendation.model_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"candidates", "suggested_hs", "missing_facts"})
        self.assertFalse(schema["$defs"]["ModelCandidate"]["additionalProperties"])
        for key in ("confidence", "reason", "sufficient", "evidence", "supplier_instructions"):
            result = answer()
            result[key] = "secret response must not leak"
            with self.subTest(key=key), self.assertRaises(EngineTechnicalError):
                self.engine(result=result).recommend(payload())
        result = answer()
        result["candidates"][0]["confidence"] = 0.9
        with self.assertRaises(EngineTechnicalError):
            self.engine(result=result).recommend(payload())

    def test_numeric_unicode_eight_digit_whitespace_and_newline_hs_rejected(self):
        for code in (8544422900, "８５４４４２２９００", "85444229", " 8544422900", "8544422900\n"):
            result = answer()
            result["candidates"][0]["hs"] = code
            result["suggested_hs"] = code
            with self.subTest(code=code), self.assertRaises(EngineTechnicalError):
                self.engine(result=result).recommend(payload())

    def test_non_json_fenced_nan_infinity_duplicate_keys_and_wrong_shape_rejected(self):
        cases = ["not json secret", "```json\n{}\n```", "NaN", "Infinity", "[]",
                 '{"candidates":[],"candidates":[],"suggested_hs":null,"missing_facts":[]}',
                 '{"candidates":[],"suggested_hs":null,"missing_facts":[],"confidence":NaN}',
                 {"candidates": [], "suggested_hs": None}, True, None]
        for raw in cases:
            model = FakeModel()
            model.result = raw
            engine = self.engine()
            engine.model = model
            with self.subTest(raw=raw), self.assertRaises(EngineTechnicalError):
                engine.recommend(payload())

    def test_json_string_response_supported(self):
        output = self.engine(result=json.dumps(answer())).recommend(payload())
        self.assertEqual(output.candidates[0].hs, HS)

    def test_duplicate_candidates_and_non_candidate_suggestion_rejected(self):
        results = [answer(), answer(suggested="1234567890")]
        results[0]["candidates"] *= 2
        for result in results:
            with self.subTest(result=result), self.assertRaises(EngineTechnicalError):
                self.engine(result=result).recommend(payload())

    def test_unknown_or_out_of_scope_evidence_rejected_without_response_leak(self):
        for refs, notes in [(["secret-in-response"], []), (["notes:SYN-1"], [note(hs_codes=["1234567890"])])]:
            try:
                self.engine(result=answer(refs=refs), notes=notes).recommend(payload())
            except EngineTechnicalError as exc:
                self.assertNotIn("secret-in-response", str(exc))
                self.assertIsNone(exc.__cause__)
            else:
                self.fail("fabricated citation accepted")

    def test_historical_evidence_cannot_be_cited_as_classification(self):
        with self.assertRaises(EngineTechnicalError):
            self.engine(result=answer(refs=["history:SYN-H-0001"])).recommend(payload())

    def test_no_default_history_is_constructed(self):
        with patch("adapters.sources.history_search.HistorySearchSource", side_effect=AssertionError("default forbidden")):
            self.engine().recommend(payload())

    def test_synthetic_history_in_live_is_excluded_even_if_requested(self):
        engine = self.engine(history_source=HistorySearchSource.load(
            "adapters/sources/fixtures/history_records.sample.json"))
        output = engine.recommend(payload(name="不锈钢法兰盘", attributes={"material": "不锈钢"}))
        self.assertIn("HISTORY_ORIGIN_UNVERIFIED", self.codes(output))
        self.assertEqual(engine.model.requests[0]["history_reference"], [])
        self.assertFalse(any(e.source_type == "history" for e in output.evidence))

    def test_history_query_failed_remains_failed_even_if_origin_excluded(self):
        from adapters.sources.history_search import HistorySearchResult, HistorySearchOutcome
        failed = HistorySearchResult(outcome=HistorySearchOutcome.QUERY_FAILED,
                                     evidence_status=EvidenceStatus.QUERY_FAILED,
                                     reason="secret=/Users/private", dataset=None)
        output = self.engine(history_source=Mock(search=Mock(return_value=failed))).recommend(payload())
        self.assertEqual(output.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIn("HISTORY_QUERY_FAILED", self.codes(output))
        self.assertNotIn("/Users", output.model_dump_json())

    def test_mock_context_does_not_upgrade_synthetic_history(self):
        engine = self.engine(history_source=HistorySearchSource.load(
            "adapters/sources/fixtures/history_records.sample.json"))
        request = payload(name="不锈钢法兰盘", attributes={"material": "不锈钢"})
        request.context.result_mode = ResultMode.MOCK
        output = engine.recommend(request)
        self.assertIn("CLASSIFICATION_SUPPORT_PENDING", self.codes(output))
        for reference in engine.model.requests[0]["history_reference"]:
            self.assertTrue(reference["reference_only"])
            self.assertTrue(reference["synthetic"])
            self.assertNotIn("suggested_hs", reference)

    def test_unreviewed_or_synthetic_or_undeclared_p14_never_clears_gate(self):
        infos = [official_tariff_dataset(reviewed=False), synthetic_dataset(),
                 official_tariff_dataset().model_copy(update={"synthetic": None})]
        for info in infos:
            with self.subTest(info=info):
                output = self.engine(
                    result=answer(refs=["notes:SYN-1"]), notes=[note()],
                    hs_source=HsLookupSource(tariff_store(info=info), clock=FIXED_CLOCK),
                ).recommend(payload())
                self.assertIn("HS_BASIS_PENDING", self.codes(output))
                self.assertFalse(any(g.suggested_role == "supplier" for g in output.gaps))

    def test_synthetic_or_undeclared_notes_are_excluded_in_live(self):
        for synthetic in (True, None):
            engine = self.engine(notes=[note(synthetic=synthetic)])
            output = engine.recommend(payload())
            self.assertIn("NOTES_ORIGIN_UNVERIFIED", self.codes(output))
            self.assertEqual(engine.model.requests[0]["classification_notes"], [])

    def test_mock_context_does_not_upgrade_synthetic_notes(self):
        request = payload()
        request.context.result_mode = ResultMode.MOCK
        output = self.engine(result=answer(refs=["notes:SYN-1"]), notes=[note(synthetic=True)]).recommend(request)
        self.assertIn("CLASSIFICATION_SUPPORT_PENDING", self.codes(output))

    def test_note_verified_bool_is_not_enough_without_basis_version_and_query(self):
        variations = [note(tariff_version="2025"), note(basis=QueryBasis()), note(verified=False), note(authoritative=False)]
        for change in ({"query": None}, {"retrieved_at": None}, {"excerpt": ""},
                       {"effective_from": None}, {"effective_from": "2026-06-01"},
                       {"effective_to": "2026-04-30"}, {"effective_to": "invalid-date"},
                       {"status": EvidenceStatus.VERSION_UNVERIFIED}, {"source_type": "history"}):
            entry = note().evidence.model_copy(update=change)
            variations.append(note(evidence=entry))
        for record in variations:
            with self.subTest(record=record):
                output = self.engine(result=answer(refs=["notes:SYN-1"]), notes=[record]).recommend(payload())
                self.assertIn("CLASSIFICATION_SUPPORT_PENDING", self.codes(output))

    def test_no_query_basis_is_not_invented(self):
        output = self.engine(result=answer(refs=["notes:SYN-1"]), notes=[note()]).recommend(payload(query_basis={}))
        self.assertEqual(output.evidence_status, EvidenceStatus.VERSION_UNVERIFIED)
        self.assertIn("HS_BASIS_PENDING", self.codes(output))
        self.assertFalse(any(g.suggested_role == "supplier" for g in output.gaps))

    def test_not_found_has_no_fabricated_hs_evidence(self):
        output = self.engine(hs_source=HsLookupSource(tariff_store(codes=[]), clock=FIXED_CLOCK)).recommend(payload())
        self.assertEqual(output.evidence_status, EvidenceStatus.NOT_FOUND)
        self.assertIn("HS_CODE_NOT_FOUND", self.codes(output))
        self.assertFalse(any(e.source_type == "official_tariff" for e in output.evidence))
        self.assertFalse(any(g.suggested_role == "supplier" for g in output.gaps))

    def test_p14_query_failure_is_not_absent_and_sanitizes_path(self):
        class BrokenStore:
            def load(self):
                raise TariffStoreError("secret=/Users/real-account/private-tariff.json")
        output = self.engine(hs_source=HsLookupSource(BrokenStore(), clock=FIXED_CLOCK)).recommend(payload())
        self.assertEqual(output.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIn("HS_QUERY_FAILED", self.codes(output))
        self.assertNotIn("real-account", output.model_dump_json())
        self.assertNotIn("secret=", output.model_dump_json())
        self.assertFalse(any(g.suggested_role == "supplier" for g in output.gaps))

    def test_insufficient_only_p14_may_request_supplier(self):
        output = self.engine(result=answer(missing=["material"])).recommend(payload(elements_text="品牌类型:无品牌;用途:工业设备连接;品牌:无"))
        supplier = [g for g in output.gaps if g.suggested_role == "supplier"]
        self.assertTrue(supplier)
        self.assertTrue(all(g.code.startswith("ELEMENT_") for g in supplier))
        self.assertTrue(all(g.suggested_role == "expert" for g in output.gaps if g.code == "MODEL_FACTS_PENDING"))

    def test_unreviewed_elements_cannot_request_supplier(self):
        output = self.engine(
            result=answer(missing=["model"]),
            elements_source=DeclarationElementsSource(element_store(info=official_elements_dataset(reviewed=False)), clock=FIXED_CLOCK),
        ).recommend(payload(elements_text=""))
        self.assertIn("ELEMENTS_BASIS_PENDING", self.codes(output))
        self.assertFalse(any(g.suggested_role == "supplier" for g in output.gaps))

    def test_failed_image_and_absent_are_distinct_and_no_metadata_leaks(self):
        failed = ImageFact(image_id="SECRET-IMAGE", status="read_failed", source_cell="SECRET-CELL",
                           visible_description="must not use", error_message="secret=/Users/account/image")
        engine = self.engine()
        output = engine.recommend(payload(images=[failed]))
        self.assertIn("IMAGE_READ_FAILED", self.codes(output))
        text = json.dumps(engine.model.requests[0])
        self.assertNotIn("SECRET", text)
        self.assertNotIn("account", text)
        self.assertNotIn("must not use", text)
        absent = failed.model_copy(update={"status": "absent"})
        self.assertNotIn("IMAGE_READ_FAILED", self.codes(self.engine().recommend(payload(images=[absent]))))

    def test_available_image_is_only_visible_facts_not_unseen_inference(self):
        engine = self.engine()
        image = ImageFact(image_id="SECRET-IMAGE", status="available", source_cell="SECRET-CELL", visible_description="银色圆形外观")
        engine.recommend(payload(images=[image]))
        self.assertEqual(engine.model.requests[0]["facts"]["images"], [{"status": "available", "visible_description": "银色圆形外观"}])

    def test_attribute_allowlist_and_context_isolation(self):
        engine = self.engine()
        request = payload(attributes={"material": "合成材质", "supplier_hs_raw": "1234567890", "secret": "SECRET-HIDDEN",
                                      "excel_fields": '{"原HS":"1234567890"}', "history": "SECRET-HISTORY"})
        request.context.mock_scenario = "SECRET-SCENARIO"
        request.context.engine_version = "SECRET-ENGINE"
        request.anchoring_notes = ["SECRET-NOTE"]
        request.query_basis["note"] = "SECRET-BASIS-NOTE"
        request.query_basis["secret"] = "SECRET-BASIS-EXTRA"
        engine.recommend(request)
        model = engine.model.requests[0]
        self.assertEqual(model["facts"]["attributes"], {"material": "合成材质"})
        text = json.dumps(model)
        for value in ("SYN-TASK", "SYN-VERSION", "SYN-ITEM", "SYN-RUN", "SECRET-", "1234567890"):
            self.assertNotIn(value, text)

    def test_embedded_path_secret_raw_code_and_business_object_rejected(self):
        for text in ("secret=unsafe", "/Users/account/private", "C:\\Users\\account", "supplier_hs_raw:1234567890",
                     ' {"task_id":"id"}', "run_id:SYN-RUN", "api_key=unsafe"):
            engine = self.engine()
            with self.subTest(text=text), self.assertRaises(EngineTechnicalError):
                engine.recommend(payload(attributes={"material": text}))
            self.assertEqual(engine.model.requests, [])

    def test_natural_codes_are_not_claimed_completely_deanchored(self):
        engine = self.engine()
        output = engine.recommend(payload(elements_text="型号:8544422900"))
        self.assertIn("8544422900", engine.model.requests[0]["facts"]["elements_text"])
        self.assertIn("未声称彻底消除", output.confidence_note)
        self.assertIn("锚定风险", engine.model.requests[0]["facts"]["anchoring_risk"])

    def test_mutated_input_live_string_revalidated_before_origin_checks(self):
        request = payload()
        request.context.result_mode = "live"
        engine = self.engine(notes=[note(synthetic=True)])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            output = engine.recommend(request)
        self.assertIn("NOTES_ORIGIN_UNVERIFIED", self.codes(output))
        self.assertEqual(engine.model.requests[0]["classification_notes"], [])

    def test_source_url_path_document_and_reviewer_not_given_to_model(self):
        entry = note().evidence.model_copy(update={"document": "/Users/private/source", "url": "https://secret.example/path"})
        engine = self.engine(notes=[note(evidence=entry)])
        engine.recommend(payload())
        text = json.dumps(engine.model.requests[0])
        self.assertNotIn("/Users/", text)
        self.assertNotIn("secret.example", text)
        self.assertNotIn("fixture-reviewer", text)

    def test_bad_or_mutated_note_revalidated_and_exception_sanitized(self):
        invalid = note()
        invalid.hs_codes = ["eight"]
        for source in (FakeNotes([invalid]), Mock(search=Mock(side_effect=RuntimeError("secret=/Users/private")))):
            engine = self.engine()
            engine.notes_source = source
            with self.assertRaises(EngineTechnicalError) as caught:
                engine.recommend(payload())
            self.assertNotIn("/Users", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(engine.model.requests, [])

    def test_mutated_nested_p14_result_revalidated(self):
        hs = HsLookupSource(tariff_store(), clock=FIXED_CLOCK).lookup(HsQuery(hs_code_raw=HS, basis=QueryBasis(**BASIS)))
        hs.evidence[0].locator = "\u200b"
        with self.assertRaises(EngineTechnicalError):
            self.engine(hs_source=Mock(lookup=Mock(return_value=hs))).recommend(payload())

    def test_model_exception_never_leaks_prompt_or_secret(self):
        engine = self.engine()
        engine.model = Mock(complete=Mock(side_effect=RuntimeError("secret=/Users/private/SYN-TASK")))
        with self.assertRaises(EngineTechnicalError) as caught:
            engine.recommend(payload())
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_empty_candidate_output_is_explicit_not_fake_hs(self):
        output = self.engine(result={"candidates": [], "suggested_hs": None, "missing_facts": []}).recommend(payload())
        self.assertEqual(output.candidates, [])
        self.assertIsNone(output.suggested_hs)
        self.assertIn("NO_INDEPENDENT_CANDIDATE", self.codes(output))

    def test_source_then_model_then_p14_call_order(self):
        events = []
        engine = self.engine(notes=[note()])
        notes, model, hs, elements = engine.notes_source, engine.model, engine.hs_source, engine.elements_source
        engine.notes_source = Mock(search=lambda *args: (events.append("notes") or notes.search(*args)))
        engine.model = Mock(complete=lambda request: (events.append("model") or model.complete(request)))
        engine.hs_source = Mock(lookup=lambda query: (events.append("hs") or hs.lookup(query)))
        engine.elements_source = Mock(check=lambda query: (events.append("elements") or elements.check(query)))
        engine.recommend(payload())
        self.assertEqual(events, ["notes", "model", "hs", "elements"])

    def test_compare_only_delegates_shared_helper_with_no_invented_basis(self):
        engine = self.engine()
        request = CompareInput(context=payload().context, recommendation=engine.recommend(payload()), supplier_hs_raw=HS)
        result = CompareOutput(comparison_status=ComparisonStatus.NEEDS_CONFIRMATION)
        shared = types.ModuleType("adapters.engines.hs_compare")
        shared.compare_original_checked = Mock(return_value=result)
        with patch.dict(sys.modules, {"adapters.engines.hs_compare": shared}):
            self.assertIs(engine.compare(request), result)
        shared.compare_original_checked.assert_called_once_with(request, hs_source=engine.hs_source)

    def test_p14_other_code_or_basis_response_rejected(self):
        from adapters.sources.declaration_elements import ElementQuery
        for raw, basis in (("1234567890", QueryBasis(**BASIS)),
                           (HS, QueryBasis(declaration_date="2026-06-01", tariff_version="2026"))):
            hs = HsLookupSource(tariff_store(), clock=FIXED_CLOCK).lookup(HsQuery(hs_code_raw=raw, basis=basis))
            elements = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(ElementQuery(hs_code_raw=raw, basis=basis))
            with self.subTest(raw=raw, basis=basis, source="hs"), self.assertRaises(EngineTechnicalError):
                self.engine(hs_source=Mock(lookup=Mock(return_value=hs))).recommend(payload())
            with self.subTest(raw=raw, basis=basis, source="elements"), self.assertRaises(EngineTechnicalError):
                self.engine(elements_source=Mock(check=Mock(return_value=elements))).recommend(payload())

    def test_p14_record_code_or_version_is_bound_to_current_candidate(self):
        from adapters.sources.declaration_elements import ElementQuery
        for change in ({"code": "1234567890"}, {"version": "2025"}):
            result = HsLookupSource(tariff_store(), clock=FIXED_CLOCK).lookup(HsQuery(hs_code_raw=HS, basis=QueryBasis(**BASIS)))
            for key, value in change.items():
                setattr(result.code, key, value)
            with self.subTest(source="hs", change=change), self.assertRaises(EngineTechnicalError):
                self.engine(hs_source=Mock(lookup=Mock(return_value=result))).recommend(payload())
        for change in ({"hs_code": "1234567890"}, {"version": "2025"}):
            result = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(ElementQuery(
                hs_code_raw=HS, basis=QueryBasis(**BASIS), declared_raw=payload().elements_text))
            for key, value in change.items():
                setattr(result.schema_record, key, value)
            with self.subTest(source="elements", change=change), self.assertRaises(EngineTechnicalError):
                self.engine(elements_source=Mock(check=Mock(return_value=result))).recommend(payload())

    def test_cached_sufficient_elements_for_other_facts_rejected(self):
        from adapters.sources.declaration_elements import ElementQuery
        cached = DeclarationElementsSource(element_store(), clock=FIXED_CLOCK).check(ElementQuery(
            hs_code_raw=HS, basis=QueryBasis(**BASIS), declared_raw=payload().elements_text))
        with self.assertRaises(EngineTechnicalError):
            self.engine(result=answer(refs=["notes:SYN-1"]), notes=[note()],
                        elements_source=Mock(check=Mock(return_value=cached))).recommend(payload(elements_text=""))
        cached.query.declared = {"extra": "stale-value"}
        with self.assertRaises(EngineTechnicalError):
            self.engine(elements_source=Mock(check=Mock(return_value=cached))).recommend(payload())

    def test_run_precheck_preserves_context_and_keeps_original_out_of_model(self):
        engine = self.engine(result=answer(refs=["notes:SYN-1"]), notes=[note()])
        original = payload()
        before = original.model_dump()
        shared = types.ModuleType("adapters.engines.hs_compare")
        shared.compare_original_checked = Mock(return_value=CompareOutput(comparison_status=ComparisonStatus.CONSISTENT))
        with patch.dict(sys.modules, {"adapters.engines.hs_compare": shared}):
            output = engine.run_precheck(original, supplier_hs_raw="1234567890", association_note="SYN-ASSOCIATION")
        self.assertEqual(original.model_dump(), before)
        compared = shared.compare_original_checked.call_args.args[0]
        self.assertEqual(compared.context.model_dump(), original.context.model_dump())
        self.assertEqual(compared.supplier_hs_raw, "1234567890")
        self.assertEqual(shared.compare_original_checked.call_args.kwargs["basis"].model_dump(), QueryBasis(**BASIS).model_dump())
        self.assertNotIn("1234567890", json.dumps(engine.model.requests[0]))
        self.assertEqual(output.comparison_status, ComparisonStatus.CONSISTENT)
        self.assertIsNone(output.confidence)

    def test_compare_explicit_basis_is_forwarded_not_inferred(self):
        engine = self.engine()
        request = CompareInput(context=payload().context, recommendation=engine.recommend(payload()), supplier_hs_raw=HS)
        shared = types.ModuleType("adapters.engines.hs_compare")
        shared.compare_original_checked = Mock(return_value=CompareOutput(comparison_status=ComparisonStatus.NEEDS_CONFIRMATION))
        basis = QueryBasis(**BASIS)
        with patch.dict(sys.modules, {"adapters.engines.hs_compare": shared}):
            engine.compare(request, basis=basis)
        shared.compare_original_checked.assert_called_once_with(request, hs_source=engine.hs_source, basis=basis)

    def test_expert_check_explicitly_unimplemented(self):
        with self.assertRaises(NotImplementedError):
            self.engine().expert_check(None)
