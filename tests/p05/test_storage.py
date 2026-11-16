"""受控存储：内容寻址、原子不覆盖、路径与文件名净化、不可删除（P05、D2）。"""

import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.files import storage
from adapters.files.storage import StorageError


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "files"
        self.root.mkdir()
        self.data = b"PK\x03\x04synthetic-workbook"
        self.task_id = "11111111-1111-4111-8111-111111111111"
        self.version_id = "22222222-2222-4222-8222-222222222222"

    def save_original(self, data=None, filename="合成模板.xlsx", version_id=None):
        return storage.save_version_original(
            self.root, task_id=self.task_id, version_id=version_id or self.version_id,
            filename=filename, data=self.data if data is None else data,
        )


class SaveOriginalTests(StorageTestCase):
    def test_saves_content_addressed_file_under_version_directory(self):
        stored = self.save_original()
        self.assertTrue(stored.created)
        self.assertEqual(stored.sha256, storage.sha256(self.data))
        self.assertTrue(stored.relative_path.startswith(
            f"tasks/{self.task_id}/versions/{self.version_id}/original/"))
        self.assertTrue(stored.relative_path.endswith(f"{stored.sha256}.xlsx"))
        self.assertEqual(stored.display_name, "合成模板.xlsx")
        self.assertEqual(storage.read_bytes(self.root, stored.relative_path), self.data)

    def test_same_content_is_idempotent_and_not_rewritten(self):
        first = self.save_original()
        second = self.save_original(filename="另一份.xlsx")
        self.assertFalse(second.created)
        self.assertEqual(first.relative_path, second.relative_path)
        self.assertEqual(second.display_name, "另一份.xlsx")  # 展示名只是元数据
        self.assertEqual(len(list((self.root).rglob("*.xlsx"))), 1)

    def test_different_version_gets_its_own_directory(self):
        other = "33333333-3333-4333-8333-333333333333"
        first = self.save_original()
        second = self.save_original(version_id=other)
        self.assertNotEqual(first.relative_path, second.relative_path)
        self.assertTrue(second.relative_path.startswith(f"tasks/{self.task_id}/versions/{other}/"))

    def test_content_at_same_path_is_never_overwritten(self):
        stored = self.save_original()
        target = storage.resolve_path(self.root, stored.relative_path)
        target.write_bytes(b"tampered")
        with self.assertRaises(StorageError) as caught:
            self.save_original()
        self.assertEqual(caught.exception.code, "storage_content_conflict")
        self.assertEqual(target.read_bytes(), b"tampered")

    def test_rejects_empty_and_oversized_content(self):
        with self.assertRaises(StorageError) as caught:
            self.save_original(data=b"")
        self.assertEqual(caught.exception.code, "empty_file")
        with self.assertRaises(StorageError) as caught:
            storage.save_version_original(
                self.root, task_id=self.task_id, version_id=self.version_id,
                filename="合成模板.xlsx", data=self.data, max_bytes=1,
            )
        self.assertEqual(caught.exception.code, "file_too_large")

    def test_rejects_non_excel_and_dangerous_names(self):
        with self.assertRaises(StorageError) as caught:
            self.save_original(filename="图片.png")
        self.assertEqual(caught.exception.code, "unsupported_file_type")
        with self.assertRaises(StorageError) as caught:
            self.save_original(filename="payload.exe")
        self.assertEqual(caught.exception.code, "unsafe_filename")

    def test_rejects_unsafe_identifiers(self):
        for task_id, version_id in (("../etc", self.version_id),
                                    (self.task_id, ".."),
                                    (self.task_id, "a/b")):
            with self.subTest(task_id=task_id, version_id=version_id):
                with self.assertRaises(StorageError) as caught:
                    storage.save_version_original(
                        self.root, task_id=task_id, version_id=version_id,
                        filename="合成模板.xlsx", data=self.data,
                    )
                self.assertEqual(caught.exception.code, "unsafe_path")


