"""Offline contract/security fixtures, not real customs data or model evaluation."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from adapters.engines.commentary_reference import CommentaryReferenceSource
from adapters.engines.mock import EngineTechnicalError
from adapters.engines.protocols import ExecutionContext, ImageFact, RecommendInput
from adapters.engines.reference_bundle import ReferenceBundle, ReferenceBundleConfig
from adapters.engines.skill_precheck import DEFAULT_SKILL_PATH, SkillPrecheckAdapter
from adapters.sources.history_search import HistoryDatasetInfo, HistoryRecord, HistorySearchSource, HistorySnapshot
from apps.api.schemas import EvidenceStatus, ResultMode
from tests.p16_real.test_reference_bundle import HS, release_fixture


class Model:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else {
            "candidates": [{"hs": HS, "evidence_ids": []}], "suggested_hs": HS, "missing_facts": []}
        self.calls = []

    def complete(self, request):
        self.calls.append(copy.deepcopy(request))
        request["facts"]["attributes"]["material"] = "MODEL-MUTATED"
        return copy.deepcopy(self.answer)


def payload(**changes):
    params = {"context": ExecutionContext(task_id="PRIVATE-TASK", version_id="PRIVATE-VERSION",
                                         item_id="PRIVATE-ITEM", run_id="PRIVATE-RUN", input_revision=1,
                                         input_hash="PRIVATE-HASH", result_mode=ResultMode.LIVE),
              "name": "UNIT TEST TEXT", "attributes": {"material": "steel", "usage": "UNIT"},
              "elements_text": "用途:UNIT", "query_basis": {"declaration_date": "2026-11-18", "tariff_version": "2026"}}
    params.update(changes)
    return RecommendInput(**params)


class SkillPrecheckTests(SimpleTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release, _ = release_fixture(self.root, reviewed=True)
        self.bundle = ReferenceBundle.load(ReferenceBundleConfig(release_dir=self.release, include_commentary=True))

    def adapter(self, **options):
        return SkillPrecheckAdapter(model=options.pop("model", Model()),
                                    reference_bundle=options.pop("reference_bundle", self.bundle), **options)

    def codes(self, result):
        return {gap.code for gap in result.gaps}

    def test_default_skill_hash_and_precheck_extraction(self):
        adapter = self.adapter()
        request = adapter.build_request(payload())
        self.assertEqual(request["skill"]["sha256"], hashlib.sha256(DEFAULT_SKILL_PATH.read_bytes()).hexdigest())
        rules = request["skill"]["rules"]
        self.assertIn("合同品名", rules)
        self.assertIn("成分百分比", rules)
        self.assertNotIn("## 三、专家审核", rules)
        self.assertNotIn("专家决定通过或退回", rules)
        self.assertNotIn("原“海关编码”**紧右侧**", rules)
        self.assertEqual(request["stage"], "precheck")
        self.assertEqual(request["reference_packet"]["images"], "not_connected")
        self.assertEqual(request["reference_packet"]["expert_review"], "not_executed")
        self.assertEqual(adapter.model.calls, [])
        self.assertEqual(request["reference_packet"]["p14_status"], "not_queried")
        self.assertEqual(request["reference_packet"]["p14"], [])

    def test_explicit_skill_hash_changes_and_unknown_layout_fails_closed(self):
        skill = self.root / "custom-skill.md"
        skill.write_bytes(DEFAULT_SKILL_PATH.read_bytes() + b"\n<!-- unit change -->\n")
        adapter = self.adapter(skill_path=skill)
        self.assertNotEqual(adapter.skill["sha256"], self.adapter().skill["sha256"])
        self.assertEqual(adapter.skill["sha256"], hashlib.sha256(skill.read_bytes()).hexdigest())
        # The loaded version is pinned for this adapter, not silently re-read.
        skill.write_text("secret-token INVALID LAYOUT", encoding="utf-8")
        self.assertNotIn("secret-token", json.dumps(adapter.build_request(payload())))
        with self.assertRaisesRegex(EngineTechnicalError, "^预审Skill读取或阶段规则校验失败$") as caught:
            self.adapter(skill_path=skill)
        self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(EngineTechnicalError):
            self.adapter(skill_path=self.root / "missing.md")

    def test_original_business_ids_secrets_and_images_are_not_forwarded(self):
        item = payload(attributes={"material": "steel", "supplier_hs_raw": "9900000000",
                                   "contract_id": "CONTRACT-PRIVATE", "api_key": "KEY-PRIVATE",
                                   "source_cell": "PRIVATE-CELL", "past_result": "PRIVATE-PAST"},
                       images=[ImageFact(image_id="PRIVATE-IMAGE", status="available", source_cell="PRIVATE-CELL",
                                         visible_description="PRIVATE-VISUAL", error_message="PRIVATE-ERROR")],
                       anchoring_notes=["PRIVATE-ANCHOR"], query_basis={"tariff_version": "2026", "note": "PRIVATE-NOTE"})
        request = self.adapter().build_request(item)
        text = json.dumps(request, ensure_ascii=False)
        for value in ("9900000000", "CONTRACT-PRIVATE", "KEY-PRIVATE", "PRIVATE-CELL", "PRIVATE-PAST",
                      "PRIVATE-IMAGE", "PRIVATE-VISUAL", "PRIVATE-ERROR", "PRIVATE-ANCHOR", "PRIVATE-NOTE",
                      "PRIVATE-TASK", "PRIVATE-VERSION", "PRIVATE-ITEM", "PRIVATE-RUN", "PRIVATE-HASH"):
            self.assertNotIn(value, text)
        self.assertEqual(request["facts"]["attributes"], {"material": "steel"})

    def test_sensitive_text_or_embedded_original_code_is_fixed_error(self):
        for value in ("password=DO-NOT-LEAK", "PRIVATE-TASK", "原编码:9900000000", "9900000000",
                      "99 0000-0000", "api_key:DO-NOT-LEAK", '{"name":"hidden"}'):
            with self.subTest(value=value), self.assertRaisesRegex(EngineTechnicalError, "^预审输入包含不允许的敏感内容或契约无效$") as caught:
                self.adapter().build_request(payload(name=value))
            self.assertIsNone(caught.exception.__cause__)
        with self.assertRaises(EngineTechnicalError):
            self.adapter().build_request(payload(name="SECRET-VALUE", attributes={"api_key": "SECRET-VALUE"}))
        with self.assertRaises(EngineTechnicalError):
            self.adapter().build_request(payload(query_basis={"tariff_version": "9900000000"}))

    def test_absolute_paths_separators_and_urls_rejected_before_model(self):
        values = ("/private/confidential", "/tmp/upload.xlsx", "/var/tmp/source", "/opt/custom/data",
                  "goods /private/secret", "path=/tmp/upload", "根目录：/var/tmp/source",
                  r"C:\private\secret", "C:/private/secret", r"\\server\share\secret",
                  "goods relative/path", "goods／private／secret", "https://example.invalid/source")
        for value in values:
            for field in ("name", "attributes", "elements_text"):
                item = payload(**({"attributes": {"material": value}} if field == "attributes" else {field: value}))
                before = item.model_dump()
                for method in ("build_request", "recommend"):
                    with self.subTest(value=value, field=field, method=method):
                        adapter = self.adapter()
                        with self.assertRaisesRegex(EngineTechnicalError, "^预审输入包含不允许的敏感内容或契约无效$") as caught:
                            getattr(adapter, method)(item)
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertEqual(item.model_dump(), before)
                        self.assertEqual(adapter.model.calls, [])

    def test_short_context_ids_and_hash_never_enter_facts(self):
        for context_field in ("task_id", "version_id", "item_id", "run_id", "input_hash"):
            for identifier in ("T7", "Q", "xyZ"):
                context = payload().context.model_copy(update={context_field: identifier})
                for field in ("name", "attributes", "elements_text"):
                    value = "goods " + identifier
                    item = payload(context=context, **({"attributes": {"material": value}} if field == "attributes" else {field: value}))
                    before = item.model_dump()
                    for method in ("build_request", "recommend"):
                        with self.subTest(context_field=context_field, identifier=identifier, field=field, method=method):
                            adapter = self.adapter()
                            with self.assertRaisesRegex(EngineTechnicalError, "^预审输入包含不允许的敏感内容或契约无效$") as caught:
                                getattr(adapter, method)(item)
                            self.assertIsNone(caught.exception.__cause__)
                            self.assertEqual(item.model_dump(), before)
                            self.assertEqual(adapter.model.calls, [])

    def test_p14_and_commentary_json_packet_has_provenance_not_support(self):
        request = self.adapter().build_request(payload(), candidate_codes=[HS])
        packet = request["reference_packet"]
        self.assertEqual(packet["p14_status"], "queried")
        self.assertEqual(packet["p14"][0]["hs"]["status"], "obtained")
        self.assertEqual(packet["p14"][0]["elements"]["sufficiency"], "sufficient")
        commentary = packet["commentary"]
        self.assertTrue(commentary["material_retrieved"])
        self.assertFalse(commentary["classification_support"])
        self.assertEqual(commentary["applicability"], "not_assessed")
        self.assertEqual(commentary["records"][0]["sections"][0]["text"], "UNIT TEST TEXT")
        self.assertIn("classification_support=false", request["system"])
        for entry in packet["evidence_index"]:
            for key in ("evidence_id", "source_type", "document", "locator", "dataset", "version", "status"):
                self.assertIn(key, entry)
            self.assertFalse(entry["classification_support"])
            self.assertIs(entry["dataset"]["synthetic"], False)
            self.assertEqual(entry["dataset"]["review_status"], "reviewed")
        self.assertNotIn("UNIT-TEST-NOT-A-REAL-REVIEWER", json.dumps(request))
        self.assertNotIn(str(self.root), json.dumps(request))
        self.assertEqual(packet["bundle"]["fingerprint"], self.bundle.fingerprint)

    def test_commentary_reference_does_not_clear_classification_gap(self):
        adapter = self.adapter()
        ref = adapter.build_request(payload())["reference_packet"]["evidence_index"][0]["evidence_id"]
        adapter.model.answer["candidates"][0]["evidence_ids"] = [ref]
        result = adapter.recommend(payload())
        self.assertEqual(result.suggested_hs, HS)
        self.assertIn("CLASSIFICATION_SUPPORT_PENDING", self.codes(result))
        self.assertIn("EXPERT_REVIEW_NOT_EXECUTED", self.codes(result))
        self.assertIn("IMAGES_NOT_CONNECTED", self.codes(result))
        self.assertIsNone(result.confidence)
        self.assertIsNone(result.candidates[0].confidence)

    def test_commentary_truncation_is_not_fabricated_as_complete(self):
        commentary = CommentaryReferenceSource(source=self.bundle.commentary_source, max_total_chars=1)
        request = self.adapter(commentary_source=commentary).build_request(payload())
        material = request["reference_packet"]["commentary"]
        self.assertTrue(material["truncated"])
        self.assertFalse(material["records"][0]["complete"])
        self.assertEqual(material["records"][0]["sections"], [])
        self.assertTrue(material["records"][0]["omitted_sections"])

    def test_commentary_no_hit_version_uncertainty_and_safe_reasons_survive(self):
        metadata = copy.deepcopy(self.bundle.source_metadata)
        metadata["tariff_commentary"]["version"] = "unmatched-snapshot"
        adapter = self.adapter(reference_bundle=replace(self.bundle, source_metadata=metadata))
        request = adapter.build_request(payload(name="NO_MATCH_UNIT"), commentary_keys=["CH99"], commentary_keyword="")
        material = request["reference_packet"]["commentary"]
        self.assertFalse(material["material_retrieved"])
        self.assertEqual(material["evidence_status"], "version_unverified")
        self.assertIn("版本未核实", material["queries"][0]["reason"])
        self.assertFalse(material["classification_support"])
        self.assertEqual(material["queries"][0]["version_check"]["status"], "unverified")
        with patch.object(adapter.commentary_source.source, "lookup", side_effect=RuntimeError("password=PRIVATE-COMMENTARY")):
            failed = adapter.build_request(payload())
            failed_material = failed["reference_packet"]["commentary"]
            self.assertEqual(failed_material["evidence_status"], "query_failed")
            self.assertNotIn("PRIVATE-COMMENTARY", json.dumps(failed))
            self.assertIn("查询失败", failed_material["queries"][0]["reason"])
            result = adapter.recommend(payload())
            self.assertIn("COMMENTARY_QUERY_FAILED", self.codes(result))
            self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)

    def test_p14_exceptions_degrade_not_not_found_or_supplier_work(self):
        hs, elements = Mock(), Mock()
        hs.lookup.side_effect = RuntimeError("password=SOURCE-SECRET /Users/private/db")
        elements.check.side_effect = RuntimeError("password=SOURCE-SECRET /Users/private/db")
        adapter = self.adapter(reference_bundle=replace(self.bundle, hs_source=hs, elements_source=elements))
        request = adapter.build_request(payload(), candidate_codes=[HS])
        for kind in ("hs", "elements"):
            self.assertEqual(request["reference_packet"]["p14"][0][kind]["status"], "query_failed")
        result = adapter.recommend(payload())
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertTrue({"HS_QUERY_FAILED", "ELEMENTS_QUERY_FAILED", "CLASSIFICATION_SUPPORT_PENDING"} <= self.codes(result))
        self.assertFalse(any(g.suggested_role == "supplier" for g in result.gaps))
        self.assertNotIn("SOURCE-SECRET", json.dumps(request))
        self.assertNotIn("SOURCE-SECRET", result.model_dump_json())

    def test_p14_returned_failure_and_wrong_query_are_conservative(self):
        source = Mock()
        actual = self.bundle.hs_source.lookup({"hs_code_raw": HS, "basis": {"tariff_version": "2026"}})
        source.lookup.return_value = actual
        adapter = self.adapter(reference_bundle=replace(self.bundle, hs_source=source))
        packet = adapter.build_request(payload(), candidate_codes=[HS])["reference_packet"]
        self.assertEqual(packet["p14"][0]["hs"]["status"], "query_failed")

    def test_bad_model_json_duplicate_keys_unknown_refs_and_extra_keys(self):
        valid = Model().answer
        for raw in ('not-json password=MODEL-SECRET', '```json\n{}\n```',
                    '{"candidates": [], "suggested_hs": null, "suggested_hs": null, "missing_facts": []}',
                    {**valid, "confidence": 0.9}, {**valid, "missing_facts": ["contract_id"]},
                    {**valid, "candidates": [{"hs": HS, "evidence_ids": [], "reason": "MODEL-SECRET"}]},
                    {**valid, "candidates": [{"hs": HS, "evidence_ids": ["invented-note"]}]}):
            with self.subTest(raw=raw), self.assertRaises(EngineTechnicalError) as caught:
                self.adapter(model=Model(raw)).recommend(payload())
            self.assertNotIn("MODEL-SECRET", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_model_exception_is_fixed_redacted(self):
        model = Mock()
        model.complete.side_effect = RuntimeError("token=MODEL-SECRET /Users/private")
        with self.assertRaisesRegex(EngineTechnicalError, "^预审模型调用失败$") as caught:
            self.adapter(model=model).recommend(payload())
        self.assertIsNone(caught.exception.__cause__)

    def test_empty_candidate_output_is_not_success(self):
        result = self.adapter(model=Model({"candidates": [], "suggested_hs": None, "missing_facts": ["material"]})).recommend(payload())
        self.assertIsNone(result.suggested_hs)
        self.assertTrue({"NO_INDEPENDENT_CANDIDATE", "MODEL_FACTS_PENDING"} <= self.codes(result))

    def test_input_request_and_bundle_metadata_remain_unchanged(self):
        item = payload()
        before = item.model_dump()
        metadata = copy.deepcopy(self.bundle.source_metadata)
        adapter = self.adapter()
        first = adapter.build_request(item, candidate_codes=[HS])
        first["skill"]["rules"] = "CHANGED"
        first["reference_packet"]["commentary"]["records"].clear()
        result = adapter.recommend(item)
        self.assertEqual(item.model_dump(), before)
        self.assertEqual(self.bundle.source_metadata, metadata)
        self.assertEqual(result.suggested_hs, HS)
        self.assertNotIn("CHANGED", adapter.build_request(item)["skill"]["rules"])
        self.assertNotEqual(adapter.model.calls[0]["facts"]["attributes"]["material"], "MODEL-MUTATED")

    def test_history_business_identifiers_are_anonymous_and_reference_only(self):
        record = HistoryRecord(record_id="BUSINESS-RECORD-42", historical_hs=HS,
                               historical_name="UNIT TEST TEXT", material="steel", recorded_at="2026-11-17",
                               outcome="approved", hs_version="2026", source={
                                   "document": "CONTRACT-BUSINESS-PRIVATE", "locator": "BUSINESS-ITEM-42"})
        store = Mock()
        store.load.return_value = HistorySnapshot(HistoryDatasetInfo(dataset_id="UNIT-HISTORY", synthetic=False), (record,))
        history = HistorySearchSource(store=store)
        adapter = self.adapter(reference_bundle=replace(self.bundle, history_source=history))
        request = adapter.build_request(payload())
        data = json.dumps(request)
        for identifier in (record.record_id, record.source.document, record.source.locator):
            self.assertNotIn(identifier, data)
        packet = request["reference_packet"]
        self.assertTrue(packet["history"]["records"])
        self.assertTrue(packet["history"]["records"][0]["reference_only"])
        entries = [e for e in packet["evidence_index"] if e["source_type"] == "history"]
        self.assertEqual(entries[0]["dataset"]["review_status"], "unknown")
        self.assertEqual(entries[0]["status"], "version_unverified")
        self.assertFalse(entries[0]["classification_support"])

    def test_no_env_default_sources_network_model_or_database_in_dry_run(self):
        with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv")), \
             patch("adapters.sources.history_search.default_history_store", side_effect=AssertionError("default")), \
             patch("socket.create_connection", side_effect=AssertionError("network")):
            adapter = self.adapter()
            with patch.object(adapter.model, "complete", side_effect=AssertionError("model")):
                adapter.build_request(payload(), candidate_codes=[HS])

    def test_run_precheck_original_code_only_enters_compare(self):
        adapter = self.adapter()
        original = "9900000000"
        result = adapter.run_precheck(payload(), supplier_hs_raw=original)
        self.assertEqual(len(adapter.model.calls), 1)
        self.assertNotIn(original, json.dumps(adapter.model.calls[0]))
        self.assertEqual(result.suggested_hs, HS)
        self.assertIsNotNone(result.comparison_status)

    def test_history_failure_is_not_empty_success(self):
        history = Mock()
        history.search.side_effect = RuntimeError("token=HISTORY-SECRET")
        adapter = self.adapter(reference_bundle=replace(self.bundle, history_source=history))
        request = adapter.build_request(payload())
        self.assertEqual(request["reference_packet"]["history"]["status"], "query_failed")
        self.assertNotIn("HISTORY-SECRET", json.dumps(request))
        result = adapter.recommend(payload())
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertIn("HISTORY_QUERY_FAILED", self.codes(result))

    def test_p14_source_returned_failure_preserves_failure_status(self):
        from adapters.sources.hs_lookup import HsLookupSource, TariffStoreError
        from adapters.sources.declaration_elements import DeclarationElementsSource
        store = Mock()
        store.load.side_effect = TariffStoreError("password=P14-STORE-SECRET")
        bundle = replace(self.bundle, hs_source=HsLookupSource(store), elements_source=DeclarationElementsSource(store))
        adapter = self.adapter(reference_bundle=bundle)
        result = adapter.recommend(payload())
        self.assertTrue({"HS_QUERY_FAILED", "ELEMENTS_QUERY_FAILED"} <= self.codes(result))
        self.assertEqual(result.evidence_status, EvidenceStatus.QUERY_FAILED)
        self.assertNotIn("P14-STORE-SECRET", result.model_dump_json())

    def test_expert_stage_is_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.adapter().expert_check(None)
