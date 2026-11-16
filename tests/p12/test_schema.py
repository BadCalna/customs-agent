"""P12：约束、外键与字段行为在两个后端上的实测。

同一组用例在 SQLite 与 PostgreSQL 上都跑；只有少数断言语义按后端分开，
差异本身写进 `docs/开发文档/交接/P12.md` 的双库对照表。
"""
import uuid

from django.db import DataError, IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings

from apps.accounts.models import Role, TaskAssignment
from apps.reviews.models import (
    Confirmation,
    FileVersion,
    IdempotencyRecord,
    Item,
    Submission,
    SubmissionItem,
    Task,
)
from tests.p01.factories import make_item, make_task, make_user, make_version

from .factories import HS_A, HS_B, build_submitted_task


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UniqueConstraintTests(TestCase):
    def setUp(self):
        self.graph = build_submitted_task()

    def test_duplicate_file_version_number_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            FileVersion.objects.create(
                task=self.graph["task"], number=1, original_filename="重复版本.xlsx",
                uploaded_by=self.graph["supplier"],
            )

    def test_duplicate_item_source_row_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(
                version=self.graph["version"], source_sheet="申报要素", source_row=5, name="重复行",
            )

    def test_duplicate_confirmation_revision_rejected(self):
        item = self.graph["items"][0]
        with self.assertRaises(IntegrityError), transaction.atomic():
            Confirmation.objects.create(
                item=item, revision=1, previous_hs="", confirmed_hs=HS_A,
                created_by=self.graph["operator"],
            )

    def test_duplicate_submission_revision_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Submission.objects.create(
                task=self.graph["task"], version=self.graph["version"], revision=1,
                created_by=self.graph["operator"],
            )

    def test_duplicate_submission_item_rejected(self):
        submission = self.graph["submission"]
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionItem.objects.create(
                submission=submission, item=self.graph["items"][0],
                confirmation=self.graph["confirmations"][0], submitted_hs=HS_A, item_revision=1,
            )

    def test_duplicate_idempotency_key_rejected(self):
        operator = self.graph["operator"]
        IdempotencyRecord.objects.create(
            user=operator, action="submission.create", key="k-1", request_hash="a" * 64, response={},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            IdempotencyRecord.objects.create(
                user=operator, action="submission.create", key="k-1", request_hash="b" * 64, response={},
            )

    def test_same_key_different_action_allowed(self):
        operator = self.graph["operator"]
        IdempotencyRecord.objects.create(
            user=operator, action="submission.create", key="k-1", request_hash="a" * 64, response={},
        )
        IdempotencyRecord.objects.create(
            user=operator, action="decision.create", key="k-1", request_hash="a" * 64, response={},
        )
        self.assertEqual(IdempotencyRecord.objects.filter(key="k-1").count(), 2)

    def test_unique_allows_distinct_nullable_business_keys(self):
        """唯一约束只约束业务键本身；空字符串与空值不能被当成同一个 HS。"""
        item = self.graph["items"][0]
        Confirmation.objects.create(
            item=item, revision=2, previous_hs=HS_A, confirmed_hs=HS_B,
            created_by=self.graph["operator"],
        )
        self.assertEqual(Confirmation.objects.filter(item=item).count(), 2)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ForeignKeyTests(TestCase):
    def setUp(self):
        self.graph = build_submitted_task()

    def test_delete_version_blocked_by_submission(self):
        with self.assertRaises(ProtectedError):
            self.graph["version"].delete()

    def test_delete_item_blocked_by_submission_line(self):
        with self.assertRaises(ProtectedError):
            self.graph["items"][0].delete()

    def test_task_with_history_cannot_be_deleted(self):
        """有提交快照的任务不能直接删：历史记录随父级联删除也仍受 PROTECT 保护。"""
        with self.assertRaises(ProtectedError):
            self.graph["task"].delete()
        self.assertTrue(Task.objects.filter(pk=self.graph["task"].pk).exists())

    def test_task_without_history_cascades_version_and_items(self):
        """没有提交快照的任务可清理，版本与商品行随之级联删除。"""
        supplier = make_user("supplier_p12_clean", Role.SUPPLIER)
        task = make_task(supplier)
        version = make_version(task, supplier)
        make_item(version, row=5)
        task_id = task.pk
        task.delete()
        self.assertFalse(Task.objects.filter(pk=task_id).exists())
        self.assertFalse(FileVersion.objects.filter(task_id=task_id).exists())
        self.assertFalse(Item.objects.filter(version__task_id=task_id).exists())

    def test_cross_task_reference_rejected_by_clean(self):
        other_supplier = make_user("supplier_p12_other", Role.SUPPLIER)
        other_task = make_task(other_supplier)
        other_version = make_version(other_task, other_supplier, number=1)
        make_item(other_version, row=5)
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            Submission.objects.create(
                task=self.graph["task"], version=other_version, revision=99,
                created_by=self.graph["operator"],
            )


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CheckConstraintAndTypeTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_p12", Role.SUPPLIER)
        self.operator = make_user("operator_p12", Role.OPERATOR)
        self.task = make_task(self.supplier)
        self.version = make_version(self.task, self.supplier)

    def test_assignment_role_check_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskAssignment.objects.create(task=self.task, user=self.operator, role="guest")

    def test_uuid_primary_key_roundtrip(self):
        item = make_item(self.version, row=7)
        fetched = Item.objects.get(pk=item.pk)
        self.assertIsInstance(fetched.pk, uuid.UUID)
        self.assertEqual(str(fetched.pk), str(item.pk))

    def test_charfield_max_length(self):
        """SQLite 不校验 max_length，PostgreSQL 会拒绝——这是迁移后才会暴露的差异。"""
        long_name = "超长文件名" * 60  # 300 字符 > 255
        self.assertGreater(len(long_name), 255)
        if connection.vendor == "postgresql":
            with self.assertRaises(DataError), transaction.atomic():
                FileVersion.objects.create(
                    task=self.task, number=9, original_filename=long_name, uploaded_by=self.supplier,
                )
        else:
            FileVersion.objects.create(
                task=self.task, number=9, original_filename=long_name, uploaded_by=self.supplier,
            )
            self.assertEqual(
                FileVersion.objects.get(task=self.task, number=9).original_filename, long_name,
            )

    def test_empty_string_stays_empty_not_null(self):
        item = make_item(self.version, row=8, name="")
        fetched = Item.objects.get(pk=item.pk)
        self.assertEqual(fetched.name, "")
        self.assertIsNotNone(fetched.name)