"""P15 存储接口：默认 JSON、内存注入与坏数据统一归入 query_failed。"""
import json
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from adapters.sources.history_search import (
    HistoryDatasetInfo,
    HistoryQuery,
    HistoryRecord,
    HistorySearchOutcome,
    HistorySearchSource,
    HistorySnapshot,
    InMemoryHistoryStore,
    JsonHistoryStore,
)

BASE_DIR = Path(settings.BASE_DIR)
RECORDS_PATH = BASE_DIR / "adapters" / "sources" / "fixtures" / "history_records.sample.json"
CLOCK = datetime.fromisoformat("2026-11-15T00:00:00+08:00")


def fixed_clock() -> datetime:
    return CLOCK


def one_record() -> HistoryRecord:
    return JsonHistoryStore(RECORDS_PATH).load().records[0]


class JsonStoreTest(SimpleTestCase):
    def test_loads_labeled_synthetic_snapshot(self):
        snapshot = JsonHistoryStore(RECORDS_PATH).load()
        self.assertEqual(len(snapshot.records), 5)
        self.assertTrue(snapshot.dataset.synthetic)
        self.assertEqual(snapshot.dataset.dataset_id, "synthetic-p15-v1")
        self.assertEqual(snapshot.records[0].recorded_at, "2023-05-18")
        self.assertEqual(snapshot.records[0].source.document, "SYN-DECL-2023-0518-0042")

    def test_bare_record_array_is_accepted_with_undeclared_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text(json.dumps([one_record().model_dump()]), encoding="utf-8")
            result = HistorySearchSource.load(path, clock=fixed_clock).search(
                HistoryQuery(name="不锈钢法兰盘")
            )
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        self.assertIsNone(result.dataset.synthetic)
        self.assertEqual(result.dataset.dataset_id, "records.json")

    def test_unknown_columns_are_ignored(self):
        payload = one_record().model_dump() | {"legacy_export_column": "忽略我"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text(json.dumps([payload]), encoding="utf-8")
            snapshot = JsonHistoryStore(path).load()
        self.assertEqual(len(snapshot.records), 1)
        self.assertEqual(snapshot.records[0].record_id, "SYN-H-0001")

    def test_unsupported_schema_version_fails_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text(
                json.dumps({"schema_version": "p99.history.9", "records": []}), encoding="utf-8"
            )
            result = HistorySearchSource.load(path, clock=fixed_clock).search(
                HistoryQuery(name="不锈钢法兰盘")
            )
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("结构版本", result.reason)

    def test_malformed_json_fails_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text('{"records": [}', encoding="utf-8")
            result = HistorySearchSource.load(path, clock=fixed_clock).search(
                HistoryQuery(name="不锈钢法兰盘")
            )
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("合法 JSON", result.reason)

    def test_record_that_is_not_an_object_fails_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text(json.dumps({"records": ["不是对象"]}), encoding="utf-8")
            result = HistorySearchSource.load(path, clock=fixed_clock).search(
                HistoryQuery(name="不锈钢法兰盘")
            )
        self.assertIs(result.outcome, HistorySearchOutcome.QUERY_FAILED)
        self.assertIn("必须是对象", result.reason)

    def test_file_is_reloaded_on_each_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.json"
            path.write_text(json.dumps([one_record().model_dump()]), encoding="utf-8")
            source = HistorySearchSource.load(path, clock=fixed_clock)
            self.assertIs(
                source.search(HistoryQuery(name="不锈钢法兰盘")).outcome,
                HistorySearchOutcome.MATCHED,
            )
            path.write_text(json.dumps([]), encoding="utf-8")
            self.assertIs(
                source.search(HistoryQuery(name="不锈钢法兰盘")).outcome,
                HistorySearchOutcome.NO_MATCH,
            )


class InMemoryStoreTest(SimpleTestCase):
    def test_accepts_models_and_mappings(self):
        record = one_record()
        store = InMemoryHistoryStore([record, record.model_dump()])
        snapshot = store.load()
        self.assertEqual(len(snapshot.records), 2)
        self.assertIsNone(snapshot.dataset.synthetic)
        self.assertEqual(snapshot.dataset.dataset_id, "in-memory")

    def test_custom_dataset_is_kept(self):
        dataset = HistoryDatasetInfo(dataset_id="real-export", synthetic=False, description="调用方声明")
        store = InMemoryHistoryStore([one_record()], dataset=dataset)
        self.assertIs(store.load().dataset.synthetic, False)
        self.assertEqual(store.load().dataset.dataset_id, "real-export")

    def test_source_can_declare_a_real_dataset(self):
        source = HistorySearchSource(
            [one_record().model_dump()],
            dataset=HistoryDatasetInfo(dataset_id="real-export", synthetic=False),
            clock=fixed_clock,
        )
        result = source.search(HistoryQuery(name="不锈钢法兰盘"))
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        self.assertIs(result.dataset.synthetic, False)

    def test_any_object_with_load_is_a_store(self):
        """存储接口是协议：接 ORM/HTTP 导出时不需要继承任何基类。"""

        class StubStore:
            def load(self) -> HistorySnapshot:
                return HistorySnapshot(
                    dataset=HistoryDatasetInfo(dataset_id="stub", synthetic=False),
                    records=(one_record(),),
                )

        result = HistorySearchSource(store=StubStore(), clock=fixed_clock).search(
            HistoryQuery(name="不锈钢法兰盘")
        )
        self.assertIs(result.outcome, HistorySearchOutcome.MATCHED)
        self.assertEqual(result.dataset.dataset_id, "stub")


class ConstructorTest(SimpleTestCase):
    def test_records_and_store_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            HistorySearchSource([one_record()], store=JsonHistoryStore(RECORDS_PATH))

    def test_dataset_only_goes_with_records(self):
        with self.assertRaises(ValueError):
            HistorySearchSource(
                store=JsonHistoryStore(RECORDS_PATH),
                dataset=HistoryDatasetInfo(dataset_id="x", synthetic=False),
            )

    def test_invalid_limits_fail_fast(self):
        with self.assertRaises(ValueError):
            HistorySearchSource([], limit=0)
        with self.assertRaises(ValueError):
            HistorySearchSource([], min_score=1.5)
        with self.assertRaises(ValueError):
            HistorySearchSource([], stale_days=-1)