class ImageStorageTests(StorageTestCase):
    def test_images_go_to_separate_directory_and_dedupe_by_content(self):
        image = b"\x89PNG\r\n\x1a\nsynthetic"
        first = storage.save_image(
            self.root, task_id=self.task_id, version_id=self.version_id,
            media_part="xl/media/image1.png", data=image,
        )
        second = storage.save_image(
            self.root, task_id=self.task_id, version_id=self.version_id,
            media_part="xl/media/image2.png", data=image,
        )
        self.assertTrue(first.relative_path.startswith(
            f"tasks/{self.task_id}/versions/{self.version_id}/images/"))
        self.assertEqual(first.relative_path, second.relative_path)
        self.assertFalse(second.created)
        self.assertEqual(len(list((self.root).rglob("*.png"))), 1)

    def test_image_suffix_is_normalized_and_sniffed(self):
        jpeg = storage.save_image(
            self.root, task_id=self.task_id, version_id=self.version_id,
            media_part="xl/media/image1.jpeg", data=b"\xff\xd8\xff\xe0jpeg",
        )
        self.assertTrue(jpeg.relative_path.endswith(".jpg"))
        sniffed = storage.save_image(
            self.root, task_id=self.task_id, version_id=self.version_id,
            media_part="xl/media/blob", data=b"\x89PNG\r\n\x1a\nsynthetic2",
        )
        self.assertTrue(sniffed.relative_path.endswith(".png"))

    def test_unknown_image_content_is_rejected(self):
        with self.assertRaises(StorageError) as caught:
            storage.save_image(
                self.root, task_id=self.task_id, version_id=self.version_id,
                media_part="xl/media/blob", data=b"not an image",
            )
        self.assertEqual(caught.exception.code, "unsupported_image_type")


class AtomicWriteTests(StorageTestCase):
    def test_no_temp_files_are_left_behind(self):
        stored = self.save_original()
        self.assertEqual(list(self.root.rglob(".tmp-*")), [])
        files = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertEqual([path.name for path in files], [stored.relative_path.rsplit("/", 1)[-1]])

    def test_falls_back_to_exclusive_create_when_hardlinks_are_unsupported(self):
        def unsupported(*args, **kwargs):
            raise OSError(errno.EPERM, "hardlinks not supported")

        with patch.object(storage.os, "link", side_effect=unsupported):
            first = self.save_original()
        self.assertTrue(first.created)
        self.assertEqual(storage.read_bytes(self.root, first.relative_path), self.data)
        # 第二次仍然命中“已存在”，不会覆盖。
        with patch.object(storage.os, "link", side_effect=unsupported):
            second = self.save_original()
        self.assertFalse(second.created)
        self.assertEqual(storage.read_bytes(self.root, first.relative_path), self.data)


class PathSafetyTests(StorageTestCase):
    def test_rejects_traversal_absolute_and_control_characters(self):
        for relative in ("../secret.xlsx", "tasks/../../etc/passwd", "/etc/passwd",
                         "C:/Windows/win.ini", "tasks\\..\\evil.xlsx", "tasks/\x00evil.xlsx",
                         ""):
            with self.subTest(relative=relative):
                with self.assertRaises(StorageError) as caught:
                    storage.resolve_path(self.root, relative)
                self.assertIn(caught.exception.code, ("unsafe_path", "path_outside_root"))

    def test_rejects_symlink_escape(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.root / "tasks").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageError) as caught:
            storage.resolve_path(self.root, "tasks/t1/versions/v1/original/a.xlsx")
        self.assertEqual(caught.exception.code, "path_outside_root")

        target = outside / "secret.xlsx"
        target.write_bytes(b"secret")
        (self.root / "link.xlsx").symlink_to(target)
        with self.assertRaises(StorageError) as caught:
            storage.read_bytes(self.root, "link.xlsx")
        self.assertEqual(caught.exception.code, "path_outside_root")

    def test_accepts_normal_relative_path(self):
        stored = self.save_original()
        resolved = storage.resolve_path(self.root, stored.relative_path)
        self.assertTrue(resolved.is_file())
        self.assertTrue(str(resolved).startswith(str(self.root.resolve())))

    def test_exists_and_missing_read(self):
        self.assertFalse(storage.exists(self.root, "tasks/none/versions/none/original/a.xlsx"))
        with self.assertRaises(StorageError) as caught:
            storage.read_bytes(self.root, "tasks/none/versions/none/original/a.xlsx")
        self.assertEqual(caught.exception.code, "file_not_found")

    def test_read_size_limit(self):
        stored = self.save_original()
        with self.assertRaises(StorageError) as caught:
            storage.read_bytes(self.root, stored.relative_path, max_bytes=1)
        self.assertEqual(caught.exception.code, "file_too_large")


