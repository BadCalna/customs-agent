"""P23 第二轮独立审查（对 diff 复审）发现的实质问题回归。

每条都先在修复前复现，再在此固定行为：

- 快照身份只看 payload → 两份“payload 相同但坏行不同”的输入撞同一快照、丢掉本次拒绝记录；
- 目录页签表头被改名 → 静默拿要素明细页当目录，丢码并报 success；
- `--acquisition-date`/批次号不进输入指纹 → 改日期后复用旧快照，退税率过期判定失效；
- 发布内容差异比较漏监管代码台账 → 台账变更可无理由覆盖；
- 复用候选快照不校验输出文件 → 被改/被删仍报 success；
- 文本退税率无百分号 → 静默按百分数处理（100 倍偏差）；
- 同名“可选”列表头重复 → 静默取左列、丢右列；
- 注释读取期不校验分段与全文能否拼回。
"""

from __future__ import annotations

import json
import os
import threading
import zipfile
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import adapters.sources.reference_import.publish as publish_module
from adapters.sources.reference_import.publish import (
    PublishError,
    PublishRequest,
    activate_release,
    dataset_paths,
    list_releases,
    publish_release,
)
from adapters.sources.reference_import.snapshot import (
    SnapshotFailed,
    SnapshotRequest,
    build_snapshot,
)
from adapters.sources.reference_import.sources import LayoutError
from adapters.sources.tariff_commentary import (
    CommentaryStoreError,
    JsonCommentaryStore,
)
from tests.p23.builders import (
    ElementRow,
    IndexRow,
    TaxRow,
    build_batch,
    commentary_item,
    core_element_rows,
    core_index_rows,
    mutate_manifest,
    write_catalog_workbook,
    write_verification,
)
from tests.p23.support import P23TestCase


def _rewrite_zip_entry(path: Path, part: str, transform: Callable[[bytes], bytes]) -> None:
    """就地替换 zip 里的一个部件（用于模拟“表头被改名”这类结构变化）。"""

    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    entries[part] = transform(entries[part])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class SnapshotIdentityTest(P23TestCase):
    """问题 1：快照身份必须同时覆盖“报告口径”，否则坏行会被静默丢弃。"""

    def test_different_rejected_rows_get_different_snapshots(self):
        clean = build_batch(
            self.root / "clean",
            index_rows=[
                IndexRow("0999999991", "合成甲", element_count=1, required_count=1)
            ],
            element_rows=[ElementRow("0999999991", "合成甲", "要素甲", "1")],
            batch_id="same-batch",
            dataset_prefix="same-batch",
            workbook_names=("同名_M01.xlsx", "同名_M02.xlsx"),
        )
        dirty = build_batch(
            self.root / "dirty",
            index_rows=[
                IndexRow("0999999991", "合成甲", element_count=1, required_count=1),
                IndexRow("101210010", "合成坏码", numeric=True),
            ],
            element_rows=[ElementRow("0999999991", "合成甲", "要素甲", "1")],
            batch_id="same-batch",
            dataset_prefix="same-batch",
            workbook_names=("同名_M01.xlsx", "同名_M02.xlsx"),
        )
        out_root = self.root / "out"
        first = build_snapshot(
            SnapshotRequest(manifest_path=clean.manifest, out_root=out_root)
        )
        second = build_snapshot(
            SnapshotRequest(manifest_path=dirty.manifest, out_root=out_root)
        )
        self.assertNotEqual(
            first.snapshot_id,
            second.snapshot_id,
            "坏行不同 → 报告口径不同 → 必须是另一个快照，不能借用旧报告",
        )
        self.assertFalse(second.reused, "第二份输入必须真算一遍")
        counts = next(
            item["counts"]
            for item in second.report["datasets"]
            if item["dataset"] == "p14_tariff"
        )
        self.assertEqual(counts["rejected"], 2, "两个来源里的坏码各拒绝一次")
        rejected = [
            item
            for item in second.report["records"]
            if item["status"] == "rejected"
            and item["dataset"] == "p14_tariff"
        ]
        self.assertEqual(
            {issue["code"] for item in rejected for issue in item["issues"]},
            {"HS_CELL_NUMERIC_PRECISION_LOST"},
            "本次的拒绝原因必须留在报告里",
        )
        self.assertEqual(int(second.exit_code), 1, "有拒绝时退出码必须是部分")

    def test_batch_id_change_creates_new_snapshot(self):
        batch = build_batch(
            self.root / "batch",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        out_root = self.root / "out"
        first = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=out_root)
        )

        def rename(payload: dict) -> None:
            payload["batch_id"] = "customs-reference-synthetic-renamed"

        renamed = mutate_manifest(batch, rename, name="reference-sources-renamed.json")
        second = build_snapshot(SnapshotRequest(manifest_path=renamed, out_root=out_root))
        self.assertNotEqual(
            first.snapshot_id, second.snapshot_id, "批次号变了不能复用旧快照"
        )
        self.assertEqual(second.report["batch_id"], "customs-reference-synthetic-renamed")


