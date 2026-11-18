"""PostgreSQL multi-connection tests; exclusively use Django's disposable TEST DB."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from apps.history_reference.models import HistoricalRecord, HistoryDataset, HistoryImportBatch
from apps.history_reference.services import import_history_snapshot
from apps.history_reference.store import DatabaseHistoryStore
from tests.p15_pg.test_persistence import payload, row


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "records.json"
        self.path.write_text(json.dumps(payload()), encoding="utf-8")

    def in_connection(self, operation):
        close_old_connections()
        try:
            return operation()
        finally:
            close_old_connections()

    def test_simultaneous_first_import_same_dataset_is_atomic_and_idempotent(self):
        barrier = Barrier(2)
        def ingest():
            barrier.wait(timeout=10)
            return import_history_snapshot(self.path, "race")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.in_connection, ingest) for _ in range(2)]
            reports = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(report["added"] for report in reports), [0, 1])
        self.assertEqual(HistoryDataset.objects.count(), 1)
        self.assertEqual(HistoricalRecord.objects.count(), 1)
        self.assertEqual(HistoryImportBatch.objects.count(), 2)
        self.assertEqual(HistoryDataset.objects.get().revision, 2)

    def test_reader_waits_for_writer_and_reads_one_metadata_record_generation(self):
        import_history_snapshot(self.path, "race")
        updated = payload([row(hs="7307990000")])
        updated["dataset"]["description"] = "new-generation"
        self.path.write_text(json.dumps(updated), encoding="utf-8")
        writer_inside = Event()
        reader_started = Event()
        reader_finished = Event()
        reader_lock_attempted = Event()
        from django.db.models.query import QuerySet
        original = QuerySet.create
        def pausing_create(queryset, *args, **kwargs):
            result = original(queryset, *args, **kwargs)
            if queryset.model is HistoryImportBatch:
                writer_inside.set()
                if not reader_started.wait(10) or not reader_lock_attempted.wait(10):
                    raise RuntimeError("reader never attempted snapshot lock")
                self.assertFalse(reader_finished.is_set())
            return result
        def read():
            if not writer_inside.wait(10):
                raise RuntimeError("writer did not acquire dataset lock")
            reader_started.set()
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql:
                    reader_lock_attempted.set()
                return execute(sql, params, many, context)
            with connection.execute_wrapper(observe):
                snapshot = DatabaseHistoryStore("race").load()
            reader_finished.set()
            return snapshot
        with patch.object(QuerySet, "create", pausing_create), ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(self.in_connection, lambda: import_history_snapshot(self.path, "race", replace_changed=True))
            reader = pool.submit(self.in_connection, read)
            writer.result(timeout=20)
            snapshot = reader.result(timeout=20)
        self.assertEqual(snapshot.dataset.description, "new-generation")
        self.assertEqual(snapshot.records[0].historical_hs, "7307990000")

    def test_database_error_uses_savepoint_without_poisoning_outer_business_transaction(self):
        import_history_snapshot(self.path, "race")
        def invalid_query(execute, sql, params, many, context):
            if "history_reference_historydataset" in sql and "SELECT" in sql:
                return execute("SELECT unknown_p15_column", None, False, context)
            return execute(sql, params, many, context)
        from adapters.sources.history_search import HistorySearchSource
        with transaction.atomic():
            with connection.execute_wrapper(invalid_query):
                result = HistorySearchSource(store=DatabaseHistoryStore("race")).search({"name": "法兰盘"})
            self.assertEqual(result.outcome.value, "query_failed")
            self.assertFalse(connection.needs_rollback)
            self.assertEqual(HistoricalRecord.objects.count(), 1)
