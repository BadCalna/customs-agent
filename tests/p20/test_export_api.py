from types import SimpleNamespace
from unittest.mock import patch
import hashlib

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import TestCase
from ninja.testing import TestClient

from apps.accounts.errors import ApiError, AuthenticationRequired
from apps.accounts.models import Role
from apps.reviews.api.exports import ExportIn, build_export, router
from apps.reviews.models import Item, Submission
from apps.runs.models import Run, RunItem
from tests.p01.factories import assign, make_task, make_user, make_version
from tests.p20.test_exports import sample


class ExportApiTests(TestCase):
    def setUp(self):
        self.supplier = make_user("p20-supplier", Role.SUPPLIER)
        self.operator = make_user("p20-operator", Role.OPERATOR)
        self.other = make_user("p20-other", Role.OPERATOR)
        self.expert = make_user("p20-expert", Role.EXPERT)
        self.task = make_task(self.supplier)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item = Item.objects.create(version=self.version, source_sheet="节点①申报要素", source_row=2, name="合成商品")
        self.run = Run.objects.create(task=self.task, version=self.version, kind="precheck", status="succeeded")
        self.row = RunItem.objects.create(run=self.run, item=self.item, status="succeeded",
                                          suggested_hs="0000000003", comparison_status="inconsistent",
                                          result={"comparison_reason": "合成测试依据", "mock_scenario": "normal"})
        self.source = sample()
        self.read = patch("apps.reviews.api.exports._read_original", return_value=SimpleNamespace(
            content=self.source, sha256=hashlib.sha256(self.source).hexdigest()))
        self.loader = self.read.start()
        self.addCleanup(self.read.stop)

    def payload(self, **kwargs):
        return ExportIn(kind="precheck", run_id=str(self.run.pk), **kwargs)

    def test_generate_file_without_advancing_workflow(self):
        artifact = build_export(self.operator, str(self.version.pk), self.payload())
        self.assertTrue(artifact.content.startswith(b"PK"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, "uploaded")
        self.assertEqual(self.version.storage_path, "")
        self.assertFalse(self.task.submissions.exists())

    def test_supplier_other_operator_and_anonymous_rejected_before_reading(self):
        for user, error in ((self.supplier, PermissionDenied), (self.other, Http404), (None, AuthenticationRequired)):
            with self.subTest(user=user), self.assertRaises(error):
                build_export(user, str(self.version.pk), self.payload())
        self.loader.assert_not_called()

    def test_wrong_run_version_and_mixed_submission_rejected(self):
        newer = make_version(self.task, self.supplier, number=2)
        for version, payload in ((newer, self.payload()), (self.version, self.payload(submission_id="wrong"))):
            with self.subTest(version=version.pk), self.assertRaises(ApiError) as caught:
                build_export(self.operator, str(version.pk), payload)
            self.assertEqual(caught.exception.code, "result_reference_mismatch")
        self.loader.assert_not_called()

    def test_queued_run_rejected(self):
        self.run.status = "running"
        self.run.save()
        with self.assertRaises(ApiError) as caught:
            build_export(self.operator, str(self.version.pk), self.payload())
        self.assertEqual(caught.exception.status, 409)

    def test_engine_configuration_cannot_relabel_old_mock_as_live(self):
        with self.settings(CUSTOMS_ENGINE="live"):
            artifact = build_export(self.operator, str(self.version.pk), self.payload())
        self.assertEqual(artifact.manifest["result_mode"], "mock")
        self.assertIn("测试结果", artifact.filename)

    def test_incomplete_run_cannot_borrow_another_execution(self):
        Item.objects.create(version=self.version, source_sheet="节点①申报要素", source_row=3, name="合成另一商品")
        with self.assertRaises(ApiError) as caught:
            build_export(self.operator, str(self.version.pk), self.payload())
        self.assertEqual(caught.exception.code, "incomplete_result_set")

    def test_stale_summary_and_unfinished_row_refuse_export(self):
        for result in ({"suggested_hs": "0000000004"}, {"confidence": .8}, {"comparison_status": "consistent"}):
            self.row.result = result
            self.row.save()
            with self.subTest(result=result), self.assertRaises(ApiError) as caught:
                build_export(self.operator, str(self.version.pk), self.payload())
            self.assertEqual(caught.exception.code, "result_value_conflict")
        self.row.status = "running"
        self.row.save()
        with self.assertRaises(ApiError) as caught:
            build_export(self.operator, str(self.version.pk), self.payload())
        self.assertEqual(caught.exception.code, "run_not_finished")

    def test_expert_requires_exact_submission_snapshot(self):
        submission = Submission.objects.create(task=self.task, version=self.version, revision=1, created_by=self.operator)
        run = Run.objects.create(task=self.task, version=self.version, kind="expert_check", submission=submission, status="succeeded")
        with self.assertRaises(ApiError) as caught:
            build_export(self.expert, str(self.version.pk), ExportIn(kind="issues", run_id=str(run.pk)))
        self.assertEqual(caught.exception.code, "result_reference_mismatch")

    def test_http_returns_attachment_with_mode_and_no_store(self):
        client = TestClient(router)
        response = client.post(f"/versions/{self.version.pk}/exports", json={"kind": "issues", "run_id": str(self.run.pk)},
                               user=self.operator)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Result-Mode"], "mock")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_malformed_source_workbook_returns_422_instead_of_500(self):
        import io
        from zipfile import ZipFile
        from adapters.files.export_workbook import _zip_replace
        from adapters.files.ooxml import Workbook
        from tests.p20.test_exports import sample

        base = sample(headers=("采购合同号", "合同项号", "品名", "海关编码", "申报要素"))
        sheet_part = Workbook(base).find_sheet("申报要素").part
        sheet_raw = Workbook(base).media_bytes(sheet_part).decode()
        out = io.BytesIO()
        with ZipFile(io.BytesIO(base)) as src, ZipFile(out, "w") as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename))
            dst.writestr(
                "xl/worksheets/_rels/sheet1.xml.rels",
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"/>'
                b'</Relationships>',
            )
        bad_cases = (
            (_zip_replace(base, {sheet_part: sheet_raw.replace("</worksheet>", "<mergeCells><mergeCell/></mergeCells></worksheet>", 1).encode()}),
             "invalid_cell_reference"),
            (out.getvalue(), "source_workbook_invalid"),
        )
        self.client.force_login(self.operator)
        for bad_bytes, expected_code in bad_cases:
            self.loader.return_value = SimpleNamespace(
                content=bad_bytes, sha256=hashlib.sha256(bad_bytes).hexdigest(),
            )
            response = self.client.post(
                f"/api/v1/versions/{self.version.pk}/exports",
                data={"kind": "precheck", "run_id": str(self.run.pk)},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], expected_code)

    def test_null_issues_in_run_item_result_does_not_raise_500(self):
        self.row.result = {"issues": None, "comparison_reason": None, "mock_scenario": "normal"}
        self.row.save()
        artifact = build_export(self.operator, str(self.version.pk), self.payload())
        self.assertTrue(artifact.content.startswith(b"PK"))