class StagingTests(StorageTestCase):
    def stage(self, data=None, *, suffix=".xlsx", max_bytes=storage.DEFAULT_MAX_BYTES):
        return storage.stage_bytes(
            self.root, self.data if data is None else data,
            suffix=suffix, max_bytes=max_bytes,
        )

    def test_stage_writes_uuid_file_in_incoming(self):
        staged = self.stage()
        self.assertTrue(staged.relative_path.startswith("incoming/"))
        self.assertTrue(staged.relative_path.endswith(".xlsx"))
        self.assertEqual(staged.sha256, storage.sha256(self.data))
        self.assertEqual(staged.size, len(self.data))
        self.assertEqual(storage.read_bytes(self.root, staged.relative_path), self.data)
        # 暂存阶段不写正式版本目录。
        self.assertFalse((self.root / "tasks").exists())

    def test_stage_enforces_size_suffix_and_token_rules(self):
        with self.assertRaises(StorageError) as caught:
            self.stage(max_bytes=1)
        self.assertEqual(caught.exception.code, "file_too_large")
        for suffix in (".exe", ".bin", ""):
            with self.subTest(suffix=suffix):
                with self.assertRaises(StorageError) as caught:
                    self.stage(suffix=suffix)
                self.assertIn(caught.exception.code, ("unsupported_file_type", "unsafe_path"))
        with self.assertRaises(StorageError) as caught:
            storage.discard_staged(self.root, "../../tasks/x")
        self.assertEqual(caught.exception.code, "invalid_stage_token")

    def test_publish_moves_to_version_original_and_is_idempotent(self):
        staged = self.stage()
        stored = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        self.assertTrue(stored.created)
        self.assertEqual(stored.relative_path, storage.version_original_relative_path(
            self.task_id, self.version_id, staged.sha256, ".xlsx",
        ))
        self.assertEqual(storage.read_bytes(self.root, stored.relative_path), self.data)
        self.assertEqual(list((self.root / "incoming").glob("*.xlsx")), [])
        self.assertTrue(storage.exists(self.root, stored.relative_path))

        again = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        self.assertFalse(again.created)
        self.assertEqual(again.relative_path, stored.relative_path)
        self.assertEqual(storage.read_bytes(self.root, again.relative_path), self.data)

    def test_same_content_different_token_publishes_once(self):
        first = self.stage()
        second = self.stage()
        one = storage.publish_staged_original(
            self.root, first.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        two = storage.publish_staged_original(
            self.root, second.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        self.assertTrue(one.created)
        self.assertFalse(two.created)
        self.assertEqual(one.relative_path, two.relative_path)

    def test_discard_removes_only_staged_files(self):
        staged = self.stage()
        storage.discard_staged(self.root, staged.token)
        self.assertEqual(list((self.root / "incoming").iterdir()), [])
        with self.assertRaises(StorageError) as caught:
            storage.discard_staged(self.root, staged.token)
        self.assertEqual(caught.exception.code, "stage_not_found")
        with self.assertRaises(StorageError) as caught:
            storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
                filename="合成模板.xlsx",
            )
        self.assertEqual(caught.exception.code, "stage_not_found")

    def test_publish_never_overwrites_existing_original(self):
        staged = self.stage()
        stored = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        target = storage.resolve_path(self.root, stored.relative_path)
        target.write_bytes(b"tampered")
        retry = self.stage()
        with self.assertRaises(StorageError) as caught:
            storage.publish_staged_original(
                self.root, retry.token, task_id=self.task_id, version_id=self.version_id,
                filename="合成模板.xlsx",
            )
        self.assertEqual(caught.exception.code, "storage_content_conflict")
        self.assertEqual(target.read_bytes(), b"tampered")
        self.assertTrue(storage.read_bytes(self.root, retry.relative_path))

    def test_published_token_cannot_be_replayed_into_other_version(self):
        staged = self.stage()
        storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        with self.assertRaises(StorageError) as caught:
            storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id,
                version_id="33333333-3333-4333-8333-333333333333",
                filename="合成模板.xlsx",
            )
        self.assertEqual(caught.exception.code, "stage_publish_conflict")


    def test_publish_honours_max_bytes(self):
        payload = b"PK\x03\x04" + b"x" * 146  # 150 字节
        staged = self.stage(data=payload, max_bytes=200)
        with self.assertRaises(StorageError) as caught:
            storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
                filename="合成模板.xlsx", max_bytes=100,
            )
        self.assertEqual(caught.exception.code, "file_too_large")
        self.assertFalse((self.root / "tasks").exists())
        stored = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx", max_bytes=200,
        )
        self.assertEqual(stored.size, 150)
        self.assertEqual(storage.read_bytes(self.root, stored.relative_path), payload)

    def test_concurrent_publish_same_token_returns_same_file(self):
        staged = self.stage()
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            barrier.wait()
            try:
                results.append(storage.publish_staged_original(
                    self.root, staged.token, task_id=self.task_id,
                    version_id=self.version_id, filename="合成模板.xlsx",
                ))
            except BaseException as exc:  # noqa: BLE001 - 测试需要收集线程异常
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({item.relative_path for item in results}), 1)
        self.assertEqual({item.sha256 for item in results}, {staged.sha256})
        files = [path for path in (self.root / "tasks").rglob("*") if path.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(storage.file_sha256(self.root, results[0].relative_path), staged.sha256)

        again = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
            filename="合成模板.xlsx",
        )
        self.assertEqual(again.relative_path, results[0].relative_path)

    def test_slow_publisher_after_staged_check_still_returns_fast_result(self):
        """慢调用越过暂存存在检查后暂停，快调用完成发布并清理，慢调用仍须成功。"""
        staged = self.stage()
        slow_paused = threading.Event()
        fast_done = threading.Event()
        results, errors = {}, []

        def hook(stage, token, details):
            if stage == "after_staged_check" and threading.current_thread().name == "slow":
                slow_paused.set()
                if not fast_done.wait(10):
                    raise AssertionError("快调用未在超时内完成")

        def slow():
            try:
                results["slow"] = storage.publish_staged_original(
                    self.root, staged.token, task_id=self.task_id,
                    version_id=self.version_id, filename="合成模板.xlsx",
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def fast():
            try:
                if not slow_paused.wait(10):
                    raise AssertionError("慢调用未到达暂停点")
                results["fast"] = storage.publish_staged_original(
                    self.root, staged.token, task_id=self.task_id,
                    version_id=self.version_id, filename="合成模板.xlsx",
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                fast_done.set()

        with patch.object(storage, "_PUBLISH_HOOK", hook):
            slow_thread = threading.Thread(target=slow, name="slow")
            fast_thread = threading.Thread(target=fast, name="fast")
            slow_thread.start()
            fast_thread.start()
            slow_thread.join(timeout=15)
            fast_thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(results["slow"].relative_path, results["fast"].relative_path)
        self.assertEqual(results["slow"].sha256, staged.sha256)
        self.assertEqual(
            len([path for path in (self.root / "tasks").rglob("*") if path.is_file()]), 1,
        )

    def test_publish_rechecks_receipt_when_staged_read_is_storage_unreadable(self):
        """文件存在检查通过后、打开文件时被清理：storage_unreadable 也要回查回执。"""
        first = self.stage()
        other = self.stage()
        stored = storage.publish_staged_original(
            self.root, other.token, task_id=self.task_id,
            version_id=self.version_id, filename="合成模板.xlsx",
        )
        receipt = {
            "token": first.token,
            "task_id": self.task_id,
            "version_id": self.version_id,
            "relative_path": stored.relative_path,
            "sha256": first.sha256,
            "size": first.size,
            "suffix": ".xlsx",
            "display_name": "合成模板.xlsx",
        }
        real_read_bytes = storage.read_bytes

        def fake_read_bytes(root, relative_path, **kwargs):
            if relative_path == first.relative_path:
                storage._write_receipt(
                    root, f"incoming/{first.token}.json", receipt,
                )
                raise StorageError("storage_unreadable", "模拟打开瞬间被另一发布清理")
            return real_read_bytes(root, relative_path, **kwargs)

        with patch.object(storage, "read_bytes", side_effect=fake_read_bytes):
            result = storage.publish_staged_original(
                self.root, first.token, task_id=self.task_id,
                version_id=self.version_id, filename="合成模板.xlsx",
            )
        self.assertEqual(result.relative_path, stored.relative_path)
        self.assertEqual(result.sha256, first.sha256)

    def test_publish_binding_without_receipt_completes_after_crash(self):
        """绑定已写、回执未写（前一次崩溃）时，重放应继续完成发布。"""
        staged = self.stage()
        storage._write_binding(
            self.root, staged.token, task_id=self.task_id, version_id=self.version_id,
        )
        stored = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id,
            version_id=self.version_id, filename="合成模板.xlsx",
        )
        self.assertTrue(stored.created)
        self.assertTrue(storage.exists(self.root, stored.relative_path))

    def test_concurrent_publish_to_different_versions_binds_only_one_target(self):
        """同一 token 并发发布到 V1/V2：只有一个成功，失败目标不产生正式文件。"""
        staged = self.stage()
        other_version = "33333333-3333-4333-8333-333333333333"
        barrier = threading.Barrier(2)
        results, errors = {}, []

        def hook(stage, token, details):
            if stage == "after_staged_check":
                barrier.wait(timeout=10)

        def worker(name, version_id):
            try:
                results[name] = storage.publish_staged_original(
                    self.root, staged.token, task_id=self.task_id,
                    version_id=version_id, filename="合成模板.xlsx",
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append((name, exc))

        with patch.object(storage, "_PUBLISH_HOOK", hook):
            threads = [
                threading.Thread(target=worker, args=("v1", self.version_id)),
                threading.Thread(target=worker, args=("v2", other_version)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1].code, "stage_publish_conflict")
        winner_name, winner_stored = next(iter(results.items()))
        loser_version = other_version if winner_name == "v1" else self.version_id
        self.assertIn(self.version_id if winner_name == "v1" else other_version,
                      winner_stored.relative_path)
        self.assertFalse((self.root / "tasks" / self.task_id / "versions" / loser_version).exists())
        with self.assertRaises(StorageError):
            storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id,
                version_id=loser_version, filename="合成模板.xlsx",
            )
        again = storage.publish_staged_original(
            self.root, staged.token, task_id=self.task_id,
            version_id=self.version_id if winner_name == "v1" else other_version,
            filename="合成模板.xlsx",
        )
        self.assertEqual(again.relative_path, winner_stored.relative_path)

    def test_binding_target_mismatch_rejected_before_formal_write(self):
        staged = self.stage()
        other_version = "33333333-3333-4333-8333-333333333333"
        storage._write_binding(
            self.root, staged.token, task_id=self.task_id, version_id=other_version,
        )
        with self.assertRaises(StorageError) as caught:
            storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id,
                version_id=self.version_id, filename="合成模板.xlsx",
            )
        self.assertEqual(caught.exception.code, "stage_publish_conflict")
        self.assertFalse((self.root / "tasks").exists())

    def test_publish_tolerates_staged_cleanup_by_other_publisher(self):
        staged = self.stage()
        real_write_receipt = storage._write_receipt

        def racing_write_receipt(root, relative_path, payload):
            real_write_receipt(root, relative_path, payload)
            # 模拟另一发布调用已写回执并清理暂存文件。
            staged_relative = storage._staged_relative_path(root, staged.token)
            storage.resolve_path(root, staged_relative).unlink()

        with patch.object(storage, "_write_receipt", side_effect=racing_write_receipt):
            stored = storage.publish_staged_original(
                self.root, staged.token, task_id=self.task_id,
                version_id=self.version_id, filename="合成模板.xlsx",
            )
        self.assertEqual(stored.sha256, staged.sha256)
        self.assertTrue(storage.exists(self.root, stored.relative_path))


