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

    def test_concurrent_readers_do_not_wait_for_exclusive_locks(self):
        import_history_snapshot(self.path, "race")
        reader1_inside = Event()
        release_reader1 = Event()
        reader2_finished = Event()
        seen_for_update = []

        def read_paused():
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    seen_for_update.append(sql)
                if "history_reference_historicalrecord" in sql and "SELECT" in sql and not reader1_inside.is_set():
                    reader1_inside.set()
                    if not release_reader1.wait(10):
                        raise RuntimeError("reader1 release timeout")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return DatabaseHistoryStore("race").load()

        def read_concurrent():
            if not reader1_inside.wait(10):
                raise RuntimeError("reader1 did not start")
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    seen_for_update.append(sql)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                snapshot = DatabaseHistoryStore("race").load()
            reader2_finished.set()
            release_reader1.set()
            return snapshot

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.in_connection, read_paused)
            f2 = pool.submit(self.in_connection, read_concurrent)
            s2 = f2.result(timeout=20)
            s1 = f1.result(timeout=20)
        self.assertTrue(reader2_finished.is_set())
        self.assertEqual(seen_for_update, [])
        self.assertEqual(s1.records[0].historical_hs, "7307210000")
        self.assertEqual(s2.records[0].historical_hs, "7307210000")

    def test_reader_does_not_block_on_uncommitted_writer_and_reads_consistent_generation(self):
        import_history_snapshot(self.path, "race")
        updated = payload([row(hs="7307990000")])
        updated["dataset"]["description"] = "new-generation"
        self.path.write_text(json.dumps(updated), encoding="utf-8")
        writer_inside = Event()
        reader_finished = Event()
        seen_for_update = []
        from django.db.models.query import QuerySet
        original = QuerySet.create

        def pausing_create(queryset, *args, **kwargs):
            result = original(queryset, *args, **kwargs)
            if queryset.model is HistoryImportBatch:
                writer_inside.set()
                if not reader_finished.wait(10):
                    raise RuntimeError("reader blocked on uncommitted writer")
            return result

        def read():
            if not writer_inside.wait(10):
                raise RuntimeError("writer did not enter batch write")
            def observe(execute, sql, params, many, context):
                if "FOR UPDATE" in sql.upper():
                    seen_for_update.append(sql)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                snapshot = DatabaseHistoryStore("race").load()
            reader_finished.set()
            return snapshot

        with patch.object(QuerySet, "create", pausing_create), ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(self.in_connection, lambda: import_history_snapshot(self.path, "race", replace_changed=True))
            reader = pool.submit(self.in_connection, read)
            snapshot = reader.result(timeout=20)
            writer.result(timeout=20)
        self.assertEqual(seen_for_update, [])
        self.assertEqual(snapshot.dataset.description, "合成测试")
        self.assertEqual(snapshot.records[0].historical_hs, "7307210000")
        after = DatabaseHistoryStore("race").load()
        self.assertEqual(after.dataset.description, "new-generation")
        self.assertEqual(after.records[0].historical_hs, "7307990000")

    def test_reader_retries_when_writer_commits_mid_read_so_metadata_and_records_match(self):
        import_history_snapshot(self.path, "race")
        updated = payload([row(hs="7307990000")])
        updated["dataset"]["description"] = "new-generation"
        self.path.write_text(json.dumps(updated), encoding="utf-8")
        dataset_read_done = Event()
        writer_committed = Event()
        paused_once = []

        def read_with_mid_commit():
            def observe(execute, sql, params, many, context):
                if "history_reference_historicalrecord" in sql and "SELECT" in sql and not paused_once:
                    paused_once.append(True)
                    dataset_read_done.set()
                    if not writer_committed.wait(10):
                        raise RuntimeError("writer never committed mid-read")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return DatabaseHistoryStore("race").load()

        def write_after_dataset_read():
            if not dataset_read_done.wait(10):
                raise RuntimeError("reader never read initial dataset")
            import_history_snapshot(self.path, "race", replace_changed=True)
            writer_committed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(self.in_connection, read_with_mid_commit)
            writer = pool.submit(self.in_connection, write_after_dataset_read)
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
