"""Local material retrieval tests: never classification support and never DB I/O."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from adapters.engines.commentary_reference import (
    CommentaryReferenceSource, candidate_commentary_keys,
)
from adapters.sources.commentary_text import render_commentary_text
from adapters.sources.tariff_commentary import (
    ENV_COMMENTARY_PATH, EnvCommentaryStore, JsonCommentaryStore, TariffCommentarySource,
)


class CommentaryReferenceTest(SimpleTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "commentary.json"
        self.payload = {
            "schema_version": "p23.commentary.1", "version": {"version": "fixture-v1"},
            "dataset": {"dataset_id": "synthetic-commentary", "source_type": "synthetic",
                        "authority": "synthetic", "synthetic": True, "review_status": "unreviewed"},
            "records": [self.record(key, kind) for key, kind in (
                ("0101", "品目"), ("CH1", "章注"), ("CA1", "类注"), ("ot3", "其他注释"))],
        }
        self.save()

    def record(self, key, kind, texts=("原文甲\n保留空白  ", "原文乙：合成发现词")):
        sections = [{"label": label, "text": text, "anchor": f"{key}#{label}-{index}"}
                    for index, (label, text) in enumerate(zip(("品目注释", "子目注释"), texts), 1)]
        text_all = render_commentary_text(sections)
        return {"tariff_no": key, "kind": kind, "sections": sections, "text_all": text_all,
                "text_sha256": hashlib.sha256(text_all.encode()).hexdigest(),
                "text_length": len(text_all), "section_char_total": sum(len(x["text"]) for x in sections),
                "source": {"document": "synthetic-original.json", "locator": f"items[{key}]",
                           "retrieved_at": "2026-11-17T00:00:00+08:00"}}

    def save(self):
        self.path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")

    def adapter(self, **kwargs):
        return CommentaryReferenceSource(store=JsonCommentaryStore(self.path), **kwargs)

    def test_explicit_dependency_required(self):
        with self.assertRaises(ValueError):
            CommentaryReferenceSource()
        with self.assertRaises(ValueError):
            CommentaryReferenceSource(source=object(), store=object())
        for kwargs in ({"max_total_chars": True}, {"keyword_limit": 0}, {"max_queries": 201}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.adapter(**kwargs)

    def test_candidate_mapping_preserves_leading_zero_and_section_ranges(self):
        for code, expected in (
            ("0101210010", ["0101", "CH1", "CA1"]),
            ("1500000000", ["1500", "CH15", "CA3"]),
            ("7200000000", ["7200", "CH72", "CA15"]),
            ("8500000000", ["8500", "CH85", "CA16"]),
            ("9700000000", ["9700", "CH97", "CA21"]),
            ("7700000000", ["7700", "CH77"]),
            ("9800000000", ["9800", "CH98"]),
            ("9900000000", ["9900", "CH99"]),
        ):
            with self.subTest(code=code):
                self.assertEqual(candidate_commentary_keys(code), expected)

        for chapter, section in ((5, 1), (6, 2), (14, 2), (15, 3), (16, 4),
                                 (24, 4), (25, 5), (71, 14), (72, 15),
                                 (83, 15), (84, 16), (85, 16), (86, 17),
                                 (89, 17), (90, 18), (92, 18), (93, 19),
                                 (94, 20), (96, 20), (97, 21)):
            with self.subTest(chapter=chapter):
                self.assertEqual(candidate_commentary_keys(f"{chapter:02d}00000000")[-1], f"CA{section}")

    def test_lookup_rejects_direct_eight_and_ten_digit_codes(self):
        for raw in ("01012100", "0101210010"):
            with self.subTest(raw=raw):
                packet = self.adapter().lookup(raw)
                self.assertEqual(packet["query_status"], "invalid_input")
                self.assertIsNone(packet["evidence_status"])
                self.assertIn("never 8/10-digit", packet["queries"][0]["reason"])
                self.assertEqual(packet["records"], [])

    def test_invalid_candidates_and_keys_never_reach_p23(self):
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        adapter = CommentaryReferenceSource(source=source)
        for kwargs in ({"candidate_codes": ["01012100"]}, {"candidate_codes": [101210010]},
                       {"candidate_codes": ["０１０１２１００１０"]}, {"candidate_codes": ["0000000000"]},
                       {"keys": ["0101210010"]}, {"keys": ["01012100"]},
                       {"keys": ["CA99"]}, {"keys": "0101"}, {"candidate_codes": "0101210010"}):
            with self.subTest(kwargs=kwargs), patch.object(source, "lookup") as lookup:
                packet = adapter.retrieve(**kwargs)
                self.assertEqual(packet["query_status"], "invalid_input")
                self.assertFalse(packet["material_retrieved"])
                self.assertEqual(packet["records"], [])
                lookup.assert_not_called()

    def test_mapping_queries_only_definite_keys_and_deduplicates(self):
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        with patch.object(source, "lookup", wraps=source.lookup) as lookup:
            packet = CommentaryReferenceSource(source).retrieve(
                ["0101210010", "0101290090"], keys=["ch01", "CA1", "ot3"], expected_version="fixture-v1")
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ["0101", "CH1", "CA1", "ot3"])
        self.assertEqual([r["level"] for r in packet["records"]],
                         ["heading", "chapter_note", "section_note", "other_note"])
        self.assertTrue(packet["material_retrieved"])
        self.assertFalse(packet["classification_support"])
        self.assertEqual(packet["applicability"], "not_assessed")
        self.assertTrue(all(not x["classification_support"] for x in packet["candidate_mappings"]))

    def test_evidence_packet_original_sections_not_p23_excerpt(self):
        self.payload["records"][0] = self.record("0101", "品目", ("原" * 900, "末尾完整"))
        self.save()
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        record, evidence = packet["records"][0], packet["evidence"][0]
        self.assertEqual(record["sections"], self.payload["records"][0]["sections"])
        self.assertEqual(evidence["evidence_id"], "commentary:fixture-v1:0101")
        for item in (record, evidence):
            self.assertEqual(item["document"], "synthetic-original.json")
            self.assertEqual(item["locator"], "items[0101]")
            self.assertEqual(item["version"], "fixture-v1")
            self.assertEqual(item["status"], "obtained")
            self.assertFalse(item["classification_support"])
            self.assertFalse(item["truncated"])
        self.assertEqual(packet["limits"]["text_characters_included"], 904)
        self.assertFalse(packet["truncated"])
        self.assertNotIn("excerpt", evidence)
        self.assertNotIn("text_all", record)
        self.assertEqual(packet, self.adapter().lookup("0101", expected_version="fixture-v1"),
                         "packet must not inherit P23's wall-clock query timestamp")

    def test_missing_or_mismatched_version_is_material_but_unverified(self):
        for expected in ("", "different-v2"):
            with self.subTest(expected=expected):
                packet = self.adapter().lookup("0101", expected_version=expected)
                self.assertTrue(packet["material_retrieved"])
                self.assertEqual(packet["evidence_status"], "version_unverified")
                self.assertEqual(packet["evidence"][0]["status"], "version_unverified")
                self.assertFalse(packet["classification_support"])
        self.payload["version"] = ""
        self.save()
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        self.assertEqual(packet["evidence_status"], "version_unverified")

    def test_synthetic_and_official_unreviewed_not_upgraded(self):
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        self.assertTrue(packet["datasets"][0]["synthetic"])
        self.assertFalse(packet["datasets"][0]["authoritative"])
        self.payload["dataset"].update(source_type="official_tariff", authority="authoritative", synthetic=False)
        self.save()
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        self.assertEqual(packet["datasets"][0]["authority"], "authoritative")
        self.assertEqual(packet["datasets"][0]["review_status"], "unreviewed")
        self.assertFalse(packet["datasets"][0]["authoritative"])
        self.assertFalse(packet["classification_support"])

    def test_reviewed_real_dataset_still_never_classification_support(self):
        self.payload["dataset"].update(source_type="official_tariff", authority="authoritative", synthetic=False,
                                       review_status="reviewed", reviewed_by="not-for-model",
                                       reviewed_at="2026-11-18", retrieved_at="2026-11-17")
        self.save()
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        self.assertTrue(packet["datasets"][0]["authoritative"])
        self.assertFalse(packet["classification_support"])
        self.assertNotIn("not-for-model", json.dumps(packet))

    def test_keyword_is_discovery_only_with_record_level_not_query_level(self):
        packet = self.adapter().retrieve(keyword="合成发现词", expected_version="fixture-v1")
        self.assertEqual(packet["queries"][0]["mode"], "keyword_discovery")
        self.assertTrue(packet["material_retrieved"])
        self.assertEqual(packet["candidate_mappings"], [])
        self.assertEqual([r["level"] for r in packet["records"]],
                         ["heading", "chapter_note", "section_note", "other_note"])
        self.assertTrue(all(not r["classification_support"] for r in packet["records"]))

    def test_whole_section_limit_marks_omission_and_no_substring(self):
        first, second = self.payload["records"][0]["sections"]
        packet = self.adapter(max_total_chars=len(second["text"])).lookup("0101")
        record = packet["records"][0]
        self.assertTrue(packet["truncated"])
        self.assertTrue(record["truncated"])
        self.assertFalse(record["complete"])
        self.assertEqual(record["sections"], [second])
        self.assertEqual(record["omitted_sections"][0]["anchor"], first["anchor"])
        self.assertEqual(packet["limits"]["text_characters_omitted"], len(first["text"]))
        packet = self.adapter(max_total_chars=1).lookup("0101")
        self.assertEqual(packet["records"][0]["sections"], [])
        self.assertTrue(packet["material_retrieved"])
        self.assertTrue(packet["evidence"][0]["truncated"])

    def test_total_limit_shared_between_keys_and_keyword_duplicates(self):
        packet = self.adapter(max_total_chars=25).retrieve(
            keys=["0101", "CH1"], keyword="合成发现词")
        self.assertLessEqual(sum(len(s["text"]) for r in packet["records"] for s in r["sections"]), 25)
        self.assertEqual(len(packet["records"]), 4)
        self.assertEqual(len({r["evidence_id"] for r in packet["records"]}), 4)
        self.assertTrue(packet["truncated"])

    def test_source_unconfigured_has_no_fallback(self):
        with patch.dict("os.environ", {ENV_COMMENTARY_PATH: ""}):
            packet = CommentaryReferenceSource(store=EnvCommentaryStore()).lookup("0101")
        self.assertEqual(packet["query_status"], "source_unconfigured")
        self.assertEqual(packet["evidence_status"], "query_failed")
        self.assertFalse(packet["material_retrieved"])
        self.assertEqual(packet["records"], [])

    def test_missing_path_and_tampered_body_are_failure_not_not_found(self):
        packet = CommentaryReferenceSource(store=JsonCommentaryStore(self.root / "missing.json")).lookup("0101")
        self.assertEqual(packet["query_status"], "source_unreadable")
        self.payload["records"][0]["text_all"] += "篡改"
        self.save()
        packet = self.adapter().lookup("0101")
        self.assertEqual(packet["query_status"], "source_unreadable")
        self.assertEqual(packet["evidence_status"], "query_failed")
        self.assertEqual(packet["evidence"], [])

    def test_not_found_structured_and_does_not_claim_no_commentary_exists(self):
        packet = self.adapter().lookup("9999", expected_version="fixture-v1")
        self.assertEqual(packet["query_status"], "ok")
        self.assertEqual(packet["evidence_status"], "not_found")
        self.assertFalse(packet["material_retrieved"])
        self.assertIn("不等于", packet["queries"][0]["reason"])

    def test_not_found_requires_explicit_matching_snapshot_version(self):
        for query in ({"keys": ["9999"]}, {"keyword": "不存在的发现关键词"}):
            for expected, status in (("", "version_unverified"),
                                     ("different-v2", "version_unverified"),
                                     ("fixture-v1", "not_found")):
                with self.subTest(query=query, expected=expected):
                    packet = self.adapter().retrieve(**query, expected_version=expected)
                    self.assertEqual(packet["query_status"], "ok")
                    self.assertEqual(packet["evidence_status"], status)
                    self.assertEqual(packet["queries"][0]["evidence_status"], status)
                    check = packet["queries"][0]["version_check"]
                    self.assertEqual(check["expected"], expected)
                    self.assertEqual(check["actual"], "fixture-v1")
                    self.assertEqual(check["status"], "verified" if status == "not_found" else "unverified")
                    self.assertFalse(packet["material_retrieved"])
                    self.assertFalse(packet["classification_support"])
                    self.assertEqual(packet["records"], [])
                    self.assertEqual(packet["evidence"], [])
        self.payload["version"] = ""
        self.save()
        packet = self.adapter().lookup("9999", expected_version="fixture-v1")
        self.assertEqual(packet["evidence_status"], "version_unverified")
        self.assertEqual(packet["queries"][0]["version_check"]["actual"], "")

    def test_source_exception_sanitized_and_never_fabricates_evidence(self):
        class Broken:
            def lookup(self, query):
                raise RuntimeError("secret source contents")
        packet = CommentaryReferenceSource(Broken()).lookup("0101")
        self.assertEqual(packet["query_status"], "source_error")
        self.assertEqual(packet["evidence_status"], "query_failed")
        self.assertNotIn("secret source contents", json.dumps(packet))
        self.assertEqual(packet["evidence"], [])

    def test_misleveled_record_and_wrong_key_fail_closed(self):
        self.payload["records"][0]["kind"] = "章注"
        self.save()
        packet = self.adapter().lookup("0101")
        self.assertEqual(packet["query_status"], "source_error")
        self.assertEqual(packet["records"], [])
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        class WrongKey:
            def lookup(self, query):
                return source.lookup("CH1")
        packet = CommentaryReferenceSource(WrongKey()).lookup("0101")
        self.assertEqual(packet["query_status"], "source_error")
        self.assertEqual(packet["evidence"], [])

    def test_upstream_unverified_and_failed_evidence_never_upgraded(self):
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        result = source.lookup("0101").model_dump(mode="json")
        result["evidence_status"] = "version_unverified"
        result["evidence"][0]["status"] = "version_unverified"
        class Fake:
            def lookup(self, query):
                return result
        packet = CommentaryReferenceSource(Fake()).lookup("0101", expected_version="fixture-v1")
        self.assertEqual(packet["evidence_status"], "version_unverified")
        self.assertEqual(packet["records"][0]["status"], "version_unverified")
        result["evidence"][0]["status"] = "query_failed"
        packet = CommentaryReferenceSource(Fake()).lookup("0101", expected_version="fixture-v1")
        self.assertEqual(packet["query_status"], "source_error")
        self.assertEqual(packet["records"], [])
        self.assertFalse(packet["material_retrieved"])

    def test_mixed_source_failure_keeps_retrieved_material_but_no_success_aggregate(self):
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        class Mixed:
            def lookup(self, query):
                if query == "CH1":
                    raise RuntimeError("failure")
                return source.lookup(query)
        packet = CommentaryReferenceSource(Mixed()).retrieve(["0101210010"], expected_version="fixture-v1")
        self.assertTrue(packet["material_retrieved"])
        self.assertEqual(packet["query_status"], "source_error")
        self.assertEqual(packet["evidence_status"], "query_failed")
        self.assertEqual([r["key"] for r in packet["records"]], ["0101", "CA1"])
        self.assertFalse(packet["classification_support"])

    def test_unspecified_synthetic_remains_unknown(self):
        self.payload["dataset"].update(source_type="official_tariff", authority="authoritative", synthetic=None)
        self.save()
        packet = self.adapter().lookup("0101", expected_version="fixture-v1")
        self.assertIsNone(packet["datasets"][0]["synthetic"])
        self.assertFalse(packet["datasets"][0]["authoritative"])
        self.assertFalse(packet["classification_support"])

    def test_store_failures_do_not_leak_wrapped_reason_or_issue_messages(self):
        from adapters.sources.tariff_commentary import (
            CommentaryStoreError, CommentaryStoreNotConfigured,
        )
        private = "/Users/private/config.json password=not-for-skill"
        for error, status in ((CommentaryStoreError, "source_unreadable"),
                              (CommentaryStoreNotConfigured, "source_unconfigured"),
                              (RuntimeError, "source_error")):
            class FailedStore:
                def load(self):
                    raise error(private)
            with self.subTest(status=status):
                packet = CommentaryReferenceSource(store=FailedStore()).lookup("0101")
                self.assertEqual(packet["query_status"], status)
                self.assertEqual(packet["evidence_status"], "query_failed")
                self.assertNotIn(private, json.dumps(packet))
                self.assertNotIn("/Users/private", json.dumps(packet))
                self.assertNotIn("not-for-skill", json.dumps(packet))
                self.assertEqual(packet["records"], [])
                self.assertEqual(packet["evidence"], [])
                query = packet["queries"][0]
                self.assertEqual(query["issues"][0]["message"], query["reason"])
        packet = CommentaryReferenceSource(store=JsonCommentaryStore(
            self.root / "private-filename.json")).lookup("0101")
        self.assertNotIn("private-filename", json.dumps(packet))
        self.assertNotIn(str(self.root), json.dumps(packet))

    def test_reviewed_official_source_unknown_synthetic_fails_closed(self):
        self.payload["dataset"].update(source_type="official_tariff", authority="authoritative", synthetic=None,
                                       review_status="reviewed", reviewed_by="private-reviewer",
                                       reviewed_at="2026-11-18", retrieved_at="2026-11-17")
        self.save()
        underlying = TariffCommentarySource(JsonCommentaryStore(self.path))
        self.assertTrue(underlying.lookup("0101").dataset.authoritative,
                        "regression must exercise permissive upstream authoritative property")
        packet = CommentaryReferenceSource(source=underlying).lookup("0101", expected_version="fixture-v1")
        for dataset in (packet["datasets"][0], packet["records"][0]["dataset"], packet["evidence"][0]["dataset"]):
            self.assertIsNone(dataset["synthetic"])
            self.assertFalse(dataset["authoritative"])
            self.assertEqual(dataset["verification_status"], "unverified")
            self.assertEqual(dataset["verification_reason"], "synthetic_status_undeclared")
        self.assertTrue(packet["material_retrieved"])
        self.assertFalse(packet["classification_support"])
        self.assertNotIn("private-reviewer", json.dumps(packet))

    def test_query_limit_rejects_before_partial_reads(self):
        source = TariffCommentarySource(JsonCommentaryStore(self.path))
        with patch.object(source, "lookup") as lookup:
            packet = CommentaryReferenceSource(source, max_queries=2).retrieve(["0101210010"])
        self.assertEqual(packet["query_status"], "invalid_input")
        lookup.assert_not_called()