class ExportStorageTests(StorageTestCase):
    def save(self, *, kind="precheck", filename="预审结果.xlsx", data=None):
        return storage.save_export(
            self.root, task_id=self.task_id, version_id=self.version_id, kind=kind,
            filename=filename, data=self.data if data is None else data,
        )

    def test_saves_export_under_version_exports_directory(self):
        stored = self.save()
        self.assertTrue(stored.relative_path.startswith(
            f"tasks/{self.task_id}/versions/{self.version_id}/exports/precheck/"))
        self.assertTrue(stored.relative_path.endswith(f"{stored.sha256}.xlsx"))
        self.assertEqual(storage.read_export(self.root, stored.relative_path), self.data)
        self.assertTrue(storage.export_exists(self.root, stored.relative_path))
        # 导出与原件/图片目录互不混用。
        self.assertNotIn("/original/", stored.relative_path)
        self.assertNotIn("/images/", stored.relative_path)

    def test_same_content_and_kind_is_idempotent(self):
        first = self.save()
        second = self.save(filename="另一份.xlsx")
        self.assertFalse(second.created)
        self.assertEqual(first.relative_path, second.relative_path)

    def test_supports_allowed_suffixes_and_rejects_others(self):
        for filename, suffix in (("结果.csv", ".csv"), ("结果.pdf", ".pdf"),
                                 ("打包.zip", ".zip"), ("宏表.xlsm", ".xlsm")):
            with self.subTest(filename=filename):
                stored = self.save(kind=f"kind{suffix[1:]}", filename=filename)
                self.assertTrue(stored.relative_path.endswith(suffix))
        with self.assertRaises(StorageError) as caught:
            self.save(filename="跑起来.exe")
        self.assertEqual(caught.exception.code, "unsafe_filename")
        with self.assertRaises(StorageError) as caught:
            self.save(filename="说明.txt")
        self.assertEqual(caught.exception.code, "unsupported_file_type")

    def test_read_export_rejects_non_export_paths(self):
        original = self.save_original()
        for relative in (original.relative_path, "../secret.pdf", "tasks/only/two"):
            with self.subTest(relative=relative):
                with self.assertRaises(StorageError) as caught:
                    storage.read_export(self.root, relative)
                self.assertEqual(caught.exception.code, "unsafe_path")

    def test_export_kind_must_be_safe_identifier(self):
        for kind in ("../evil", "a/b", ""):
            with self.subTest(kind=kind):
                with self.assertRaises(StorageError) as caught:
                    self.save(kind=kind)
                self.assertEqual(caught.exception.code, "unsafe_path")


