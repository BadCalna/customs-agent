"""回滚韧性：当前版本损坏仍可显式切回完好旧版；首次发布与读取重叠不误报。"""
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from adapters.sources.reference_import import publish as publishing
from adapters.sources.reference_import.publish import (
    PublishError, PublishRequest, activate_release, dataset_paths, list_releases, publish_release,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_publish_batch, write_verification
from tests.p23.support import P23TestCase


class RollbackResilienceTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.releases = []
        for label in ("A", "B"):
            batch = build_publish_batch(self.root / label, label=label)
            built = build_snapshot(SnapshotRequest(batch.manifest, self.out))
            write_verification(self.out, built.snapshot_id)
            result = publish_release(PublishRequest(
                built.snapshot_dir, self.out,
                allow_content_change=bool(self.releases),
                reason="测试换版" if self.releases else "",
            ))
            self.releases.append(result)
        self.a, self.b = self.releases

    def current_id(self):
        return publishing._active_release_id(self.out)

    def events(self):
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.out / "published" / "activations").glob("*.json"))]

    def force_current(self, release):
        """测试内把 current 指回指定版本，绕开目标校验以构造“当前版本损坏”前置。"""
        link = self.out / "published" / "current"
        temp = self.out / "published" / ".test-current"
        temp.unlink(missing_ok=True)
        os.symlink(os.path.join("releases", release.release_id), temp)
        os.replace(temp, link)

    def test_missing_current_file_still_allows_rollback(self):
        """B 缺数据文件时，A 仍通过校验并允许切回；缺件差异按“仅一侧存在”计算。"""
        (self.b.release_dir / "tariff.json").unlink()
        result = activate_release(self.out, self.a.release_id)
        self.assertEqual(result["release_id"], self.a.release_id)
        self.assertEqual(self.current_id(), self.a.release_id)
        event = next(e for e in self.events() if e["event_id"] == result["activation_event_id"])
        self.assertEqual(event["previous_release"], self.b.release_id)
        # 前驱缺件也属于前驱不完整，差异不得按“仅一侧存在”算成 computed。
        self.assertEqual(event["content_diff_status"], "unavailable")
        self.assertIsNone(event["content_changed"])
        self.assertIn("p14_tariff", event["content_diff_error"])
        self.assertTrue(dataset_paths(self.out))

    def test_corrupt_current_json_does_not_block_rollback(self):
        path = self.b.release_dir / "tariff.json"
        path.write_text("{broken")
        before_a = {p.name: p.read_bytes() for p in self.a.release_dir.iterdir() if p.is_file()}
        before_b = {p.name: p.read_bytes() for p in self.b.release_dir.iterdir() if p.is_file()}
        result = activate_release(self.out, self.a.release_id)
        self.assertEqual(result["release_id"], self.a.release_id)
        self.assertEqual(self.current_id(), self.a.release_id)
        event = next(e for e in self.events() if e["event_id"] == result["activation_event_id"])
        self.assertEqual(event["previous_release"], self.b.release_id)
        self.assertEqual(event["content_diff_status"], "unavailable")
        self.assertIsNone(event["content_changed"])
        self.assertIn("p14_tariff", event["content_diff_error"])
        self.assertIn("content_diff_warning", result)
        self.assertEqual({p.name: p.read_bytes() for p in self.a.release_dir.iterdir()
                          if p.is_file()}, before_a)
        self.assertEqual({p.name: p.read_bytes() for p in self.b.release_dir.iterdir()
                          if p.is_file()}, before_b)

    def test_valid_json_but_sha_mismatch_marks_diff_unavailable(self):
        """前驱 JSON 仍可解析但内容被改（SHA 不匹配）时，不能基于坏文件算出 computed。"""
        (self.b.release_dir / "tariff.json").write_text(json.dumps({"codes": []}))
        result = activate_release(self.out, self.a.release_id)
        event = next(e for e in self.events() if e["event_id"] == result["activation_event_id"])
        self.assertEqual(event["content_diff_status"], "unavailable")
        self.assertIsNone(event["content_changed"])
        self.assertIn("p14_tariff", event["content_diff_error"])
        self.assertEqual(self.current_id(), self.a.release_id)

    def test_activate_cli_prints_diff_warning_on_corrupt_previous(self):
        (self.b.release_dir / "tariff.json").write_text(json.dumps({"codes": []}))
        stderr = io.StringIO()
        stdout = io.StringIO()
        from adapters.sources.reference_import.cli import main
        with redirect_stderr(stderr), __import__("contextlib").redirect_stdout(stdout):
            code = main(["activate", "--out-root", str(self.out), "--release-id", self.a.release_id])
        self.assertEqual(code, 0)
        self.assertIn("差异警告", stderr.getvalue())
        self.assertIn("p14_tariff", stderr.getvalue())

    def test_non_object_current_json_does_not_block_rollback(self):
        """合法 JSON 但顶层不是对象（[]/null/数字）也必须降级为差异不可算。"""
        path = self.b.release_dir / "tariff.json"
        for payload in ("[]", "null", "42", '"text"'):
            self.force_current(self.b)
            path.write_text(payload)
            with self.subTest(payload=payload):
                result = activate_release(self.out, self.a.release_id)
                self.assertEqual(result["release_id"], self.a.release_id)
                event = next(e for e in self.events()
                             if e["event_id"] == result["activation_event_id"])
                self.assertEqual(event["content_diff_status"], "unavailable")
                self.assertIn("p14_tariff", event["content_diff_error"])
                self.assertEqual(self.current_id(), self.a.release_id)

    def test_missing_derived_pointer_with_current_link_is_tolerated(self):
        """首次发布时 current.json 尚未创建、current 已可解析，不应报人工修复。"""
        pointer = self.out / "published" / "current.json"
        pointer.unlink()
        self.assertEqual(self.current_id(), self.b.release_id)
        paths = dataset_paths(self.out)
        self.assertTrue(paths)
        self.assertTrue(all(self.b.release_id in value for value in paths.values()))
        self.assertEqual([r["release_id"] for r in list_releases(self.out) if r["active"]],
                         [self.b.release_id])

    def test_pointer_missing_then_current_appears_during_first_read(self):
        """模拟首次发布：current 第一次不可见、重试期间出现，读取应成功。"""
        pointer = self.out / "published" / "current.json"
        pointer.unlink()
        current = self.out / "published" / "current"
        real_readlink = os.readlink
        attempts = {"count": 0}

        def readlink(path, *args, **kwargs):
            if str(path) == str(current) and attempts["count"] == 0:
                attempts["count"] += 1
                raise FileNotFoundError(str(path))
            return real_readlink(path, *args, **kwargs)

        with patch.object(publishing.os, "readlink", side_effect=readlink):
            self.assertEqual(publishing._active_release_id(self.out), self.b.release_id)
        self.assertEqual(attempts["count"], 1)

    def test_pointer_created_before_current_during_first_publish_retries(self):
        """首次发布先建 current.json、后切 current 的瞬时窗口：短暂重试后成功。"""
        pointer = self.out / "published" / "current.json"
        current = self.out / "published" / "current"
        pointer.unlink()
        os.symlink(os.path.join("current", ".current.json"), pointer)
        real_readlink = os.readlink
        attempts = {"count": 0}

        def readlink(path, *args, **kwargs):
            if str(path) == str(current) and attempts["count"] < 2:
                attempts["count"] += 1
                raise FileNotFoundError(str(path))
            return real_readlink(path, *args, **kwargs)

        with patch.object(publishing.os, "readlink", side_effect=readlink):
            self.assertEqual(publishing._active_release_id(self.out), self.b.release_id)
        self.assertEqual(attempts["count"], 2)

    def test_no_release_still_returns_none(self):
        empty = self.root / "empty-out"
        self.assertIsNone(publishing._active_release_id(empty))
        with self.assertRaises(PublishError):
            dataset_paths(empty)

    def test_publish_rejects_when_current_pointer_lost(self):
        """已发布 B 但 current 被删、current.json 悬空：内容不同的 C 不得绕过换版门禁。"""
        (self.out / "published" / "current").unlink()
        self.assertTrue((self.out / "published" / "current.json").is_symlink())
        c_batch = build_publish_batch(self.root / "C", label="C", name_suffix="（内容不同 C）")
        c_built = build_snapshot(SnapshotRequest(c_batch.manifest, self.out))
        write_verification(self.out, c_built.snapshot_id)
        before_releases = sorted(p.name for p in (self.out / "published" / "releases").iterdir())
        with self.assertRaises(PublishError) as caught:
            publish_release(PublishRequest(c_built.snapshot_dir, self.out))
        self.assertIn("activate", str(caught.exception))
        self.assertIn("current", str(caught.exception))
        self.assertEqual(sorted(p.name for p in (self.out / "published" / "releases").iterdir()),
                         before_releases, "拒绝时不得新增 release")
        repaired = activate_release(self.out, self.a.release_id)
        self.assertEqual(repaired["release_id"], self.a.release_id)
        self.assertTrue(dataset_paths(self.out))

    def test_legacy_plain_json_pointer_still_requires_repair(self):
        pointer = self.out / "published" / "current.json"
        pointer.unlink()
        pointer.write_text(json.dumps({"release_id": self.a.release_id}))
        with self.assertRaises(PublishError):
            dataset_paths(self.out)
        repaired = activate_release(self.out, self.b.release_id)
        self.assertEqual(repaired["release_id"], self.b.release_id)
        self.assertTrue(dataset_paths(self.out))

    def test_missing_current_with_plain_pointer_is_not_treated_as_unpublished(self):
        """current 缺失但仍有普通 JSON 指针：必须要求修复，不能当成首次发布；
        显式修复时仍要把指针指向的真实前驱写进激活审计。"""
        (self.out / "published" / "current").unlink()
        pointer = self.out / "published" / "current.json"
        if pointer.is_symlink():
            pointer.unlink()
        pointer.write_text(json.dumps({"release_id": self.b.release_id}))
        with self.assertRaises(PublishError):
            publishing._active_release_id(self.out)
        with self.assertRaises(PublishError):
            dataset_paths(self.out)
        repaired = activate_release(self.out, self.a.release_id)
        self.assertEqual(repaired["release_id"], self.a.release_id)
        event = next(e for e in self.events() if e["event_id"] == repaired["activation_event_id"])
        self.assertEqual(event["previous_release"], self.b.release_id)
        self.assertEqual(event["previous_source"], "legacy_current_json")
        self.assertTrue(dataset_paths(self.out))

    def test_pointer_permission_error_is_publish_error(self):
        """current 正常、读取 current.json 软链时权限错误，也必须转 PublishError。"""
        pointer = self.out / "published" / "current.json"
        if pointer.is_symlink():
            pointer.unlink()
        os.symlink(os.path.join("current", ".current.json"), pointer)
        real_readlink = os.readlink

        def readlink(path, *args, **kwargs):
            if str(path) == str(pointer):
                raise PermissionError("pointer denied")
            return real_readlink(path, *args, **kwargs)

        with patch.object(publishing.os, "readlink", side_effect=readlink):
            with self.assertRaises(PublishError) as caught:
                dataset_paths(self.out)
        self.assertIn("current/current.json", str(caught.exception))

    def test_diff_failure_is_recorded_without_rewriting_either_release(self):
        before_a = {p.name: p.read_bytes() for p in self.a.release_dir.iterdir() if p.is_file()}
        before_b = {p.name: p.read_bytes() for p in self.b.release_dir.iterdir() if p.is_file()}
        (self.b.release_dir / "tariff.json").write_text("{broken")
        before_b["tariff.json"] = (self.b.release_dir / "tariff.json").read_bytes()
        result = activate_release(self.out, self.a.release_id)
        self.assertEqual({p.name: p.read_bytes() for p in self.a.release_dir.iterdir()
                          if p.is_file()}, before_a)
        self.assertEqual({p.name: p.read_bytes() for p in self.b.release_dir.iterdir()
                          if p.is_file()}, before_b)
        self.assertTrue(result.get("activation_event_id"))
