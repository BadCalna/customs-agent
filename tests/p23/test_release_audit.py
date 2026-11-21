"""发布审计不可变：重复发布/激活另记追加事件，不改写旧 release。"""
from __future__ import annotations

import json
from unittest.mock import patch

from adapters.sources.reference_import import publish as publishing
from adapters.sources.reference_import.publish import (
    PublishError, PublishRequest, activate_release, publish_release,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_publish_batch, write_verification
from tests.p23.support import P23TestCase, fail_activation_commit


class ReleaseAuditTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.snapshots = []
        for label in ("A", "B"):
            batch = build_publish_batch(self.root / label, label=label)
            built = build_snapshot(SnapshotRequest(batch.manifest, self.out))
            write_verification(self.out, built.snapshot_id)
            self.snapshots.append(built.snapshot_dir)

    def publish(self, index, **kwargs):
        return publish_release(PublishRequest(self.snapshots[index], self.out, **kwargs))

    def release_bytes(self, release):
        return {p.name: p.read_bytes() for p in release.release_dir.iterdir() if p.is_file()}

    def events(self):
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.out / "published" / "activations").glob("*.json"))]

    def test_a_b_republish_a_preserves_original_release_bytes(self):
        first = self.publish(0)
        original_a = self.release_bytes(first)
        second = self.publish(1, allow_content_change=True, reason="首次换到 B")
        original_b = self.release_bytes(second)
        repeated = self.publish(0, allow_content_change=True, reason="本次从 B 换回 A")
        self.assertEqual(repeated.release_id, first.release_id)
        self.assertEqual(self.release_bytes(first), original_a,
                         "旧 A 的发布元数据和数据必须逐字不变")
        self.assertEqual(self.release_bytes(second), original_b)
        self.assertIsNone(repeated.release["previous_release"])
        self.assertEqual(second.release["previous_release"], first.release_id)
        self.assertTrue(repeated.changed, "本次 B→A 的变化仍须单独报告")
        self.assertEqual(repeated.activation["previous_release"], second.release_id)
        self.assertEqual(repeated.activation["reason"], "本次从 B 换回 A")
        self.assertTrue(repeated.activation["content_changed"])
        self.assertEqual(repeated.activation["release_id"], first.release_id)
        saved = [e for e in self.events() if e.get("event_id") == repeated.activation["event_id"]]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0], repeated.activation)
        self.assertEqual(repeated.activation_result["status"], "committed")

    def test_republish_without_activation_never_changes_audit(self):
        first = self.publish(0)
        before = self.release_bytes(first)
        self.publish(1, allow_content_change=True, reason="换到 B")
        repeated = self.publish(0, activate=False, allow_content_change=True, reason="仅复用 A")
        self.assertEqual(self.release_bytes(first), before)
        self.assertFalse(repeated.activated)
        self.assertIsNone(repeated.activation)

    def test_explicit_rollback_is_append_only(self):
        first = self.publish(0)
        before = self.release_bytes(first)
        self.publish(1, allow_content_change=True, reason="换到 B")
        old_events = {p.name: p.read_bytes() for p in
                      (self.out / "published" / "activations").glob("*.json")}
        activate_release(self.out, first.release_id)
        self.assertEqual(self.release_bytes(first), before)
        for name, data in old_events.items():
            self.assertEqual((self.out / "published" / "activations" / name).read_bytes(), data)
        self.assertTrue(any(e.get("operation") == "activate" for e in self.events()))

    def test_activate_can_repair_legacy_split_pointers(self):
        first = self.publish(0)
        second = self.publish(1, activate=False, allow_content_change=True, reason="准备 B")
        pointer = self.out / "published" / "current.json"
        pointer.unlink()
        pointer.write_text(json.dumps({"release_id": second.release_id}))
        repaired = activate_release(self.out, second.release_id)
        self.assertEqual(repaired["release_id"], second.release_id)
        self.assert_current_layout(self.out, second)
        intent = next(e for e in self.events() if e["event_id"] == repaired["activation_event_id"])
        self.assertEqual(intent["previous_release"],
                         second.release_id if publishing.POINTER_BACKEND == "atomic_json" else first.release_id)
        if publishing.POINTER_BACKEND == "atomic_json":
            self.assertEqual(intent["previous_source"], "legacy_current_json")

    def test_result_write_failure_reports_committed_with_audit_warning(self):
        self.publish(0)
        second = self.publish(1, activate=False, allow_content_change=True, reason="准备 B")
        before = self.release_bytes(second)
        original = publishing.write_json_atomic

        def fail_result(path, payload):
            if path.parent.name == "activation-results":
                raise OSError("模拟提交后结果审计落盘失败")
            return original(path, payload)

        with patch.object(publishing, "write_json_atomic", side_effect=fail_result):
            current = activate_release(self.out, second.release_id)
        self.assertEqual(current["release_id"], second.release_id)
        self.assertIn("已提交", current["audit_warning"])
        self.assertIn(current["activation_event_id"], current["audit_warning"])
        # B 首次准备了指针元数据，但其 release.json 和全部原业务文件未改写。
        after = self.release_bytes(second)
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertTrue(any(e["event_id"] == current["activation_event_id"] for e in self.events()))

    def test_after_commit_failure_never_rolls_back_an_observed_release(self):
        self.publish(0)
        second = self.publish(1, activate=False, allow_content_change=True, reason="准备 B")

        def fail_after_commit(stage):
            if stage == "committed":
                self.assertEqual(publishing.current_release_dir(self.out), second.release_dir)
                raise OSError("提交后发生故障")

        with patch.object(publishing, "_activation_checkpoint", side_effect=fail_after_commit):
            result = activate_release(self.out, second.release_id)
        self.assertEqual(result["release_id"], second.release_id)
        self.assertIn("已提交", result["audit_warning"])
        self.assert_current_layout(self.out, second)
        failed = [json.loads(path.read_text(encoding="utf-8")) for path in
                  (self.out / "published" / "activation-results").glob("*.json")
                  if path.stem == result["activation_event_id"]]
        self.assertEqual(failed, [], "提交后的故障不得伪造 failed 激活结果")

    def test_failed_activation_records_failure_without_touching_release(self):
        first = self.publish(0)
        before = self.release_bytes(first)
        second = self.publish(1, activate=False, allow_content_change=True, reason="准备 B")
        with fail_activation_commit():
            with self.assertRaises(PublishError):
                activate_release(self.out, second.release_id)
        self.assertEqual(self.release_bytes(first), before)
        outcomes = [json.loads(p.read_text()) for p in
                    (self.out / "published" / "activation-results").glob("*.json")]
        self.assertTrue(any(e["status"] == "failed" for e in outcomes))
        current = json.loads((self.out / "published" / "current.json").read_text())
        self.assertEqual(current["release_id"], first.release_id)
