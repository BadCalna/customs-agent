"""受控存储：内容寻址、原子不覆盖、路径与文件名净化、不可删除（P05、D2）。"""

import errno
import tempfile
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
