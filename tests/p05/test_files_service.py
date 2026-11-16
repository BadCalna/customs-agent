"""P05 服务层：版本原件入库、附图入库、受控读取、权限与版本隔离（D2、D3）。"""

import tempfile
import unittest
import uuid
from pathlib import Path

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings

from adapters.files import storage
from apps.accounts.errors import ApiError, AuthenticationRequired
from apps.accounts.models import Role
from apps.reviews.models import FileVersion, ItemImage
from apps.reviews.services import files
from tests.p01.factories import assign, make_item, make_task, make_user, make_version
from tests.p05.workbooks import (
    DECLARATION_SHEET,
    ambiguous_image_column_package,
    mixed_package,
    no_image_column_package,
    package_bytes,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class FileServiceTestCase(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "files"
        settings_override = override_settings(CUSTOMS_FILE_ROOT=str(self.root))
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.supplier = make_user("p05_supplier", Role.SUPPLIER)
        self.other_supplier = make_user("p05_supplier_other", Role.SUPPLIER)
        self.operator = make_user("p05_operator", Role.OPERATOR)
        self.other_operator = make_user("p05_operator_other", Role.OPERATOR)
        self.expert = make_user("p05_expert", Role.EXPERT)
        self.task = make_task(self.supplier)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.items = {
            row: make_item(self.version, row=row, name=f"合成商品{row}")
            for row in range(2, 17)
        }
        self.workbook_bytes = package_bytes(mixed_package())

    # 常用动作 -------------------------------------------------------------
    def store(self, *, user=None, version=None, filename="合成模板.xlsx", data=None):
        return files.store_version_original(
            user or self.supplier,
            (version or self.version).pk,
            filename=filename,
            data=self.workbook_bytes if data is None else data,
        )

    def extract(self, *, user=None, version=None, **kwargs):
        return files.extract_version_images(
            user or self.supplier, (version or self.version).pk,
            sheet_name=DECLARATION_SHEET, **kwargs,
        )

    def assert_api_error(self, status, code, callable_, *args, **kwargs):
        with self.assertRaises(ApiError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.status, status, caught.exception.message)
        if code is not None:
            self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def stored_version(self, *, number=2, workbook=None):
        """在任务下新建一个已入库原件的版本，用于“图片列缺失”等需要另一份工作簿的用例。"""
        version = make_version(self.task, self.supplier, number=number)
        for row in range(2, 17):
            make_item(version, row=row, name=f"合成商品{row}")
        files.store_version_original(
            self.supplier, version.pk, filename=f"合成模板V{number}.xlsx",
            data=workbook if workbook is not None else package_bytes(mixed_package()),
        )
        return version


class StoreOriginalTests(FileServiceTestCase):
    def test_stores_original_as_relative_path_and_reads_back(self):
        version = self.store()
        version.refresh_from_db()
        self.assertTrue(version.storage_path)
        self.assertFalse(Path(version.storage_path).is_absolute())
        self.assertTrue(version.storage_path.startswith(
            f"tasks/{self.task.pk}/versions/{version.pk}/original/"))
        self.assertTrue(storage.exists(self.root, version.storage_path))

        download = files.read_version_original(self.supplier, version.pk)
        self.assertEqual(download.content, self.workbook_bytes)
        self.assertEqual(download.relative_path, version.storage_path)
        self.assertEqual(download.sha256, storage.sha256(self.workbook_bytes))
        self.assertEqual(download.filename, "合成模板.xlsx")
        self.assertEqual(
            download.content_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_same_content_is_idempotent_and_different_content_is_rejected(self):
        first = self.store()
        first.refresh_from_db()
        second = self.store(filename="另一份.xlsx")
        second.refresh_from_db()
        self.assertEqual(first.storage_path, second.storage_path)
        self.assertEqual(len(list(self.root.rglob("*.xlsx"))), 1)

        shifted = package_bytes(no_image_column_package())
        self.assert_api_error(
            409, "original_already_stored", self.store, data=shifted,
        )
        self.assertEqual(
            files.read_version_original(self.supplier, self.version.pk).content,
            self.workbook_bytes,
        )

    def test_only_supplier_owner_can_store_latest_version(self):
        with self.assertRaises(PermissionDenied):
            self.store(user=self.operator)
        with self.assertRaises(PermissionDenied):
            self.store(user=self.expert)
        with self.assertRaises(AuthenticationRequired):
            self.store(user=AnonymousUser())
        with self.assertRaises(files.Http404):
            self.store(user=self.other_supplier)

        # 旧版本不能再入库原件：补料必须新建版本。
        make_version(self.task, self.supplier, number=2)
        self.assert_api_error(409, "stale_version", self.store)

    def test_rejects_dangerous_and_unsupported_filenames(self):
        self.assert_api_error(422, "unsafe_filename", self.store, filename="payload.exe")
        self.assert_api_error(
            422, "unsupported_file_type", self.store, filename="说明.pdf",
        )

    def test_download_filename_is_sanitized(self):
        self.store()
        # 原文件名由 P06 在上传时写库；这里模拟被写入带路径分隔符的名字，读取时必须净化。
        FileVersion.objects.filter(pk=self.version.pk).update(
            original_filename="..\\..\\危险名称.xlsx",
        )
        download = files.read_version_original(self.supplier, self.version.pk)
        self.assertEqual(download.filename, "危险名称.xlsx")

    def test_not_stored_and_unknown_ids(self):
        self.assert_api_error(
            404, "file_not_stored", files.read_version_original, self.supplier, self.version.pk,
        )
        with self.assertRaises(files.Http404):
            files.read_version_original(self.supplier, uuid.uuid4())
        with self.assertRaises(files.Http404):
            files.read_version_original(self.supplier, "not-a-uuid")


class ReadPermissionTests(FileServiceTestCase):
    def setUp(self):
        super().setUp()
        self.store()

    def test_visible_roles_can_read_original(self):
        for user in (self.supplier, self.operator, self.expert):
            with self.subTest(role=user.role):
                self.assertEqual(
                    files.read_version_original(user, self.version.pk).content,
                    self.workbook_bytes,
                )

    def test_invisible_and_anonymous_get_404_or_401(self):
        for user in (self.other_supplier, self.other_operator):
            with self.subTest(user=user.username):
                with self.assertRaises(files.Http404):
                    files.read_version_original(user, self.version.pk)
        with self.assertRaises(AuthenticationRequired):
            files.read_version_original(AnonymousUser(), self.version.pk)


class ExtractImagesTests(FileServiceTestCase):
    def setUp(self):
        super().setUp()
        self.store()
        self.report = self.extract()

    def test_writes_item_images_with_three_statuses(self):
        self.assertEqual(self.report["counts"], {
            "available": 3, "absent": 4, "read_failed": 8, "skipped": 0,
        })
        self.assertEqual(ItemImage.objects.filter(item__version=self.version).count(), 15)
        rows = {entry["source_row"]: entry for entry in self.report["rows"]}
        self.assertEqual(rows[2]["status"], ItemImage.Status.AVAILABLE)
        self.assertEqual(rows[2]["dispimg_id"], "ID_A")
        self.assertEqual(rows[5]["status"], ItemImage.Status.ABSENT)
        self.assertEqual(rows[7]["status"], ItemImage.Status.READ_FAILED)
        self.assertIn("dispimg_definition_missing", rows[7]["error_message"])
        for row in (2, 3, 4):
            record = ItemImage.objects.get(item=self.items[row], source_cell=f"E{row}")
            self.assertEqual(record.status, ItemImage.Status.AVAILABLE)
            self.assertTrue(record.storage_path)
            self.assertFalse(Path(record.storage_path).is_absolute())
            self.assertIsNotNone(record.extracted_at)
        for row in (5, 6, 7, 10, 11, 14):
            record = ItemImage.objects.get(item=self.items[row])
            self.assertEqual(record.storage_path, "")
        self.assertTrue(ItemImage.objects.get(item=self.items[6]).error_message)

    def test_equal_images_share_one_stored_file(self):
        first = ItemImage.objects.get(item=self.items[2])
        second = ItemImage.objects.get(item=self.items[3])
        other = ItemImage.objects.get(item=self.items[4])
        self.assertEqual(first.storage_path, second.storage_path)
        self.assertNotEqual(first.storage_path, other.storage_path)
        self.assertEqual(len(list(self.root.rglob("*.png"))), 2)

    def test_reads_image_bytes_and_relative_path(self):
        record = ItemImage.objects.get(item=self.items[2])
        download = files.read_item_image(self.supplier, record.pk)
        self.assertEqual(download.sha256, Path(record.storage_path).stem)
        self.assertEqual(download.content_type, "image/png")
        self.assertEqual(download.filename, "image.png")
        self.assertEqual(download.content, storage.read_bytes(self.root, record.storage_path))
        relative = files.read_item_image_path(self.supplier, record.pk)
        self.assertEqual(relative, record.storage_path)
        self.assertFalse(Path(relative).is_absolute())

    def test_image_read_permissions_follow_task_visibility(self):
        record = ItemImage.objects.get(item=self.items[2])
        for user in (self.supplier, self.operator, self.expert):
            with self.subTest(role=user.role):
                self.assertTrue(files.read_item_image(user, record.pk).content)
        for user in (self.other_supplier, self.other_operator):
            with self.subTest(user=user.username):
                with self.assertRaises(files.Http404):
                    files.read_item_image(user, record.pk)
        with self.assertRaises(AuthenticationRequired):
            files.read_item_image(AnonymousUser(), record.pk)

    def test_user_entry_requires_upload_version_action(self):
        """用户入口是系统派生写动作：只读角色不能触发图片入库。"""
        for user in (self.operator, self.expert):
            with self.subTest(role=user.role):
                with self.assertRaises(PermissionDenied):
                    files.extract_version_images(
                        user, self.version.pk, sheet_name=DECLARATION_SHEET,
                    )
        with self.assertRaises(AuthenticationRequired):
            files.extract_version_images(
                AnonymousUser(), self.version.pk, sheet_name=DECLARATION_SHEET,
            )
        with self.assertRaises(files.Http404):
            files.extract_version_images(
                self.other_supplier, self.version.pk, sheet_name=DECLARATION_SHEET,
            )
        # 拒绝发生在写库之前：图片记录没有被改动。
        self.assertEqual(ItemImage.objects.filter(item__version=self.version).count(), 15)

    def test_unavailable_images_are_not_downloadable(self):
        absent = ItemImage.objects.get(item=self.items[5])
        failed = ItemImage.objects.get(item=self.items[7])
        for record in (absent, failed):
            with self.subTest(status=record.status):
                self.assert_api_error(
                    404, "image_not_available", files.read_item_image, self.supplier, record.pk,
                )
                self.assert_api_error(
                    404, "image_not_available", files.read_item_image_path, self.supplier, record.pk,
                )
        with self.assertRaises(files.Http404):
            files.read_item_image(self.supplier, uuid.uuid4())

    def test_repeat_extraction_is_idempotent_and_replaces_records(self):
        first = ItemImage.objects.get(item=self.items[2])
        ItemImage.objects.filter(item=self.items[2]).update(
            status=ItemImage.Status.ABSENT, storage_path="", error_message="人工改动",
        )
        ItemImage.objects.create(
            item=self.items[2], source_sheet=DECLARATION_SHEET, source_cell="E99",
            status=ItemImage.Status.ABSENT,
        )
        report = self.extract()
        self.assertEqual(report["counts"]["available"], 3)
        self.assertEqual(ItemImage.objects.filter(item__version=self.version).count(), 15)
        refreshed = ItemImage.objects.get(item=self.items[2])
        self.assertEqual(refreshed.status, ItemImage.Status.AVAILABLE)
        self.assertEqual(refreshed.storage_path, first.storage_path)
        self.assertFalse(ItemImage.objects.filter(item=self.items[2], source_cell="E99").exists())



class ExplicitRowTests(FileServiceTestCase):
    def test_provided_rows_skip_missing_items_without_crossing_versions(self):
        self.store()
        report = self.extract(rows=[2, 99])
        self.assertEqual(report["counts"]["skipped"], 1)
        self.assertEqual(report["skipped_rows"][0]["source_row"], 99)
        self.assertTrue(ItemImage.objects.filter(item=self.items[2]).exists())
        self.assertFalse(ItemImage.objects.filter(item=self.items[3]).exists())


class VersionIsolationTests(FileServiceTestCase):
    def test_new_version_reparse_does_not_touch_old_images(self):
        self.store()
        old_report = self.extract()
        self.assertEqual(old_report["counts"]["available"], 3)
        old_images = list(
            ItemImage.objects.filter(item__version=self.version).order_by("item_id")
            .values_list("item_id", "status", "storage_path")
        )

        new_version = make_version(self.task, self.supplier, number=2)
        for row in range(2, 17):
            make_item(new_version, row=row, name=f"合成商品{row}")
        files.store_version_original(
            self.supplier, new_version.pk, filename="合成模板V2.xlsx",
            data=package_bytes(mixed_package()),
        )
        new_report = files.extract_version_images(
            self.supplier, new_version.pk, sheet_name=DECLARATION_SHEET,
        )
        self.assertEqual(new_report["counts"]["available"], 3)

        self.assertEqual(
            list(ItemImage.objects.filter(item__version=self.version).order_by("item_id")
                 .values_list("item_id", "status", "storage_path")),
            old_images,
        )
        self.assertEqual(ItemImage.objects.filter(item__version=new_version).count(), 15)
        for image in ItemImage.objects.filter(item__version=new_version, status=ItemImage.Status.AVAILABLE):
            self.assertIn(str(new_version.pk), image.storage_path)
        for image in ItemImage.objects.filter(item__version=self.version, status=ItemImage.Status.AVAILABLE):
            self.assertIn(str(self.version.pk), image.storage_path)


class ErrorAndLimitTests(FileServiceTestCase):
    def test_missing_image_column_is_an_error_not_all_absent(self):
        version = self.stored_version(workbook=package_bytes(no_image_column_package()))
        self.assert_api_error(422, "image_column_missing", files.extract_version_images, self.supplier,
                              version.pk, sheet_name=DECLARATION_SHEET)
        self.assertEqual(ItemImage.objects.filter(item__version=version).count(), 0)

    def test_ambiguous_image_column_is_an_error(self):
        version = self.stored_version(workbook=package_bytes(ambiguous_image_column_package()))
        self.assert_api_error(422, "image_column_ambiguous", files.extract_version_images, self.supplier,
                              version.pk, sheet_name=DECLARATION_SHEET)
        self.assertEqual(ItemImage.objects.filter(item__version=version).count(), 0)

    def test_unreadable_workbook_maps_to_422(self):
        version = self.stored_version(workbook=b"PK\x03\x04not-a-workbook")
        error = self.assert_api_error(
            422, None, files.extract_version_images, self.supplier, version.pk,
            sheet_name=DECLARATION_SHEET,
        )
        self.assertTrue(error.code.startswith("workbook_"), error.code)

    def test_no_items_is_an_error(self):
        empty_version = make_version(self.task, self.supplier, number=2)
        files.store_version_original(
            self.supplier, empty_version.pk, filename="合成模板V2.xlsx",
            data=package_bytes(mixed_package()),
        )
        self.assert_api_error(422, "no_items", files.extract_version_images, self.supplier,
                              empty_version.pk, sheet_name=DECLARATION_SHEET)

    def test_rejects_non_bytes_content(self):
        self.assert_api_error(422, "invalid_content", self.store, data="不是字节")

    def test_size_limits(self):
        with self.settings(CUSTOMS_FILE_MAX_BYTES=10):
            self.assert_api_error(422, "file_too_large", self.store)
        self.assertFalse(FileVersion.objects.get(pk=self.version.pk).storage_path)

        self.store()
        with self.settings(CUSTOMS_IMAGE_MAX_BYTES=1):
            report = self.extract()
        self.assertEqual(report["counts"], {
            "available": 0, "absent": 4, "read_failed": 11, "skipped": 0,
        })
        self.assertEqual(
            ItemImage.objects.filter(item__version=self.version, storage_path="").count(), 15,
        )

    def test_tampered_storage_paths_are_404_and_not_leaked(self):
        self.store()
        FileVersion.objects.filter(pk=self.version.pk).update(storage_path="../secret.xlsx")
        self.assert_api_error(404, None, files.read_version_original, self.supplier, self.version.pk)
        FileVersion.objects.filter(pk=self.version.pk).update(storage_path="/etc/passwd")
        self.assert_api_error(404, None, files.read_version_original, self.supplier, self.version.pk)

        FileVersion.objects.filter(pk=self.version.pk).update(storage_path="")
        self.store()
        report = self.extract()
        self.assertEqual(report["counts"]["available"], 3)
        available = ItemImage.objects.get(item=self.items[2])
        ItemImage.objects.filter(pk=available.pk).update(storage_path="../../secret.png")
        self.assert_api_error(404, None, files.read_item_image, self.supplier, available.pk)
        self.assert_api_error(404, None, files.read_item_image_path, self.supplier, available.pk)

    def test_recorded_file_missing_on_disk_is_404(self):
        self.store()
        version = FileVersion.objects.get(pk=self.version.pk)
        storage.resolve_path(self.root, version.storage_path).unlink()
        self.assert_api_error(404, "file_content_missing", files.read_version_original,
                              self.supplier, version.pk)


if __name__ == "__main__":
    unittest.main()
