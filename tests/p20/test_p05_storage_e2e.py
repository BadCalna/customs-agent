"""P20 真实 P05 存储端到端：真实模板 multipart 上传 → inline mock 预审 → 真实读原件导出。

这条链路刻意**不 patch** ``apps.reviews.api.exports._read_original``：导出必须按
``FileVersion.storage_path`` 从 ``settings.CUSTOMS_FILE_ROOT`` 读原件；用例同时验证
导出前后原件字节、``sha256`` 与存储路径不变，并验证原件被移走后导出会明确失败
（证明读的是真实存储，而不是测试里事先放在内存里的字节）。

使用的是仓库内真实业务模板（``data/import-basis/2026-11-17/historical-success``），
不是为导出伪造的合成工作簿。``H01`` 解析无阻断问题；``H02`` 因 ``reversed_item_range``
被 P04/P06 拒绝，无法进入 P05 存储，故不作为本用例输入。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from urllib.parse import unquote

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role
from apps.notifications.models import Message
from apps.reviews.models import (
    AuditEvent, Confirmation, Decision, FileVersion, Issue, Item, Submission, Task,
)
from apps.reviews.services.claims import claim_task
from apps.runs.models import Run
from tests.p01.factories import make_user

BASE_DIR = Path(settings.BASE_DIR)
TEMPLATE = BASE_DIR / "data" / "import-basis" / "2026-11-17" / "historical-success" / "H01-20251017-BJXFS-XBY.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DECLARATION_SHEET = "申报要素"
# 独立补料清单的八列表头，顺序与 adapters/files/export_issues.HEADERS 一致。
ISSUE_HEADERS = ("合同号", "项号", "品名", "原位置", "问题", "需补充信息", "建议处理人", "反馈")
ISSUE_SHEET = "补料清单"

#: 用 openpyxl（独立第三方实现）另开一次导出文件，不复用仓库自己的 OOXML 读取器。
_INSPECT = """
import json, sys, openpyxl
book = openpyxl.load_workbook(sys.argv[1], data_only=True)
values = {}
for name in sys.argv[2:]:
    sheet = book[name]
    values[name] = [["" if cell is None else str(cell) for cell in row]
                    for row in sheet.iter_rows(values_only=True)]
