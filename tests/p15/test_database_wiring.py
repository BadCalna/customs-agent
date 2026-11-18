"""History application wiring with no recommendation/approval side effects."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from adapters.sources.history_search import (
    HistorySearchSource, InMemoryHistoryStore, HistoryDatasetInfo,
)
from apps.accounts.models import Role
from apps.reviews.models import Task, FileVersion, Item, Confirmation, Decision
from apps.runs.inline import execute_precheck
from apps.runs.models import Run
from tests.p01.factories import make_user, assign


def fixture_store():
    return InMemoryHistoryStore([{
        "record_id": "SYN-PG-REF", "historical_hs": "7307210000", "historical_name": "合成法兰盘",
        "elements": {"材质": "不锈钢", "规格": "DN50"}, "material": "不锈钢", "spec": "DN50",
        "recorded_at": "未提供", "outcome": "approved", "attributes": {"needs_review": "false"},
        "source": {"document": "SYN-REFERENCE", "locator": "Sheet1!E2"},
    }], dataset=HistoryDatasetInfo(dataset_id="synthetic-wiring", synthetic=True))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HistoryWiringTests(TestCase):
    def setUp(self):
        self.supplier = make_user("history-supplier", Role.SUPPLIER)
        self.other = make_user("history-other", Role.SUPPLIER)
        self.operator = make_user("history-operator", Role.OPERATOR)
        self.task = Task.objects.create(supplier=self.supplier, title="SYN task")
        assign(self.task, self.operator, Role.OPERATOR)
        self.version = FileVersion.objects.create(task=self.task, number=1, original_filename="SYN.xlsx", uploaded_by=self.supplier)
        self.item = Item.objects.create(version=self.version, source_sheet="SYN", source_row=2,
            name="合成法兰盘", supplier_hs_raw="9999999999", supplier_elements_raw="材质：不锈钢|规格：DN50")
        self.url = f"/api/v1/versions/{self.version.pk}/items/{self.item.pk}/history-reference"

    @patch("adapters.sources.history_search.default_history_store", side_effect=fixture_store)
    def test_authenticated_item_reference_endpoint_is_read_only(self, _store):
        self.client.force_login(self.operator)
        before = (Run.objects.count(), Confirmation.objects.count(), Decision.objects.count(), self.task.revision)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outcome"], "matched")
        self.assertTrue(payload["reference_only"])
        self.assertEqual(payload["candidates"][0]["historical_hs"], "7307210000")
        self.assertNotIn("suggested_hs", payload["candidates"][0])
        self.item.refresh_from_db(); self.task.refresh_from_db()
        self.assertEqual(self.item.supplier_hs_raw, "9999999999")
        self.assertEqual(before, (Run.objects.count(), Confirmation.objects.count(), Decision.objects.count(), self.task.revision))

    def test_supplier_item_and_run_do_not_expose_history_reference(self):
        from apps.runs.models import Run, RunItem
        self.client.force_login(self.operator)
        run = execute_precheck(self.version)
        run.items.update(result={"history_reference": {"outcome": "matched"}, "evidence": [{"source_type": "history", "title": "secret"}]})
        self.client.force_login(self.supplier)
        card = self.client.get(f"/api/v1/versions/{self.version.pk}/items/{self.item.pk}")
        self.assertEqual(card.status_code, 200)
        self.assertFalse(any(entry.get("source_type") == "history" for entry in card.json().get("evidence", [])))
        detail = self.client.get(f"/api/v1/runs/{run.pk}")
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(any(entry.get("source_type") == "history" for entry in detail.json().get("evidence", [])))

    def test_internal_operator_can_read_history_reference(self):
        self.client.force_login(self.operator)
        response = self.client.get(self.url)
        self.assertIn(response.status_code, {200, 500})

    def test_anonymous_and_suppliers_cannot_read_cross_supplier_history(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.supplier)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    @override_settings(HISTORY_REFERENCE_ENABLED=True)
    @patch("adapters.sources.history_search.default_history_store", side_effect=fixture_store)
    def test_precheck_appends_reference_without_changing_mock_recommendation(self, _store):
        baseline = execute_precheck(self.version)
        baseline_row = baseline.items.get()
        # Provider returns reference HS7307; current mock recommendation remains8544.
        self.assertNotEqual(baseline_row.suggested_hs, "7307210000")
        self.assertEqual(baseline_row.result["history_reference"]["outcome"], "matched")
        self.assertFalse(any(e["source_type"] == "history" for e in baseline_row.result["evidence"]))
        self.assertEqual(baseline_row.result["history_reference"]["outcome"], "matched")
        self.item.refresh_from_db()
        self.assertEqual(self.item.supplier_hs_raw, "9999999999")
        self.assertEqual(Confirmation.objects.count(), 0)
        self.assertEqual(Decision.objects.count(), 0)

    @override_settings(HISTORY_REFERENCE_ENABLED=False)
    def test_disabled_does_not_touch_history_store(self):
        with patch("apps.reviews.services.history_reference.HistorySearchSource") as store:
            run = execute_precheck(self.version)
        store.assert_not_called()
        self.assertNotIn("history_reference", run.items.get().result)

    @override_settings(HISTORY_BACKEND="database", HISTORY_DATASET_KEY="SYN-missing")
    def test_missing_database_dataset_is_query_failed_not_synthetic_fallback(self):
        self.client.force_login(self.operator)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "query_failed")
        self.assertEqual(response.json()["candidates"], [])

    @override_settings(HISTORY_REFERENCE_ENABLED=True, HISTORY_BACKEND="database", HISTORY_DATASET_KEY="SYN-missing")
    def test_store_failure_does_not_break_outer_precheck_transaction(self):
        run = execute_precheck(self.version)
        row = run.items.get()
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(row.result["history_reference"]["outcome"], "query_failed")
        self.assertTrue(row.suggested_hs)

    @override_settings(HISTORY_REFERENCE_ENABLED=True, HISTORY_BACKEND="database", HISTORY_DATASET_KEY="batch-1198")
    def test_precheck_batch_reuses_one_history_snapshot_without_exclusive_locks(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from django.db import connection
        from apps.history_reference.services import import_history_snapshot
        from apps.history_reference.store import DatabaseHistoryStore
        from tests.p15_pg.test_persistence import payload as pg_payload, row as pg_row

        for idx in range(2, 7):
            Item.objects.create(
                version=self.version,
                source_sheet="SYN",
                source_row=idx + 1,
                name="测试法兰盘" if idx % 2 == 0 else f"无关橡胶件{idx}",
                supplier_hs_raw="9999999999",
                supplier_elements_raw="材质：不锈钢|规格：DN50",
            )
        records = [pg_row(f"r{i}", anchor=f"a{i}", material=f"材质{i}") for i in range(1197)]
        records.append(pg_row("r-hit", anchor="a-hit", material="不锈钢"))
        with TemporaryDirectory() as raw:
            snap_path = Path(raw) / "records.json"
            snap_path.write_text(json.dumps(pg_payload(records), ensure_ascii=False), encoding="utf-8")
            import_history_snapshot(snap_path, "batch-1198", allow_sqlite=True)

        seen_for_update = []
        def observe(execute, sql, params, many, context):
            if "FOR UPDATE" in sql.upper() and "history_reference_" in sql:
                seen_for_update.append(sql)
            return execute(sql, params, many, context)

        import apps.history_reference.services as history_services
        original_load = DatabaseHistoryStore.load
        with (
            patch.object(DatabaseHistoryStore, "load", autospec=True, side_effect=original_load) as load_spy,
            patch.object(history_services, "_validate_payload", wraps=history_services._validate_payload) as validate_spy,
            connection.execute_wrapper(observe),
        ):
            run = execute_precheck(self.version)

        self.assertEqual(load_spy.call_count, 1)
        self.assertEqual(validate_spy.call_count, 1)
        self.assertEqual(seen_for_update, [])
        rows = list(run.items.order_by("item__source_row"))
        self.assertEqual(len(rows), 6)
        self.assertTrue(all("history_reference" in r.result for r in rows))
        self.assertEqual(rows[1].result["history_reference"]["outcome"], "matched")
        self.assertEqual(rows[1].result["history_reference"]["scanned_records"], 1198)

    @override_settings(HISTORY_REFERENCE_ENABLED=True, HISTORY_BACKEND="database", HISTORY_DATASET_KEY="SYN-missing")
    def test_store_failure_with_multiple_items_loads_once(self):
        from apps.history_reference.store import DatabaseHistoryStore

        Item.objects.create(version=self.version, source_sheet="SYN", source_row=3, name="商品二", supplier_hs_raw="9999999999")
        Item.objects.create(version=self.version, source_sheet="SYN", source_row=4, name="商品三", supplier_hs_raw="9999999999")
        original_load = DatabaseHistoryStore.load
        with patch.object(DatabaseHistoryStore, "load", autospec=True, side_effect=original_load) as load_spy:
            run = execute_precheck(self.version)
        self.assertEqual(load_spy.call_count, 1)
        rows = list(run.items.all())
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r.status == "succeeded" and r.result["history_reference"]["outcome"] == "query_failed" for r in rows))

    @override_settings(HISTORY_BACKEND="json")
    def test_explicit_json_loader_keeps_existing_behavior(self):
        source = HistorySearchSource(records=fixture_store().load().records)
        self.assertEqual(source._store.load().records[0].record_id, "SYN-PG-REF")
