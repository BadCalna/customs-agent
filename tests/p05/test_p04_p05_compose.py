"""P04→P05 组合契约：P04 取申报行与图片列，P05 系统入口落 ItemImage。

这条用例就是给 P06 的编排契约：先用 ``packing_links.map_workbook`` 得到申报行、
图片列与商品字段，再在已经建好版本/商品行的前提下调用
``files.extract_version_images_for_version``（不接收 user，调用方已完成权限校验）。
"""

import tempfile
import unittest
from pathlib import Path

from django.test import TestCase, override_settings

from adapters.files.packing_links import map_workbook
from adapters.files.ooxml import Workbook
from apps.accounts.models import Role
from apps.reviews.models import Item, ItemImage
from apps.reviews.services import files
from tests.p01.factories import make_task, make_user, make_version
from tests.p05.workbooks import compose_package, package_bytes


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class P04P05ComposeTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "files"
        settings_override = override_settings(CUSTOMS_FILE_ROOT=str(self.root))
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.supplier = make_user("p05_compose_supplier", Role.SUPPLIER)
        self.task = make_task(self.supplier)
        self.workbook_bytes = package_bytes(compose_package())
        self.mapping = map_workbook(Workbook(self.workbook_bytes))
        self.sheet = self.mapping.declaration.layout.sheet
        self.image_column = self.mapping.declaration.layout.columns["image"]
        self.links = {link.row: link for link in self.mapping.links}

        self.version = make_version(self.task, self.supplier)
        self.items = self._create_items(self.version)
        files.store_version_original(
            self.supplier, self.version.pk, filename="合成模板.xlsx",
            data=self.workbook_bytes,
        )

    def _create_items(self, version):
        return {
            row: Item.objects.create(version=version, **link.item_values())
            for row, link in self.links.items()
        }

    def extract(self, version=None):
        return files.extract_version_images_for_version(
            version or self.version,
            sheet_name=self.sheet,
            rows=sorted(self.links),
            column=self.image_column,
        )

    def test_p04_gives_rows_and_image_column_for_p05(self):
        self.assertEqual(self.image_column, "E")
        self.assertEqual(sorted(self.links), [2, 3, 4, 5, 6])
        floating_only = self.mapping.declaration.record(6)
        self.assertIsNotNone(floating_only)
        self.assertEqual(floating_only.floating_image_cells, ("E6",))
        # 第 4 行有其它字段、附图格是浮图；第 6 行只有单格浮图。
        self.assertEqual(self.mapping.declaration.record(4).floating_image_cells, ("E4",))

    def test_every_declaration_row_gets_the_right_item_image(self):
        report = self.extract()
        self.assertEqual(report["counts"], {
            "available": 4, "absent": 1, "read_failed": 0, "skipped": 0,
        })
        self.assertEqual(ItemImage.objects.filter(item__version=self.version).count(), 5)

        rows = {entry["source_row"]: entry for entry in report["rows"]}
        self.assertEqual(rows[2]["status"], ItemImage.Status.AVAILABLE)
        self.assertEqual(rows[2]["id_source"], "formula")
        self.assertEqual(rows[3]["id_source"], "shared_formula")
        for row in (4, 6):
            self.assertEqual(rows[row]["status"], ItemImage.Status.AVAILABLE)
            self.assertEqual(rows[row]["id_source"], "single_cell_floating_anchor")
            record = ItemImage.objects.get(item=self.items[row], source_cell=f"E{row}")
            self.assertTrue(record.storage_path)
        self.assertEqual(rows[5]["status"], ItemImage.Status.ABSENT)
        self.assertEqual(ItemImage.objects.get(item=self.items[5]).storage_path, "")
        for row, item in self.items.items():
            with self.subTest(row=row):
                self.assertTrue(item.images.exists())

    def test_repeat_extraction_is_idempotent(self):
        self.extract()
        first = sorted(ItemImage.objects.filter(item__version=self.version).values_list(
            "pk", "item__source_row", "status", "storage_path"))
        second = self.extract()
        self.assertEqual(second["counts"]["available"], 4)
        self.assertEqual(ItemImage.objects.filter(item__version=self.version).count(), 5)
        self.assertEqual(
            sorted(ItemImage.objects.filter(item__version=self.version).values_list(
                "pk", "item__source_row", "status", "storage_path")),
            first,
        )

    def test_v2_reparse_does_not_touch_v1(self):
        self.extract()
        old_images = list(
            ItemImage.objects.filter(item__version=self.version).order_by("item_id")
            .values_list("item_id", "status", "storage_path")
        )
        new_version = make_version(self.task, self.supplier, number=2)
        new_items = self._create_items(new_version)
        files.store_version_original(
            self.supplier, new_version.pk, filename="合成模板V2.xlsx",
            data=self.workbook_bytes,
        )
        report = self.extract(version=new_version)
        self.assertEqual(report["counts"]["available"], 4)

        self.assertEqual(
            list(ItemImage.objects.filter(item__version=self.version).order_by("item_id")
                 .values_list("item_id", "status", "storage_path")),
            old_images,
        )
        self.assertEqual(ItemImage.objects.filter(item__version=new_version).count(), 5)
        self.assertEqual(
            set(ItemImage.objects.filter(item__version=new_version).values_list("item_id", flat=True)),
            {item.pk for item in new_items.values()},
        )
        for image in ItemImage.objects.filter(
                item__version=new_version, status=ItemImage.Status.AVAILABLE):
            self.assertIn(str(new_version.pk), image.storage_path)


if __name__ == "__main__":
    unittest.main()