print(json.dumps({"sheets": book.sheetnames, "values": values}, ensure_ascii=False))
"""


def openpyxl_python() -> str | None:
    """找一个能 ``import openpyxl`` 的解释器；仓库运行环境本身不带这个依赖。"""
    seen: list[str] = []
    for candidate in (os.environ.get("CUSTOMS_OPENPYXL_PYTHON"), sys.executable,
                      shutil.which("python3"), shutil.which("python")):
        if not candidate or candidate in seen:
            continue
        seen.append(candidate)
        try:
            probe = subprocess.run([candidate, "-c", "import openpyxl"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def inspect_with_openpyxl(interpreter: str, content: bytes, sheets) -> dict:
    with tempfile.TemporaryDirectory(prefix="p20-openpyxl-") as directory:
        path = Path(directory) / "export.xlsx"
        path.write_bytes(content)
        result = subprocess.run([interpreter, "-c", _INSPECT, str(path), *sheets],
                                capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise AssertionError("openpyxl 无法打开导出文件：" + result.stdout + result.stderr)
    return json.loads(result.stdout)


def disposition_filename(header: str) -> str:
    """解析 Content-Disposition，兼容 RFC 5987 的 ``filename*`` 与普通 ``filename``。"""
    for part in header.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        value = value.strip()
        if key.lower() == "filename*":
            return unquote(value.split("''", 1)[-1].strip('"'))
        if key.lower() == "filename":
            return value.strip('"')
    return ""


class RealStorageExportTests(TestCase):
    """一个任务只上传一次：上传、认领、两种导出、副作用与原件缺失都在同一批真实数据上验证。"""

    @classmethod
    def setUpClass(cls):
        # 先落好真实存储根与 openpyxl 解释器：父类 setUpClass 里就会执行 setUpTestData（含真实上传）。
        cls._tmp = tempfile.TemporaryDirectory(prefix="p20-e2e-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.root = Path(cls._tmp.name) / "files"
        cls._settings = override_settings(CUSTOMS_FILE_ROOT=cls.root)
        cls._settings.enable()
        cls.addClassCleanup(cls._settings.disable)
        cls._openpyxl = openpyxl_python()
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.supplier = make_user("p20-e2e-supplier", Role.SUPPLIER, "供应商甲")
        cls.operator = make_user("p20-e2e-operator", Role.OPERATOR, "业务员甲")
        cls.source = TEMPLATE.read_bytes()
        cls.source_sha256 = hashlib.sha256(cls.source).hexdigest()
        supplier = Client()
        supplier.force_login(cls.supplier)
        response = supplier.post("/api/v1/tasks", {"file": SimpleUploadedFile(TEMPLATE.name, cls.source)},
                                 HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        assert response.status_code == 200, response.content[:800]
        cls.upload = response.json()
        cls.task = Task.objects.get(pk=cls.upload["task_id"])
        cls.version = FileVersion.objects.get(pk=cls.upload["version_id"])
        cls.precheck_run = Run.objects.get(pk=cls.upload["precheck_run_id"])
        claim_task(cls.operator, str(cls.task.pk), "p20-e2e-claim")
        cls.operator_client = Client()
        cls.operator_client.force_login(cls.operator)
        cls.stored = cls.root / cls.version.storage_path

    # ------------------------------------------------------------------ 工具
    def export(self, kind: str, *, run_id: str | None = None, client: Client | None = None):
        payload = {"kind": kind, "run_id": run_id or str(self.precheck_run.pk)}
        return (client or self.operator_client).post(
            f"/api/v1/versions/{self.version.pk}/exports", data=json.dumps(payload),
            content_type="application/json")

    def require_openpyxl(self) -> None:
        """独立解析断言需要第三方 xlsx 实现；缺失时显式失败，不把整条用例降级为 skip。"""
        if self._openpyxl is None:
            self.fail("导出内容的独立解析断言需要能 import openpyxl 的解释器"
                      "（可用 CUSTOMS_OPENPYXL_PYTHON 指定）")

    def storage_files(self) -> set[str]:
        return {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}

    def assert_original_untouched(self, before_files: set[str], before_path: str) -> None:
        self.assertEqual(self.storage_files(), before_files, "导出不得向受控存储写入或删除文件")
        self.assertEqual(self.version.storage_path, before_path)
        self.assertEqual(hashlib.sha256(self.stored.read_bytes()).hexdigest(), self.source_sha256,
                         "导出前后原件 sha256 必须不变")

    # -------------------------------------------------------------- 前置事实
    def test_upload_wrote_template_bytes_to_real_storage(self):
        self.assertTrue(self.version.storage_path, "上传后必须写入 P05 受控存储路径")
        self.assertFalse(self.version.storage_path.startswith("/"), "存储路径必须是受控相对路径")
        self.assertTrue(self.stored.is_file(), f"受控存储中缺少原件：{self.stored}")
        self.assertEqual(hashlib.sha256(self.stored.read_bytes()).hexdigest(), self.source_sha256)
        self.assertEqual(self.upload["parse_status"], "succeeded")
        self.assertEqual(self.upload["precheck_status"], Run.Status.SUCCEEDED)
        self.assertEqual(self.precheck_run.kind, Run.Kind.PRECHECK)
        self.assertEqual(self.precheck_run.version_id, self.version.pk)
        self.assertEqual(self.precheck_run.status, Run.Status.SUCCEEDED)
        self.assertEqual(self.version.items.count(), len(self.precheck_run.items.all()))

    def test_import_detail_exposes_the_run_reference_the_page_uses(self):
        # 页面导出按钮的引用来源：GET /versions/{id}/import 的 precheck_run_id / precheck_status。
        response = self.operator_client.get(f"/api/v1/versions/{self.version.pk}/import")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["precheck_run_id"], str(self.precheck_run.pk))
        self.assertEqual(body["precheck_status"], Run.Status.SUCCEEDED)
        self.assertEqual(body["version_id"], str(self.version.pk))
        self.assertEqual(body["result_mode"], "mock")

    def test_export_rejects_run_belonging_to_another_version(self):
        """run 引用必须属于请求版本：用 V2 的预审执行去导 V1，必须 422 而不是拿到别版结果。

        这条把「结果集属于请求版本」的校验放进真实存储链路，而不是只靠合成替身
        （`tests/p20/test_export_api.py` 覆盖的那层）。
        """
        supplier = Client()
        supplier.force_login(self.supplier)
        second = supplier.post(
            f"/api/v1/tasks/{self.task.pk}/versions",
            {"file": SimpleUploadedFile(TEMPLATE.name, self.source),
             "expected_current_version_id": str(self.version.pk), "confirm_duplicate": "true"},
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(second.status_code, 200, second.content[:800])
        other = second.json()
        self.assertNotEqual(other["version_id"], str(self.version.pk), "补料应产生新版本")

        response = self.operator_client.post(
            f"/api/v1/versions/{self.version.pk}/exports",
            data=json.dumps({"kind": "issues", "run_id": other["precheck_run_id"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 422, response.content[:400])
        self.assertEqual(response.json()["error"]["code"], "result_reference_mismatch")
        # 原件仍在：被拒的请求不产生任何持久化副作用。
        self.assertEqual(hashlib.sha256(self.stored.read_bytes()).hexdigest(), self.source_sha256)

    def test_export_refuses_when_stored_original_is_missing(self):
        """原件被移走后导出必须失败：证明导出读的是 P05 存储，而不是任何内存副本。"""
        backup = Path(self._tmp.name) / "original.bak"
        shutil.move(self.stored, backup)
        try:
            response = self.export("issues")
        finally:
            backup.replace(self.stored)
        self.assertEqual(response.status_code, 404, response.content[:400])
        error = response.json()["error"]
        self.assertEqual(error["code"], "file_content_missing")
        self.assertEqual(hashlib.sha256(self.stored.read_bytes()).hexdigest(), self.source_sha256)

    # ---------------------------------------------------------- 预审结果导出
    def test_precheck_export_returns_xlsx_with_run_headers_and_keeps_original(self):
        before_files = self.storage_files()
        before_path = self.version.storage_path
        response = self.export("precheck")
        content = b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 200, content[:800])

        # 响应头：xlsx 二进制 + 下载名 + 本次执行引用 + 结果来源。
        self.assertEqual(response["Content-Type"], XLSX_MIME)
        self.assertIn("attachment", response["Content-Disposition"].lower())
        self.assertEqual(disposition_filename(response["Content-Disposition"]), "预审结果-测试结果.xlsx")
        self.assertEqual(response["X-Export-Run"], str(self.precheck_run.pk))
        self.assertEqual(response["X-Result-Mode"], "mock")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(content.startswith(b"PK"), "导出内容必须是 ZIP 容器（xlsx）")
        self.assert_original_untouched(before_files, before_path)

    def test_precheck_export_xlsx_contents_keep_template_columns(self):
        """导出文件的表头保真与结果列写入，用独立第三方实现解析（openpyxl）。"""
        self.require_openpyxl()
        items = list(Item.objects.filter(version=self.version).order_by("source_row"))
        response = self.export("precheck")
        content = b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 200, content[:800])
        grid = inspect_with_openpyxl(self._openpyxl, content, (DECLARATION_SHEET,))["values"][DECLARATION_SHEET]
        # 表头保真：原有的合同/品名/海关编码列不动，只在 L 之后新增结果两列。
        self.assertEqual(grid[0][0], "供应商")
        self.assertEqual(grid[0][1], "采购合同号")
        self.assertEqual(grid[0][5], "品名\n（与合同保持一致）")
        self.assertTrue(grid[0][11].startswith("海关编码"))
        self.assertEqual(grid[0][12], "AI推荐HScode")
        self.assertEqual(grid[0][13], "预审结论")
        # 结果列按本次执行的 RunItem 写入（M=推荐编码，N=预审结论），且标注 MOCK 测试结果。
        for item, row in zip(items, self.precheck_run.items.select_related("item").order_by("item__source_row")):
            self.assertEqual(row.item_id, item.pk)
            line = grid[item.source_row - 1]
            self.assertEqual(line[12], row.suggested_hs, f"第{item.source_row}行推荐编码应为本次执行结果")
            self.assertIn("测试结果", line[13])
            self.assertIn("推荐码与原码不一致", line[13])

    # ------------------------------------------------------ 独立补料清单导出
    def test_issues_export_has_eight_column_sheet_and_no_workflow_side_effects(self):
        before_files = self.storage_files()
        before_path = self.version.storage_path
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.AWAITING_CONFIRMATION)
        audit_before = AuditEvent.objects.count()
        response = self.export("issues")
        content = b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 200, content[:800])
        self.assertEqual(response["Content-Type"], XLSX_MIME)
        self.assertEqual(disposition_filename(response["Content-Disposition"]), "独立补料清单-测试结果.xlsx")
        self.assertEqual(response["X-Export-Run"], str(self.precheck_run.pk))
        self.assertEqual(response["X-Result-Mode"], "mock")

        # 导出不产生退回、提交、决定、确认、站内消息或审计事件，也不改阶段与存储。
        self.assertEqual((Issue.objects.count(), Submission.objects.count(), Decision.objects.count(),
                          Confirmation.objects.count(), Message.objects.count()), (0, 0, 0, 0, 0))
        self.assertEqual(AuditEvent.objects.count(), audit_before)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.AWAITING_CONFIRMATION)
        self.assert_original_untouched(before_files, before_path)

    def test_issues_export_xlsx_has_eight_column_sheet(self):
        """八列补料清单的结构与逐行来源位置，用独立第三方实现解析（openpyxl）。"""
        self.require_openpyxl()
        response = self.export("issues")
        content = b"".join(response.streaming_content)
        self.assertEqual(response.status_code, 200, content[:800])
        inspected = inspect_with_openpyxl(self._openpyxl, content, (ISSUE_SHEET,))
        self.assertEqual(inspected["sheets"], [ISSUE_SHEET])
        grid = inspected["values"][ISSUE_SHEET]
        self.assertIn("独立补料与核对清单", grid[0][0])
        self.assertIn("测试结果", grid[0][0])
        self.assertEqual(tuple(grid[3][:8]), ISSUE_HEADERS, "八列清单表头必须逐列一致")
        body = [row for row in grid[4:] if any(cell for cell in row[:8])]
        expected = list(Item.objects.filter(version=self.version).order_by("source_sheet", "source_row")
                        .values_list("source_sheet", "source_row", "name"))
        self.assertEqual(len(body), len(expected), "每个商品一行问题记录")
        for row, (sheet, source_row, name) in zip(body, expected):
            self.assertEqual(row[2], name.strip())
            self.assertEqual(row[3], f"{sheet}!第{source_row}行")
            self.assertTrue(row[0], "合同号不能为空")
            self.assertTrue(row[1], "项号不能为空")
            self.assertTrue(row[4], "问题列不能为空")
            self.assertIn(row[6], ("供应商", "业务员", "物流/专家", "待人工指定"))
            self.assertEqual(row[7], "", "反馈列由人工填写，导出不预置内容")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class RealStorageImageTemplateExportTests(TestCase):
    """第二份真实模板（多页签 + 25 张 DISPIMG 附图）：原件与附图都真实入库后再导出。

    ``H03`` 的申报页叫「节点①申报要素」，图片列是 WPS 的 ``DISPIMG`` 公式；导出要读的是
    P05 存储里的原件，而不是上传时留在内存里的字节。``H02`` 因 ``reversed_item_range``
    被 P04/P06 阻断，无法进入本链路。
    """

    template = BASE_DIR / "data" / "import-basis" / "2026-11-17" / "historical-success" / "H03-SZCX-multi-contract.xlsx"
    declaration_sheet = "节点①申报要素"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="p20-e2e-img-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.root = Path(cls._tmp.name) / "files"
        cls._settings = override_settings(CUSTOMS_FILE_ROOT=cls.root)
        cls._settings.enable()
        cls.addClassCleanup(cls._settings.disable)
        cls._openpyxl = openpyxl_python()
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.supplier = make_user("p20-e2e-img-supplier", Role.SUPPLIER, "供应商乙")
        cls.operator = make_user("p20-e2e-img-operator", Role.OPERATOR, "业务员乙")
        cls.source = cls.template.read_bytes()
        cls.source_sha256 = hashlib.sha256(cls.source).hexdigest()
        client = Client()
        client.force_login(cls.supplier)
        response = client.post("/api/v1/tasks", {"file": SimpleUploadedFile(cls.template.name, cls.source)},
                               HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        assert response.status_code == 200, response.content[:800]
        cls.upload = response.json()
        cls.version = FileVersion.objects.get(pk=cls.upload["version_id"])
        cls.precheck_run = Run.objects.get(pk=cls.upload["precheck_run_id"])
        claim_task(cls.operator, cls.upload["task_id"], "p20-e2e-img-claim")
        cls.operator_client = Client()  # 不要叫 client：会覆盖 TestCase 自带的未登录 client
        cls.operator_client.force_login(cls.operator)

    def export(self, kind: str):
        payload = {"kind": kind, "run_id": self.upload["precheck_run_id"]}
        return self.operator_client.post(f"/api/v1/versions/{self.version.pk}/exports",
                                data=json.dumps(payload), content_type="application/json")

    def require_openpyxl(self) -> None:
        """独立解析断言需要第三方 xlsx 实现；缺失时显式失败，不把整条用例降级为 skip。"""
        if self._openpyxl is None:
            self.fail("导出内容的独立解析断言需要能 import openpyxl 的解释器"
                      "（可用 CUSTOMS_OPENPYXL_PYTHON 指定）")

    def test_upload_stores_original_and_every_dispimg_image(self):
        from apps.reviews.models import ItemImage

        stored = self.root / self.version.storage_path
        self.assertTrue(stored.is_file())
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), self.source_sha256)
        self.assertEqual(self.upload["item_count"], 25)
        self.assertEqual(self.upload["image_counts"]["available"], 25)
        images = ItemImage.objects.filter(item__version=self.version, status=ItemImage.Status.AVAILABLE)
        self.assertEqual(images.count(), 25)
        for image in images:
            path = self.root / image.storage_path
            self.assertTrue(path.is_file(), f"附图未落盘：{image.storage_path}")
            self.assertGreater(path.stat().st_size, 0)
        # 附图按内容 sha256 去重（H03 里有两行共用同一张图），只锁“都在受控目录内”。
        for path in (self.root).rglob("*"):
            if path.is_file() and path.suffix != ".lock":
                self.assertIn(f"tasks/{self.version.task_id}/versions/{self.version.pk}/",
                              str(path.relative_to(self.root)), f"存储里出现意外文件：{path}")

    def test_both_exports_read_the_stored_original_of_a_many_sheet_workbook(self):
        from adapters.files.ooxml import Workbook

        before_files = {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}
        precheck = self.export("precheck")
        self.assertEqual(precheck.status_code, 200, getattr(precheck, "content", b"")[:800])
        precheck_body = b"".join(precheck.streaming_content)
        self.assertEqual(precheck["X-Export-Run"], self.upload["precheck_run_id"])
        self.assertEqual(precheck["X-Result-Mode"], "mock")
        issues = self.export("issues")
        self.assertEqual(issues.status_code, 200, getattr(issues, "content", b"")[:800])
        issues_body = b"".join(issues.streaming_content)
        self.assert_untouched(before_files)

    def test_many_sheet_export_xlsx_keeps_all_sheets_and_order(self):
        """多页签工作簿导出后页签顺序与结果列写入，用独立第三方实现解析（openpyxl）。"""
        from adapters.files.ooxml import Workbook

        self.require_openpyxl()
        precheck = self.export("precheck")
        self.assertEqual(precheck.status_code, 200, getattr(precheck, "content", b"")[:800])
        precheck_body = b"".join(precheck.streaming_content)
        issues = self.export("issues")
        self.assertEqual(issues.status_code, 200, getattr(issues, "content", b"")[:800])
        issues_body = b"".join(issues.streaming_content)
        inspected = inspect_with_openpyxl(self._openpyxl, precheck_body, (self.declaration_sheet,))
        self.assertEqual(inspected["sheets"], [sheet.name for sheet in Workbook(self.source).sheets],
                         "导出必须保留全部页签与顺序")
        grid = inspected["values"][self.declaration_sheet]
        for item, row in zip(Item.objects.filter(version=self.version).order_by("source_row"),
                             self.precheck_run.items.select_related("item").order_by("item__source_row")):
            self.assertEqual(grid[item.source_row - 1][12], row.suggested_hs)
            self.assertIn("测试结果", grid[item.source_row - 1][13])
        issues_grid = inspect_with_openpyxl(self._openpyxl, issues_body, (ISSUE_SHEET,))["values"][ISSUE_SHEET]
        self.assertEqual(tuple(issues_grid[3][:8]), ISSUE_HEADERS)
        self.assertEqual(len([row for row in issues_grid[4:] if any(cell for cell in row[:8])]), 25)

    def assert_untouched(self, before_files: set[str]) -> None:
        after_files = {str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after_files, before_files, "导出不得写入或删除受控存储文件")
        stored = self.root / self.version.storage_path
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), self.source_sha256)
