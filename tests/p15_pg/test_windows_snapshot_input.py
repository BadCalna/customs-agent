"""Windows public snapshot paths must resolve before the database importer reads."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.test import TestCase

from adapters.sources.history_publish import CURRENT_POINTER_NAME, publish_staging, resolve_history_file
from apps.history_reference.models import HistoricalRecord, HistoryDataset, HistoryImportBatch
from apps.history_reference.services import HistoryImportError, import_history_snapshot
from tests.p15_pg.test_persistence import payload, row


@skipUnless(os.name == "nt", "native Windows JSON-pointer snapshot input")
class WindowsSnapshotInputTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="p15-snapshot-input-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.out = self.root / "out"
        self.public = self.out / "history_records.json"

    def publish(self, record_id, hs="7307210000"):
        stage = self.root / f"stage-{record_id}"
        stage.mkdir()
        source = stage / "history_records.json"
        source.write_text(json.dumps(payload([row(record_id, hs=hs)])), encoding="utf-8")
        publish_staging(stage, self.out, [source])
        return source.read_bytes()

    def assert_imported_bytes(self, expected, record_id, hs):
        result = import_history_snapshot(self.public, "synthetic-pointer-input", allow_sqlite=True)
        self.assertEqual(result["added"], 1)
        record = HistoricalRecord.objects.get()
        self.assertEqual((record.record_id, record.historical_hs), (record_id, hs))
        self.assertEqual(HistoryImportBatch.objects.get().sha256, hashlib.sha256(expected).hexdigest())

    def test_first_pointer_publication_imports_without_a_top_level_flat_file(self):
        expected = self.publish("pointer-current")
        self.assertFalse(self.public.exists())
        self.assertTrue(resolve_history_file(self.public).is_file())
        self.assert_imported_bytes(expected, "pointer-current", "7307210000")

    def test_preserved_legacy_flat_is_not_imported_instead_of_current_generation(self):
        self.out.mkdir()
        old = json.dumps(payload([row("legacy-old")])).encode("utf-8")
        self.public.write_bytes(old)
        expected = self.publish("pointer-new", hs="7307990000")
        self.assertEqual(self.public.read_bytes(), old)
        self.assertNotEqual(expected, old)
        self.assert_imported_bytes(expected, "pointer-new", "7307990000")
        self.assertEqual(self.public.read_bytes(), old)

    def test_bad_pointer_never_falls_back_to_readable_legacy_flat(self):
        self.out.mkdir()
        old = json.dumps(payload([row("legacy-old")])).encode("utf-8")
        self.public.write_bytes(old)
        self.publish("pointer-new", hs="7307990000")
        pointer = self.out / CURRENT_POINTER_NAME
        original = json.loads(pointer.read_bytes())
        invalid_generation = dict(original, generation="../outside")
        invalid_digest = dict(original, files={"history_records.json": "0" * 64})
        for invalid in (b"{invalid-json", json.dumps(invalid_generation).encode(), json.dumps(invalid_digest).encode()):
            with self.subTest(pointer=invalid[:30]):
                pointer.write_bytes(invalid)
                with self.assertRaisesRegex(HistoryImportError, "输入快照不可读"):
                    import_history_snapshot(self.public, "synthetic-pointer-input", allow_sqlite=True)
                self.assertFalse(HistoryDataset.objects.exists())
                self.assertFalse(HistoricalRecord.objects.exists())
                self.assertFalse(HistoryImportBatch.objects.exists())
                self.assertEqual(self.public.read_bytes(), old)
                self.assertEqual(pointer.read_bytes(), invalid)