class FilenameTests(unittest.TestCase):
    def test_strips_paths_control_characters_and_normalizes_unicode(self):
        self.assertEqual(storage.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(storage.sanitize_filename("..\\..\\窗口.xlsx"), "窗口.xlsx")
        self.assertEqual(storage.sanitize_filename("目录／文件.xlsx"), "文件.xlsx")
        self.assertEqual(storage.sanitize_filename("bad\x00name\x1f.xlsx"), "badname.xlsx")
        self.assertEqual(storage.sanitize_filename("   .  "), "file")
        self.assertEqual(storage.sanitize_filename(""), "file")

    def test_truncates_long_names_but_keeps_suffix(self):
        name = storage.sanitize_filename("长" * 300 + ".xlsx")
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".xlsx"))

    def test_avoids_windows_reserved_names(self):
        self.assertEqual(storage.sanitize_filename("CON.xlsx"), "_CON.xlsx")
        self.assertEqual(storage.sanitize_filename("lpt1.xlsx"), "_lpt1.xlsx")

    def test_rejects_dangerous_or_disallowed_suffixes(self):
        with self.assertRaises(StorageError) as caught:
            storage.sanitize_filename("run.exe")
        self.assertEqual(caught.exception.code, "unsafe_filename")
        with self.assertRaises(StorageError) as caught:
            storage.sanitize_filename("sheet.csv", allowed_suffixes=storage.ORIGINAL_SUFFIXES)
        self.assertEqual(caught.exception.code, "unsupported_file_type")

    def test_keeps_display_name_for_chinese_and_normalizes_fullwidth(self):
        self.assertEqual(storage.sanitize_filename("合 同 模 板.xlsx"), "合 同 模 板.xlsx")


class ImmutableTests(StorageTestCase):
    def test_delete_is_forbidden(self):
        stored = self.save_original()
        with self.assertRaises(StorageError) as caught:
            storage.delete(self.root, stored.relative_path)
        self.assertEqual(caught.exception.code, "immutable_storage")
        self.assertTrue(storage.exists(self.root, stored.relative_path))


if __name__ == "__main__":
    unittest.main()
