"""Supplier format regression through real HTTP upload, parsing and worker.

Only the external AI HTTP transport is mocked; upload/import/queue/comparison
and persisted/API results use production code. All workbook data is synthetic.
"""
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import Item
from apps.runs.jobs import perform_work
from apps.runs.models import Run
from tests.p01.factories import make_user
from tests.p04.builders import build_package


@override_settings(EXECUTOR_MODE="rq", CUSTOMS_RETRY_BACKOFF_SECONDS=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SupplierHsUploadWorkerTests(TestCase):
    def workbook(self, root):
        # Include malformed non-ASCII/letter/overlong codes as well as the three
        # acceptance cases. Text cells preserve leading zeroes and whitespace.
        self.raw_codes = ["123456789", "", "0123456789", " 0123456789 ",
                          "12345678901", "123456789A", "１２３４５６７８９０", "   ", "01234567"]
        package = build_package(
            declaration_rows=[["SYNTHETIC", str(i), f"合成部件{i}", raw,
                               "用途：合成测试", "13%"]
                              for i, raw in enumerate(self.raw_codes, 1)],
            packing_rows=[["SYNTHETIC", str(i), f"合成部件{i}", "", "TEST", "", "", 1, "件"]
                          for i in range(1, len(self.raw_codes) + 1)],
        )
        return Path(package.build(root / "synthetic.xlsx")).read_bytes()

    def upload_and_run(self, expected_ten_digit, mode):
        with tempfile.TemporaryDirectory() as temp, override_settings(CUSTOMS_FILE_ROOT=Path(temp) / "files"):
            data = self.workbook(Path(temp))
            supplier = make_user(f"hs-{mode}", Role.SUPPLIER)
            self.client.force_login(supplier)
            with patch("apps.runs.dispatch.safe_dispatch", return_value=0):
                response = self.client.post("/api/v1/tasks", {
                    "file": SimpleUploadedFile("synthetic.xlsx", data,
                                               content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                }, HTTP_IDEMPOTENCY_KEY=f"hs-format-{mode}")
            self.assertEqual(response.status_code, 200, response.content)
            result = response.json()
            self.assertEqual(result["parse_status"], "staged")
            run = Run.objects.get(pk=result["precheck_run_id"])
            perform_work(str(run.pk))
            run.refresh_from_db()
            self.assertEqual(run.status, "succeeded")
            self.assertEqual(run.engine_snapshot["rules_version"], "d4-0.3")
            items = list(Item.objects.filter(version_id=result["version_id"]).order_by("source_row"))
            self.assertEqual([item.supplier_hs_raw for item in items], self.raw_codes)
            expected = ["supplier_invalid", "supplier_missing", expected_ten_digit, expected_ten_digit,
                        "supplier_invalid", "supplier_invalid", "supplier_invalid", "supplier_missing",
                        "needs_confirmation"]
            for item, status in zip(items, expected):
                with self.subTest(raw=item.supplier_hs_raw, mode=mode):
                    row = run.items.get(item=item)
                    self.assertEqual(row.result["rules_version"], run.engine_snapshot["rules_version"])
                    self.assertEqual(row.comparison_status, status)
                    if status == "supplier_invalid":
                        self.assertIn("SUPPLIER_HS_INVALID", [issue["code"] for issue in row.result["issues"]])
                    elif status == "supplier_missing":
                        self.assertIn("SUPPLIER_HS_MISSING", [issue["code"] for issue in row.result["issues"]])
                    elif len(item.supplier_hs_raw.strip()) == 8:
                        self.assertIn("SUPPLIER_HS_SUBHEADING_ONLY", [issue["code"] for issue in row.result["issues"]])
                    elif mode == "live":
                        self.assertIn("EVIDENCE_PENDING", [issue["code"] for issue in row.result["issues"]])
            detail = self.client.get(f"/api/v1/runs/{run.pk}")
            self.assertEqual(detail.status_code, 200, detail.content)
            by_item = {row["item_id"]: row for row in detail.json()["items"]}
            self.assertEqual([by_item[str(item.pk)]["comparison_status"] for item in items], expected)

    @override_settings(ENGINE_MODE="mock")
    def test_mock_upload_worker_checks_format_without_scenario_flags(self):
        self.upload_and_run("inconsistent", "mock")

    @override_settings(ENGINE_MODE="openai", CUSTOMS_AI_API_BASE="https://ai.example.invalid/v1",
                       CUSTOMS_AI_API_KEY="synthetic-key", CUSTOMS_AI_MODEL="synthetic-model")
    def test_live_upload_worker_prioritizes_format_before_unverified_evidence(self):
        content = {"suggested_hs": "0123456789", "candidates": [{"hs": "0123456789"}], "gaps": []}
        def response(*args, **kwargs):
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode())
        with patch("adapters.engines.openai.request.urlopen", side_effect=response) as transport:
            self.upload_and_run("needs_confirmation", "live")
        self.assertEqual(transport.call_count, len(self.raw_codes))