class HeaderStrictnessTest(P23TestCase):
    """问题 2：声明了 hs_catalog 就必须识别到目录表头，不能降级用明细页。"""

    def test_renamed_catalog_header_is_rejected(self):
        batch = build_batch(
            self.root / "src",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        for path in batch.workbooks:
            _rewrite_zip_entry(
                path,
                "xl/worksheets/sheet1.xml",
                lambda data: data.replace("HS编码".encode(), "税则号列".encode()),
            )

        def fix_hashes(payload: dict) -> None:
            from adapters.sources.reference_import.sources import sha256_file

            for entry in payload["inputs"]:
                target = (
                    batch.workbooks[0]
                    if entry["source_id"] == "M01"
                    else batch.workbooks[1]
                )
                entry["sha256"] = sha256_file(target)
                entry["bytes"] = target.stat().st_size

        manifest = mutate_manifest(batch, fix_hashes, name="reference-sources-broken.json")
        with self.assertRaises(LayoutError) as caught:
            build_snapshot(
                SnapshotRequest(manifest_path=manifest, out_root=self.root / "out")
            )
        message = str(caught.exception)
        # 目录表头不可识别时，按“清单声明了 hs_catalog 但识别不到目录表头”或
        # “目录与明细落到同一页签、结构无法区分”两条防线之一拒绝，二者都算明确失败。
        self.assertTrue(
            "hs_catalog" in message or "结构无法区分" in message,
            f"必须写明拒绝原因，实际：{message}",
        )
        self.assertFalse(
            list((self.root / "out" / "candidates").glob("*/")),
            "结构不可信时不能留下候选快照",
        )

    def test_duplicate_optional_header_is_ambiguous(self):
        batch = build_batch(
            self.root / "dup",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        # 同名“可选列”（监管条件代码）在同一表头行出现两次：不能静默取左列。
        import re

        def duplicate_header(data: bytes) -> bytes:
            text = data.decode("utf-8")
            extra = '<c r="AA9" t="inlineStr"><is><t>监管条件代码</t></is></c>'
            return re.sub(
                r'(<row r="9"[^>]*>)(.*?)(</row>)',
                lambda match: match.group(1) + match.group(2) + extra + match.group(3),
                text,
                count=1,
                flags=re.S,
            ).encode("utf-8")

        _rewrite_zip_entry(batch.workbooks[0], "xl/worksheets/sheet1.xml", duplicate_header)

        def fix_hashes(payload: dict) -> None:
            from adapters.sources.reference_import.sources import sha256_file

            for entry in payload["inputs"]:
                target = (
                    batch.workbooks[0]
                    if entry["source_id"] == "M01"
                    else batch.workbooks[1]
                )
                entry["sha256"] = sha256_file(target)
                entry["bytes"] = target.stat().st_size

        manifest = mutate_manifest(batch, fix_hashes, name="reference-sources-duphdr.json")
        with self.assertRaises(LayoutError) as caught:
            build_snapshot(
                SnapshotRequest(manifest_path=manifest, out_root=self.root / "out")
            )
        self.assertEqual(
            caught.exception.code, "header_ambiguous", "同名表头重复必须报结构歧义"
        )
        self.assertIn("表头重复", str(caught.exception))


class AcquisitionDateTest(P23TestCase):
    """问题 3：采集/核对日期参与退税率状态判定，必须进输入指纹。"""

    def test_acquisition_date_participates_in_identity_and_state(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[
                IndexRow("0999999991", "合成甲", element_count=1, required_count=1)
            ],
            element_rows=[ElementRow("0999999991", "合成甲", "要素甲", "1")],
            tax_rows=(
                TaxRow(
                    "0999999991",
                    "合成甲",
                    "0.13",
                    effective_from="2026-01-01",
                    effective_to="2026-12-31",
                ),
            ),
        )
        early = build_snapshot(
            SnapshotRequest(
                manifest_path=batch.manifest,
                out_root=self.root / "out",
                acquisition_date="2026-06-01",
            )
        )
        late = build_snapshot(
            SnapshotRequest(
                manifest_path=batch.manifest,
                out_root=self.root / "out",
                acquisition_date="2027-06-01",
            )
        )
        self.assertNotEqual(
            early.snapshot_id, late.snapshot_id, "采集日期变了不能复用旧快照"
        )
        self.assertFalse(late.reused)
        early_rate = json.loads(
            (early.snapshot_dir / "tax_rates.json").read_text(encoding="utf-8")
        )["rates"][0]
        late_rate = json.loads(
            (late.snapshot_dir / "tax_rates.json").read_text(encoding="utf-8")
        )["rates"][0]
        self.assertEqual(early_rate["rate_state"], "in_force")
        self.assertEqual(
            late_rate["rate_state"], "unknown", "取得日期晚于失效日 → 保持待核"
        )


class PublishDiffTest(P23TestCase):
    """问题 4：监管代码台账也是发布物，内容变化必须触发覆盖守卫。"""

    def test_ledger_change_requires_explicit_allow(self):
        def batch_for(reg_codes: str, label: str):
            return build_batch(
                self.root / label,
                index_rows=[
                    IndexRow(
                        "0999999991",
                        "合成甲",
                        reg_codes=reg_codes,
                        element_count=1,
                        required_count=1,
                    )
                ],
                element_rows=[ElementRow("0999999991", "合成甲", "要素甲", "1")],
                kind="real",
                batch_id=f"ledger-{label}",
                dataset_prefix=f"ledger-{label}",
                workbook_names=(f"台账_{label}_M01.xlsx", f"台账_{label}_M02.xlsx"),
            )

        first_batch = batch_for("A", "a")
        second_batch = batch_for("B", "b")
        out_root = self.root / "out"
        first = build_snapshot(
            SnapshotRequest(manifest_path=first_batch.manifest, out_root=out_root)
        )
        write_verification(out_root, first.snapshot_id)
        publish_release(PublishRequest(snapshot_dir=first.snapshot_dir, out_root=out_root))
        second = build_snapshot(
            SnapshotRequest(manifest_path=second_batch.manifest, out_root=out_root)
        )
        write_verification(out_root, second.snapshot_id)
        with self.assertRaises(PublishError) as caught:
            publish_release(
                PublishRequest(snapshot_dir=second.snapshot_dir, out_root=out_root)
            )
        self.assertIn("内容", str(caught.exception))
        self.assertIn("regulatory_code_ledger", str(caught.exception))
        allowed = publish_release(
            PublishRequest(
                snapshot_dir=second.snapshot_dir,
                out_root=out_root,
                allow_content_change=True,
                reason="合成测试：监管代码由 A 改为 B",
            )
        )
        self.assertTrue(allowed.changed)
        self.assertIn("regulatory_code_ledger", allowed.release["content_diff"]["datasets"])
        self.assertEqual(allowed.release["reason"], "合成测试：监管代码由 A 改为 B")


class ReuseIntegrityTest(P23TestCase):
    """问题 5：候选快照被改动后不能在复用时报 success。"""

    def test_corrupted_candidate_is_refused(self):
        batch = build_batch(
            self.root / "src",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
        )
        out_root = self.root / "out"
        first = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=out_root)
        )
        path = first.snapshot_dir / "tariff.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(SnapshotFailed) as caught:
            build_snapshot(SnapshotRequest(manifest_path=batch.manifest, out_root=out_root))
        message = str(caught.exception)
        self.assertIn("拒绝复用", message)
        self.assertTrue("SHA-256" in message or "字节数" in message, message)


