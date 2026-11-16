"""仅 PostgreSQL 多连接验证。SQLite 跳过，绝不把顺序测试写成并发验收。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import tempfile
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from apps.accounts.errors import ApiError
from apps.accounts.models import Role, User, TaskAssignment
from apps.reviews.models import FileVersion, ImportJob, Item, TaskClaim
from apps.reviews.services import imports
from apps.reviews.services.claims import claim_task
from apps.runs.models import Run
from tests.p01.factories import make_user
from tests.p06.samples import sample


@skipUnless(connection.vendor == "postgresql", "PostgreSQL 多连接专项待验收；SQLite 无行锁")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        config=override_settings(CUSTOMS_FILE_ROOT=temp.name);config.enable();self.addCleanup(config.disable)
        self.supplier=make_user("pg-s",Role.SUPPLIER);self.a=make_user("pg-a",Role.OPERATOR);self.b=make_user("pg-b",Role.OPERATOR)
        self.data=sample()

    def race(self, calls):
        barrier=Barrier(len(calls))
        def invoke(call):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return call()
            except ApiError as exc:return {"error":exc.code,"status":exc.status}
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:return list(pool.map(invoke,calls))

    def upload(self,key="k",data=None):
        return imports.upload(User.objects.get(pk=self.supplier.pk),filename="pg.xlsx",data=data or self.data,key=key)

    def test_two_operators_one_winner(self):
        result=self.upload();tid=result["task_id"]
        results=self.race([lambda:claim_task(User.objects.get(pk=self.a.pk),tid,"a"),lambda:claim_task(User.objects.get(pk=self.b.pk),tid,"b")])
        self.assertEqual(sum("operator_id" in r for r in results),1)
        self.assertEqual(sum(r.get("error")=="task_already_claimed" for r in results),1)
        self.assertEqual(TaskClaim.objects.count(),1);self.assertEqual(TaskAssignment.objects.filter(task_id=tid,role=Role.OPERATOR).count(),1)

    def test_concurrent_same_claim_key_replays(self):
        tid=self.upload()["task_id"]
        results=self.race([lambda:claim_task(User.objects.get(pk=self.a.pk),tid,"a") for _ in range(2)])
        self.assertTrue(all("operator_id" in r for r in results));self.assertEqual(TaskClaim.objects.count(),1)

    def test_concurrent_upload_retry_one_version_one_run(self):
        results=self.race([lambda:self.upload() for _ in range(2)])
        self.assertEqual(results[0]["version_id"],results[1]["version_id"])
        self.assertEqual(FileVersion.objects.count(),1);self.assertEqual(ImportJob.objects.count(),1)
        self.assertEqual(Run.objects.count(),1);self.assertEqual(Item.objects.count(),3)

    def test_concurrent_same_upload_key_different_content(self):
        results=self.race([lambda:self.upload(),lambda:self.upload(data=sample(order=(3,2,1)))])
        self.assertEqual(sum("version_id" in r for r in results),1)
        self.assertEqual(sum(r.get("error")=="idempotency_conflict" for r in results),1)
        self.assertEqual(FileVersion.objects.count(),1)
