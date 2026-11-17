"""P23 品目注释口径：这是独立解释资料快照，层级键不是申报编码，正文不改写。

守的规则：

- 4 位品目、``CH`` 章注、``CA`` 类注、``ot`` 其他注释都能归一化命中，证据带定位；
- 传 8/10 位编码一律 ``invalid_input``，并说明“注释不是编码层级键”；
- ``char_count`` 口径 = 各分段正文长度之和，不是 ``text_all`` 的长度；
- 正文被改写一个字符 → 读取期直接判存储不可读，不降级成“查无”；
- 同一层级键两份不同正文 → ``conflict``，保留先出现的一版。
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.sources.reference_import.convert_commentary import (
    build_commentary,
    render_text_all,
)
from adapters.sources.reference_import.convert_catalog import Versioning
from adapters.sources.reference_import.model import (
    InputKind,
    SourceFile,
    sha256_file,
)
from adapters.sources.reference_import.snapshot import SnapshotRequest, build_snapshot
from adapters.sources.reference_import.sources import load_source_manifest
from adapters.sources.tariff_commentary import (
    CommentaryLevel,
    CommentaryStoreError,
    JsonCommentaryStore,
    TariffCommentarySource,
)
from tests.p23.builders import (
    build_core_batch,
    commentary_item,
    write_commentary,
    write_manifest,
)
from tests.p23.support import P23TestCase


class CommentaryLookupTest(P23TestCase):
    """先转快照，再用真实适配器查询。"""

    def setUp(self):
        super().setUp()
        self.batch = build_core_batch(self.root / "src")
        self.build = build_snapshot(
            SnapshotRequest(manifest_path=self.batch.manifest, out_root=self.batch.out_root)
        )
        self.snapshot_path = self.build.snapshot_dir / "commentary.json"
        self.snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        self.source = TariffCommentarySource(JsonCommentaryStore(self.snapshot_path))

    def test_heading_hit_carries_evidence_and_engine_flag(self):
        result = self.source.lookup("9991")
        self.assertEqual(result.query_status.value, "ok")
        self.assertEqual(result.evidence_status.value, "obtained")
        self.assertIs(result.level, CommentaryLevel.HEADING)
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record.tariff_no, "9991")
        self.assertEqual([section.label for section in record.sections], ["品目注释", "子目注释"])
        self.assertEqual(len(result.evidence), 1)
        evidence = result.evidence[0]
        self.assertTrue(evidence.document, "证据必须带 document")
        self.assertTrue(evidence.locator, "证据必须带 locator")
        self.assertIn("9991", evidence.locator)
        self.assertEqual(result.engine_integration, "not_wired_p16")
        self.assertIn("未接入 P16", result.reason)
        self.assertEqual(result.version, "2026-11-18T10-00")
        self.assertIsNotNone(result.dataset)
        self.assertTrue(result.evidence[0].scope.startswith("品目注释层级键 9991"))

    def test_prefix_keys_are_normalized(self):
        for raw, expected_key, expected_level in (
            ("ch1", "CH1", CommentaryLevel.CHAPTER),
            ("CA1", "CA1", CommentaryLevel.SECTION),
            ("OT3", "ot3", CommentaryLevel.OTHER),
            (" ot3 ", "ot3", CommentaryLevel.OTHER),
        ):
            with self.subTest(raw=raw):
                result = self.source.lookup(raw)
                self.assertEqual(result.query_status.value, "ok")
                self.assertEqual(result.evidence_status.value, "obtained")
                self.assertEqual([item.tariff_no for item in result.records], [expected_key])
                self.assertIs(result.level, expected_level)
                self.assertTrue(result.evidence[0].locator)

    def test_declaration_codes_are_invalid_input(self):
        for raw, digits in (("99910100", 8), ("9991010010", 10)):
            with self.subTest(raw=raw):
                result = self.source.lookup(raw)
                self.assertEqual(result.query_status.value, "invalid_input")
                self.assertIsNone(result.evidence_status, "invalid_input 不得携带证据状态")
                self.assertEqual(result.records, [])
                self.assertEqual(result.evidence, [])
                self.assertEqual([issue.code for issue in result.issues], ["COMMENTARY_QUERY_INVALID"])
                message = result.issues[0].message
                self.assertIn("8/10 位", message, "必须说明 8/10 位编码不是注释层级键")
                self.assertIn("不属于注释层级", message)
                self.assertIn(f"{digits} 位", message)

    def test_char_count_is_section_text_sum(self):
        snapshot = JsonCommentaryStore(self.snapshot_path).load()
        record = next(item for item in snapshot.records if item.tariff_no == "9991")
        section_total = sum(len(section.text) for section in record.sections)
        self.assertEqual(record.section_char_total, section_total)
        self.assertEqual(record.source_char_count, section_total)
        self.assertEqual(record.text_length, len(record.text_all))
        self.assertLess(record.section_char_total, record.text_length,
                        "text_all 含【标签】与空行，长度必然大于分段正文合计")
        self.assertEqual(
            record.text_all,
            render_text_all(
                [{"label": section.label, "text": section.text} for section in record.sections]
            ),
            "text_all 必须等于按来源格式重新拼接的结果",
        )

    def test_tampered_text_all_is_unreadable(self):
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        original = payload["records"][0]["text_all"]
        payload["records"][0]["text_all"] = ("修" if original[0] != "修" else "改") + original[1:]
        tainted = self.root / "commentary_tainted.json"
        tainted.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(CommentaryStoreError) as caught:
            JsonCommentaryStore(tainted).load()
        self.assertIn("哈希不匹配", str(caught.exception))

        result = TariffCommentarySource(JsonCommentaryStore(tainted)).lookup("9991")
        self.assertEqual(result.query_status.value, "source_unreadable",
                         "正文被改过必须是“存储不可读”，不能伪装成查无")
        self.assertEqual(result.evidence_status.value, "query_failed")
        self.assertEqual(result.records, [])
        self.assertEqual(result.evidence, [])
        self.assertIn("哈希不匹配", result.reason)

    def test_unknown_key_is_not_fabricated(self):
        result = self.source.lookup("9999")
        self.assertEqual(result.query_status.value, "ok")
        self.assertEqual(result.evidence_status.value, "not_found")
        self.assertEqual(result.records, [])
        self.assertEqual(result.evidence, [])
        self.assertEqual([issue.code for issue in result.issues], ["COMMENTARY_NOT_FOUND"])
        self.assertIn("不等于", result.reason)


class CommentaryConversionTest(P23TestCase):
    """转换层：同一层级键两份不同正文 → conflict，保留先出现的一版。"""

    def make_source(self, path: Path) -> SourceFile:
        return SourceFile(
            source_id="C02",
            kind=InputKind.COMMENTARY_JSON,
            path=path,
            sha256=sha256_file(path),
            size=path.stat().st_size,
            retrieved_at="2026-11-18T00:36:05+08:00",
            original_filename=path.name,
        )

    def test_conflicting_text_keeps_first(self):
        items = [
            commentary_item(
                tariff_no="9992",
                kind="品目",
                name="合成冲突样本",
                sections=(("品目注释", "合成样本：第一版正文。"),),
            ),
            commentary_item(
                tariff_no="9992",
                kind="品目",
                name="合成冲突样本",
                sections=(("品目注释", "合成样本：第二版正文。"),),
            ),
        ]
        path = self.root / "c.json"
        write_commentary(path, items)
        manifest = write_manifest(
            self.root,
            batch_id="synthetic-commentary-conflict",
            kind="synthetic",
            inputs=[
                {
                    "source_id": "C01",
                    "kind": "commentary_json",
                    "repository_path": "c.json",
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            ],
            datasets=[{"dataset": "tariff_commentary", "status": "provided", "inputs": ["C01"]}],
            defaults={"commentary_version": "2026-11-18T10-00"},
        )
        core = build_core_batch(self.root / "core")
        parsed = load_source_manifest(core.manifest)
        build = build_commentary(
            parsed,
            versioning=Versioning(),
            sources=[self.make_source(path)],
        )
        outcome = build.outcome
        self.assertEqual(outcome.input_total, 2)
        self.assertEqual(outcome.imported, 1)
        self.assertEqual(outcome.conflict, 1, "同层级键不同正文必须记 conflict")
        self.assertEqual(
            outcome.duplicate, 0, "冲突条目不等于重复条目，不能同时记 duplicate"
        )
        self.assertEqual(len(build.payload["records"]), 1, "冲突不得写出第二条记录")
        record = build.payload["records"][0]
        self.assertIn("第一版正文", record["text_all"], "必须保留先出现的一版")
        self.assertNotIn("第二版正文", record["text_all"])
        conflicts = [
            item for item in build.outcomes if item.status.value == "conflict"
        ]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].business_key, "9992")
        self.assertEqual([issue.code for issue in conflicts[0].issues], ["COMMENTARY_RECORD_CONFLICT"])
        self.assertTrue(manifest.is_file(), "清单只用于构造来源，不参与转换")
