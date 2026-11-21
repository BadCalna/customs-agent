"""P12 数据复制回归：检查真实序列化、模型行、受控附件和 SQLite 快照。

此文件由主控在 SQLite/PostgreSQL 测试库执行；编写期间只做 py_compile。
可空外键孤儿用例依赖两种后端的延迟外键约束，并在检查结束前恢复引用。
"""
import datetime as dt
import hashlib
import io
import json
import sqlite3
import sys
import uuid
from contextlib import closing, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.apps import apps
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core import serializers
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.reviews.models import FileVersion, ImportJob, Issue, Item, ItemImage, Task, TaskClaim
from apps.runs.models import Run, RunItem
from scripts.database import dumpdata_full, model_inventory, snapshot_sqlite, verify_copy

from .factories import build_submitted_task
from tests.platform_files import create_directory_link


MOMENT = dt.datetime(2026, 11, 15, 7, 30, 45, 123456, tzinfo=dt.timezone.utc)
NESTED_JSON = {
    "候选": [{"hs": "8544421900", "sources": ["税则", "历史 ✅"], "evidence": None}],
    "query": {"ok": True, "elapsed": 0.9300000000000001, "notes": []},
}
GENERATED_LABELS = {"contenttypes.contenttype", "auth.permission"}


class ModelInventoryTests(SimpleTestCase):
    def test_migration_inventory_keeps_all_managed_concrete_models(self):
        expected = {
            model for model in apps.get_models()
            if model._meta.managed and not model._meta.proxy
            and model._meta.label_lower not in GENERATED_LABELS
        }
        actual = model_inventory.migration_models()
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(set(actual), expected)
        for model in (ImportJob, TaskClaim, LogEntry, Session, Group, User):
            with self.subTest(model=model._meta.label):
                self.assertIn(model, actual)
        self.assertNotIn(Permission, actual)
        self.assertNotIn(ContentType, actual)

    def test_verification_inventory_also_keeps_auto_created_m2m_tables(self):
        expected = {
            model for model in apps.get_models(include_auto_created=True)
            if model._meta.managed and not model._meta.proxy
            and model._meta.label_lower not in GENERATED_LABELS
        }
        actual = model_inventory.verification_models()
        self.assertIsInstance(actual, list)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(set(actual), expected)
        for through in (User.groups.through, User.user_permissions.through, Group.permissions.through):
            with self.subTest(table=through._meta.db_table):
                self.assertIn(through, actual)
                self.assertNotIn(through, model_inventory.migration_models())

    def test_source_schema_rejects_populated_unknown_table_but_allows_empty_one(self):
        with closing(sqlite3.connect(":memory:")) as source:
            source.execute("CREATE TABLE branch_only_evidence (id INTEGER PRIMARY KEY, payload TEXT)")
            self.assertIsNone(model_inventory.validate_source_schema(source))
            source.execute("INSERT INTO branch_only_evidence VALUES (1, 'preserve this evidence')")
            with self.assertRaisesRegex(ValueError, "branch_only_evidence"):
                model_inventory.validate_source_schema(source)
            self.assertEqual(
                source.execute("SELECT id, payload FROM branch_only_evidence").fetchall(),
                [(1, "preserve this evidence")],
            )

    def test_source_schema_rejects_extra_columns_in_a_populated_known_table(self):
        with closing(sqlite3.connect(":memory:")) as source:
            source.execute('CREATE TABLE "reviews_task" ("id" TEXT PRIMARY KEY, "title" TEXT)')
            source.execute('INSERT INTO "reviews_task" VALUES (?, ?)', (uuid.uuid4().hex, "旧分支任务"))
            self.assertIsNone(model_inventory.validate_source_schema(source))
            source.execute('ALTER TABLE "reviews_task" ADD COLUMN "legacy_branch_notes" TEXT')
            source.execute('UPDATE "reviews_task" SET "legacy_branch_notes" = ?', ("不可静默丢弃",))
            with self.assertRaisesRegex(ValueError, "reviews_task.*legacy_branch_notes"):
                model_inventory.validate_source_schema(source)
            self.assertEqual(
                source.execute('SELECT "legacy_branch_notes" FROM "reviews_task"').fetchone()[0],
                "不可静默丢弃",
            )


