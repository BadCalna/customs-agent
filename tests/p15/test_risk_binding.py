"""PR35 P1/P2: retained facts keep their review state; HS is ASCII-only."""
from __future__ import annotations

import tempfile
import json
from pathlib import Path

from django.test import SimpleTestCase

from adapters.sources.history_import import _provenance_updates, import_history_templates
from adapters.sources.history_search import HistoryQuery, HistorySearchSource
from tests.p15.consolidated_fixtures import BASE_ROW, CONSOLIDATED_HEADERS, build_consolidated_workbook
from tests.p15.test_consolidated_import import _request, _records


class ReviewStateBindingTest(SimpleTestCase):
    def test_retained_invalid_hs_cannot_inherit_corrected_rows_clean_state(self):
        for original_id in (False, True):
            with self.subTest(original_id=original_id), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                headers = CONSOLIDATED_HEADERS + (("原始记录ID",) if original_id else ())
                row = dict(BASE_ROW, hs="730721000", original_record_id="SYN-1")
                path = build_consolidated_workbook(tmp / "in.xlsx", headers=headers, rows=[row])
                first = import_history_templates(_request(tmp, [path], skip_validation=True))
                old = _records(first)[0]
                old_flags = {key: old["attributes"].get(key, "") for key in (
                    "needs_review", "import_issue_codes", "blocking_issue_codes", "review_issue_codes")}
                self.assertEqual(old_flags["needs_review"], "true")
                build_consolidated_workbook(path, headers=headers, rows=[dict(row, hs="7307210000")])
                retained = import_history_templates(_request(tmp, [path], skip_validation=True))
                record = _records(retained)[0]
                self.assertEqual(record["historical_hs"], "730721000")
                self.assertEqual({key: record["attributes"].get(key, "") for key in old_flags}, old_flags)
                self.assertEqual(retained.report["totals"]["records_needs_review"], 1)
                self.assertEqual(retained.report["totals"]["records_changed"], 1)
                query = HistoryQuery(name=row["name"], elements=old["elements"], material=old["material"], spec=old["spec"])
                self.assertEqual(HistorySearchSource.load(retained.records_path).search(query).candidates, [])
                audit = HistorySearchSource.load(retained.records_path, include_needs_review=True).search(query)
                self.assertTrue(audit.candidates[0].needs_review)
                # Explicit replacement binds the corrected business data to corrected flags.
                replaced = import_history_templates(_request(tmp, [path], replace_changed=True))
                record = _records(replaced)[0]
                self.assertEqual(record["historical_hs"], "7307210000")
                self.assertEqual(record["attributes"]["needs_review"], "false")
                self.assertTrue(HistorySearchSource.load(replaced.records_path).search(query).candidates)

    def test_retained_clean_record_does_not_inherit_incoming_invalid_flags(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW)])
            first = import_history_templates(_request(tmp, [path]))
            old = _records(first)[0]
            build_consolidated_workbook(path, rows=[dict(BASE_ROW, hs="730721000")])
            out = import_history_templates(_request(tmp, [path]))
            record = _records(out)[0]
            self.assertEqual(record["historical_hs"], old["historical_hs"])
            self.assertEqual(record["attributes"]["needs_review"], "false")
            self.assertEqual(record["attributes"].get("review_issue_codes", ""), "")

    def test_already_corrupted_snapshot_recovers_review_for_retained_hs(self):
        for hs in ("730721000", "７３０７２１００００"):
            with self.subTest(hs=hs), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                path = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW, hs=hs)])
                first = import_history_templates(_request(tmp, [path], skip_validation=True))
                payload = json.loads(first.records_path.read_text())
                payload["records"][0]["attributes"].update({
                    "needs_review": "false", "review_issue_codes": "", "blocking_issue_codes": "", "import_issue_codes": ""})
                # Emulate a pre-fix broken flat snapshot, without altering published immutable generations.
                broken = tmp / "broken-output"
                broken.mkdir()
                (broken / "history_records.json").write_text(json.dumps(payload))
                build_consolidated_workbook(path, rows=[dict(BASE_ROW)])
                out = import_history_templates(_request(tmp, [path], out_dir=broken, skip_validation=True))
                record = _records(out)[0]
                self.assertEqual(record["historical_hs"], hs)
                self.assertEqual(record["attributes"]["needs_review"], "true")
                self.assertEqual(out.report["totals"]["records_review_state_repaired"], 1)
                self.assertEqual(out.report["review_state_repairs"][0]["record_id"], record["record_id"])
                self.assertEqual(HistorySearchSource.load(out.records_path).search(HistoryQuery(name=BASE_ROW["name"])).candidates, [])
                repeat = import_history_templates(_request(tmp, [path], out_dir=broken, skip_validation=True))
                self.assertEqual(repeat.report["totals"]["records_review_state_repaired"], 0)

    def test_repeated_uncertain_identity_does_not_clear_merge_review(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            a = dict(BASE_ROW, elements="材质：甲|规格：A")
            b = dict(BASE_ROW, elements="材质：乙|规格：B")
            path = build_consolidated_workbook(tmp / "in.xlsx", rows=[a, b])
            import_history_templates(_request(tmp, [path]))
            build_consolidated_workbook(path, rows=[dict(a, remark="changed-a"), dict(b, remark="changed-b")])
            out = import_history_templates(_request(tmp, [path], accept_conflicts=True))
            uncertain = {r["record_id"] for r in _records(out) if r["attributes"]["needs_review"] == "true"}
            self.assertEqual(len(uncertain), 2)
            repeat = import_history_templates(_request(tmp, [path], accept_conflicts=True))
            for record in _records(repeat):
                if record["record_id"] in uncertain:
                    self.assertEqual(record["attributes"]["needs_review"], "true")
                    self.assertIn("identity_uncertain", record["attributes"]["review_issue_codes"])
            candidates = HistorySearchSource.load(repeat.records_path).search(HistoryQuery(name=BASE_ROW["name"])).candidates
            self.assertTrue(uncertain.isdisjoint({c.record_id for c in candidates}))

    def test_same_content_image_copy_can_update_provenance(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW)], with_image=True)
            first = import_history_templates(_request(tmp, [path]))
            old_id = _records(first)[0]["record_id"]
            out = import_history_templates(_request(tmp, [path], copy_images=True))
            record = _records(out)[0]
            self.assertEqual(record["record_id"], old_id)
            self.assertTrue((out.out_dir / record["attributes"]["image_output_path"]).is_file())
            self.assertEqual(out.report["totals"]["records_duplicate"], 1)

    def test_business_change_preserves_blocking_codes_and_identity_metadata(self):
        old = {"record_id": "same", "historical_hs": "730721000", "attributes": {
            "source_format": "consolidated", "content_hash": "old", "needs_review": "true",
            "import_issue_codes": "missing_contract", "blocking_issue_codes": "missing_contract",
            "review_issue_codes": "missing_contract", "change_anchor": "old-anchor",
            "business_key": "old-key", "change_key": "old-change"}}
        new = {"record_id": "same", "historical_hs": "7307210000", "attributes": {
            "source_format": "consolidated", "content_hash": "new", "needs_review": "false",
            "change_anchor": "new-anchor", "business_key": "new-key", "change_key": "new-change"}}
        updates = _provenance_updates(old, new)
        for key in ("needs_review", "import_issue_codes", "blocking_issue_codes", "review_issue_codes",
                    "change_anchor", "business_key", "change_key"):
            self.assertNotIn(key, updates)


