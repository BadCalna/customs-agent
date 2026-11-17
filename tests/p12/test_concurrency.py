"""P12：PostgreSQL 上的真实并发（多线程 = 多连接）。

顺序请求不能冒充并发（D6 第 3 节）。这些用例只在 PostgreSQL 上跑：
SQLite 只有单写者，行锁是空操作，跑出来的结论不能用于生产判断。
"""
import threading
import unittest

from django.db import IntegrityError, connection, connections
from django.db.models import F
from django.test import TransactionTestCase, override_settings

from apps.accounts.errors import ApiError
from apps.accounts.models import Role
from apps.reviews.models import Confirmation, Submission, Task
from apps.reviews.schemas import ConfirmationIn, SubmissionIn, SubmissionLineIn
from apps.reviews.services import confirmations, idempotency, submissions
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

PG_ONLY = unittest.skipUnless(
    connection.vendor == "postgresql", "并发语义只在 PostgreSQL 上验证（P12）"
)


def run_concurrently(fn, count=2):
    """让 count 个线程在同一屏障后同时执行 fn，收集返回值或异常。"""
    barrier = threading.Barrier(count)
    results = []
    lock = threading.Lock()

    def worker():
        connections.close_all()  # 每个线程用自己的连接，不复用主线程的
        try:
            barrier.wait(timeout=10)
            value = fn()
        except BaseException as exc:  # noqa: BLE001 - 需要把异常带回主线程断言
            value = exc
        finally:
            connections.close_all()
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


@PG_ONLY
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.supplier = make_user("supplier_p12", Role.SUPPLIER)
        self.operator = make_user("operator_p12", Role.OPERATOR)
        # P06 起提交会在没有任务专家时按默认专家建立授权；并发测试显式准备专家，避免 409 干扰并发断言。
        self.expert = make_user("expert_p12", Role.EXPERT)
        self.task = make_task(self.supplier, stage=Task.Stage.AWAITING_CONFIRMATION)
        assign(self.task, self.operator, Role.OPERATOR)
        assign(self.task, self.expert, Role.EXPERT)
        self.version = make_version(self.task, self.supplier)
        self.item_a = make_item(self.version, row=5, name="合成商品A")
        self.item_b = make_item(self.version, row=6, name="合成商品B", supplier_hs="7304210000")
        self.confirm_a = self._confirm(self.item_a, "8544421900")
        self.confirm_b = self._confirm(self.item_b, "7304210000")

    def tearDown(self):
        connections.close_all()

    def _confirm(self, item, hs):
        return Confirmation.objects.create(
            item=item, revision=1, previous_hs="", confirmed_hs=hs,
            reason="合成确认", created_by=self.operator,
        )

    def submission_payload(self):
        # 用真实契约 DTO，而不是 SimpleNamespace：服务层依赖 model_dump()，
        # 测试桩必须和线上请求形状一致，否则测的是不存在的调用方式。
        return SubmissionIn(
            version_id=str(self.version.pk),
            revision=self.task.revision,
            items=[
                SubmissionLineIn(item_id=str(self.item_a.pk), confirmation_id=str(self.confirm_a.pk)),
                SubmissionLineIn(item_id=str(self.item_b.pk), confirmation_id=str(self.confirm_b.pk)),
            ],
        )

    def test_confirmations_same_revision_only_one_wins(self):
        """两个并发确认都带 expected_revision=1：一个成功，另一个 409，不产生重复修订。"""
        payload = ConfirmationIn(expected_revision=1, confirmed_hs="8544422000", reason="并发改码")

        def confirm():
            try:
                confirmations.write_confirmation(
                    self.operator, str(self.version.pk), str(self.item_a.pk), payload,
                )
                return "ok"
            except ApiError as exc:
                return f"api-error:{exc.status}:{exc.code}"

        results = run_concurrently(confirm)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertIn("api-error:409:stale_revision", results)
        self.assertEqual(Confirmation.objects.filter(item=self.item_a).count(), 2)

    def test_concurrent_submissions_create_one_snapshot(self):
        """两个并发提交同版本同修订：只落一份快照，另一路得到重放或明确冲突。"""
        def submit():
            try:
                submissions.create_submission(
                    self.operator, str(self.task.pk), self.submission_payload(), None,
                )
                return "ok"
            except ApiError as exc:
                return f"api-error:{exc.status}:{exc.code}"

        results = run_concurrently(submit)
        self.assertEqual(Submission.objects.filter(task=self.task).count(), 1, results)
        # 一路成功落库；另一路要么重放同一快照，要么因任务修订已变而 409，二者都不产生第二份提交。
        self.assertEqual(results.count("ok"), 1, results)
        self.assertTrue(all(r == "ok" or r.startswith("api-error:409") for r in results), results)

    def test_concurrent_same_idempotency_key_single_record(self):
        payload = {"version_id": str(self.version.pk), "revision": 1}

        def store():
            idempotency.store(self.operator, "submission.create", "same-key", payload, {"ok": True})
            return "ok"

        results = run_concurrently(store)
        self.assertEqual(results.count("ok"), 2, results)
        from apps.reviews.models import IdempotencyRecord

        self.assertEqual(IdempotencyRecord.objects.filter(key="same-key").count(), 1)

    def test_unique_snapshot_constraint_survives_race(self):
        def create():
            try:
                Submission.objects.create(
                    task=self.task, version=self.version, revision=self.task.revision,
                    created_by=self.operator,
                )
                return "ok"
            except IntegrityError:
                return "integrity-error"

        results = run_concurrently(create)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("integrity-error"), 1, results)
        self.assertEqual(Submission.objects.filter(task=self.task).count(), 1)

    def test_increment_is_not_lost_across_connections(self):
        """F() 自增在 20 条并发连接下不丢更新；这是 SQLite 上无法给出的结论。"""
        start = Task.objects.get(pk=self.task.pk).revision

        def bump():
            Task.objects.filter(pk=self.task.pk).update(revision=F("revision") + 1)
            return "ok"

        results = run_concurrently(bump, count=20)
        self.assertEqual(results.count("ok"), 20, results)
        self.assertEqual(Task.objects.get(pk=self.task.pk).revision, start + 20)