class CanonicalCopyTests(SimpleTestCase):
    def setUp(self):
        self.json_field = Item._meta.get_field("attributes")

    def test_pg_json_scalar_strings_stay_strings(self):
        for value in ("", "普通字符串", "null", "true", "123", "[]", '{"nested": []}', '"quoted"'):
            with self.subTest(value=value):
                actual = verify_copy.canon(self.json_field, value)
                self.assertIsInstance(actual, str)
                self.assertEqual(actual, value)

    def test_sqlite_json_text_is_decoded_exactly_once(self):
        values = ("", "普通字符串", "null", "true", "123", "[]", '{"nested": []}',
                  None, True, 123, [], NESTED_JSON)
        for value in values:
            with self.subTest(value=value):
                raw = json.dumps(value, ensure_ascii=False)
                actual = verify_copy.canon(self.json_field, raw, sqlite_value=True)
                expected = verify_copy.canon(self.json_field, value)
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), type(expected))

    def test_nested_json_matches_across_raw_sqlite_and_python_values(self):
        pg_value = verify_copy.canon(self.json_field, NESTED_JSON)
        raw_value = verify_copy.canon(
            self.json_field, json.dumps(NESTED_JSON, ensure_ascii=False), sqlite_value=True,
        )
        self.assertEqual(raw_value, pg_value)
        self.assertEqual(pg_value["query"]["elapsed"], 0.9300000000000001)
        self.assertEqual(pg_value["候选"][0]["sources"], ["税则", "历史 ✅"])
        self.assertIsNone(pg_value["候选"][0]["evidence"])

    def test_json_digest_preserves_float_differences_below_twelve_decimal_places(self):
        for sqlite_value in (False, True):
            with self.subTest(sqlite_value=sqlite_value):
                tiny, zero = {"score": 1e-13}, {"score": 0.0}
                if sqlite_value:
                    tiny, zero = json.dumps(tiny), json.dumps(zero)
                tiny = verify_copy.canon(self.json_field, tiny, sqlite_value=sqlite_value)
                zero = verify_copy.canon(self.json_field, zero, sqlite_value=sqlite_value)
                self.assertEqual(tiny["score"], 1e-13)
                self.assertEqual(zero["score"], 0.0)
                self.assertNotEqual(verify_copy.digest([(tiny,)]), verify_copy.digest([(zero,)]))

    def test_datetime_normalization_keeps_microseconds_and_instant(self):
        field = Run._meta.get_field("started_at")
        local = MOMENT.astimezone(dt.timezone(dt.timedelta(hours=8)))
        expected = "2026-11-15T07:30:45.123456"
        self.assertEqual(verify_copy.canon(field, MOMENT), expected)
        self.assertEqual(verify_copy.canon(field, local.isoformat(), sqlite_value=True), expected)
        self.assertIsNone(verify_copy.canon(field, None))

    def test_uuid_primary_and_foreign_keys_match_sqlite_hex_values(self):
        value = uuid.uuid4()
        for field in (Item._meta.pk, Item._meta.get_field("version")):
            with self.subTest(field=field.name):
                self.assertEqual(
                    verify_copy.canon(field, value),
                    verify_copy.canon(field, value.hex, sqlite_value=True),
                )

    def test_digest_accepts_nested_json_and_ignores_object_and_row_order(self):
        left = [("b", {"z": [None, {"b": 2, "a": 1}], "a": True}), ("a", [])]
        right = [("a", []), ("b", {"a": True, "z": [None, {"a": 1, "b": 2}]})]
        self.assertEqual(verify_copy.digest(left), verify_copy.digest(right))

    def test_digest_detects_nested_changes_array_order_and_row_multiplicity(self):
        original = [("a", {"entries": [1, 2], "note": "true"})]
        different = (
            [("a", {"entries": [2, 1], "note": "true"})],
            [("a", {"entries": [1, 2], "note": True})],
            original + original,
            [],
        )
        for rows in different:
            with self.subTest(rows=rows):
                self.assertNotEqual(verify_copy.digest(original), verify_copy.digest(rows))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DataCopyDatabaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.graph = build_submitted_task()
        cls.precheck_run = Run.objects.create(
            task=cls.graph["task"], version=cls.graph["version"],
            kind=Run.Kind.PRECHECK, started_at=MOMENT,
        )
        cls.job = ImportJob.objects.create(
            version=cls.graph["version"], precheck=cls.precheck_run,
            sha256="a" * 64, stage_token="p12-copy-stage", report=NESTED_JSON,
            status=ImportJob.Status.SUCCEEDED,  # This fixture represents an already submitted version.
        )
        cls.claim = TaskClaim.objects.create(task=cls.graph["task"], operator=cls.graph["operator"])
        Task.objects.filter(pk=cls.graph["task"].pk).update(created_at=MOMENT)
        FileVersion.objects.filter(pk=cls.graph["version"].pk).update(uploaded_at=MOMENT)
        cls.content_type = ContentType.objects.get_for_model(Task)
        cls.permission = Permission.objects.get(content_type=cls.content_type, codename="view_task")
        cls.group = Group.objects.create(name="p12-copy-group")
        cls.group.permissions.add(cls.permission)
        cls.graph["operator"].groups.add(cls.group)
        cls.graph["operator"].user_permissions.add(cls.permission)
        cls.log = LogEntry.objects.create(
            user=cls.graph["operator"], content_type=cls.content_type,
            object_id=str(cls.graph["task"].pk), object_repr="合成任务", action_flag=ADDITION,
        )
        cls.session = Session.objects.create(
            session_key="p12-copy-session", session_data="p12-session-data",
            expire_date=MOMENT + dt.timedelta(days=7),
        )

    def _dump(self, *labels):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "fixture.json"
            with patch.object(sys, "argv", ["dumpdata_full.py", str(target), *labels]):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(dumpdata_full.main(), 0)
            return json.loads(target.read_text(encoding="utf-8"))

    def _fixture_row(self, fixture, obj):
        return next(row for row in fixture
                    if row["model"] == obj._meta.label_lower and str(row["pk"]) == str(obj.pk))

    def _set_original_path(self, path):
        FileVersion.objects.filter(pk=self.graph["version"].pk).update(storage_path=path)

    def _image(self, path, *, cell="A5"):
        return ItemImage.objects.create(
            item=self.graph["items"][0], source_sheet="申报要素", source_cell=cell,
            storage_path=path, status=ItemImage.Status.AVAILABLE,
        )

    def test_default_dump_retains_every_row_including_jobs_claims_logs_and_sessions(self):
        fixture = self._dump()
        expected = {
            (model._meta.label_lower, str(pk))
            for model in apps.get_models()
            if model._meta.managed and not model._meta.proxy
            and model._meta.label_lower not in GENERATED_LABELS
            for pk in model._default_manager.values_list("pk", flat=True)
        }
        actual = {(row["model"], str(row["pk"])) for row in fixture}
        self.assertEqual(actual, expected)
        self.assertEqual(len(fixture), len(expected))
        for obj in (self.job, self.claim, self.log, self.session):
            with self.subTest(model=obj._meta.label):
                self.assertIn((obj._meta.label_lower, str(obj.pk)), actual)
        self.assertEqual(self._fixture_row(fixture, self.job)["fields"]["report"], NESTED_JSON)
        self.assertEqual(self._fixture_row(fixture, self.session)["fields"]["session_data"], "p12-session-data")

    def test_source_history_rejects_unknown_applied_migration_for_an_installed_app(self):
        with closing(sqlite3.connect(":memory:")) as source:
            source.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
            source.execute("INSERT INTO django_migrations VALUES ('reviews', '0001_initial')")
            self.assertIsNone(model_inventory.validate_source_history(source))
            unknown = "9999_p12_unmerged_branch_data"
            source.execute("INSERT INTO django_migrations VALUES (?, ?)", ("reviews", unknown))
            with self.assertRaisesRegex(ValueError, unknown):
                model_inventory.validate_source_history(source)
            self.assertEqual(
                source.execute("SELECT name FROM django_migrations WHERE name = ?", (unknown,)).fetchone(),
                (unknown,),
            )

    def test_dump_datetime_roundtrip_preserves_all_six_microsecond_digits(self):
        fixture = self._dump("reviews.Task", "reviews.FileVersion", "runs.Run")
        for obj, field_name in (
            (self.graph["task"], "created_at"),
            (self.graph["version"], "uploaded_at"),
            (self.precheck_run, "started_at"),
        ):
            with self.subTest(model=obj._meta.label, field=field_name):
                row = self._fixture_row(fixture, obj)
                self.assertEqual(dt.datetime.fromisoformat(row["fields"][field_name]), MOMENT)
                restored = next(serializers.deserialize("python", [row])).object
                self.assertEqual(getattr(restored, field_name), MOMENT)
                self.assertEqual(getattr(restored, field_name).microsecond, 123456)

    def test_dump_resolves_generated_foreign_keys_and_m2m_with_natural_keys(self):
        fixture = self._dump("auth.Group", "accounts.User", "admin.LogEntry")
        group_row = self._fixture_row(fixture, self.group)
        user_row = self._fixture_row(fixture, self.graph["operator"])
        log_row = self._fixture_row(fixture, self.log)
        permission_key = list(self.permission.natural_key())
        self.assertEqual(group_row["fields"]["permissions"], [permission_key])
        self.assertEqual(user_row["fields"]["groups"], [[self.group.name]])
        self.assertEqual(user_row["fields"]["user_permissions"], [permission_key])
        self.assertEqual(log_row["fields"]["content_type"], list(self.content_type.natural_key()))
        restored_group = next(serializers.deserialize("python", [group_row]))
        restored_log = next(serializers.deserialize("python", [log_row])).object
        self.assertEqual(restored_group.m2m_data["permissions"], [self.permission.pk])
        self.assertEqual(restored_log.content_type_id, self.content_type.pk)
        self.assertEqual(restored_log.user_id, self.graph["operator"].pk)

    def test_sqlite_and_orm_row_readers_keep_nested_json_and_json_string_values(self):
        first, second = self.graph["items"]
        Item.objects.filter(pk=first.pk).update(attributes=NESTED_JSON, created_at=MOMENT)
        Item.objects.filter(pk=second.pk).update(attributes="null", created_at=MOMENT)
        fields = [Item._meta.pk, Item._meta.get_field("attributes"),
                  Item._meta.get_field("version"), Item._meta.get_field("created_at")]
        with closing(sqlite3.connect(":memory:")) as source:
            source.execute(
                'CREATE TABLE "reviews_item" '
                '("id" TEXT PRIMARY KEY, "attributes" TEXT, "version_id" TEXT, "created_at" TEXT)'
            )
            source.executemany(
                'INSERT INTO "reviews_item" VALUES (?, ?, ?, ?)',
                [(item.pk.hex, json.dumps(value, ensure_ascii=False), item.version_id.hex,
                  MOMENT.replace(tzinfo=None).isoformat())
                 for item, value in ((first, NESTED_JSON), (second, "null"))],
            )
            sqlite_rows = verify_copy.sqlite_table_rows(source, Item, fields)
        target_rows = verify_copy.pg_rows(Item, fields)
        self.assertEqual(verify_copy.digest(sqlite_rows), verify_copy.digest(target_rows))
        by_pk = {row[0]: row for row in sqlite_rows}
        self.assertIsInstance(by_pk[first.pk.hex][1], dict)
        self.assertEqual(by_pk[second.pk.hex][1], "null")
        self.assertIsInstance(by_pk[second.pk.hex][1], str)
        self.assertEqual(by_pk[first.pk.hex][3], "2026-11-15T07:30:45.123456")

    def test_run_item_float_digest_distinguishes_tiny_nonzero_confidence_from_zero(self):
        result = RunItem.objects.create(
            run=self.precheck_run, item=self.graph["items"][0], confidence=1e-13,
        )
        fields = [RunItem._meta.pk, RunItem._meta.get_field("confidence")]
        tiny_rows = verify_copy.pg_rows(RunItem, fields)
        self.assertEqual(tiny_rows, [(result.pk.hex, 1e-13)])
        RunItem.objects.filter(pk=result.pk).update(confidence=0.0)
        zero_rows = verify_copy.pg_rows(RunItem, fields)
        self.assertEqual(zero_rows, [(result.pk.hex, 0.0)])
        self.assertNotEqual(verify_copy.digest(tiny_rows), verify_copy.digest(zero_rows))

    def test_fk_check_accepts_null_and_existing_nullable_references(self):
        issue = Issue.objects.create(task=self.graph["task"], message="合成问题")
        self.assertEqual(verify_copy.fk_orphans(Issue), [])
        Issue.objects.filter(pk=issue.pk).update(
            item_id=self.graph["items"][0].pk, run_id=self.precheck_run.pk,
            created_by_id=self.graph["operator"].pk,
        )
        self.assertEqual(verify_copy.fk_orphans(Issue), [])

    def test_fk_check_detects_orphans_in_each_nullable_foreign_key(self):
        issue = Issue.objects.create(task=self.graph["task"], message="合成孤儿引用")
        for field_name in ("item", "run", "created_by"):
            with self.subTest(field=field_name):
                # 两种后端均延迟检查 FK；finally 在事务结束前恢复合法值。
                Issue.objects.filter(pk=issue.pk).update(**{f"{field_name}_id": uuid.uuid4()})
                try:
                    problems = verify_copy.fk_orphans(Issue)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(f"Issue.{field_name}", problems[0])
                finally:
                    Issue.objects.filter(pk=issue.pk).update(**{f"{field_name}_id": None})
        self.assertEqual(verify_copy.fk_orphans(Issue), [])

    def test_fk_check_still_detects_required_foreign_key_orphans(self):
        issue = Issue.objects.create(task=self.graph["task"], message="合成必填孤儿引用")
        Issue.objects.filter(pk=issue.pk).update(task_id=uuid.uuid4())
        try:
            problems = verify_copy.fk_orphans(Issue)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("Issue.task", problems[0])
        finally:
            Issue.objects.filter(pk=issue.pk).update(task_id=self.graph["task"].pk)

    def test_attachment_report_ignores_empty_references(self):
        self._image("")
        with TemporaryDirectory() as directory:
            self.assertEqual(verify_copy.attachment_report(Path(directory)), (0, []))

    def test_attachment_report_finds_original_and_image_under_file_root(self):
        original_path = "tasks/example/original.xlsx"
        image_path = "tasks/example/images/image.png"
        self._set_original_path(original_path)
        self._image(image_path)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (original_path, image_path):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"p12 attachment")
            self.assertEqual(verify_copy.attachment_report(root), (2, []))

    def test_attachment_manifest_is_sorted_and_records_each_file_size_and_sha256(self):
        payloads = {"z-original.xlsx": b"source document", "a-image.png": b"image contents"}
        self._set_original_path("z-original.xlsx")
        self._image("a-image.png")
        self._image("z-original.xlsx", cell="B5")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path, payload in payloads.items():
                (root / path).write_bytes(payload)
            manifest = verify_copy.attachment_manifest(root)
        self.assertEqual(manifest, [
            {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for path, payload in sorted(payloads.items())
        ])

    def test_attachment_manifest_detects_same_path_same_size_file_replacement(self):
        self._set_original_path("original.xlsx")
        original, replacement = b"attachment-A", b"attachment-B"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "original.xlsx"
            target.write_bytes(original)
            before = verify_copy.attachment_manifest(root)
            target.write_bytes(replacement)
            after = verify_copy.attachment_manifest(root)
            self.assertEqual(verify_copy.attachment_report(root), (1, []))
        self.assertEqual(before[0]["path"], after[0]["path"])
        self.assertEqual(before[0]["size"], after[0]["size"])
        self.assertEqual(after[0]["sha256"], hashlib.sha256(replacement).hexdigest())
        self.assertNotEqual(before[0]["sha256"], after[0]["sha256"])
        self.assertNotEqual(before, after)

    def test_attachment_manifest_detects_truncation_of_an_existing_file(self):
        self._set_original_path("original.xlsx")
        payload = b"attachment contents before truncation"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "original.xlsx"
            target.write_bytes(payload)
            before = verify_copy.attachment_manifest(root)
            with target.open("r+b") as handle:
                handle.truncate(3)
            after = verify_copy.attachment_manifest(root)
            self.assertEqual(verify_copy.attachment_report(root), (1, []))
        self.assertEqual(after, [{
            "path": "original.xlsx", "size": 3,
            "sha256": hashlib.sha256(payload[:3]).hexdigest(),
        }])
        self.assertNotEqual(before[0]["size"], after[0]["size"])
        self.assertNotEqual(before[0]["sha256"], after[0]["sha256"])
        self.assertNotEqual(before, after)

    def test_attachment_report_lists_missing_files_without_stopping(self):
        original_path, image_path = "missing/original.xlsx", "missing/image.png"
        self._set_original_path(original_path)
        self._image(image_path)
        with TemporaryDirectory() as directory:
            count, missing = verify_copy.attachment_report(Path(directory))
        self.assertEqual(count, 2)
        self.assertCountEqual(missing, [original_path, image_path])

    def test_attachment_report_rejects_directories_as_files(self):
        self._set_original_path("directory.xlsx")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "directory.xlsx").mkdir()
            self.assertEqual(verify_copy.attachment_report(root), (1, ["directory.xlsx"]))

    def test_attachment_report_rejects_existing_files_outside_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "files"
            root.mkdir()
            sibling = Path(directory) / "files-backup"
            sibling.mkdir()
            outside = sibling / "original.xlsx"
            outside.write_bytes(b"outside root")
            for path in ("../files-backup/original.xlsx", str(outside)):
                with self.subTest(path=path):
                    self._set_original_path(path)
                    self.assertEqual(verify_copy.attachment_report(root), (1, [path]))

    def test_attachment_report_rejects_symlinks_escaping_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "files"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "original.xlsx").write_bytes(b"outside root")
            if sys.platform == "win32":
                # A genuine NTFS directory junction requires no symlink
                # privilege; the referenced file still resolves outside root.
                create_directory_link(root / "linked-file-parent", outside)
                linked_file = "linked-file-parent/original.xlsx"
            else:
                (root / "linked.xlsx").symlink_to(outside / "original.xlsx")
                linked_file = "linked.xlsx"
            create_directory_link(root / "linked-directory", outside)
            for path in (linked_file, "linked-directory/original.xlsx"):
                with self.subTest(path=path):
                    self._set_original_path(path)
                    self.assertEqual(verify_copy.attachment_report(root), (1, [path]))


