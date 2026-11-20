"""P23 发布门禁口径：验证报告、人工复核、换版理由与回滚都必须显式。

守的规则：

- 没有验证报告、验证未通过 → 拒绝发布；
- 复核文件缺复核人、时间不带时区、结论不是 approved、少确认一条业务假设 → 拒绝，
  且错误信息必须点明原因；
- 程序不自己填复核人：``reviewed_by`` 只能来自复核文件；
- 没有复核文件可以发布，但数据必须保持 ``unreviewed``；
- 覆盖已发布内容需要 ``allow_content_change`` + 理由，release.json 里要留差异与理由；
- 旧 release 不删除，``activate_release`` 可切回，``list_releases`` 的 active 标记要准。
"""

from __future__ import annotations

import json

from adapters.sources.reference_import.publish import (
    PublishError,
    PublishRequest,
    activate_release,
    list_releases,
    publish_release,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from tests.p23.builders import build_publish_batch, write_review, write_verification
from tests.p23.support import P23TestCase


class PublishGateTest(P23TestCase):
    def setUp(self):
        super().setUp()
        self.source_batch = build_publish_batch(self.root / "src", label="A")
        self.out_root = self.root / "out"
        self.build = build_snapshot(
            SnapshotRequest(manifest_path=self.source_batch.manifest, out_root=self.out_root)
        )
        self.assertEqual(self.build.report["status"], "success", "发布前置：快照必须是成功导入")
        self.snapshot_id = self.build.snapshot_id
        manifest = json.loads((self.build.snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assumptions = list(manifest["assumptions"])
        self.assumption_keys = [item.partition("：")[0] for item in self.assumptions]
        self.assertTrue(self.assumption_keys, "夹具前提：快照必须带可确认的业务假设键")
        write_verification(self.out_root, self.snapshot_id)

    # --- 辅助
    def review(self, name: str = "review.json", **overrides):
        confirmations = overrides.pop("confirmations", {key: True for key in self.assumption_keys})
        return write_review(self.root / name, confirmations=confirmations, **overrides)

    def publish(self, **kwargs):
        return publish_release(
            PublishRequest(snapshot_dir=self.build.snapshot_dir, out_root=self.out_root, **kwargs)
        )

    # --- 验证报告门
    def test_rejected_without_verification(self):
        verification = self.out_root / "verification" / f"{self.snapshot_id}.json"
        verification.unlink()
        with self.assertRaises(PublishError) as caught:
            self.publish()
        message = str(caught.exception)
        self.assertIn("查询验证报告", message)
        self.assertIn(self.snapshot_id, message)
        self.assertFalse((self.out_root / "published").exists(), "拒绝时不得创建 release 目录")

    def test_rejected_when_verification_failed(self):
        write_verification(
            self.out_root,
            self.snapshot_id,
            all_passed=False,
            checks=[{"id": "p14.tariff.leading_zero_hit", "status": "fail", "detail": "合成样本"}],
        )
        with self.assertRaises(PublishError) as caught:
            self.publish()
        message = str(caught.exception)
        self.assertIn("验证未通过", message)
        self.assertIn("p14.tariff.leading_zero_hit", message, "必须列出失败的检查 id")

    def test_verification_shape_cannot_upgrade_failed_checks(self):
        path = self.out_root / "verification" / f"{self.snapshot_id}.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        variants = (
            {"schema_version": "unknown.query-check.999"}, {"snapshot_dir": "another-snapshot"},
            {"all_passed": "false"}, {"all_passed": 1}, {"checks": []},
            {"counts": {"pass": 1, "fail": False, "skip": 0}},
            {"checks": [{"id": "actually-failed", "status": "fail"}],
             "counts": {"pass": 0, "fail": 1, "skip": 0}, "all_passed": True},
        )
        for overrides in variants:
            with self.subTest(overrides=overrides):
                path.write_text(json.dumps({**original, **overrides}), encoding="utf-8")
                with self.assertRaises(PublishError):
                    self.publish()
                self.assertFalse((self.out_root / "published").exists())

    # --- 复核文件门
    def test_rejected_without_reviewed_by(self):
        path = self.review(reviewed_by="", name="no-reviewer.json")
        with self.assertRaises(PublishError) as caught:
            self.publish(review_path=path)
        self.assertIn("reviewed_by", str(caught.exception))
        self.assertIn("不会替你填复核人", str(caught.exception))

    def test_rejected_with_naive_reviewed_at(self):
        path = self.review(reviewed_at="2026-11-18T03:00:00", name="naive-time.json")
        with self.assertRaises(PublishError) as caught:
            self.publish(review_path=path)
        self.assertIn("带时区", str(caught.exception))

    def test_rejected_when_decision_not_approved(self):
        path = self.review(decision="rejected", name="rejected.json")
        with self.assertRaises(PublishError) as caught:
            self.publish(review_path=path)
        message = str(caught.exception)
        self.assertIn("rejected", message)
        self.assertIn("approved", message)

    def test_rejected_when_assumption_not_confirmed(self):
        missing = self.assumption_keys[0]
        path = self.review(
            confirmations={key: True for key in self.assumption_keys[1:]},
            name="missing-confirmation.json",
        )
        with self.assertRaises(PublishError) as caught:
            self.publish(review_path=path)
        message = str(caught.exception)
        self.assertIn("逐项确认", message)
        self.assertIn(missing, message, "必须点出没被确认的那条假设")

    def test_rejected_when_scope_is_unknown(self):
        path = self.review(scope=["p13_unknown"], name="bad-scope.json")
        with self.assertRaises(PublishError) as caught:
            self.publish(review_path=path)
        self.assertIn("scope", str(caught.exception))
        self.assertIn("p13_unknown", str(caught.exception))

    def test_falsey_review_container_types_are_rejected(self):
        for field in ("scope", "confirmations"):
            for value in (False, 0, "", None, [""] if field == "scope" else []):
                with self.subTest(field=field, value=value):
                    path = self.review(name="invalid-review.json")
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload[field] = value
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(PublishError) as caught:
                        self.publish(review_path=path)
                    self.assertIn(field, str(caught.exception))
                    self.assertFalse((self.out_root / "published").exists())

    # --- 成功路径
    def test_publish_with_review_uses_reviewer_from_file(self):
        path = self.review(name="approved.json", notes="合成复核说明")
        result = self.publish(review_path=path)
        self.assertTrue(result.activated)
        self.assertEqual(result.release["review_status"], "reviewed")
        self.assertEqual(result.release["review"]["reviewed_by"], "合成复核人")
        self.assertFalse(result.changed)
        tariff = json.loads((result.release_dir / "tariff.json").read_text(encoding="utf-8"))
        self.assertEqual(tariff["dataset"]["review_status"], "reviewed")
        self.assertEqual(tariff["dataset"]["reviewed_by"], "合成复核人",
                         "复核人只能来自复核文件")
        self.assertEqual(tariff["dataset"]["reviewed_at"], "2026-11-18T03:00:00+08:00")
        self.assertIn("合成复核说明", tariff["dataset"]["notes"])
        elements = json.loads((result.release_dir / "elements.json").read_text(encoding="utf-8"))
        self.assertEqual(elements["dataset"]["review_status"], "reviewed")
        release = json.loads((result.release_dir / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(release["snapshot_id"], self.snapshot_id)
        self.assertEqual(release["review_status"], "reviewed")
        self.assertEqual(release["content_changed"], False)
        self.assertTrue(release["published_at"])
        self.assertEqual(sorted(release["files"]),
                         ["p14_elements", "p14_tariff", "regulatory_code_ledger"])

    def test_publish_without_review_is_unreviewed(self):
        result = self.publish()
        self.assertEqual(result.release["review_status"], "unreviewed")
        self.assertIsNone(result.release["review"])
        tariff = json.loads((result.release_dir / "tariff.json").read_text(encoding="utf-8"))
        self.assertEqual(tariff["dataset"]["review_status"], "unreviewed")
        self.assertEqual(tariff["dataset"]["reviewed_by"], "")
        self.assertIsNone(tariff["dataset"]["reviewed_at"])

    # --- 内容变化
    def test_content_change_needs_flag_and_reason(self):
        first = self.publish()
        second_batch = build_publish_batch(
            self.root / "b", label="B", name_suffix="（内容变更 B）"
        )
        second = build_snapshot(
            SnapshotRequest(manifest_path=second_batch.manifest, out_root=self.out_root)
        )
        write_verification(self.out_root, second.snapshot_id)

        with self.assertRaises(PublishError) as caught:
            publish_release(
                PublishRequest(snapshot_dir=second.snapshot_dir, out_root=self.out_root)
            )
        message = str(caught.exception)
        self.assertIn("默认不覆盖已发布数据", message)
        self.assertIn("--allow-content-change", message)
        self.assertIn("p14_tariff", message, "差异报告必须列出受影响的数据类别")
        self.assertEqual(len(list_releases(self.out_root)), 1,
                         "被拒绝的换版不得留下 release 目录")

        allowed = publish_release(
            PublishRequest(
                snapshot_dir=second.snapshot_dir,
                out_root=self.out_root,
                allow_content_change=True,
                reason="合成样本：验证换版理由入库",
            )
        )
        self.assertTrue(allowed.changed)
        self.assertTrue(allowed.diff["changed"])
        self.assertIn("p14_tariff", allowed.diff["datasets"])
        self.assertEqual(allowed.diff["datasets"]["p14_tariff"]["changed"], 2,
                         "两条编码的记录内容（含来源文件名）都变了")
        release = json.loads((allowed.release_dir / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(release["reason"], "合成样本：验证换版理由入库")
        self.assertTrue(release["content_changed"])
        self.assertTrue(release["content_diff"])
        self.assertEqual(release["previous_release"], first.release_id)
        self.assertTrue((self.out_root / "published" / "releases" / first.release_id).is_dir(),
                        "换版不得删除旧 release")

    # --- 回滚与列表
    def test_activate_release_rolls_back(self):
        first = self.publish()
        second_batch = build_publish_batch(
            self.root / "b", label="B", name_suffix="（内容变更 B）"
        )
        second = build_snapshot(
            SnapshotRequest(manifest_path=second_batch.manifest, out_root=self.out_root)
        )
        write_verification(self.out_root, second.snapshot_id)
        second_release = publish_release(
            PublishRequest(
                snapshot_dir=second.snapshot_dir,
                out_root=self.out_root,
                allow_content_change=True,
                reason="合成样本：验证回滚",
            )
        )
        current = json.loads(
            (self.out_root / "published" / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(current["release_id"], second_release.release_id)

        activated = activate_release(self.out_root, first.release_id)
        self.assertEqual(activated["release_id"], first.release_id)
        current = json.loads(
            (self.out_root / "published" / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(current["release_id"], first.release_id)
        self.assert_current_layout(self.out_root, first)
        self.assertTrue((first.release_dir / "release.json").is_file())
        self.assertTrue((second_release.release_dir / "release.json").is_file())

        releases = {item["release_id"]: item for item in list_releases(self.out_root)}
        self.assertEqual(set(releases), {first.release_id, second_release.release_id})
        self.assertTrue(releases[first.release_id]["active"], "切回后旧 release 必须标记 active")
        self.assertFalse(releases[second_release.release_id]["active"])
        self.assertEqual(releases[first.release_id]["datasets"],
                         ["p14_elements", "p14_tariff", "regulatory_code_ledger"])
        self.assertEqual(releases[first.release_id]["snapshot_id"], self.snapshot_id)
