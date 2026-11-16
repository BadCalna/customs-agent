"""P12：JSONField 在 SQLite（文本）与 PostgreSQL（jsonb）上的行为对照。"""
from django.core.exceptions import ValidationError
from django.db import NotSupportedError, connection, transaction
from django.test import TestCase, override_settings

from apps.runs.models import Run, RunItem
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

NESTED = {
    "candidates": [
        {"hs": "8544421900", "score": 0.93, "sources": ["税则库", "历史成功记录"], "evidence": None},
        {"hs": "8544422000", "score": 0.11, "sources": [], "evidence": {"note": "证据不足"}},
    ],
    "query": {"provider": "mock", "duration_ms": 12, "ok": True},
    "备注": "合成数据，含中文与 emoji ✅",
}


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class JsonFieldTests(TestCase):
    def setUp(self):
        supplier = make_user("supplier_p12", "supplier")
        operator = make_user("operator_p12", "operator")
        self.task = make_task(supplier)
        assign(self.task, operator, "operator")
        self.version = make_version(self.task, supplier)
        self.item = make_item(self.version, row=5)
        self.run = Run.objects.create(task=self.task, version=self.version, kind=Run.Kind.PRECHECK)
        self.row = RunItem.objects.create(run=self.run, item=self.item, result=NESTED)

    def test_nested_payload_roundtrip(self):
        fetched = RunItem.objects.get(pk=self.row.pk)
        self.assertEqual(fetched.result, NESTED)
        self.assertEqual(fetched.result["candidates"][0]["sources"], ["税则库", "历史成功记录"])
        self.assertIsNone(fetched.result["candidates"][0]["evidence"])
        self.assertIn("✅", fetched.result["备注"])

    def test_key_order_is_not_significant(self):
        reordered = dict(reversed(list(NESTED.items())))
        self.row.result = reordered
        self.row.save(update_fields=["result"])
        self.assertEqual(RunItem.objects.get(pk=self.row.pk).result, NESTED)

    def test_default_and_empty_object(self):
        plain = RunItem.objects.create(run=self.run, item=make_item(self.version, row=9))
        self.assertEqual(RunItem.objects.get(pk=plain.pk).result, {})

    def test_containment_lookup(self):
        """jsonb 支持包含查询；SQLite 后端不支持，P16 若依赖它必须先确认后端。"""
        if connection.vendor == "postgresql":
            hits = RunItem.objects.filter(result__contains={"query": {"provider": "mock"}})
            self.assertEqual(list(hits), [self.row])
        else:
            with self.assertRaises(NotSupportedError):
                list(RunItem.objects.filter(result__contains={"query": {"provider": "mock"}}))

    def test_invalid_json_is_rejected_on_write(self):
        """JSONField 不接受不可序列化对象；两个后端都应在 Python 层拒绝。"""
        self.row.result = {"bad": object()}
        with self.assertRaises(TypeError):
            with transaction.atomic():
                self.row.save(update_fields=["result"])