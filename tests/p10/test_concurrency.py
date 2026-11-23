"""需要真实行锁的竞争回归；SQLite 明确跳过，不冒充 PostgreSQL 并发证据。"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections
from django.test import Client, TransactionTestCase, override_settings, skipUnlessDBFeature

from apps.accounts.models import Role
from apps.reviews.models import Confirmation, Decision, DecisionItem, IdempotencyRecord, Issue, Submission, SubmissionItem, Task
from tests.p01.factories import assign, make_item, make_task, make_user, make_version


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DecisionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.supplier = make_user("race_supplier", Role.SUPPLIER)
        self.operator = make_user("race_operator", Role.OPERATOR)
        self.expert = make_user("race_expert", Role.EXPERT)

    def _submission(self):
        task = make_task(self.supplier, stage=Task.Stage.SUBMITTED)
        assign(task, self.expert, Role.EXPERT)
        version = make_version(task, self.supplier)
        item = make_item(version)
        confirmation = Confirmation.objects.create(
            item=item, revision=1, confirmed_hs="8544421900", created_by=self.operator,
        )
        submission = Submission.objects.create(task=task, version=version, revision=1, created_by=self.operator)
        SubmissionItem.objects.create(
            submission=submission, item=item, confirmation=confirmation,
            submitted_hs=confirmation.confirmed_hs, item_revision=1,
        )
        return submission, item

    def _race(self, requests):
        barrier = Barrier(len(requests))
        expert = self.expert

        def post(request):
            close_old_connections()
            try:
                submission, body, key = request
                client = Client()
                client.force_login(expert)
                headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
                barrier.wait(timeout=10)
                response = client.post(
                    f"/api/v1/submissions/{submission.pk}/decisions",
                    data=json.dumps(body), content_type="application/json", **headers,
                )
                return response.status_code, response.json()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            futures = [pool.submit(post, request) for request in requests]
            return [future.result(timeout=30) for future in futures]

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_experts_racing_same_snapshot_create_one_whole_decision(self):
        submission, item = self._submission()
        body = {"action": "approve", "revision": 1, "items": [{"item_id": str(item.pk), "final_hs": "8544421900"}]}
        responses = self._race([(submission, body, None), (submission, body, None)])
        self.assertEqual(sorted(status for status, _ in responses), [200, 409])
        rejected = next(body for status, body in responses if status == 409)
        self.assertEqual(rejected["error"]["code"], "submission_closed")
        self.assertEqual(Decision.objects.count(), 1)
        self.assertEqual(DecisionItem.objects.count(), 1)
        submission.task.refresh_from_db()
        self.assertEqual(submission.task.revision, 2)

    @skipUnlessDBFeature("has_select_for_update")
    def test_same_key_racing_different_targets_reserves_before_any_losing_side_effects(self):
        first, _ = self._submission()
        second, _ = self._submission()
        body = {"action": "return", "return_to": "supplier", "reason": "补料", "issues": [{"message": "缺签章"}]}
        responses = self._race([(first, body, "cross-target-race"), (second, body, "cross-target-race")])
        self.assertEqual(sorted(status for status, _ in responses), [200, 409])
        rejected = next(body for status, body in responses if status == 409)
        self.assertEqual(rejected["error"]["code"], "idempotency_conflict")
        self.assertEqual(Decision.objects.count(), 1)
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        self.assertEqual(Submission.objects.filter(status=Submission.Status.RETURNED).count(), 1)
        self.assertEqual(Submission.objects.filter(status=Submission.Status.PENDING).count(), 1)
