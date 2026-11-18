"""P23 来源清单口径：结构版本/数据类别/批次性质/原件哈希必须逐项核对，不一致就拒绝。

守的规则：清单说的和文件实际的对不上时，流水线必须**明说哪里不对**并停下，
不能挑一个“最像”的继续——否则后面所有计数都建立在错误的输入上。
"""

from __future__ import annotations

from pathlib import Path

from adapters.sources.reference_import.model import DatasetKind, SourceFile
from adapters.sources.reference_import.snapshot import (
    SnapshotFailed,
    SnapshotRequest,
    build_snapshot,
)
from adapters.sources.reference_import.sources import SourceError, load_source_manifest
from tests.p23.builders import build_core_batch, mutate_manifest
from tests.p23.support import P23TestCase


class ManifestStructureTest(P23TestCase):
    """结构版本 / 未知枚举 / 重复 id：一律 SourceError，且信息里点出问题。"""

    def setUp(self):
        super().setUp()
        self.batch = build_core_batch(self.root / "src")

    def load_variant(self, mutate, *, name: str):
        path = mutate_manifest(self.batch, mutate, name=name)
        return load_source_manifest(path)

    def test_missing_schema_version_is_rejected(self):
        with self.assertRaises(SourceError) as caught:
            self.load_variant(lambda payload: payload.pop("schema_version"), name="no-schema.json")
        message = str(caught.exception)
        self.assertIn("结构版本不受支持", message, "必须说明是结构版本问题")
        self.assertIn("p23.sources.1", message, "必须报出期望的结构版本")

    def test_unsupported_schema_version_is_rejected(self):
        with self.assertRaises(SourceError) as caught:
            self.load_variant(
                lambda payload: payload.__setitem__("schema_version", "p23.sources.0"),
                name="old-schema.json",
            )
        message = str(caught.exception)
        self.assertIn("p23.sources.0", message, "必须回显实际读到的版本")
        self.assertIn("p23.sources.1", message, "必须回显支持的版本")

    def test_unknown_dataset_is_rejected(self):
        def mutate(payload):
            payload["datasets"][0]["dataset"] = "p13_unknown"

        with self.assertRaises(SourceError) as caught:
            self.load_variant(mutate, name="bad-dataset.json")
        message = str(caught.exception)
        self.assertIn("dataset 未知", message)
        self.assertIn("p13_unknown", message)
        self.assertIn("第 1 个", message, "必须指出是第几个 datasets 条目")

    def test_unknown_dataset_status_is_rejected(self):
        def mutate(payload):
            payload["datasets"][0]["status"] = "maybe"

        with self.assertRaises(SourceError) as caught:
            self.load_variant(mutate, name="bad-status.json")
        message = str(caught.exception)
        self.assertIn("status 未知", message)
        self.assertIn("maybe", message)

    def test_unknown_input_kind_is_rejected(self):
        def mutate(payload):
            payload["inputs"][0]["kind"] = "unknown_workbook"

        with self.assertRaises(SourceError) as caught:
            self.load_variant(mutate, name="bad-kind.json")
        message = str(caught.exception)
        self.assertIn("kind 未知", message)
        self.assertIn("M01", message, "必须点出是哪一份来源")

    def test_missing_batch_id_is_rejected(self):
        with self.assertRaises(SourceError) as caught:
            self.load_variant(lambda payload: payload.pop("batch_id"), name="no-batch.json")
        self.assertIn("batch_id", str(caught.exception))

    def test_duplicate_source_id_is_rejected(self):
        def mutate(payload):
            payload["inputs"].append(dict(payload["inputs"][0]))

        with self.assertRaises(SourceError) as caught:
            self.load_variant(mutate, name="dup-source.json")
        self.assertIn("重复 source_id", str(caught.exception))
        self.assertIn("M01", str(caught.exception))

    def test_bad_declared_sha256_is_rejected(self):
        def mutate(payload):
            payload["inputs"][0]["sha256"] = "not-a-sha256"

        with self.assertRaises(SourceError) as caught:
            self.load_variant(mutate, name="bad-sha.json")
        self.assertIn("64 位小写十六进制", str(caught.exception))


