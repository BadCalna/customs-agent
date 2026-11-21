"""Synthetic contract fixtures for the bundle's explicit P15 history path.

The synthetic=False metadata exercises this gate only; these empty datasets
contain no actual history and establish no professional classification claim.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless

from django.test import SimpleTestCase

from adapters.engines.reference_bundle import ReferenceBundleConfig, ReferenceBundleError, _history_source
from adapters.sources.history_publish import CURRENT_POINTER_NAME, publish_staging


@skipUnless(os.name == "nt", "native Windows JSON-pointer history path")
class WindowsBundleHistoryInputTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="p16-history-input-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.out = self.root / "out"
        self.public = self.out / "history_records.json"

    @staticmethod
    def payload(token):
        return {"schema_version": "p15.history.1", "dataset": {
            "dataset_id": token, "synthetic": False,
            "description": "SYNTHETIC CONTRACT TEST ONLY; no actual history",
        }, "records": []}

    def publish(self, token):
        stage = self.root / f"stage-{token}"
        stage.mkdir()
        source = stage / "history_records.json"
        source.write_text(json.dumps(self.payload(token)), encoding="utf-8")
        publish_staging(stage, self.out, [source])

    def load(self):
        return _history_source(ReferenceBundleConfig(history_path=self.public))

    def test_explicit_history_path_loads_first_pointer_without_a_flat_file(self):
        self.publish("current")
        self.assertFalse(self.public.exists())
        source, metadata = self.load()
        self.assertEqual(metadata["dataset_id"], "current")
        self.assertEqual(source._store.load().dataset.dataset_id, "current")
        # A loaded bundle keeps its own snapshot after the pointer changes.
        self.publish("next")
        self.assertEqual(source._store.load().dataset.dataset_id, "current")
        self.assertEqual(self.load()[1]["dataset_id"], "next")

    def test_explicit_history_path_uses_current_instead_of_preserved_legacy_flat(self):
        self.out.mkdir()
        old = json.dumps(self.payload("legacy-old")).encode("utf-8")
        self.public.write_bytes(old)
        self.publish("current")
        self.assertEqual(self.public.read_bytes(), old)
        source, metadata = self.load()
        self.assertEqual(metadata["dataset_id"], "current")
        self.assertEqual(source._store.load().dataset.dataset_id, "current")
        self.assertEqual(self.public.read_bytes(), old)

    def test_explicit_history_path_rejects_bad_pointer_without_legacy_fallback(self):
        self.out.mkdir()
        old = json.dumps(self.payload("legacy-old")).encode("utf-8")
        self.public.write_bytes(old)
        self.publish("current")
        pointer = self.out / CURRENT_POINTER_NAME
        original = json.loads(pointer.read_bytes())
        invalid_generation = dict(original, generation="../outside")
        invalid_digest = dict(original, files={"history_records.json": "0" * 64})
        for invalid in (b"{invalid-json", json.dumps(invalid_generation).encode(), json.dumps(invalid_digest).encode()):
            with self.subTest(pointer=invalid[:30]):
                pointer.write_bytes(invalid)
                with self.assertRaisesRegex(ReferenceBundleError, "Explicit history source unavailable.*PublishError"):
                    self.load()
                self.assertEqual(self.public.read_bytes(), old)
                self.assertEqual(pointer.read_bytes(), invalid)