class CommentaryRejoinTest(P23TestCase):
    """问题：分段正文被删掉但哈希重算过，也要在读取期被抓住。"""

    def test_sections_text_all_mismatch_is_rejected(self):
        batch = build_batch(
            self.root / "src",
            index_rows=core_index_rows(),
            element_rows=core_element_rows(),
            commentary_items=(
                commentary_item(
                    tariff_no="9995",
                    kind="品目",
                    sections=(
                        ("品目注释", "合成样本：第一段。"),
                        ("子目注释", "合成样本：第二段。"),
                    ),
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        path = build.snapshot_dir / "commentary.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload["records"][0]
        # 模拟“转换器回归”：删掉一个分段，并把长度/哈希一起改成自洽的样子。
        import hashlib

        record["sections"] = record["sections"][:-1]
        record["section_char_total"] = sum(len(item["text"]) for item in record["sections"])
        record["source_char_count"] = record["section_char_total"]
        record["text_length"] = len(record["text_all"])
        record["text_sha256"] = hashlib.sha256(record["text_all"].encode("utf-8")).hexdigest()
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CommentaryStoreError) as caught:
            JsonCommentaryStore(path).load()
        self.assertIn("拼接", str(caught.exception))


class TextRateEndToEndTest(P23TestCase):
    """问题 6：文本退税率没有百分号时，端到端只能是 pending。"""

    def test_text_rate_without_percent_is_pending(self):
        batch = build_batch(
            self.root / "src",
            index_rows=[
                IndexRow("0999999991", "合成甲", element_count=1, required_count=1)
            ],
            element_rows=[ElementRow("0999999991", "合成甲", "要素甲", "1")],
            tax_rows=(
                TaxRow(
                    "0999999991",
                    "合成甲",
                    "13",
                    kind="inlineStr",
                    percent=None,
                ),
            ),
        )
        build = build_snapshot(
            SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
        )
        counts = next(
            item["counts"]
            for item in build.report["datasets"]
            if item["dataset"] == "p17_tax_rates"
        )
        self.assertEqual(counts["output_records"], 0, "看不清方向时不能生成税率记录")
        self.assertGreaterEqual(counts["rejected"] + counts["pending"], 1)
        pending = [
            item
            for item in build.report["records"]
            if item["dataset"] == "p17_tax_rates"
        ]
        self.assertEqual(
            {issue["code"] for item in pending for issue in item["issues"]},
            {"TAX_RATE_TEXT_AMBIGUOUS"},
        )


class Pr34ReviewFixesTest(P23TestCase):
    """PR #34 审查意见专项回归：有效期门禁、换版理由、并发发布、原子激活、落盘报告。"""

    def _real_batch(self, label: str, *, name: str = "合成甲", effective_from: str = "2026-11-16"):
        batch = build_batch(
            self.root / label,
            index_rows=[
                IndexRow("0999999991", name, reg_codes="A", element_count=1, required_count=1)
            ],
            element_rows=[ElementRow("0999999991", name, "要素甲", "1")],
            kind="real",
            batch_id=f"pr34-{label}",
            dataset_prefix=f"pr34-{label}",
            workbook_names=(f"批次_{label}_M01.xlsx", f"批次_{label}_M02.xlsx"),
        )
        if effective_from != "2026-11-16":
            def update_effective(payload: dict) -> None:
                payload["defaults"]["tariff_effective_from"] = effective_from
                payload["defaults"]["elements_effective_from"] = effective_from

            manifest = mutate_manifest(batch, update_effective, name="sources-effective.json")
            return batch, manifest
        return batch, batch.manifest

    def test_effective_from_change_requires_allow_content_change(self):
        """#34-1：将版本起点从 2026-11-16 改成 2025-01-01，必须触发换版门禁并报告内容变更。"""

        out_root = self.root / "out"
        _, m1 = self._real_batch("v1", effective_from="2026-11-16")
        _, m2 = self._real_batch("v2", effective_from="2025-01-01")
        # 让两个批次拥有完全相同的原件文件名，仅版本有效期起点不同。
        p1 = json.loads(m1.read_text(encoding="utf-8"))
        p2 = json.loads(m2.read_text(encoding="utf-8"))
        for entry1, entry2 in zip(p1["inputs"], p2["inputs"]):
            entry2["original_filename"] = entry1["original_filename"]
        m2.write_text(json.dumps(p2, ensure_ascii=False, indent=2), encoding="utf-8")

        first = build_snapshot(SnapshotRequest(manifest_path=m1, out_root=out_root))
        write_verification(out_root, first.snapshot_id)
        first_pub = publish_release(
            PublishRequest(snapshot_dir=first.snapshot_dir, out_root=out_root)
        )

        second = build_snapshot(SnapshotRequest(manifest_path=m2, out_root=out_root))
        write_verification(out_root, second.snapshot_id)

        with self.assertRaises(PublishError) as caught:
            publish_release(
                PublishRequest(snapshot_dir=second.snapshot_dir, out_root=out_root)
            )
        self.assertIn("--allow-content-change", str(caught.exception))
        self.assertIn("p14_tariff", str(caught.exception))
        self.assertIn("versions:2026", str(caught.exception))

        allowed = publish_release(
            PublishRequest(
                snapshot_dir=second.snapshot_dir,
                out_root=out_root,
                allow_content_change=True,
                reason="将税则与要素版本起点调整为 2025-01-01",
            )
        )
        self.assertTrue(allowed.changed)
        self.assertTrue(allowed.release["content_changed"])
        self.assertEqual(allowed.release["previous_release"], first_pub.release_id)
        self.assertIn(
            "versions:2026",
            allowed.diff["datasets"]["p14_tariff"]["samples"]["changed"],
        )

    def test_allow_content_change_rejects_empty_or_whitespace_reason(self):
        """#34-2：允许换版后，空理由或纯空白理由必须拒绝发布与激活。"""

        out_root = self.root / "out"
        _, m1 = self._real_batch("r1", name="合成甲")
        _, m2 = self._real_batch("r2", name="合成乙")
        first = build_snapshot(SnapshotRequest(manifest_path=m1, out_root=out_root))
        write_verification(out_root, first.snapshot_id)
        first_pub = publish_release(
            PublishRequest(snapshot_dir=first.snapshot_dir, out_root=out_root)
        )
        second = build_snapshot(SnapshotRequest(manifest_path=m2, out_root=out_root))
        write_verification(out_root, second.snapshot_id)

        for blank_reason in ("", "   ", "\t\n "):
            with self.subTest(reason=blank_reason):
                with self.assertRaises(PublishError) as caught:
                    publish_release(
                        PublishRequest(
                            snapshot_dir=second.snapshot_dir,
                            out_root=out_root,
                            allow_content_change=True,
                            reason=blank_reason,
                        )
                    )
                self.assertIn("--reason", str(caught.exception))

        current = json.loads((out_root / "published" / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(current["release_id"], first_pub.release_id)
        self.assertEqual(len(list_releases(out_root)), 1)

    def test_concurrent_interleaved_publish_enforces_change_gate_and_previous_release(self):
        """#34-3：两份不同内容交错发布时，后提交者不得绕过换版门禁或漏记 previous_release。"""

        out_root = self.root / "out"
        _, m1 = self._real_batch("c1", name="并发内容甲")
        _, m2 = self._real_batch("c2", name="并发内容乙")
        snap1 = build_snapshot(SnapshotRequest(manifest_path=m1, out_root=out_root))
        snap2 = build_snapshot(SnapshotRequest(manifest_path=m2, out_root=out_root))
        write_verification(out_root, snap1.snapshot_id)
        write_verification(out_root, snap2.snapshot_id)

        barrier = threading.Barrier(2)
        orig_check = publish_module._check_content_change

        def synced_check(previous, candidate_dir, **kwargs):
            result = orig_check(previous, candidate_dir, **kwargs)
            try:
                barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass
            return result

        outcomes: list[tuple[str, object]] = []
        lock = threading.Lock()

        def worker(label: str, snap_dir: Path, allow: bool = False, reason: str = "") -> None:
            try:
                res = publish_release(
                    PublishRequest(
                        snapshot_dir=snap_dir,
                        out_root=out_root,
                        allow_content_change=allow,
                        reason=reason,
                    )
                )
                with lock:
                    outcomes.append((label, res))
            except PublishError as exc:
                with lock:
                    outcomes.append((label, exc))

        with patch.object(publish_module, "_check_content_change", side_effect=synced_check):
            t1 = threading.Thread(target=worker, args=("A", snap1.snapshot_dir))
            t2 = threading.Thread(target=worker, args=("B", snap2.snapshot_dir))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        successes = [item for _, item in outcomes if not isinstance(item, Exception)]
        errors = [item for _, item in outcomes if isinstance(item, PublishError)]
        self.assertEqual(len(successes), 1, "未授权换版时交错发布只能有一方成功")
        self.assertEqual(len(errors), 1, "后提交者必须因内容不同且未授权换版而被拒绝")
        winner = successes[0]
        current = json.loads((out_root / "published" / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(current["release_id"], winner.release_id)
        self.assertEqual(len(list_releases(out_root)), 1)

        # 若后提交者显式授权换版并填写理由，则必须记录前一版本为 winner.release_id
        loser_snap = snap2 if winner.release["snapshot_id"] == snap1.snapshot_id else snap1
        second_pub = publish_release(
            PublishRequest(
                snapshot_dir=loser_snap.snapshot_dir,
                out_root=out_root,
                allow_content_change=True,
                reason="并发后显式授权换版",
            )
        )
        self.assertEqual(second_pub.release["previous_release"], winner.release_id)
        self.assertTrue(second_pub.release["content_changed"])

    def test_interrupted_activation_keeps_current_json_and_symlink_consistent(self):
        """#34-4：激活过程中断（软链替换失败或写指针失败）都必须转为 PublishError 并保持单一当前版本。"""

        out_root = self.root / "out"
        _, m1 = self._real_batch("a1", name="旧版本")
        _, m2 = self._real_batch("a2", name="新版本")
        snap1 = build_snapshot(SnapshotRequest(manifest_path=m1, out_root=out_root))
        snap2 = build_snapshot(SnapshotRequest(manifest_path=m2, out_root=out_root))
        write_verification(out_root, snap1.snapshot_id)
        write_verification(out_root, snap2.snapshot_id)

        first = publish_release(
            PublishRequest(snapshot_dir=snap1.snapshot_dir, out_root=out_root)
        )
        second = publish_release(
            PublishRequest(
                snapshot_dir=snap2.snapshot_dir,
                out_root=out_root,
                allow_content_change=True,
                reason="先生成不激活的新版本用于测试激活中断",
                activate=False,
            )
        )
        pointer = out_root / "published" / "current.json"
        link = out_root / "published" / "current"

        def assert_all_readers_point_to(expected_release_id: str) -> None:
            self.assertEqual(
                json.loads(pointer.read_text(encoding="utf-8"))["release_id"],
                expected_release_id,
            )
            self.assertEqual(Path(os.readlink(link)).name, expected_release_id)
            active_items = [item["release_id"] for item in list_releases(out_root) if item["active"]]
            self.assertEqual(active_items, [expected_release_id])
            paths = dataset_paths(out_root)
            self.assertTrue(all(expected_release_id in p for p in paths.values()))

        # 场景 1：创建软链失败 → 抛 PublishError，所有读取入口保持旧版
        with patch.object(publish_module.os, "symlink", side_effect=OSError("模拟软链中断")):
            with self.assertRaises(PublishError):
                activate_release(out_root, second.release_id)
        assert_all_readers_point_to(first.release_id)

        # 场景 2：os.replace 替换软链失败 → 抛 PublishError，所有读取入口保持旧版
        orig_replace = publish_module.os.replace

        def fail_on_link_replace(src, dst):
            if Path(dst).name == "current":
                raise OSError("模拟软链原子替换中断")
            return orig_replace(src, dst)

        with patch.object(publish_module.os, "replace", side_effect=fail_on_link_replace):
            with self.assertRaises(PublishError):
                activate_release(out_root, second.release_id)
        assert_all_readers_point_to(first.release_id)

        # 场景 3：写目标 release 的 .current.json 失败 → 抛 PublishError，所有读取入口保持旧版
        orig_write = publish_module.write_json_atomic

        def fail_on_pointer(path: Path, payload):
            if Path(path).name in ("current.json", ".current.json"):
                raise OSError("模拟写指针元数据中断")
            return orig_write(path, payload)

        with patch.object(publish_module, "write_json_atomic", side_effect=fail_on_pointer):
            with self.assertRaises(PublishError):
                activate_release(out_root, second.release_id)
        assert_all_readers_point_to(first.release_id)

    def test_failed_verification_updates_on_disk_report_json_and_markdown(self):
        """#34-5：首次构建验证失败时，落盘的 report.json 与 report.md 也必须是 partial / 退出码 1。"""

        out_root = self.root / "out"
        _, m1 = self._real_batch("vfail", name="验证失败样本")

        def failing_hook(_snapshot_dir: Path) -> dict:
            return {
                "schema_version": "p23.query-check.1",
                "all_passed": False,
                "checks": [
                    {"id": "p14.tariff.sample", "status": "fail", "detail": "模拟验证未通过"}
                ],
                "counts": {"pass": 0, "fail": 1, "skip": 0},
            }

        built = build_snapshot(
            SnapshotRequest(manifest_path=m1, out_root=out_root),
            verify_hook=failing_hook,
        )
        self.assertFalse(built.reused)
        self.assertEqual(int(built.exit_code), 1)
        self.assertEqual(built.report["status"], "partial")

        saved_report = json.loads(built.report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_report["status"], "partial")
        self.assertEqual(saved_report["exit_code"], 1)
        self.assertFalse(saved_report["verification"]["all_passed"])

        saved_md = built.report_markdown_path.read_text(encoding="utf-8")
        self.assertIn("结果：**partial**（退出码 1）", saved_md)
        self.assertIn("结论：**未通过**", saved_md)

