"""Synthetic inputs and real HTTP/ORM/filesystem helpers for P21 journeys.

Only the configured AI is mock. No business endpoint, parser, storage adapter,
submission service, decision service or notification service is replaced.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.accounts.models import Role
from apps.reviews.models import FileVersion, ImportJob, Task
from apps.runs.models import Run
from tests.p01.factories import make_user
from tests.p04.builders import DECLARATION_SHEET, build_package, sheet_of
from tests.p04.fixtures import png
from tests.p06.samples import sample


CONFIRMED_HS = "8544421900"  # Synthetic test values, never customs truth.
RECODED_HS = "8536901900"


def exportable_sample():
    """Add the style part omitted by the parser-only P06 synthetic fixture."""
    from adapters.files.export_issues import STYLES

    output = io.BytesIO()
    with ZipFile(io.BytesIO(sample())) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            if name != "xl/styles.xml":
                target.writestr(name, source.read(name))
        target.writestr("xl/styles.xml", STYLES)
    return output.getvalue()


def changed_members_workbook(order=(3, 1, 4)):
    """Delete member 2, add member 4 and reorder; both sheets agree explicitly."""
    package = build_package(
        declaration_rows=[
            ["SYNTHETIC-C", str(n), f"合成部件{n}", "0000000000",
             "用途：合成变更试验", "13%", None, "历史备注原文"]
            for n in order
        ],
        packing_rows=[
            ["SYNTHETIC-C", str(n), f"合成部件{n}", "", "SYNTHETIC-SPEC", "M", "", 1, "件"]
            for n in order
        ],
    )
    with tempfile.TemporaryDirectory(prefix="p21-synthetic-") as directory:
        return Path(package.build(Path(directory) / "members.xlsx")).read_bytes()


def amended_workbook(*, facts_only=False):
    """Keep row/member identity fields, changing facts or all supplied evidence."""
    declarations = [
        ["SYNTHETIC-C", str(n), f"合成部件{n}", "0000000000",
         "用途：合成试验", "13%", None, "历史备注原文"]
        for n in (1, 2, 3)
    ]
    declarations[0][4] = "用途：合成补充试验；规格：SYNTHETIC-SPEC-V2；材质：合成材料"
    if not facts_only:
        declarations[0][2] = "合成部件1修订品名"
        declarations[0][3] = "1111111111"
    package = build_package(declaration_rows=declarations, packing_rows=[
        ["SYNTHETIC-C", str(n), declarations[n - 1][2], "",
         "SYNTHETIC-SPEC-V2" if n == 1 else "SYNTHETIC-SPEC", "M", "", 1, "件"]
        for n in (1, 2, 3)
    ])
    sheet = sheet_of(package, DECLARATION_SHEET)
    sheet.formula("G2", '_xlfn.DISPIMG("ID_OK",1)')
    sheet.formula("G4", '_xlfn.DISPIMG("ID_MISSING",1)')
    package.cell_image("ID_OK", "xl/media/synthetic.png")
    package.image("xl/media/synthetic.png", png(b"\x10\x80\x20" if facts_only else b"\xc0\x20\x70"))
    with tempfile.TemporaryDirectory(prefix="p21-amended-") as directory:
        return Path(package.build(Path(directory) / "amended.xlsx")).read_bytes()


def grouped_workbook(*, merged, generation):
    """Reconstruct 1-3 as one declaration or three reordered declarations."""
    members = (0,) if merged else (3, 1, 2)
    package = build_package(declaration_rows=[
        ["SYNTHETIC-C", "1-3" if merged else str(n),
         "合成组合部件" if merged else f"合成部件{n}", "0000000000",
         f"用途：合成拆并试验{generation}", "13%", None, "历史备注原文"]
        for n in members
    ], packing_rows=[
        ["SYNTHETIC-C", str(n), f"合成部件{n}", "", "SYNTHETIC-SPEC", "M", "", n, "件"]
        for n in (1, 2, 3)
    ])
    sheet = sheet_of(package, DECLARATION_SHEET)
    for row, n in enumerate(members, 2):
        image_id = f"GROUP_{generation}_{n}"
        media = f"xl/media/group-{generation}-{n}.png"
        sheet.formula(f"G{row}", f'_xlfn.DISPIMG("{image_id}",1)')
        package.cell_image(image_id, media)
        package.image(media, png(bytes((generation, n, 128))))
    with tempfile.TemporaryDirectory(prefix="p21-grouped-") as directory:
        return Path(package.build(Path(directory) / "grouped.xlsx")).read_bytes()


class JourneyCase(TestCase):
    """Each test owns its accounts, database transaction and temporary files."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="p21-journey-")
        self.addCleanup(temporary.cleanup)
        self.file_root = Path(temporary.name) / "files"
        self.overrides = override_settings(
            CUSTOMS_FILE_ROOT=self.file_root,
            ENGINE_MODE="mock",
            EXECUTOR_MODE="inline",
            NOTIFICATION_MODE="mock",
            CUSTOMS_DEFAULT_EXPERT_USERNAME="p21-expert",
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        )
        self.overrides.enable()
        self.addCleanup(self.overrides.disable)
        self.supplier = make_user("p21-supplier", Role.SUPPLIER)
        self.operator = make_user("p21-operator", Role.OPERATOR)
        self.expert = make_user("p21-expert", Role.EXPERT)
        self.outsider = make_user("p21-outsider", Role.OPERATOR)
        self.sessions = {}
        for user in (self.supplier, self.operator, self.expert, self.outsider):
            client = Client()
            client.force_login(user)
            self.sessions[user.pk] = client

    def request(self, user, method, path, body=None, *, expected=200, key=None):
        kwargs = {"HTTP_IDEMPOTENCY_KEY": key or uuid.uuid4().hex}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type="application/json")
        with self.captureOnCommitCallbacks(execute=True):
            response = getattr(self.sessions[user.pk], method.lower())(path, **kwargs)
        detail = b"<streaming response>" if response.streaming else response.content[:1500]
        self.assertEqual(response.status_code, expected, (method, path, detail))
        return response

    def upload(self, *, data=None, task=None, previous=None, confirm_duplicate=False):
        data = sample() if data is None else data
        body = {"file": SimpleUploadedFile("P21-synthetic.xlsx", data)}
        path = "/api/v1/tasks"
        if task is not None:
            path = f"/api/v1/tasks/{task.pk}/versions"
            body.update(expected_current_version_id=str(previous.pk), confirm_duplicate=confirm_duplicate)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.sessions[self.supplier.pk].post(
                path, body, HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
            )
        self.assertEqual(response.status_code, 200, response.content[:1500])
        result = response.json()
        self.assertEqual(result["parse_status"], "succeeded")
        self.assertEqual(result["result_mode"], "mock")
        version = FileVersion.objects.get(pk=result["version_id"])
        task = Task.objects.get(pk=result["task_id"])
        run = Run.objects.get(pk=result["precheck_run_id"])
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertEqual(run.items.count(), version.items.count())
        return task, version, run

    def claim(self, task):
        return self.request(self.operator, "POST", f"/api/v1/tasks/{task.pk}/claim").json()

    def confirm_all(self, version, *, hs=CONFIRMED_HS):
        confirmations = []
        for item in version.items.order_by("source_row"):
            result = self.request(
                self.operator, "PATCH", f"/api/v1/versions/{version.pk}/items/{item.pk}/confirmation",
                {"confirmed_hs": hs, "reason": "P21 合成流程人工确认", "expected_revision": item.revision},
            ).json()
            confirmations.append({"item_id": str(item.pk), "confirmation_id": result["confirmation_id"]})
        return confirmations

    def submit(self, task, version, *, hs=CONFIRMED_HS):
        confirmations = self.confirm_all(version, hs=hs)
        task.refresh_from_db()
        result = self.request(
            self.operator, "POST", f"/api/v1/tasks/{task.pk}/submissions",
            {"version_id": str(version.pk), "revision": task.revision, "items": confirmations},
        ).json()
        return result

    def approve(self, submission, version, *, recode=False):
        rows = [{"item_id": str(item.pk), "final_hs": CONFIRMED_HS}
                for item in version.items.order_by("source_row")]
        if recode:
            rows[0].update(final_hs=RECODED_HS, change_reason="P21 合成专家改码原因")
        return self.request(
            self.expert, "POST", f"/api/v1/submissions/{submission['submission_id']}/decisions",
            {"action": "approve", "reason": "P21 合成人工审核", "items": rows},
        ).json()

    def card(self, version, item, user=None):
        return self.request(
            user or self.operator, "GET", f"/api/v1/versions/{version.pk}/items/{item.pk}",
        ).json()

    def original(self, version):
        response = self.request(self.supplier, "GET", f"/api/v1/files/{version.pk}")
        data = response.content
        self.assertEqual(hashlib.sha256(data).hexdigest(), ImportJob.objects.get(version=version).sha256)
        return data

    def export(self, version, run, *, kind="issues", submission_id=None, decision_id=None,
               excel2016_images=True):
        body = {"kind": kind, "run_id": str(run.pk), "excel2016_images": excel2016_images}
        if submission_id is not None:
            body["submission_id"] = submission_id
        if decision_id is not None:
            body["decision_id"] = decision_id
        response = self.request(
            self.operator, "POST", f"/api/v1/versions/{version.pk}/exports", body,
        )
        content = b"".join(response.streaming_content)
        # Django's test handler closes a fully consumed streaming iterator
        # while temporarily disconnecting close_old_connections. A second
        # explicit close would close PostgreSQL's surrounding TestCase
        # transaction (SQLite's in-memory connection masks that mistake).
        self.assertTrue(content.startswith(b"PK"))
        self.assertEqual(response["X-Export-Run"], str(run.pk))
        return content
