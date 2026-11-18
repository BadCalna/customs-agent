"""P15 原子发布：generation 快照、稳定 symlink、SIGKILL 中断与旧输出迁移。

这些测试直接调用 ``adapters.sources.history_publish``（lead 之后只需要把
``history_import._publish_staging`` 薄包装到它上面），并用真实子进程 + 真实
``SIGKILL`` 验证"进程在任意发布阶段被杀，磁盘上要么是完整旧快照、要么是完整
新快照"。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from adapters.sources.history_publish import (
    CURRENT_LINK_NAME,
    KNOWN_SNAPSHOT_NAMES,
    PublishError,
    publish_staging,
)

BASE_DIR = Path(settings.BASE_DIR)
POSIX_SIGKILL = os.name == "posix" and hasattr(signal, "SIGKILL")


def write_file(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


class PublishTestBase(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staging = self.root / "staging"
        self.out = self.root / "out"

    # -- 构造/读取 --------------------------------------------------------
    def stage(self, files: dict[str, bytes | str], *, staging: Path | None = None) -> list[Path]:
        target = staging or self.staging
        target.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for name, data in files.items():
            path = target / name
            write_file(path, data)
            paths.append(path)
        return sorted(paths)

    def publish(self, files: list[Path], out: Path | None = None) -> None:
        publish_staging(self.staging, out or self.out, files)

    def read(self, name: str) -> bytes:
        return (self.out / name).read_bytes()

    def read_text(self, name: str) -> str:
        return (self.out / name).read_text(encoding="utf-8")

    def current_generation(self, *, out: Path | None = None) -> Path:
        out = out or self.out
        link = out / CURRENT_LINK_NAME
        self.assertTrue(link.is_symlink(), f"{link} 不是符号链接")
        target = os.readlink(link)
        self.assertFalse(os.path.isabs(target), f".current 目标必须是相对路径：{target}")
        self.assertNotIn("..", Path(target).parts)
        generation = out / target
        self.assertTrue(generation.is_dir(), f"{generation} 不存在")
        return generation

    def assert_stable_link(self, name: str, *, out: Path | None = None) -> None:
        out = out or self.out
        path = out / name
        self.assertTrue(path.is_symlink(), f"{path} 不是稳定 symlink")
        self.assertEqual(
            os.readlink(path),
            os.path.join(CURRENT_LINK_NAME, *Path(name).parts),
        )
        self.assertTrue(path.is_file(), f"稳定 symlink 未指向可读文件：{path}")

    def assert_regular_file(self, rel: str, *, out: Path | None = None) -> None:
        out = out or self.out
        path = out / rel
        self.assertFalse(path.is_symlink(), f"{path} 不应是符号链接")
        self.assertTrue(path.is_file(), f"{path} 不是普通文件")

    def generation_dirs(self, *, out: Path | None = None) -> list[Path]:
        out = out or self.out
        snapshots = out / ".snapshots"
        return sorted(
            path
            for path in snapshots.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )

    # -- 真实子进程故障注入 ----------------------------------------------
    def child_command(
        self,
        staging: Path,
        out: Path,
        *,
        kill_stage: str | None = None,
        sleep_stage: str | None = None,
        sleep_seconds: float = 0.0,
    ) -> list[str]:
        lines = [
            "import os, signal, sys, time",
            f"sys.path.insert(0, {str(BASE_DIR)!r})",
            "from pathlib import Path",
            "from adapters.sources import history_publish",
            f"staging = Path({str(staging)!r})",
            f"out = Path({str(out)!r})",
            "files = sorted(p for p in staging.rglob('*') if p.is_file())",
        ]
        if kill_stage is not None or sleep_stage is not None:
            lines.append("def hook(stage):")
            if kill_stage is not None:
                lines += [
                    f"    if stage == {kill_stage!r}:",
                    "        os.kill(os.getpid(), signal.SIGKILL)",
                ]
            if sleep_stage is not None:
                lines += [
                    f"    if stage == {sleep_stage!r}:",
                    f"        time.sleep({float(sleep_seconds)!r})",
                ]
            lines.append("history_publish._FAULT_INJECTION_HOOK = hook")
        lines.append("history_publish.publish_staging(staging, out, files)")
        return [sys.executable, "-c", "\n".join(lines) + "\n"]

    def run_child(self, *args, staging: Path | None = None, out: Path | None = None, **kwargs):
        command = self.child_command(staging or self.staging, out or self.out, *args, **kwargs)
        return subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def assert_killed_by_sigkill(self, proc: subprocess.CompletedProcess, stage: str) -> None:
        self.assertTrue(POSIX_SIGKILL)
        if proc.returncode != -signal.SIGKILL:
            self.fail(
                f"子进程未在阶段 {stage!r} 被 SIGKILL 杀死："
                f"returncode={proc.returncode}, stderr={proc.stderr!r}"
            )

    # -- 常用快照 --------------------------------------------------------
    def snapshot_files(self, token: str, *, image: bool = True) -> list[Path]:
        files: dict[str, bytes | str] = {
            "history_records.json": json.dumps({"token": token, "records": []}),
            "import_report.json": json.dumps({"token": token, "report": True}),
            "import_report.md": f"# report {token}\n",
        }
        if image:
            # 图片按内容地址发布，不同批次复用同一内容时同名同内容，必须幂等。
            files["images/ID.12345678.png"] = b"PNG-STATIC-CONTENT"
        return self.stage(files)


class GenerationLayoutTest(PublishTestBase):
    def test_publish_creates_hidden_generation_and_stable_links(self):
        staging_images_dir = self.staging / "images"
        staging_images_dir.mkdir(parents=True)
        files = self.snapshot_files("v1")
        # 旧实现会忽略目录条目；这里显式传入确保兼容。
        self.publish(files + [staging_images_dir])

        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assert_stable_link(name)
            self.assertIsNotNone((self.out / name).read_bytes())
        self.assert_regular_file("images/ID.12345678.png")
        self.assertEqual(self.read("images/ID.12345678.png"), b"PNG-STATIC-CONTENT")

        generation = self.current_generation()
        self.assertEqual(generation.parent.resolve(), (self.out / ".snapshots").resolve())
        self.assertEqual(
            sorted(path.name for path in generation.iterdir()),
            sorted(["history_records.json", "import_report.md", "import_report.json"]),
        )
        # 快照文件与顶层链接内容一致
        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assertEqual((generation / name).read_bytes(), self.read(name))
        # 顶层快照名都是 symlink，不是普通文件
        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assertTrue((self.out / name).is_symlink())

    def test_republish_switches_current_and_keeps_old_generation_readable(self):
        files_v1 = self.snapshot_files("v1")
        self.publish(files_v1)
        generation_v1 = self.current_generation()
        files_v2 = self.snapshot_files("v2")
        self.publish(files_v2)
        generation_v2 = self.current_generation()

        self.assertNotEqual(generation_v1, generation_v2)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v2")
        self.assertIn("v2", self.read_text("import_report.md"))
        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assert_stable_link(name)

        # 旧 generation 不被清理，仍可整体读取（旧读者/诊断可用）。
        self.assertTrue(generation_v1.is_dir())
        self.assertEqual(
            json.loads((generation_v1 / "history_records.json").read_text(encoding="utf-8"))["token"],
            "v1",
        )
        self.assertIn("v1", (generation_v1 / "import_report.md").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(self.generation_dirs()), 2)

    def test_symlink_targets_never_escape_output_dir(self):
        self.publish(self.snapshot_files("v1"))
        links = [self.out / name for name in ("history_records.json", "import_report.json")]
        links.append(self.out / CURRENT_LINK_NAME)
        real_out = Path(os.path.realpath(self.out))
        for link in links:
            self.assertTrue(link.is_symlink())
            target = os.readlink(link)
            self.assertFalse(os.path.isabs(target))
            self.assertNotIn("..", Path(target).parts)
            resolved = Path(os.path.realpath(link))
            self.assertTrue(
                resolved == real_out or real_out in resolved.parents,
                f"{link} -> {target} 解析到输出目录之外：{resolved}",
            )

    def test_republish_does_not_recreate_image_file(self):
        files = self.snapshot_files("v1")
        self.publish(files)
        image = self.out / "images/ID.12345678.png"
        image_inode = os.stat(image).st_ino
        self.publish(files)
        # 同名同内容图片幂等复用，不重写/不覆盖。
        self.assertEqual(os.stat(image).st_ino, image_inode)


class ImagePublishTest(PublishTestBase):
    def test_image_keeps_p15_path_and_is_published_before_snapshot(self):
        self.publish(self.snapshot_files("v1"))
        self.assert_regular_file("images/ID.12345678.png")
        generation = self.current_generation()
        self.assertFalse((generation / "images").exists())

    def test_same_image_name_with_different_content_is_rejected(self):
        self.publish(self.snapshot_files("v1"))
        generation_before = self.current_generation()

        staging = self.root / "staging-conflict"
        files = self.stage(
            {
                "history_records.json": json.dumps({"token": "v2"}),
                "import_report.json": json.dumps({"token": "v2"}),
                "images/ID.12345678.png": b"DIFFERENT-PNG-BYTES",
            },
            staging=staging,
        )
        with self.assertRaisesRegex(PublishError, "同名但内容不同"):
            publish_staging(staging, self.out, files)

        # 旧快照、旧图片、current 指针都没变。
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v1")
        self.assertEqual(self.read("images/ID.12345678.png"), b"PNG-STATIC-CONTENT")
        self.assertEqual(self.current_generation(), generation_before)

    def test_images_directory_symlink_is_refused(self):
        outside = self.root / "outside-images"
        outside.mkdir()
        self.out.mkdir(parents=True)
        os.symlink(outside, self.out / "images")
        staging = self.root / "staging-images"
        files = self.stage(
            {"history_records.json": "{}", "images/ID.12345678.png": b"PNG"},
            staging=staging,
        )
        with self.assertRaisesRegex(PublishError, "符号链接"):
            publish_staging(staging, self.out, files)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.out / "history_records.json").exists())


class LegacyMigrationTest(PublishTestBase):
    def _make_flat_output(self) -> None:
        self.out.mkdir(parents=True)
        write_file(self.out / "history_records.json", '{"token": "old-records"}')
        write_file(self.out / "import_report.json", '{"token": "old-report"}')
        write_file(self.out / "import_report.md", "old-md")
        write_file(self.out / "query_validation.json", '{"token": "old-validation"}')

    def test_flat_output_is_migrated_and_content_is_preserved(self):
        self._make_flat_output()
        files = self.snapshot_files("new")
        self.publish(files)

        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "new")
        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assert_stable_link(name)
        # 旧平面输出里未被本次发布覆盖的文件（验证报告）与原实现一样继续可见。
        self.assertEqual(
            json.loads(self.read_text("query_validation.json"))["token"], "old-validation"
        )

        generations = self.generation_dirs()
        legacy_generations = [path for path in generations if "legacy" in path.name]
        self.assertEqual(len(legacy_generations), 1)
        legacy = legacy_generations[0]
        self.assertEqual(
            json.loads((legacy / "history_records.json").read_text(encoding="utf-8"))["token"],
            "old-records",
        )
        self.assertEqual(
            json.loads((legacy / "query_validation.json").read_text(encoding="utf-8"))["token"],
            "old-validation",
        )
        # 旧 generation 保留，不清理。
        self.assertTrue(legacy.is_dir())

    def test_migration_interrupted_at_each_stage_keeps_old_content(self):
        if not POSIX_SIGKILL:
            self.skipTest("需要 POSIX SIGKILL")
        stages = ["legacy-ready", "legacy-current-set", "legacy-replace-0"]
        for stage in stages:
            with self.subTest(stage=stage):
                shutil.rmtree(self.out, ignore_errors=True)
                shutil.rmtree(self.staging, ignore_errors=True)
                self._make_flat_output()
                files = self.snapshot_files("new")

                proc = self.run_child(kill_stage=stage)
                self.assert_killed_by_sigkill(proc, stage)

                # 任何中断点，旧记录都必须是旧内容（普通文件或等内容的 symlink）。
                self.assertEqual(
                    json.loads(self.read_text("history_records.json"))["token"],
                    "old-records",
                )
                self.assertEqual(
                    json.loads(self.read_text("import_report.json"))["token"],
                    "old-report",
                )
                self.assertEqual(self.read_text("import_report.md"), "old-md")
                self.assertEqual(
                    json.loads(self.read_text("query_validation.json"))["token"],
                    "old-validation",
                )

                if stage == "legacy-current-set":
                    # 已设 current、尚未替换任何顶层文件：仍是纯普通文件。
                    self.assertFalse((self.out / "history_records.json").is_symlink())
                if stage == "legacy-replace-0":
                    # 迁移到一半：新旧布局混合，但内容全部是旧内容。
                    self.assertTrue((self.out / "history_records.json").is_symlink())
                    self.assertFalse((self.out / "import_report.json").is_symlink())

                # 中断后继续发布可以正常完成，且旧 generation 保留。
                publish_staging(self.staging, self.out, files)
                self.assertEqual(
                    json.loads(self.read_text("history_records.json"))["token"], "new"
                )
                self.assertTrue(any("legacy" in p.name for p in self.generation_dirs()))

    def test_foreign_top_level_symlink_is_refused(self):
        outside = self.root / "outside-target.json"
        write_file(outside, "outside")
        self.out.mkdir(parents=True)
        os.symlink(outside, self.out / "history_records.json")
        files = self.snapshot_files("new")
        with self.assertRaisesRegex(PublishError, "符号链接"):
            self.publish(files)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        self.assertEqual(os.readlink(self.out / "history_records.json"), str(outside))


class CrashSafetyTest(PublishTestBase):
    def test_sigkill_before_commit_keeps_old_snapshot_complete(self):
        if not POSIX_SIGKILL:
            self.skipTest("需要 POSIX SIGKILL")
        self.publish(self.snapshot_files("v1"))
        generation_v1 = self.current_generation()
        files_v2 = self.snapshot_files("v2")

        for stage in ("images-after", "generation-ready", "before-commit"):
            with self.subTest(stage=stage):
                proc = self.run_child(kill_stage=stage)
                self.assert_killed_by_sigkill(proc, stage)
                # 旧快照完整、current 未变、顶层链接未变。
                self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v1")
                self.assertEqual(json.loads(self.read_text("import_report.json"))["token"], "v1")
                self.assertIn("v1", self.read_text("import_report.md"))
                self.assertEqual(self.current_generation(), generation_v1)
                for name in ("history_records.json", "import_report.json", "import_report.md"):
                    self.assert_stable_link(name)

        # 中断后正常重试：整体切到 v2。
        publish_staging(self.staging, self.out, files_v2)
        for name in ("history_records.json", "import_report.json"):
            self.assertEqual(json.loads(self.read_text(name))["token"], "v2")
        self.assertIn("v2", self.read_text("import_report.md"))

    def test_sigkill_after_commit_exposes_complete_new_snapshot(self):
        if not POSIX_SIGKILL:
            self.skipTest("需要 POSIX SIGKILL")
        self.publish(self.snapshot_files("v1"))
        generation_v1 = self.current_generation()
        files_v2 = self.snapshot_files("v2")

        proc = self.run_child(kill_stage="after-commit")
        self.assert_killed_by_sigkill(proc, "after-commit")

        for name in ("history_records.json", "import_report.json"):
            self.assertEqual(json.loads(self.read_text(name))["token"], "v2")
        self.assertIn("v2", self.read_text("import_report.md"))
        generation_v2 = self.current_generation()
        self.assertNotEqual(generation_v2, generation_v1)
        # 旧 generation 仍保留且完整（SIGKILL 后读者仍可查旧批次）。
        self.assertEqual(
            json.loads((generation_v1 / "history_records.json").read_text(encoding="utf-8"))["token"],
            "v1",
        )
        self.assertTrue((generation_v1 / "import_report.md").is_file())

    def test_first_publish_killed_before_commit_then_retry_succeeds(self):
        if not POSIX_SIGKILL:
            self.skipTest("需要 POSIX SIGKILL")
        files = self.snapshot_files("v1")
        proc = self.run_child(kill_stage="before-commit")
        self.assert_killed_by_sigkill(proc, "before-commit")
        # 首次发布被中断：没有 current，也没有可读的历史输出。
        self.assertFalse(os.path.lexists(self.out / CURRENT_LINK_NAME))
        self.assertFalse((self.out / "history_records.json").exists())

        publish_staging(self.staging, self.out, files)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v1")
        self.assert_stable_link("history_records.json")

    def test_sigkill_while_creating_top_level_links_then_retry_succeeds(self):
        if not POSIX_SIGKILL:
            self.skipTest("需要 POSIX SIGKILL")
        files = self.snapshot_files("v1")
        proc = self.run_child(kill_stage="link-created-0")
        self.assert_killed_by_sigkill(proc, "link-created-0")
        # 已建好的稳定链接在 current 提交前是悬空的；旧输出本来就不存在。
        self.assertFalse(os.path.lexists(self.out / CURRENT_LINK_NAME))
        self.assertFalse((self.out / "history_records.json").exists())

        publish_staging(self.staging, self.out, files)
        for name in ("history_records.json", "import_report.json", "import_report.md"):
            self.assert_stable_link(name)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v1")

    def test_ordinary_exception_before_commit_rolls_back_and_keeps_old_output(self):
        from adapters.sources import history_publish

        self.publish(self.snapshot_files("v1"))
        generation_v1 = self.current_generation()
        generation_count = len(self.generation_dirs())
        files_v2 = self.snapshot_files("v2")

        def boom(stage: str) -> None:
            if stage == "before-commit":
                raise RuntimeError("simulated publish failure")

        previous = history_publish._FAULT_INJECTION_HOOK
        history_publish._FAULT_INJECTION_HOOK = boom
        self.addCleanup(setattr, history_publish, "_FAULT_INJECTION_HOOK", previous)
        with self.assertRaisesRegex(PublishError, "simulated publish failure"):
            publish_staging(self.staging, self.out, files_v2)

        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v1")
        self.assertEqual(self.current_generation(), generation_v1)
        # 失败回滚删除未提交的 generation，不留下半成品。
        self.assertEqual(len(self.generation_dirs()), generation_count)

        history_publish._FAULT_INJECTION_HOOK = previous
        publish_staging(self.staging, self.out, files_v2)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "v2")


class FailureModeTest(PublishTestBase):
    def test_file_outside_staging_is_rejected(self):
        outside = self.root / "outside.json"
        write_file(outside, "outside")
        self.staging.mkdir()
        with self.assertRaisesRegex(PublishError, "不在暂存目录"):
            publish_staging(self.staging, self.out, [outside])
        self.assertFalse((self.out / "history_records.json").exists())

    def test_staged_symlink_is_rejected(self):
        outside = self.root / "outside.json"
        write_file(outside, "outside")
        self.staging.mkdir()
        os.symlink(outside, self.staging / "history_records.json")
        with self.assertRaisesRegex(PublishError, "符号链接"):
            publish_staging(self.staging, self.out, [self.staging / "history_records.json"])

    def test_hidden_or_internal_names_are_rejected(self):
        self.staging.mkdir()
        for name in (".current", ".snapshots/x.json", "images/.hidden.png"):
            path = self.staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            write_file(path, "x")
            with self.subTest(name=name):
                with self.assertRaises(PublishError):
                    publish_staging(self.staging, self.out, [path])

    def test_symlink_unsupported_fails_without_downgrade(self):
        # 旧平面输出：顶层是普通文件。
        self.out.mkdir(parents=True)
        write_file(self.out / "history_records.json", "old-records")
        write_file(self.out / "import_report.json", "old-report")
        files = self.snapshot_files("new")

        with mock.patch("os.symlink", side_effect=OSError(1, "Operation not permitted")):
            with self.assertRaisesRegex(PublishError, "不支持符号链接"):
                self.publish(files)

        # 旧文件没有被逐文件覆盖，也没有生成非原子的半成品。
        self.assertEqual(self.read_text("history_records.json"), "old-records")
        self.assertEqual(self.read_text("import_report.json"), "old-report")
        self.assertFalse(os.path.lexists(self.out / CURRENT_LINK_NAME))
        self.assertFalse((self.out / "history_records.json").is_symlink())
        self.assertFalse((self.out / "import_report.md").exists())

    def test_fresh_output_symlink_unsupported_leaves_no_snapshot(self):
        files = self.snapshot_files("v1")
        with mock.patch("os.symlink", side_effect=OSError(1, "Operation not permitted")):
            with self.assertRaisesRegex(PublishError, "不支持符号链接"):
                self.publish(files)
        self.assertFalse(os.path.lexists(self.out / CURRENT_LINK_NAME))
        for name in KNOWN_SNAPSHOT_NAMES:
            self.assertFalse((self.out / name).exists(), f"{name} 不应可见")


class ConcurrencyTest(PublishTestBase):
    def test_two_candidates_same_baseline_second_is_rejected(self):
        self.publish(self.snapshot_files("baseline"))
        baseline_bytes = self.read("history_records.json")
        expected = hashlib.sha256(baseline_bytes).hexdigest()
        staging_a = self.root / "candidate-a"
        staging_b = self.root / "candidate-b"
        files_a = self.stage(
            {"history_records.json": '{"token":"a"}', "import_report.json": '{"token":"a"}'},
            staging=staging_a,
        )
        files_b = self.stage(
            {
                "history_records.json": '{"token":"b"}',
                "import_report.json": '{"token":"b"}',
                "images/rejected.abcdef12.png": b"must-not-be-published",
            },
            staging=staging_b,
        )
        publish_staging(staging_a, self.out, files_a, expected_records_sha256=expected)
        generation_a = self.current_generation()
        generation_count = len(self.generation_dirs())
        with self.assertRaisesRegex(PublishError, "并发发布冲突.*重新读取"):
            publish_staging(staging_b, self.out, files_b, expected_records_sha256=expected)
        self.assertEqual(self.current_generation(), generation_a)
        self.assertEqual(len(self.generation_dirs()), generation_count)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "a")
        self.assertEqual(json.loads(self.read_text("import_report.json"))["token"], "a")
        self.assertFalse((self.out / "images/rejected.abcdef12.png").exists())

    def test_expect_absent_then_second_initializer_is_rejected(self):
        files = self.snapshot_files("first")
        publish_staging(self.staging, self.out, files, expected_records_sha256="")
        first_generation = self.current_generation()
        files_second = self.snapshot_files("second")
        with self.assertRaisesRegex(PublishError, "并发发布冲突"):
            publish_staging(self.staging, self.out, files_second, expected_records_sha256="")
        self.assertEqual(self.current_generation(), first_generation)
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "first")

    def test_baseline_hash_accepts_legacy_flat_records_and_empty_file(self):
        self.out.mkdir()
        write_file(self.out / "history_records.json", b"")
        files = self.snapshot_files("new")
        with self.assertRaisesRegex(PublishError, "并发发布冲突"):
            publish_staging(self.staging, self.out, files, expected_records_sha256="")
        self.assertFalse((self.out / "history_records.json").is_symlink())
        publish_staging(
            self.staging, self.out, files,
            expected_records_sha256=hashlib.sha256(b"").hexdigest(),
        )
        self.assertEqual(json.loads(self.read_text("history_records.json"))["token"], "new")

    def test_concurrent_publishers_serialize_to_one_complete_snapshot(self):
        staging_a = self.root / "staging-a"
        staging_b = self.root / "staging-b"
        files_a = self.stage(
            {
                "history_records.json": json.dumps({"token": "a"}),
                "import_report.json": json.dumps({"token": "a"}),
            },
            staging=staging_a,
        )
        files_b = self.stage(
            {
                "history_records.json": json.dumps({"token": "b"}),
                "import_report.json": json.dumps({"token": "b"}),
            },
            staging=staging_b,
        )
        self.assertTrue(files_a and files_b)

        command = self.child_command(
            staging_a, self.out, sleep_stage="generation-ready", sleep_seconds=0.4
        )
        command_b = self.child_command(
            staging_b, self.out, sleep_stage="generation-ready", sleep_seconds=0.4
        )
        first = subprocess.Popen(
            command, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        second = subprocess.Popen(
            command_b, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        _, error_first = first.communicate(timeout=120)
        _, error_second = second.communicate(timeout=120)
        self.assertEqual(first.returncode, 0, error_first)
        self.assertEqual(second.returncode, 0, error_second)

        records_token = json.loads(self.read_text("history_records.json"))["token"]
        report_token = json.loads(self.read_text("import_report.json"))["token"]
        self.assertIn(records_token, {"a", "b"})
        self.assertEqual(records_token, report_token)

        generation = self.current_generation()
        self.assertEqual(
            json.loads((generation / "history_records.json").read_text(encoding="utf-8"))["token"],
            records_token,
        )
        self.assertEqual(
            json.loads((generation / "import_report.json").read_text(encoding="utf-8"))["token"],
            records_token,
        )
        self.assertGreaterEqual(len(self.generation_dirs()), 2)