class GeneratedMetadataTests(TestCase):
    def _source_with_generated_metadata(self):
        source = sqlite3.connect(":memory:")
        self.addCleanup(source.close)
        source.execute("PRAGMA foreign_keys = ON")
        source.execute(
            "CREATE TABLE django_content_type "
            "(id INTEGER PRIMARY KEY, app_label TEXT NOT NULL, model TEXT NOT NULL)"
        )
        source.execute(
            "CREATE TABLE auth_permission "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "content_type_id INTEGER NOT NULL REFERENCES django_content_type(id), codename TEXT NOT NULL)"
        )
        # 复制真实 migrate 生成的元数据；无需硬编码权限显示名或重复生成逻辑。
        source.executemany(
            "INSERT INTO django_content_type VALUES (?, ?, ?)",
            ContentType.objects.values_list("pk", "app_label", "model"),
        )
        source.executemany(
            "INSERT INTO auth_permission VALUES (?, ?, ?, ?)",
            Permission.objects.values_list("pk", "name", "content_type_id", "codename"),
        )
        return source

    def test_generated_metadata_accepts_default_migrate_content_types_and_permissions(self):
        source = self._source_with_generated_metadata()
        self.assertGreater(source.execute("SELECT COUNT(*) FROM django_content_type").fetchone()[0], 0)
        self.assertGreater(source.execute("SELECT COUNT(*) FROM auth_permission").fetchone()[0], 0)
        self.assertIsNone(model_inventory.validate_generated_metadata(source))

    def test_generated_metadata_rejects_unreferenced_custom_permission(self):
        source = self._source_with_generated_metadata()
        content_type_id = source.execute(
            "SELECT id FROM django_content_type WHERE app_label = 'reviews' AND model = 'task'"
        ).fetchone()[0]
        codename = "p12_custom_release"
        # 源库只有元数据两表，权限没有任何用户、组或 M2M 引用，仍不可丢弃。
        source.execute(
            "INSERT INTO auth_permission (name, content_type_id, codename) VALUES (?, ?, ?)",
            ("P12 custom release", content_type_id, codename),
        )
        with self.assertRaisesRegex(ValueError, codename):
            model_inventory.validate_generated_metadata(source)
        self.assertEqual(
            source.execute("SELECT name FROM auth_permission WHERE codename = ?", (codename,)).fetchone(),
            ("P12 custom release",),
        )

    def test_generated_metadata_rejects_unregistered_content_type_without_permissions(self):
        source = self._source_with_generated_metadata()
        source.execute(
            "INSERT INTO django_content_type (app_label, model) VALUES (?, ?)",
            ("retired_p12_branch", "archivedtask"),
        )
        with self.assertRaisesRegex(ValueError, "retired_p12_branch"):
            model_inventory.validate_generated_metadata(source)
        self.assertEqual(source.execute(
            "SELECT model FROM django_content_type WHERE app_label = 'retired_p12_branch'"
        ).fetchone(), ("archivedtask",))


class SQLiteSnapshotTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "源库 #1.sqlite3"
        self.target = self.root / "snapshot.sqlite3"

    def _create_source(self):
        with closing(sqlite3.connect(self.source)) as source:
            source.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, value TEXT)")
            source.execute("INSERT INTO payload VALUES (1, 'before snapshot')")
            source.commit()

    def test_snapshot_includes_committed_wal_rows_without_changing_source(self):
        with closing(sqlite3.connect(self.source)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE payload (id INTEGER PRIMARY KEY, value TEXT)")
            writer.execute("INSERT INTO payload VALUES (1, 'checkpointed')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            main_before = self.source.read_bytes()
            writer.execute("INSERT INTO payload VALUES (2, 'only in WAL')")
            writer.commit()
            wal_path = Path(f"{self.source}-wal")
            wal_before = wal_path.read_bytes()
            self.assertGreater(len(wal_before), 0)
            self.assertEqual(self.source.read_bytes(), main_before)

            snapshot_sqlite.snapshot(self.source, self.target)

            self.assertEqual(self.source.read_bytes(), main_before)
            self.assertEqual(wal_path.read_bytes(), wal_before)
            writer.execute("INSERT INTO payload VALUES (3, 'after snapshot')")
            writer.commit()
            with closing(sqlite3.connect(self.target.as_uri() + "?mode=ro", uri=True)) as copied:
                self.assertEqual(
                    copied.execute("SELECT id, value FROM payload ORDER BY id").fetchall(),
                    [(1, "checkpointed"), (2, "only in WAL")],
                )
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM payload").fetchone()[0], 3)

    def test_snapshot_uses_backup_from_a_read_only_source_connection(self):
        self._create_source()
        checked = []
        case = self
        real_connect = sqlite3.connect

        class ReadOnlyProbe(sqlite3.Connection):
            def backup(self, target, *args, **kwargs):
                # 实际尝试写源连接，避免只断言连接参数或源码包含 mode=ro。
                with case.assertRaisesRegex(sqlite3.OperationalError, "readonly|read-only"):
                    self.execute("CREATE TABLE forbidden_write (value TEXT)")
                checked.append(True)
                return super().backup(target, *args, **kwargs)

        def connect_with_probe(*args, **kwargs):
            kwargs["factory"] = ReadOnlyProbe
            return real_connect(*args, **kwargs)

        with patch.object(snapshot_sqlite.sqlite3, "connect", side_effect=connect_with_probe):
            snapshot_sqlite.snapshot(self.source, self.target)
        self.assertEqual(checked, [True])
        with closing(sqlite3.connect(self.target)) as copied:
            self.assertEqual(copied.execute("SELECT value FROM payload").fetchone()[0], "before snapshot")

    def test_snapshot_refuses_to_overwrite_existing_target(self):
        self._create_source()
        source_before = self.source.read_bytes()
        self.target.write_bytes(b"keep existing target")
        with self.assertRaises(FileExistsError):
            snapshot_sqlite.snapshot(self.source, self.target)
        self.assertEqual(self.target.read_bytes(), b"keep existing target")
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_snapshot_refuses_source_as_target(self):
        self._create_source()
        source_before = self.source.read_bytes()
        with self.assertRaises(FileExistsError):
            snapshot_sqlite.snapshot(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_snapshot_does_not_create_a_missing_source(self):
        with self.assertRaises(FileNotFoundError):
            snapshot_sqlite.snapshot(self.source, self.target)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.target.exists())
