"""Synthetic-only P15 persistence contracts; Django always creates a TEST database."""
import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test import TestCase

from adapters.sources.history_consolidated import CONSOLIDATED_IDENTITY_VERSION
from adapters.sources.history_search import HistoryQuery, HistorySearchSource, HistoryStoreError
from apps.history_reference.models import (
    HistoricalRecord, HistoricalRecordRevision, HistoryDataset, HistoryImportBatch,
)
from apps.history_reference.services import HistoryImportError, import_history_snapshot
from apps.history_reference.store import DatabaseHistoryStore


def row(record_id="r1", *, anchor="a1", hs="7307210000", review=False, material="不锈钢"):
    return {
        "record_id": record_id, "historical_hs": hs, "historical_name": "测试法兰盘",
        "elements": {"用途": "管道连接"}, "material": material, "spec": "DN50",
        "recorded_at": "未提供", "outcome": "approved", "hs_version": None,
        "applicable_scope": None, "valid_until": None, "notes": "完整原文，不能丢失",
        "source": {"document": "fixture/a.xlsx", "locator": "Sheet1!2", "url": None,
                   "system": "synthetic-historical-consolidated"},
        "attributes": {"dataset_kind": "synthetic", "source_format": "consolidated",
                       "source_file_id": "fixture", "identity_version": CONSOLIDATED_IDENTITY_VERSION,
                       "content_hash": record_id + hs + material, "change_anchor": anchor,
                       "business_key": anchor, "change_key": anchor,
                       "needs_review": "true" if review else "false",
                       "blocking_issue_codes": "missing_contract" if review else "",
                       "elements_raw": "用途：管道连接", "opaque_raw": "原样保留"},
    }


def payload(records=None):
    records = [row()] if records is None else records
    return {"schema_version": "p15.history.1", "dataset": {
        "dataset_id": "unit-fixture", "synthetic": True, "description": "合成测试",
        "record_count": len(records), "input_formats": ["consolidated"] if records else [],
        "identity_version": CONSOLIDATED_IDENTITY_VERSION,
        "source_files": [{"source_id": "fixture", "filename": "a.xlsx"}],
    }, "records": records}


class PersistenceTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "records.json"

    def ingest(self, data=None, **kwargs):
        self.path.write_text(json.dumps(data or payload(), ensure_ascii=False), encoding="utf-8")
        return import_history_snapshot(self.path, "unit", allow_sqlite=True, **kwargs)

    def assert_empty(self):
        self.assertEqual(HistoryDataset.objects.count(), 0)
        self.assertEqual(HistoricalRecord.objects.count(), 0)
        self.assertEqual(HistoryImportBatch.objects.count(), 0)
        self.assertEqual(HistoricalRecordRevision.objects.count(), 0)

    def test_first_then_repeat_and_complete_dto(self):
        first = self.ingest()
        second = self.ingest()
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["duplicate"], 1)
        self.assertEqual(HistoricalRecord.objects.count(), 1)
        self.assertEqual(HistoricalRecordRevision.objects.count(), 1)
        self.assertEqual(HistoryImportBatch.objects.count(), 2)
        loaded = DatabaseHistoryStore("unit").load().records[0]
        self.assertEqual(loaded.model_dump(), payload()["records"][0])

    def test_changed_same_id_keeps_old_risk_and_full_source(self):
        old = row(review=True)
        self.ingest(payload([old]))
        clean = row(hs="7307990000", material="碳钢")
        clean["source"]["locator"] = "Sheet1!500"
        report = self.ingest(payload([clean]))
        stored = HistoricalRecord.objects.get()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["changed_kept"], 1)
        self.assertEqual(stored.payload, old)
        self.assertTrue(stored.needs_review)
        self.assertEqual(stored.blocking_issue_codes, ["missing_contract"])
        self.assertEqual(stored.revision, 1)

    def test_replace_clears_risk_and_audits_previous_revision(self):
        self.ingest(payload([row(review=True)]))
        pk = HistoricalRecord.objects.get().pk
        report = self.ingest(payload([row(hs="7307990000")]), replace_changed=True)
        stored = HistoricalRecord.objects.get()
        self.assertEqual(report["replaced"], 1)
        self.assertEqual(stored.pk, pk)
        self.assertFalse(stored.needs_review)
        self.assertEqual(stored.blocking_issue_codes, [])
        self.assertEqual(stored.revision, 2)
        self.assertEqual(stored.revisions.get(revision=1).payload["historical_hs"], "7307210000")

    def test_changed_content_identity_is_not_appended_and_replace_keeps_physical_pk(self):
        self.ingest(payload([row(review=True)]))
        pk = HistoricalRecord.objects.get().pk
        corrected = payload([row("r2", hs="7307990000")])
        report = self.ingest(corrected)
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["changed_kept"], 1)
        self.assertEqual(HistoricalRecord.objects.get().record_id, "r1")
        report = self.ingest(corrected, replace_changed=True)
        self.assertEqual(report["replaced"], 1)
        stored = HistoricalRecord.objects.get()
        self.assertEqual(stored.record_id, "r2")
        self.assertEqual(stored.pk, pk)
        self.assertEqual(list(stored.revisions.order_by("revision").values_list("historical_record_id", flat=True)), ["r1", "r2"])

    def test_unicode_hs_restores_review_on_actual_retained_content(self):
        original = row(hs="７３０７２１００００")
        self.ingest(payload([original]))
        self.assertTrue(HistoricalRecord.objects.get().needs_review)
        report = self.ingest(payload([row(hs="7307210000")]))
        stored = HistoricalRecord.objects.get()
        self.assertEqual(stored.historical_hs, original["historical_hs"])
        self.assertTrue(stored.needs_review)
        self.assertEqual(report["changed_kept"], 1)
        self.assertIn("hs_non_ascii_digits", stored.payload["attributes"]["review_issue_codes"])

    def test_mixed_dataset_refused(self):
        self.ingest()
        real = payload()
        real["dataset"]["synthetic"] = False
        real["records"][0]["source"]["system"] = "historical-consolidated-import"
        real["records"][0]["attributes"]["dataset_kind"] = "real"
        with self.assertRaises(HistoryImportError):
            self.ingest(real)
        self.assertEqual(HistoryImportBatch.objects.count(), 1)

    def test_require_real_refuses_synthetic(self):
        with self.assertRaises(HistoryImportError):
            self.ingest(require_real=True)
        self.assert_empty()

    def test_duplicate_ids_refuse_whole_batch(self):
        with self.assertRaises(HistoryImportError):
            self.ingest(payload([row(), row()]))
        self.assert_empty()

    def test_bad_schema_refuses_whole_batch(self):
        for bad in ("unknown", None):
            data = payload()
            data["schema_version"] = bad
            with self.assertRaises(HistoryImportError):
                self.ingest(data)
        self.assert_empty()

    def test_bad_row_refuses_whole_batch(self):
        data = payload([row(), row("r2", anchor="a2")])
        del data["records"][1]["source"]
        with self.assertRaises(HistoryImportError):
            self.ingest(data)
        self.assert_empty()

    def test_unknown_or_mixed_source_refused(self):
        for system in ("unknown", "historical-consolidated-import"):
            data = payload()
            data["records"][0]["source"]["system"] = system
            with self.assertRaises(HistoryImportError):
                self.ingest(data)
        self.assert_empty()

    def test_wrong_expected_count_no_writes(self):
        with self.assertRaises(HistoryImportError):
            self.ingest(expected_count=1198)
        self.assert_empty()

    def test_declared_count_wrong_no_writes(self):
        data = payload()
        data["dataset"]["record_count"] = 2
        with self.assertRaises(HistoryImportError):
            self.ingest(data)
        self.assert_empty()

    def test_missing_reported_and_kept(self):
        self.ingest(payload([row(), row("r2", anchor="a2")]))
        report = self.ingest()
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["total"], 2)
        self.assertEqual(len(DatabaseHistoryStore("unit").load().records), 2)

    def test_failure_in_middle_rolls_back_dataset_records_batch_and_revisions(self):
        from django.db.models.query import QuerySet
        original = QuerySet.create
        calls = []
        def failing(queryset, *args, **kwargs):
            if queryset.model is HistoricalRecordRevision:
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError("injected failure")
            return original(queryset, *args, **kwargs)
        with patch.object(QuerySet, "create", failing):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.ingest(payload([row(), row("r2", anchor="a2")]))
        self.assert_empty()

    def test_failure_preserves_previous_generation(self):
        self.ingest()
        with patch("apps.history_reference.services._record_values", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.ingest(payload([row(hs="7307990000")]), replace_changed=True)
        self.assertEqual(HistoricalRecord.objects.get().historical_hs, "7307210000")
        self.assertEqual(HistoryDataset.objects.get().revision, 1)
        self.assertEqual(HistoryImportBatch.objects.count(), 1)

    def test_dry_run_no_writes(self):
        report = self.ingest(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["added"], 1)
        self.assert_empty()

    def test_database_store_search_exclusion_diff_and_no_match(self):
        self.ingest(payload([row(review=True), row("r2", anchor="a2")]))
        source = HistorySearchSource(store=DatabaseHistoryStore("unit"))
        found = source.search(HistoryQuery(name="测试法兰盘", material="碳钢"))
        self.assertEqual(found.outcome.value, "matched")
        self.assertEqual(found.excluded_needs_review, 1)
        self.assertEqual(found.candidates[0].diff_keys, ["材质"])
        self.assertTrue(found.candidates[0].key_attribute_conflict)
        self.assertEqual(source.search({"name": "完全不相关的橡胶玩具"}).outcome.value, "no_match")

    def test_missing_and_empty_dataset_query_failed(self):
        source = HistorySearchSource(store=DatabaseHistoryStore("absent"))
        self.assertEqual(source.search({"name": "法兰"}).outcome.value, "query_failed")
        HistoryDataset.objects.create(key="empty", synthetic=True, metadata={})
        with self.assertRaises(HistoryStoreError):
            DatabaseHistoryStore("empty").load()

    def test_store_corruption_detected_and_outer_transaction_remains_usable(self):
        self.ingest()
        HistoricalRecord.objects.update(needs_review=True)
        with transaction.atomic():
            self.assertEqual(HistorySearchSource(store=DatabaseHistoryStore("unit")).search({"name": "法兰"}).outcome.value, "query_failed")
            self.assertEqual(HistoryDataset.objects.count(), 1)

    def test_store_count_mismatch_query_failed(self):
        self.ingest()
        HistoryDataset.objects.update(record_count=2)
        with self.assertRaises(HistoryStoreError):
            DatabaseHistoryStore("unit").load()

    def test_sql_direct_fields_and_count_match_snapshot(self):
        self.ingest(payload([row(), row("r2", anchor="a2", review=True)]))
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*), SUM(CASE WHEN needs_review THEN 1 ELSE 0 END) FROM history_reference_historicalrecord")
            self.assertEqual(cursor.fetchone(), (2, 1))
            cursor.execute("SELECT historical_hs, historical_name FROM history_reference_historicalrecord WHERE record_id = %s", ["r1"])
            self.assertEqual(cursor.fetchone(), ("7307210000", "测试法兰盘"))
        self.assertEqual(len(DatabaseHistoryStore("unit").load().records), 2)

    def test_1200_row_scale_not_hs_name_dedup(self):
        data = payload([row(f"r{i}", anchor=f"a{i}", material=f"材质{i}") for i in range(1200)])
        report = self.ingest(data, expected_count=1200)
        self.assertEqual(report["added"], 1200)
        self.assertEqual(self.ingest(data)["added"], 0)
        self.assertEqual(len(DatabaseHistoryStore("unit").load().records), 1200)

    def test_real_requires_source_manifest_and_declaration_and_preserves_it(self):
        data = payload()
        data["dataset"]["synthetic"] = False
        data["records"][0]["source"]["system"] = "historical-consolidated-import"
        data["records"][0]["attributes"]["dataset_kind"] = "real"
        with self.assertRaises(HistoryImportError):
            self.ingest(data, require_real=True)
        declaration = {"declared_by": "fixture-person", "declared_at": "2026-11-18",
                       "statement": "synthetic fixture of a human declaration", "scope_source_ids": ["fixture"]}
        data["dataset"]["human_declaration"] = declaration
        self.ingest(data, require_real=True)
        self.assertEqual(HistoryDataset.objects.get().metadata["human_declaration"], declaration)
        self.assertEqual(HistoryImportBatch.objects.get().report["input_dataset"]["human_declaration"], declaration)

    def test_all_missing_kept_and_reported(self):
        self.ingest()
        data = payload([])
        data["dataset"]["input_formats"] = ["consolidated"]
        report = self.ingest(data, expected_count=0)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["total"], 1)
        self.assertEqual(len(DatabaseHistoryStore("unit").load().records), 1)

    def test_command_refuses_nonpostgres(self):
        if connection.vendor == "postgresql":
            self.skipTest("SQLite-specific command safety check")
        with self.assertRaisesRegex(CommandError, "PostgreSQL"):
            call_command("import_history_records", records="absent", dataset_key="unit", expect_database="wrong", stdout=StringIO())
        self.assert_empty()

    def test_command_expect_database_refuses_before_input(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires actual PostgreSQL current_database()")
        with self.assertRaisesRegex(CommandError, "不匹配"):
            call_command("import_history_records", records="absent", dataset_key="unit", expect_database="never_this_database", stdout=StringIO())
        self.assert_empty()

    def test_command_postgres_import_dry_run_private_report_and_partial_exit(self):
        if connection.vendor != "postgresql":
            self.skipTest("requires PostgreSQL command")
        self.path.write_text(json.dumps(payload([row(review=True)])), encoding="utf-8")
        common = {"records": str(self.path), "dataset_key": "unit", "expect_database": connection.settings_dict["NAME"]}
        output = StringIO()
        call_command("import_history_records", **common, dry_run=True, stdout=output)
        self.assert_empty()
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        report_path = Path(self.temp.name) / "private-report.json"
        call_command("import_history_records", **common, report=str(report_path), stdout=StringIO())
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report_path.read_text())["added"], 1)
        self.path.write_text(json.dumps(payload([row(hs="7307990000")])), encoding="utf-8")
        with self.assertRaises(CommandError) as caught:
            call_command("import_history_records", **common, stdout=StringIO())
        self.assertEqual(caught.exception.returncode, 2)
        self.assertTrue(HistoricalRecord.objects.get().needs_review)
        self.assertEqual(HistoryImportBatch.objects.count(), 2)

    def test_identity_version_and_source_manifest_mismatch_refused(self):
        for field, value in (("identity_version", "unknown"), ("source_files", [{"source_id": "wrong"}])):
            data = payload()
            data["dataset"][field] = value
            with self.assertRaises(HistoryImportError):
                self.ingest(data)
        self.assert_empty()

    def test_api_requires_postgres_without_test_escape(self):
        if connection.vendor == "postgresql":
            self.skipTest("SQLite-only API safeguard")
        self.path.write_text(json.dumps(payload()), encoding="utf-8")
        with self.assertRaisesRegex(HistoryImportError, "PostgreSQL"):
            import_history_snapshot(self.path, "unit")
        self.assert_empty()

    @staticmethod
    def _real_batch(records, *, sha256, declared_by, batch_id, statement="人工确认成功"):
        for rec in records:
            rec["source"]["system"] = "historical-consolidated-import"
            rec["attributes"]["dataset_kind"] = "real"
            rec["attributes"]["source_file_id"] = "src-real"
            rec["attributes"]["source_file_sha256"] = sha256
            rec["attributes"]["batch_id"] = batch_id
            rec["attributes"]["success_basis"] = statement
        return {
            "schema_version": "p15.history.1",
            "dataset": {
                "dataset_id": "real-unit",
                "synthetic": False,
                "description": f"真实导入 {batch_id}",
                "import_batch_id": batch_id,
                "record_count": len(records),
                "input_formats": ["consolidated"],
                "identity_version": CONSOLIDATED_IDENTITY_VERSION,
                "source_files": [{"source_id": "src-real", "filename": "history.xlsx", "sha256": sha256}],
                "human_declaration": {
                    "declaration_id": f"decl-{batch_id}",
                    "declared_by": declared_by,
                    "declared_at": "2026-11-18T10:00:00+00:00",
                    "statement": statement,
                    "scope_source_ids": ["src-real"],
                },
            },
            "records": records,
        }

    def test_retained_changed_record_preserves_original_hash_row_and_declaration(self):
        r1 = row("r1", anchor="a1", hs="7307210000")
        r1["source"]["locator"] = "Sheet1!第2行；合同 C1"
        r1["attributes"]["source_row"] = "2"
        self.ingest(self._real_batch([r1], sha256="H1", declared_by="Jay", batch_id="batch-1"), require_real=True)

        r1_revised = row("r1", anchor="a1", hs="7307990000")
        r1_revised["source"]["locator"] = "Sheet1!第500行；合同 C1"
        r1_revised["attributes"]["source_row"] = "500"
        report = self.ingest(
            self._real_batch([r1_revised], sha256="H2", declared_by="Reviewer2", batch_id="batch-2", statement="修正版声明"),
            require_real=True,
        )

        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["changed_kept"], 1)
        stored = HistoricalRecord.objects.get()
        self.assertEqual(stored.historical_hs, "7307210000")
        self.assertEqual(stored.source_locator, "Sheet1!第2行；合同 C1")
        self.assertEqual(stored.payload["attributes"]["source_row"], "2")
        self.assertEqual(stored.payload["attributes"]["source_file_sha256"], "H1")

        dataset = HistoryDataset.objects.get()
        self.assertEqual(dataset.metadata["source_files"][0]["sha256"], "H1")
        self.assertEqual(dataset.metadata["human_declaration"]["declared_by"], "Jay")
        self.assertEqual(dataset.metadata["import_batch_id"], "batch-1")

        traced = HistoricalRecord.objects.get(
            dataset=dataset,
            payload__attributes__source_file_sha256="H1",
            payload__attributes__source_row="2",
        )
        self.assertEqual(traced.record_id, "r1")
        self.assertEqual(DatabaseHistoryStore("unit").load().records[0].historical_hs, "7307210000")

        audit_batch = HistoryImportBatch.objects.get(dataset=dataset, revision=2)
        self.assertEqual(audit_batch.report["input_dataset"]["source_files"][0]["sha256"], "H2")
        self.assertEqual(audit_batch.report["input_dataset"]["human_declaration"]["declared_by"], "Reviewer2")

    def test_mixed_retained_and_adopted_records_preserve_provenance_and_cleanup_on_replace(self):
        r1 = row("r1", anchor="a1", hs="7307210000")
        r1["source"]["locator"] = "Sheet1!第2行"
        r1["attributes"]["source_row"] = "2"
        r2 = row("r2", anchor="a2", hs="8501109990", material="铜")
        r2["source"]["locator"] = "Sheet1!第3行"
        r2["attributes"]["source_row"] = "3"
        self.ingest(self._real_batch([r1, r2], sha256="H1", declared_by="Jay", batch_id="batch-1"), require_real=True)

        r1_changed = row("r1", anchor="a1", hs="7307990000")
        r1_changed["source"]["locator"] = "Sheet1!第20行"
        r1_changed["attributes"]["source_row"] = "20"
        r2_same = row("r2", anchor="a2", hs="8501109990", material="铜")
        r2_same["source"]["locator"] = "Sheet1!第30行"
        r2_same["attributes"]["source_row"] = "30"
        r3_new = row("r3", anchor="a3", hs="8481804090", material="黄铜")
        r3_new["source"]["locator"] = "Sheet1!第40行"
        r3_new["attributes"]["source_row"] = "40"
        report = self.ingest(
            self._real_batch([r1_changed, r2_same, r3_new], sha256="H2", declared_by="Reviewer2", batch_id="batch-2"),
            require_real=True,
        )

        self.assertEqual(report["changed_kept"], 1)
        self.assertEqual(report["added"], 1)
        dataset = HistoryDataset.objects.get()
        self.assertEqual(dataset.metadata["source_files"][0]["sha256"], "H1")
        self.assertEqual(dataset.metadata["human_declaration"]["declared_by"], "Jay")
        self.assertEqual({item["sha256"] for item in dataset.metadata["active_source_files"]}, {"H1", "H2"})
        self.assertEqual({item["declared_by"] for item in dataset.metadata["active_declarations"]}, {"Jay", "Reviewer2"})
        self.assertEqual(HistoricalRecord.objects.get(record_id="r1").payload["attributes"]["source_file_sha256"], "H1")
        self.assertEqual(HistoricalRecord.objects.get(record_id="r2").payload["attributes"]["source_file_sha256"], "H2")
        self.assertEqual(HistoricalRecord.objects.get(record_id="r3").payload["attributes"]["source_file_sha256"], "H2")
        self.assertEqual(len(DatabaseHistoryStore("unit").load().records), 3)

        audit_batch = HistoryImportBatch.objects.get(dataset=dataset, revision=2)
        self.assertEqual(audit_batch.report["input_dataset"]["human_declaration"]["declared_by"], "Reviewer2")

        r1_v3 = copy.deepcopy(r1_changed)
        r2_v3 = copy.deepcopy(r2_same)
        r3_v3 = copy.deepcopy(r3_new)
        self.ingest(
            self._real_batch([r1_v3, r2_v3, r3_v3], sha256="H3", declared_by="Carol", batch_id="batch-3"),
            replace_changed=True,
            require_real=True,
        )
        dataset.refresh_from_db()
        self.assertEqual(dataset.metadata["source_files"][0]["sha256"], "H3")
        self.assertEqual(dataset.metadata["human_declaration"]["declared_by"], "Carol")
        self.assertNotIn("active_source_files", dataset.metadata)
        self.assertNotIn("active_declarations", dataset.metadata)
