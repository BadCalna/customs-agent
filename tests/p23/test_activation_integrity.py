"""激活前目标完整性与读取方单次 current 解析的回归。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from adapters.sources.reference_import import publish as publishing
from adapters.sources.reference_import.publish import (
    PublishError, PublishRequest, activate_release, dataset_paths, list_releases, publish_release,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_publish_batch, write_verification
from tests.p23.support import P23TestCase, active_pointer
from tests.platform_files import create_directory_link


class ActivationIntegrityTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.releases = []
        for label in ("A", "B"):
            batch = build_publish_batch(self.root / label, label=label)
            built = build_snapshot(SnapshotRequest(batch.manifest, self.out))
            write_verification(self.out, built.snapshot_id)
            result = publish_release(PublishRequest(
                built.snapshot_dir, self.out, allow_content_change=bool(self.releases),
                reason="测试换版" if self.releases else "",
            ))
            self.releases.append(result)
        self.old, self.current = self.releases

    def assert_rejected_without_switch(self):
        pointer = active_pointer(self.out)
        before = pointer.read_bytes() if publishing.POINTER_BACKEND == "atomic_json" else os.readlink(pointer)
        results = set((self.out / "published" / "activation-results").glob("*.json"))
        with self.assertRaises(PublishError):
            activate_release(self.out, self.old.release_id)
        self.assertEqual(pointer.read_bytes() if publishing.POINTER_BACKEND == "atomic_json" else os.readlink(pointer), before)
        self.assertEqual(json.loads((self.out / "published" / "current.json").read_text())[
            "release_id"], self.current.release_id)
        self.assertEqual(set((self.out / "published" / "activation-results").glob("*.json")), results,
                         "校验失败不得追加 committed 结果")

    def test_missing_data_file_refuses_activation(self):
        (self.old.release_dir / "tariff.json").unlink()
        self.assert_rejected_without_switch()

    def test_changed_data_file_refuses_activation(self):
        path = self.old.release_dir / "tariff.json"
        raw = path.read_bytes()
        path.write_bytes(raw.replace("合成".encode(), "伪造".encode(), 1))
        self.assertEqual(path.stat().st_size, len(raw), "同字节长度也必须检测 SHA-256")
        self.assert_rejected_without_switch()

    def test_invalid_release_metadata_refuses_activation(self):
        path = self.old.release_dir / "release.json"
        payload = json.loads(path.read_text())
        payload["release_id"] = self.current.release_id
        path.write_text(json.dumps(payload))
        self.assert_rejected_without_switch()

    def test_metadata_fingerprint_change_refuses_activation(self):
        path = self.old.release_dir / "release.json"
        payload = json.loads(path.read_text())
        payload["business_fingerprint"] = "0" * 64
        path.write_text(json.dumps(payload))
        self.assert_rejected_without_switch()

    def test_bad_current_metadata_refuses_activation(self):
        for field in ("release_id", "release_fingerprint", "business_fingerprint", "review_status"):
            path = self.old.release_dir / ".current.json"
            original = path.read_bytes()
            payload = json.loads(original)
            payload[field] = "wrong"
            path.write_text(json.dumps(payload))
            with self.subTest(field=field):
                self.assert_rejected_without_switch()
            path.write_bytes(original)

    def test_unactivated_target_cannot_forge_review_or_dataset_metadata(self):
        activate_release(self.out, self.old.release_id)
        self.old, self.current = self.current, self.old
        # 构造从未准备 .current.json 的发布物，不依赖指针副本兜底校验。
        (self.old.release_dir / ".current.json").unlink()
        path = self.old.release_dir / "release.json"
        original = path.read_bytes()
        for field, value in (("review_status", "reviewed"), ("dataset_kind", "synthetic")):
            payload = json.loads(original)
            payload[field] = value
            path.write_text(json.dumps(payload))
            with self.subTest(field=field):
                self.assert_rejected_without_switch()
            path.write_bytes(original)
        payload = json.loads(original)
        payload["files"]["p14_tariff"]["review_status"] = "reviewed"
        path.write_text(json.dumps(payload))
        self.assert_rejected_without_switch()

    def test_bad_json_refuses_activation(self):
        (self.old.release_dir / "release.json").write_text("{broken")
        self.assert_rejected_without_switch()

    def test_bad_metadata_rejected_through_cli(self):
        from adapters.sources.reference_import.cli import main
        (self.old.release_dir / "tariff.json").unlink()
        code = main(["activate", "--out-root", str(self.out), "--release-id", self.old.release_id])
        self.assertEqual(code, 2)
        self.assertEqual(publishing._active_release_id(self.out), self.current.release_id)

    def test_invalid_current_target_is_not_treated_as_unconfigured(self):
        link = self.out / "published" / "current"
        link.unlink(missing_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        create_directory_link(link, outside)
        with self.assertRaises(PublishError):
            dataset_paths(self.out)

    def test_intact_rollback_passes_without_rewriting_old_release(self):
        before = {p.name: p.read_bytes() for p in self.old.release_dir.iterdir() if p.is_file()}
        result = activate_release(self.out, self.old.release_id)
        self.assertEqual(result["release_id"], self.old.release_id)
        self.assertEqual({p.name: p.read_bytes() for p in self.old.release_dir.iterdir()
                          if p.is_file()}, before)

    def read_during_switch(self, reader):
        if publishing.POINTER_BACKEND == "atomic_json":
            original = publishing._read_release_json
            reads = []
            switched = False
            inside_switch = False

            def interleaved_read(path):
                nonlocal switched, inside_switch
                value = original(path)
                if Path(path) == active_pointer(self.out) and not inside_switch:
                    reads.append(value)
                    if not switched:
                        switched = True
                        inside_switch = True
                        try:
                            activate_release(self.out, self.old.release_id)
                        finally:
                            inside_switch = False
                return value

            with patch.object(publishing, "_read_release_json", side_effect=interleaved_read):
                result = reader()
            self.assertTrue(switched, "必须真实穿插一次正常激活")
            self.assertEqual(len(reads), 1, "每次读取只可解析一次权威指针")
            return result, reads
        original = publishing.os.readlink
        reads = []
        switched = False

        def interleaved_readlink(path, *args, **kwargs):
            nonlocal switched
            value = original(path, *args, **kwargs)
            if Path(path) == self.out / "published" / "current":
                reads.append(value)
                if not switched:
                    switched = True
                    activate_release(self.out, self.old.release_id)
            return value

        with patch.object(publishing.os, "readlink", side_effect=interleaved_readlink):
            result = reader()
        self.assertTrue(switched, "必须真实穿插一次正常激活")
        return result, reads

    def test_dataset_paths_pins_one_release_during_switch(self):
        result, _ = self.read_during_switch(lambda: dataset_paths(self.out))
        self.assertTrue(result)
        self.assertTrue(all(self.current.release_id in p for p in result.values()),
                        "本次调用应完整返回切换前已解析的 B，不跨版本混读")
        self.assertTrue(all(self.old.release_id in p for p in dataset_paths(self.out).values()))

    def test_list_releases_pins_one_active_release_during_switch(self):
        result, _ = self.read_during_switch(lambda: list_releases(self.out))
        self.assertEqual([r["release_id"] for r in result if r["active"]],
                         [self.current.release_id])

    def test_legacy_independent_pointer_requires_explicit_repair(self):
        pointer = self.out / "published" / "current.json"
        pointer.unlink()
        pointer.write_text(json.dumps({"release_id": self.old.release_id}))
        with self.assertRaises(PublishError):
            dataset_paths(self.out)
        repaired = activate_release(self.out, self.current.release_id)
        self.assertEqual(repaired["release_id"], self.current.release_id)
        self.assertTrue(dataset_paths(self.out))

    def test_current_release_dir_pins_one_release_during_switch(self):
        result, _ = self.read_during_switch(lambda: publishing._current_release_dir(self.out))
        self.assertEqual(result, self.current.release_dir)
