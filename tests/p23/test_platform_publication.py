"""Native publication checkpoints: actual process death around the sole commit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from adapters.sources.reference_import import publish as publishing
from adapters.sources.reference_import.publish import (
    PublishError, PublishRequest, activate_release, current_release_dir, dataset_paths, publish_release,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_publish_batch, write_verification
from tests.p23.support import P23TestCase
from tests.p23.test_platform_lock import stop_child, wait_for_file
from tests.platform_files import create_directory_link


class NativePublicationTests(P23TestCase):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.releases = []
        for index, label in enumerate(("A", "B")):
            batch = build_publish_batch(self.root / label, label=label)
            built = build_snapshot(SnapshotRequest(batch.manifest, self.out))
            write_verification(self.out, built.snapshot_id)
            self.releases.append(publish_release(PublishRequest(
                built.snapshot_dir, self.out, activate=index == 0,
                allow_content_change=index == 1, reason="native crash fixture" if index else "",
            )))
        self.a, self.b = self.releases

    def test_process_kill_before_and_after_atomic_commit(self):
        script = """
from pathlib import Path
import sys, time
from adapters.sources.reference_import import publish as p
root, release_id, stage, marker = sys.argv[1:]
def checkpoint(actual):
    if actual == stage:
        Path(marker).write_text(stage)
        while True:
            time.sleep(1)
p._ACTIVATION_HOOK = checkpoint
p.activate_release(root, release_id)
"""
        original = {r.release_id: {p.name: p.read_bytes() for p in r.release_dir.iterdir()
                                   if p.is_file() and p.name != ".current.json"}
                    for r in self.releases}
        for stage in ("prepared", "committed"):
            with self.subTest(stage=stage):
                activate_release(self.out, self.a.release_id)
                previous_events = set((self.out / "published" / "activations").glob("*.json"))
                marker = self.root / f"checkpoint-{stage}"
                child = subprocess.Popen([sys.executable, "-c", script, str(self.out), self.b.release_id,
                                          stage, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(stop_child, child)
                wait_for_file(marker, child, timeout=10)
                child.kill()
                child.communicate(timeout=5)
                self.assertNotEqual(child.returncode, 0, "must terminate the real holder process")
                expected = self.a if stage == "prepared" else self.b
                self.assertEqual(current_release_dir(self.out), expected.release_dir)
                self.assertTrue(all(expected.release_id in value for value in dataset_paths(self.out).values()))
                self.assert_current_layout(self.out, expected)
                for release in self.releases:
                    self.assertEqual({p.name: p.read_bytes() for p in release.release_dir.iterdir()
                                      if p.is_file() and p.name != ".current.json"}, original[release.release_id])
                new_events = set((self.out / "published" / "activations").glob("*.json")) - previous_events
                self.assertEqual(len(new_events), 1)
                event_path = new_events.pop()
                event = json.loads(event_path.read_text(encoding="utf-8"))
                self.assertEqual(event["release_id"], self.b.release_id)
                self.assertFalse((self.out / "published" / "activation-results" / event_path.name).exists(),
                                 "killed operation has an intent, never a fabricated committed/failed result")
                with publishing._publish_lock(self.out, timeout=0.5):
                    pass
                activate_release(self.out, self.a.release_id)

    def test_release_directory_redirection_is_rejected(self):
        moved = self.root / "redirected-release"
        self.b.release_dir.rename(moved)
        create_directory_link(self.b.release_dir, moved)
        with self.assertRaises(PublishError):
            activate_release(self.out, self.b.release_id)
        with self.assertRaises(PublishError):
            dataset_paths(self.out, release_id=self.b.release_id)
        self.assertEqual(current_release_dir(self.out), self.a.release_dir)

    def test_explicit_dataset_path_rejects_traversal_and_tampering(self):
        with self.assertRaises(PublishError):
            dataset_paths(self.out, release_id="../../outside")
        (self.b.release_dir / "tariff.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(PublishError):
            dataset_paths(self.out, release_id=self.b.release_id)

    def test_redirected_publication_ancestor_cannot_be_read_or_written(self):
        outside = self.root / "outside-published"
        (self.out / "published").rename(outside)
        create_directory_link(self.out / "published", outside)
        before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
        with self.assertRaises(PublishError):
            current_release_dir(self.out)
        with self.assertRaises(PublishError):
            activate_release(self.out, self.b.release_id)
        with self.assertRaises(PublishError):
            with publishing._publish_lock(self.out):
                self.fail("redirected lock must not be acquired")
        self.assertEqual({p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}, before)

    def test_redirected_audit_directories_cannot_be_written(self):
        for name in ("activations", "activation-results"):
            with self.subTest(directory=name):
                guarded = self.out / "published" / name
                outside = self.root / f"outside-{name}"
                guarded.rename(outside)
                create_directory_link(guarded, outside)
                before = {p.name: p.read_bytes() for p in outside.iterdir()}
                with self.assertRaises(PublishError):
                    activate_release(self.out, self.b.release_id)
                self.assertEqual({p.name: p.read_bytes() for p in outside.iterdir()}, before)
                if os.name == "nt":
                    guarded.rmdir()
                else:
                    guarded.unlink()
                outside.rename(guarded)
                self.assertEqual(current_release_dir(self.out), self.a.release_dir)

    def test_unknown_dataset_schema_is_rejected_even_with_matching_fingerprints(self):
        from tests.p16_real.test_reference_bundle import payloads, release_fixture
        data = payloads()
        data["p14_tariff"]["schema_version"] = "unknown.tariff.999"
        foreign_root = self.root / "unknown-schema"
        path, release = release_fixture(foreign_root, data=data, activate=False)
        with self.assertRaisesRegex(PublishError, "schema_version"):
            activate_release(foreign_root, release["release_id"])
        self.assertFalse((foreign_root / "published" / "activations").exists())
        self.assertTrue(path.is_dir(), "validation refuses activation without deleting the candidate release")