class AsciiHsTest(SimpleTestCase):
    def test_unicode_digits_preserved_and_reviewed_not_default_searchable(self):
        for hs in ("７３０７２１００００", "٧٣٠٧٢١٠٠٠٠", "730721000０"):
            with self.subTest(hs=hs), tempfile.TemporaryDirectory() as raw:
                tmp = Path(raw)
                path = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW, hs=hs)])
                out = import_history_templates(_request(tmp, [path], skip_validation=True))
                record = _records(out)[0]
                self.assertEqual(record["historical_hs"], hs)
                self.assertEqual(record["attributes"]["hs_raw"], hs)
                self.assertEqual(record["attributes"]["needs_review"], "true")
                self.assertIn("hs_non_ascii_digits", record["attributes"]["review_issue_codes"])
                quality = out.report["files"][0]["data_quality"]["hs"]
                self.assertEqual(len(quality["non_10_digit_rows"]), 1)
                query = HistoryQuery(name=record["historical_name"], elements=record["elements"])
                self.assertEqual(HistorySearchSource.load(out.records_path).search(query).candidates, [])
                audit = HistorySearchSource.load(out.records_path, include_needs_review=True).search(query)
                self.assertTrue(audit.candidates[0].needs_review)

    def test_ascii_hs_remains_clean_and_searchable(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = build_consolidated_workbook(tmp / "in.xlsx", rows=[dict(BASE_ROW)])
            out = import_history_templates(_request(tmp, [path]))
            self.assertEqual(_records(out)[0]["attributes"]["needs_review"], "false")
            self.assertTrue(HistorySearchSource.load(out.records_path).search(HistoryQuery(name=BASE_ROW["name"])).candidates)
