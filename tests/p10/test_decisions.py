"""P10 专家处置：整批通过、改码留痕、退回、失效提交与幂等。"""
import json

from apps.accounts.models import Role
from apps.reviews.models import AuditEvent, Confirmation, Decision, DecisionItem, Issue, Submission, SubmissionItem, Task

from .factories import HS_A, HS_B, SubmissionFixture

HS_ALT = "8536901900"


class DecisionApiTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        self.url = f"/api/v1/submissions/{self.submission.pk}/decisions"
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

    def post(self, body=None, key=None):
        headers = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
        return self.client.post(
            self.url, data=json.dumps(body or self.approve_payload()),
            content_type="application/json", **headers,
        )

    def test_approve_whole_batch(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["submission_status"], "approved")
        self.assertEqual(body["task_stage"], "approved")
        self.assertEqual(len(body["items"]), 2)

        self.submission.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.submission.status, Submission.Status.APPROVED)
        self.assertEqual(self.task.stage, Task.Stage.APPROVED)
        decision = Decision.objects.get(submission=self.submission)
        self.assertEqual(decision.created_by, self.expert)
        self.assertEqual(decision.items.count(), 2)
        # 不改码：终值等于提交值，前值仍是快照里的待审 HS。
        row = decision.items.get(item=self.item_a)
        self.assertEqual(row.final_hs, HS_A)
        self.assertEqual(row.previous_hs, HS_A)
        self.assertEqual(row.change_reason, "")

        card = self.client.get(f"/api/v1/versions/{self.version.pk}/items/{self.item_a.pk}").json()
        self.assertEqual(card["expert_final_hs"]["value"], HS_A)
        self.assertEqual(card["expert_final_hs"]["source_id"], str(decision.pk))

    def test_recode_keeps_previous_value_reason_and_audit(self):
        body = self.approve_payload()
        body["items"][0] = {"item_id": str(self.item_a.pk), "final_hs": HS_ALT, "change_reason": "要素冲突"}
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        row = DecisionItem.objects.get(decision__submission=self.submission, item=self.item_a)
        self.assertEqual(row.previous_hs, HS_A)
        self.assertEqual(row.final_hs, HS_ALT)
        event = AuditEvent.objects.get(task=self.task, action="decision.recoded")
        self.assertEqual(event.previous_value, HS_A)
        self.assertEqual(event.new_value, HS_ALT)
        # 专家改码后，快照里的待审 HS 与专家 AI 核查对象都不被改写。
        self.assertEqual(self.submission.items.get(item=self.item_a).submitted_hs, HS_A)

    def test_recode_without_reason_rejected(self):
        body = self.approve_payload()
        body["items"][0] = {"item_id": str(self.item_a.pk), "final_hs": HS_ALT, "change_reason": "  "}
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "missing_change_reason")
        self.assertEqual(Decision.objects.count(), 0)

    def test_partial_approve_rejected(self):
        body = self.approve_payload(items=[self.approve_payload()["items"][0]])
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "incomplete_items")

    def test_item_outside_submission_rejected(self):
        body = self.approve_payload()
        body["items"][0]["item_id"] = str(self.item_b.pk)
        response = self.post(body)
        self.assertEqual(response.status_code, 422)

    def test_invalid_final_hs_rejected(self):
        body = self.approve_payload()
        body["items"][0]["final_hs"] = "ABC"
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_hs")

    def test_approve_requires_items(self):
        response = self.post(self.approve_payload(items=[]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "empty_decision")

    def test_return_records_target_and_issues(self):
        body = {
            "action": "return", "return_to": "supplier", "reason": "缺申报要素",
            "issues": [{"item_id": str(self.item_a.pk), "message": "请补充用途与材质"}],
        }
        response = self.post(body)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["submission_status"], "returned")
        self.assertEqual(payload["task_stage"], "returned")
        self.assertEqual(payload["return_to"], "supplier")
        self.assertEqual(payload["items"], [])

        decision = Decision.objects.get(submission=self.submission)
        self.assertEqual(decision.action, Decision.Action.RETURN)
        self.assertEqual(decision.items.count(), 0)  # 退回不产生终值
        issue = Issue.objects.get(task=self.task, kind="expert_return")
        self.assertEqual(issue.item_id, self.item_a.pk)
        self.assertEqual(issue.message, "请补充用途与材质")
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, Task.Stage.RETURNED)

    def test_return_requires_target(self):
        body = {"action": "return", "reason": "x", "issues": [{"message": "y"}]}
        self.assertEqual(self.post(body).status_code, 422)

    def test_return_requires_issues(self):
        body = {"action": "return", "return_to": "operator", "reason": "x", "issues": [{"message": "  "}]}
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "missing_issues")

    def test_return_cannot_carry_final_values(self):
        body = self.approve_payload(action="return", return_to="supplier")
        response = self.post(body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "return_has_final_values")

    def test_decided_submission_cannot_be_decided_twice(self):
        self.post()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "submission_closed")
        self.assertEqual(Decision.objects.count(), 1)

    def test_old_submission_after_new_version_is_invalid(self):
        # 新版本上传后，旧提交的批准不能授予新版本（B08）。
        from tests.p01.factories import make_version

        make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_submission")
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, Submission.Status.PENDING)

    def test_operator_cannot_decide(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.post().status_code, 403)

    def test_supplier_cannot_decide(self):
        self.client.force_login(self.supplier)
        self.assertEqual(self.post().status_code, 403)

    def test_unassigned_expert_gets_404(self):
        self.client.force_login(self.other_expert)
        response = self.post()
        self.assertEqual(response.status_code, 404)

    def test_anonymous_gets_401(self):
        self.client.logout()
        self.assertEqual(self.post().status_code, 401)

    def test_return_issue_must_belong_to_the_submission_being_decided(self):
        """专家退回 V2 时传入 V1 的商品行必须被拒绝（Jay 评审）。"""
        from tests.p01.factories import make_item, make_version

        v2 = make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        item_v2 = make_item(v2, row=5, name="V2商品")
        confirmation = Confirmation.objects.create(
            item=item_v2, revision=1, previous_hs="", confirmed_hs=HS_A,
            reason="合成确认", created_by=self.operator,
        )
        sub2 = Submission.objects.create(
            task=self.task, version=v2, revision=self.task.revision, created_by=self.operator,
        )
        SubmissionItem.objects.create(
            submission=sub2, item=item_v2, confirmation=confirmation,
            submitted_hs=HS_A, item_revision=1,
        )
        body = {
            "action": "return", "return_to": "supplier", "reason": "缺资料",
            "issues": [{"item_id": str(self.item_a.pk), "message": "错挂到 V1 商品行"}],
        }
        response = self.client.post(
            f"/api/v1/submissions/{sub2.pk}/decisions",
            data=json.dumps(body), content_type="application/json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "item_not_in_submission")
        self.assertEqual(Decision.objects.count(), 0)

    def test_idempotent_replay_returns_same_decision(self):
        first = self.post(key="d-1").json()
        second = self.post(key="d-1").json()
        self.assertEqual(second["decision_id"], first["decision_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(Decision.objects.count(), 1)

    def test_same_key_different_payload_returns_409(self):
        self.post(key="d-2")
        response = self.post(self.approve_payload(reason="另一个决定"), key="d-2")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
