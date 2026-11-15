"""P12：时间、浮点、空值排序与文本行为在两个后端上的实测差异。"""
import datetime as dt

from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.runs.models import Run, RunItem
from tests.p01.factories import make_item, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TypeBehaviorTests(TestCase):
    def setUp(self):
        self.supplier = make_user("supplier_p12", "supplier")
        self.task = make_task(self.supplier)
        self.version = make_version(self.task, self.supplier)
        self.item = make_item(self.version, row=5)
        self.run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK)

    def test_microsecond_precision_preserved(self):
        moment = dt.datetime(2026, 11, 15, 7, 30, 45, 123456, tzinfo=dt.timezone.utc)
        self.run.started_at = moment
        self.run.save(update_fields=["started_at"])
        fetched = Run.objects.get(pk=self.run.pk).started_at
        self.assertEqual(fetched.microsecond, 123456)
        self.assertEqual(fetched.astimezone(dt.timezone.utc), moment)

    def test_datetime_is_timezone_aware(self):
        fetched = Run.objects.get(pk=self.run.pk).created_at
        self.assertTrue(timezone.is_aware(fetched))

    def test_float_confidence_roundtrip(self):
        row = RunItem.objects.create(run=self.run, item=self.item, confidence=0.9300000000000001)
        self.assertEqual(RunItem.objects.get(pk=row.pk).confidence, 0.9300000000000001)

    def test_null_ordering_differs_by_backend(self):
        """ASC 排序时 PostgreSQL 把 NULL 放最后，SQLite 放最前；列表顺序断言不能写死。"""
        RunItem.objects.create(run=self.run, item=self.item, suggested_hs=None)
        second = make_item(self.version, row=6)
        RunItem.objects.create(run=self.run, item=second, suggested_hs="8544421900")
        ordered = list(RunItem.objects.order_by("suggested_hs").values_list("suggested_hs", flat=True))
        if connection.vendor == "postgresql":
            self.assertEqual(ordered, ["8544421900", None])
        else:
            self.assertEqual(ordered, [None, "8544421900"])

    def test_null_and_empty_are_distinct(self):
        null_row = RunItem.objects.create(run=self.run, item=self.item, suggested_hs=None)
        empty_row = RunItem.objects.create(
            run=self.run, item=make_item(self.version, row=7), suggested_hs="",
        )
        self.assertIsNone(RunItem.objects.get(pk=null_row.pk).suggested_hs)
        self.assertEqual(RunItem.objects.get(pk=empty_row.pk).suggested_hs, "")
        self.assertEqual(RunItem.objects.filter(suggested_hs__isnull=True).count(), 1)

    def test_text_case_sensitivity_is_explicit(self):
        """SQLite 的 LIKE 对 ASCII 不区分大小写，PostgreSQL 区分；本项目不用 icontains，
        这里锁住“精确匹配必须区分大小写”，避免以后误用 LIKE 引入跨库差异。"""
        from tests.p01.factories import make_user as _make_user

        _make_user("MixCaseUser", "operator")
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.assertEqual(User.objects.filter(username="MixCaseUser").count(), 1)
        self.assertEqual(User.objects.filter(username="mixcaseuser").count(), 0)