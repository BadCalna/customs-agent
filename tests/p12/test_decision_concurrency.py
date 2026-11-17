"""P10 专家决定在 PostgreSQL 上的真实多连接竞争与幂等重放。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from apps.accounts.errors import ApiError
from apps.accounts.models import User
from apps.reviews.models import (
    AuditEvent, Decision, DecisionItem, IdempotencyRecord, Submission, Task,
)
from apps.reviews.schemas import DecisionIn, DecisionLineIn
from apps.reviews.services import decisions
from tests.p01.factories import make_version
from tests.p10.factories import HS_A, HS_B, SubmissionFixture

HS_ALT = "8536901900"
CHANGE_REASON = "专家按规格核查后改码"


@skipUnless(connection.vendor == "postgresql", "P10 多连接竞争只在 PostgreSQL 上验收")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DecisionConcurrencyTests(TransactionTestCase):
    # 复用 P10 的完整业务图，但不能继承其 TestCase：该类的外层事务
    # 会让其它连接看不到 setUp 创建的任务、快照和专家授权。
    _confirm = SubmissionFixture._confirm

    def setUp(self):
        super().setUp()
        SubmissionFixture.setUp(self)
        self.initial_revision = self.task.revision

    def race(self, calls):
        """独立连接就绪后经屏障启动；异常传回主线程，所有连接最终关闭。"""
        barrier = Barrier(len(calls))

        def invoke(call):
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    # 锁回归应使测试失败，不能无限挂住测试库或收尾。
                    cursor.execute("SET lock_timeout = '10s'")
                    cursor.execute("SET statement_timeout = '15s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    backend_pid = cursor.fetchone()[0]
                barrier.wait(timeout=10)
                try:
                    outcome = call()
                except ApiError as exc:
                    outcome = exc
                return backend_pid, outcome
            finally:
                connections.close_all()

        # 上下文退出会回收所有工作线程；非 ApiError 的异常会由 result 原样抛出。
        with ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="p12-decision") as pool:
            futures = [pool.submit(invoke, call) for call in calls]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(len({pid for pid, _ in results}), len(calls), results)
        return [outcome for _, outcome in results]

    def decide(self, reason, key):
        # 每次在工作线程重读用户、构造独立 DTO，只共享不可变的对象 ID。
        user = User.objects.get(pk=self.expert.pk)
        payload = DecisionIn(
            action=Decision.Action.APPROVE,
            reason=reason,
            items=[
                DecisionLineIn(
                    item_id=str(self.item_a.pk), final_hs=HS_ALT,
                    change_reason=CHANGE_REASON,
                ),
                DecisionLineIn(item_id=str(self.item_b.pk), final_hs=HS_B),
            ],
        )
        return decisions.decide(user, str(self.submission.pk), payload, key)

    def assert_single_approval(self, response):
        self.assertFalse(response["idempotent_replay"])
        self.assertEqual(Decision.objects.filter(submission=self.submission).count(), 1)
        decision = Decision.objects.get(submission=self.submission)
        self.assertEqual(str(decision.pk), response["decision_id"])
        self.assertEqual(decision.created_by_id, self.expert.pk)
        self.assertEqual(decision.reason, response["reason"])

        rows = DecisionItem.objects.filter(decision=decision)
        self.assertEqual(rows.count(), 2)
        row_a = rows.get(item=self.item_a)
        self.assertEqual(
            (row_a.previous_hs, row_a.final_hs, row_a.change_reason),
            (HS_A, HS_ALT, CHANGE_REASON),
        )
        row_b = rows.get(item=self.item_b)
        self.assertEqual((row_b.previous_hs, row_b.final_hs, row_b.change_reason), (HS_B, HS_B, ""))

        self.task.refresh_from_db()
        self.submission.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.APPROVED)
        self.assertEqual(self.task.revision, self.initial_revision + 1)
        self.assertEqual(self.submission.status, Submission.Status.APPROVED)
        self.assertEqual(response["task_revision"], self.task.revision)

        events = AuditEvent.objects.filter(task=self.task, action__startswith="decision.")
        self.assertCountEqual(
            events.values_list("action", flat=True), ["decision.approved", "decision.recoded"],
        )
        approved = events.get(action="decision.approved")
        recoded = events.get(action="decision.recoded")
        self.assertEqual(approved.reason, decision.reason)
        self.assertEqual(approved.details["decision_id"], str(decision.pk))
        self.assertEqual(approved.details["submission_id"], str(self.submission.pk))
        self.assertEqual(recoded.details["decision_id"], str(decision.pk))
        self.assertEqual(recoded.item_id, self.item_a.pk)
        self.assertEqual(
            (recoded.previous_value, recoded.new_value, recoded.reason),
            (HS_A, HS_ALT, CHANGE_REASON),
        )

        records = IdempotencyRecord.objects.filter(user=self.expert, action=decisions.ACTION)
        self.assertEqual(records.count(), 1)
        record = records.get()
        self.assertEqual(record.response, response)
        return record

    def test_different_requests_only_create_one_decision_and_audit(self):
        keys = {"并发请求 A 的专家意见": "decision-a", "并发请求 B 的专家意见": "decision-b"}
        outcomes = self.race([
            lambda: self.decide("并发请求 A 的专家意见", "decision-a"),
            lambda: self.decide("并发请求 B 的专家意见", "decision-b"),
        ])
        successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, ApiError)]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(failures), 1, outcomes)
        self.assertEqual((failures[0].status, failures[0].code), (409, "submission_closed"))
        record = self.assert_single_approval(successes[0])
        # 失败请求既不能重放另一请求，也不能留下自己的幂等记录或审计。
        self.assertEqual(record.key, keys[successes[0]["reason"]])

    def test_same_key_concurrently_replays_one_decision_without_duplicate_audit(self):
        outcomes = self.race([
            lambda: self.decide("同键专家意见", "decision-same-key") for _ in range(2)
        ])
        self.assertTrue(all(isinstance(outcome, dict) for outcome in outcomes), outcomes)
        self.assertCountEqual([outcome["idempotent_replay"] for outcome in outcomes], [False, True])
        original = next(outcome for outcome in outcomes if not outcome["idempotent_replay"])
        replay = next(outcome for outcome in outcomes if outcome["idempotent_replay"])
        self.assertEqual(replay, {**original, "idempotent_replay": True})
        record = self.assert_single_approval(original)
        self.assertEqual(record.key, "decision-same-key")

    def test_newer_version_rejects_both_concurrent_old_submission_decisions(self):
        # DecisionIn 没有 revision 参数；P10 的过期保护针对提交所属文件版本。
        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        outcomes = self.race([
            lambda: self.decide("旧提交请求 A", "old-decision-a"),
            lambda: self.decide("旧提交请求 B", "old-decision-b"),
        ])
        self.assertTrue(all(isinstance(outcome, ApiError) for outcome in outcomes), outcomes)
        self.assertCountEqual(
            [(outcome.status, outcome.code) for outcome in outcomes],
            [(409, "stale_submission"), (409, "stale_submission")],
        )
        self.assertFalse(Decision.objects.filter(submission=self.submission).exists())
        self.assertFalse(AuditEvent.objects.filter(task=self.task, action__startswith="decision.").exists())
        self.assertFalse(IdempotencyRecord.objects.filter(user=self.expert, action=decisions.ACTION).exists())
        self.task.refresh_from_db()
        self.submission.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.SUBMITTED)
        self.assertEqual(self.task.revision, self.initial_revision)
        self.assertEqual(self.submission.status, Submission.Status.PENDING)
