"""P10 审查遗留回归：M-06 退回对象枚举、M-09 畸形 UUID、M-02 幂等绑目标、迟退不留预占。"""
import json

from apps.reviews.models import Decision, IdempotencyRecord, Issue, Submission, SubmissionItem
from apps.reviews.schemas import DecisionIn
from apps.reviews.services import idempotency
from tests.p10.factories import HS_A, HS_B, SubmissionFixture


class DecisionReviewFixTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.expert)

    def approve_payload(self, **overrides):
        body = {
            "action": "approve", "reason": "专家复核一致",
            "items": [
                {"item_id": str(self.item_a.pk), "final_hs": HS_A},
                {"item_id": str(self.item_b.pk), "final_hs": HS_B},
            ],
        }
        body.update(overrides)
        return body

    def post(self, body=None, key=None, url=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            url or f"/api/v1/submissions/{self.submission.pk}/decisions",
            data=json.dumps(body or self.approve_payload()),
            content_type="application/json", **headers,
        )

    def _extra_submission(self) -> Submission:
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=2, created_by=self.operator,
        )
        for line in (self.line_a, self.line_b):
            SubmissionItem.objects.create(
                submission=submission, item=line.item, confirmation=line.confirmation,
                submitted_hs=line.submitted_hs, item_revision=line.item_revision,
            )
        return submission

    # --- M-06：退回对象必须是已定义角色 -----------------------------------
    def test_invalid_return_to_is_rejected(self):
        body = {"action": "return", "return_to": "attacker", "reason": "补料",
                "issues": [{"message": "缺签章"}]}
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_return_to")
        self.assertEqual(Decision.objects.count(), 0)
        self.assertEqual(Issue.objects.count(), 0)

    def test_valid_return_to_still_works(self):
        body = {"action": "return", "return_to": "supplier", "reason": "补料",
                "issues": [{"message": "缺签章"}]}
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(Issue.objects.count(), 1)

    # --- M-09：畸形 UUID 一律 404 ----------------------------------------
    def test_malformed_submission_id_returns_404(self):
        response = self.post(url="/api/v1/submissions/not-a-uuid/decisions")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    # --- M-02：同一幂等键不能把 A 提交的结果重放给 B 提交 -----------------
    def test_idempotency_key_cannot_replay_across_submissions(self):
        body = {"action": "return", "return_to": "supplier", "reason": "缺材料",
                "issues": [{"message": "补签章"}]}
        self.assertEqual(self.post(body, key="target-bound").status_code, 200)
        other = self._extra_submission()
        response = self.post(
            body, key="target-bound", url=f"/api/v1/submissions/{other.pk}/decisions",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
        self.assertEqual(Decision.objects.count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.status, Submission.Status.PENDING)

    def test_legacy_decision_formats_replay_same_target_and_reject_another(self):
        body = {"action": "return", "return_to": "supplier", "reason": "补料",
                "issues": [{"message": "缺签章"}]}
        first = self.post(body, key="legacy-decision").json()
        payload = DecisionIn.model_validate(body).model_dump(mode="json")
        record = IdempotencyRecord.objects.get(key="legacy-decision")
        other = self._extra_submission()
        for legacy_payload in (
            payload,
            {"submission_id": str(self.submission.pk), "decision": payload},
        ):
            with self.subTest(legacy_payload=legacy_payload):
                record.request_hash = idempotency.legacy_request_hash(legacy_payload)
                record.save(update_fields=["request_hash"])
                conflict = self.post(
                    body, key="legacy-decision", url=f"/api/v1/submissions/{other.pk}/decisions",
                )
                self.assertEqual(conflict.status_code, 409)
                replay = self.post(body, key="legacy-decision")
                self.assertEqual(replay.status_code, 200, replay.content)
                self.assertEqual(replay.json()["decision_id"], first["decision_id"])
                self.assertTrue(replay.json()["idempotent_replay"])
                record.refresh_from_db()
                self.assertEqual(record.request_hash, idempotency.request_hash(payload, str(self.submission.pk)))
        self.assertEqual(Decision.objects.count(), 1)

    def test_legacy_unbound_hash_cannot_replay_across_submissions(self):
        body = {"action": "return", "return_to": "supplier", "reason": "缺材料",
                "issues": [{"message": "补签章"}]}
        self.assertEqual(self.post(body, key="legacy-target").status_code, 200)
        record = IdempotencyRecord.objects.get(key="legacy-target")
        payload = DecisionIn.model_validate(body).model_dump(mode="json")
        record.request_hash = idempotency.legacy_request_hash(payload)
        record.save(update_fields=["request_hash"])
        # 确认实际能识别旧指纹，避免只因 payload 缺默认字段而误冲突的假绿。
        self.assertTrue(idempotency._matches(record, payload, str(self.submission.pk)))
        other = self._extra_submission()

        response = self.post(
            body, key="legacy-target", url=f"/api/v1/submissions/{other.pk}/decisions",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")

    # --- 预占键必须随被拒请求回滚 ---------------------------------------
    def test_rejected_request_leaves_no_reserved_key(self):
        body = {"action": "return", "return_to": "attacker", "reason": "x",
                "issues": [{"message": "缺签章"}]}
        self.assertEqual(self.post(body, key="rolled-back").status_code, 422)
        self.assertEqual(IdempotencyRecord.objects.count(), 0)
        valid = {"action": "return", "return_to": "supplier", "reason": "补料",
                 "issues": [{"message": "缺签章"}]}
        self.assertEqual(self.post(valid, key="rolled-back").status_code, 200)
