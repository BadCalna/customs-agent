"""Native Windows reader/publication races, using temporary synthetic files only."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase

from adapters.sources import history_publish as hp
from adapters.sources import history_windows as hw
from adapters.sources.history_consolidated import FORMAT_CONSOLIDATED
from adapters.sources.history_import import (
    ImportIncompleteError, ImportRequest, import_history_templates, validate_history_queries,
)
from adapters.sources.history_search import HistorySearchSource
from tests.p15.consolidated_fixtures import BASE_ROW, build_consolidated_workbook


@skipUnless(os.name == "nt", "native Windows JSON-pointer publication")
class WindowsReaderGuardTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p15-windows-reader-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "out"
        self.fixture = json.loads((Path(settings.BASE_DIR) / "adapters/sources/fixtures/history_records.sample.json")
                                  .read_text(encoding="utf-8"))

    def publish(self, token):
        staging = self.root / f"stage-{token}"
        staging.mkdir()
        payload = deepcopy(self.fixture)
        payload["dataset"]["dataset_id"] = token
        (staging / "history_records.json").write_text(json.dumps(payload), encoding="utf-8")
        (staging / "import_report.json").write_text(json.dumps({"token": token}), encoding="utf-8")
        hp.publish_staging(staging, self.out, list(staging.iterdir()))
        return hp.pin_history_output(self.out)

    def test_validation_retains_cas_when_another_writer_commits_after_input_pin(self):
        first = self.publish("first")
        queries = self.root / "queries.json"
        queries.write_text(json.dumps([{
            "case_id": "synthetic-no-match", "kind": "no_match",
            "query": {"name": "SYNTHETIC-DOES-NOT-EXIST-XYZ"},
            "expected": {"outcome": "no_match"},
        }]), encoding="utf-8")
        original_search = HistorySearchSource.search
        second = []

        def commit_during_search(source, query):
            if not second:
                second.append(self.publish("second"))
            return original_search(source, query)

        with patch.object(HistorySearchSource, "search", new=commit_during_search):
            with self.assertRaisesRegex(ImportIncompleteError, "并发发布冲突"):
                validate_history_queries(self.out / "history_records.json", out_dir=self.out, queries_path=queries)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second[0])
        self.assertEqual(hp.pin_history_output(self.out), second[0])
        self.assertFalse((second[0] / "query_validation.json").exists())
        self.assertEqual(json.loads((second[0] / "history_records.json").read_text())["dataset"]["dataset_id"], "second")

    def test_interrupt_after_real_pointer_replace_keeps_committed_generation_readable(self):
        first = self.publish("first")
        real_replace = hw.os.replace

        def replace_then_interrupt(source, destination):
            result = real_replace(source, destination)
            if Path(destination).name == hp.CURRENT_POINTER_NAME:
                raise KeyboardInterrupt("synthetic interruption after actual pointer replacement")
            return result

        with patch.object(hw.os, "replace", side_effect=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.publish("second")
        current = hp.pin_history_output(self.out)
        self.assertNotEqual(current, first)
        self.assertTrue(first.is_dir())
        self.assertEqual(json.loads((current / "history_records.json").read_text())["dataset"]["dataset_id"], "second")
        self.assertEqual(json.loads((current / "import_report.json").read_text())["token"], "second")

    def test_import_outcome_keeps_own_generation_when_next_writer_commits_before_return(self):
        workbook = build_consolidated_workbook(self.root / "input.xlsx", rows=[BASE_ROW])
        own = []

        def commit_next_writer(stage):
            if stage != "after-commit" or own:
                return
            generation = hp.pin_history_output(self.out)
            own.append(generation)
            self.publish("next-writer")

        request = ImportRequest(
            files=[workbook], out_dir=self.out, dataset_kind="synthetic", declared_success=False,
            declared_by="synthetic-test", declaration_note="synthetic race test",
            input_format=FORMAT_CONSOLIDATED, skip_validation=True,
        )
        with patch.object(hp, "_FAULT_INJECTION_HOOK", commit_next_writer):
            outcome = import_history_templates(request)
        self.assertEqual(len(own), 1)
        self.assertNotEqual(hp.pin_history_output(self.out), own[0])
        self.assertEqual(outcome.records_path.parent, own[0])
        self.assertEqual(outcome.report_json_path.parent, own[0])
        self.assertEqual(json.loads(outcome.report_json_path.read_text(encoding="utf-8")), outcome.report)
        self.assertNotEqual(json.loads(outcome.records_path.read_text())["dataset"]["dataset_id"], "next-writer")