class ManifestKindTest(P23TestCase):
    """命令行批次性质必须与清单一致：不允许把真实批次当合成批次，或反过来。"""

    def test_dataset_kind_mismatch_is_rejected(self):
        batch = build_core_batch(self.root / "src")
        with self.assertRaises(SourceError) as caught:
            load_source_manifest(batch.manifest, kind=DatasetKind.REAL)
        message = str(caught.exception)
        self.assertIn("real", message)
        self.assertIn("synthetic", message)
        self.assertIn("不一致", message)

    def test_declared_kind_is_parsed(self):
        batch = build_core_batch(self.root / "src")
        manifest = load_source_manifest(batch.manifest, kind=DatasetKind.SYNTHETIC)
        self.assertIs(manifest.kind, DatasetKind.SYNTHETIC)
        self.assertEqual(manifest.batch_id, "customs-reference-synthetic-2026-11-18")
        self.assertEqual(manifest.prepared_at, "2026-11-18T01:00:00+08:00")
        self.assertEqual(len(manifest.sources), 3, "M01/M02/C01 三份合成原件都应被解析")
        self.assertEqual(manifest.unresolved, ())
        self.assertEqual([item.dataset.value for item in manifest.declared], [
            "p14_tariff",
            "p14_elements",
            "p17_tax_rates",
            "regulatory_code_ledger",
            "p17_regulatory",
            "tariff_commentary",
        ])


class SourceVerificationTest(P23TestCase):
    """原件被改一个字节就必须 verified=False；没声明哈希时只记录实测值。"""

    def test_tampered_file_is_not_verified(self):
        batch = build_core_batch(self.root / "src")
        original = load_source_manifest(batch.manifest)
        m01 = original.source("M01")
        self.assertIsInstance(m01, SourceFile)
        self.assertTrue(m01.verified, "未篡改时应通过核对")
        original_sha = m01.sha256
        original_size = m01.size

        target = Path(m01.path)
        data = bytearray(target.read_bytes())
        index = len(data) // 2
        data[index] = data[index] ^ 0xFF
        target.write_bytes(bytes(data))

        tampered = load_source_manifest(batch.manifest)
        m01_after = tampered.source("M01")
        self.assertFalse(m01_after.verified, "改一个字节后 verified 必须为 False")
        self.assertFalse(m01_after.hash_verified, "哈希核对必须失败")
        self.assertTrue(m01_after.size_verified, "字节数没变，size_verified 仍应为 True")
        self.assertEqual(m01_after.declared_sha256, original_sha)
        self.assertNotEqual(m01_after.sha256, original_sha)
        self.assertEqual(m01_after.size, original_size)
        # 其它原件不受影响
        self.assertTrue(tampered.source("M02").verified)

    def test_bytes_only_tamper_is_not_verified(self):
        batch = build_core_batch(self.root / "src")
        m01 = load_source_manifest(batch.manifest).source("M01")
        Path(m01.path).write_bytes(Path(m01.path).read_bytes() + b"\x00")
        after = load_source_manifest(batch.manifest).source("M01")
        self.assertFalse(after.verified, "追加字节必须被字节数核对发现")
        self.assertFalse(after.size_verified)

    def test_undeclared_hash_is_reported_as_verified(self):
        batch = build_core_batch(self.root / "src")

        def drop_declaration(payload):
            payload["inputs"][0].pop("sha256")
            payload["inputs"][0].pop("bytes")

        path = mutate_manifest(batch, drop_declaration, name="no-hash.json")
        source = load_source_manifest(path).source("M01")
        self.assertIsNone(source.declared_sha256)
        self.assertIsNone(source.declared_bytes)
        self.assertTrue(source.hash_verified, "没有声明哈希时不能判为失败")
        self.assertTrue(source.verified)

    def test_snapshot_refuses_tampered_input(self):
        batch = build_core_batch(self.root / "src")
        m01 = load_source_manifest(batch.manifest).source("M01")
        data = bytearray(Path(m01.path).read_bytes())
        data[0] = data[0] ^ 0xFF
        Path(m01.path).write_bytes(bytes(data))
        with self.assertRaises(SnapshotFailed) as caught:
            build_snapshot(
                SnapshotRequest(manifest_path=batch.manifest, out_root=batch.out_root)
            )
        message = str(caught.exception)
        self.assertIn("SHA-256", message)
        self.assertIn("M01", message, "必须点出是哪一份原件的哈希对不上")
        self.assertFalse(
            (batch.out_root / "candidates").exists(),
            "哈希核对失败时不应创建任何快照目录",
        )
