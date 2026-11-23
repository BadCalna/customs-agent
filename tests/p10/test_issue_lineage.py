"""人工退回问题按版本/提交定位；同名商品绝不继承历史问题与批准。"""
import json

from django.core.exceptions import ValidationError

from apps.reviews.models import Decision, DecisionItem, Issue, Submission, SubmissionItem
from apps.runs.models import Run, RunItem
from tests.p01.factories import make_item, make_task, make_version
from tests.p10.factories import HS_A, SubmissionFixture


class IssueLineageTests(SubmissionFixture):
    def _post(self, url, body, user):
        self.client.force_login(user)
        return self.client.post(url, data=json.dumps(body), content_type="application/json")

    def _expert_return(self, submission=None, issues=None):
        submission = submission or self.submission
        return self._post(f"/api/v1/submissions/{submission.pk}/decisions", {
            "action": "return", "return_to": "supplier", "reason": "需要补充材料",
            "issues": issues or [
                {"item_id": str(self.item_a.pk), "message": "V1商品A缺规格书"},
                {"message": "V1整版缺签章"},
            ],
        }, self.expert)

    def _card(self, item):
        response = self.client.get(f"/api/v1/versions/{item.version_id}/items/{item.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _new_snapshot(self, revision):
        submission = Submission.objects.create(
            task=self.task, version=self.version, revision=revision, created_by=self.operator,
        )
        for line in (self.line_a, self.line_b):
            SubmissionItem.objects.create(
                submission=submission, item=line.item, confirmation=line.confirmation,
                submitted_hs=line.submitted_hs, item_revision=line.item_revision,
            )
        return submission

    def test_return_response_and_history_retain_exact_provenance_and_location(self):
        response = self._expert_return()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(len(body["issues"]), 2)
        self.assertEqual(body["items"], [])
        self.assertEqual(DecisionItem.objects.count(), 0)
        for entry in body["issues"]:
            issue = Issue.objects.get(pk=entry["issue_id"])
            self.assertEqual(issue.version_id, self.version.pk)
            self.assertEqual(issue.submission_id, self.submission.pk)
            self.assertEqual(str(issue.decision_id), body["decision_id"])
            self.assertEqual(entry["task_id"], str(self.task.pk))
            self.assertEqual(entry["version_id"], str(self.version.pk))
            self.assertEqual(entry["submission_id"], str(self.submission.pk))
            self.assertEqual(entry["decision_id"], body["decision_id"])
            self.assertEqual(entry["return_to"], "supplier")
            self.assertEqual(entry["message"], issue.message)
            self.assertEqual(entry["source_location"] is None, issue.item_id is None)
            if issue.item_id:
                self.assertEqual(entry["source_location"]["row_number"], issue.item.source_row)
        history = self.client.get(f"/api/v1/tasks/{self.task.pk}/history").json()
        self.assertEqual(history["issues"], body["issues"])

    def test_same_name_v1_v2_item_and_version_issues_never_cross(self):
        Run.objects.create(
            task=self.task, version=self.version, submission=self.submission,
            kind=Run.Kind.EXPERT_CHECK, is_current=True,
        )
        first = self._expert_return().json()
        old_issue_ids = {entry["issue_id"] for entry in first["issues"]}
        v2 = make_version(self.task, self.supplier, number=2)
        a2 = make_item(v2, row=self.item_a.source_row, name=self.item_a.name, supplier_hs="8536901900")
        self.task.refresh_from_db()
        second = self._post(f"/api/v1/tasks/{self.task.pk}/returns", {
            "version_id": str(v2.pk), "revision": self.task.revision,
            "reason": "V2仍需补料", "issues": [
                {"item_id": str(a2.pk), "message": "V2缺材质说明"},
                {"message": "V2缺运输证明"},
            ],
        }, self.operator)
        self.assertEqual(second.status_code, 200, second.content)
        new_issues = second.json()["issues"]
        new_issue_ids = {entry["issue_id"] for entry in new_issues}
        self.assertTrue(old_issue_ids.isdisjoint(new_issue_ids))
        for entry in new_issues:
            self.assertEqual(entry["version_id"], str(v2.pk))
            self.assertIsNone(entry["submission_id"])
            self.assertIsNone(entry["decision_id"])
        old_card = self._card(self.item_a)
        new_card = self._card(a2)
        self.assertEqual({i["issue_id"] for i in old_card["review_issues"]}, old_issue_ids)
        self.assertEqual({i["issue_id"] for i in new_card["review_issues"]}, new_issue_ids)
        for field in ("business_confirmed_hs", "submitted_hs", "expert_final_hs"):
            self.assertIsNone(new_card[field]["value"])
            self.assertIsNone(new_card[field]["source_id"])
        detail = self.client.get(f"/api/v1/tasks/{self.task.pk}").json()
        self.assertIsNone(detail["current_submission_id"])
        self.assertIsNone(detail["current_submission_status"])
        self.assertIsNone(detail["latest_run_id"])
        self.assertIsNone(detail["latest_run_status"])
        self.assertEqual({i["issue_id"] for i in detail["review_issues"]}, new_issue_ids)
        history = self.client.get(f"/api/v1/tasks/{self.task.pk}/history").json()
        self.assertEqual({i["issue_id"] for i in history["issues"]}, old_issue_ids | new_issue_ids)

    def test_v1_approved_code_never_becomes_v2_expert_final_code_by_name(self):
        response = self._post(f"/api/v1/submissions/{self.submission.pk}/decisions", {
            "action": "approve", "revision": self.task.revision, "items": [
                {"item_id": str(self.item_a.pk), "final_hs": "8536901900", "change_reason": "按实际功能改码"},
                {"item_id": str(self.item_b.pk), "final_hs": self.line_b.submitted_hs},
            ],
        }, self.expert)
        self.assertEqual(response.status_code, 200, response.content)
        v2 = make_version(self.task, self.supplier, number=2)
        new_item = make_item(v2, row=self.item_a.source_row, name=self.item_a.name)
        old_card = self._card(self.item_a)
        new_card = self._card(new_item)
        self.assertEqual(old_card["expert_final_hs"]["value"], "8536901900")
        self.assertEqual(old_card["expert_final_hs"]["source_id"], response.json()["decision_id"])
        self.assertIsNone(new_card["expert_final_hs"]["value"])
        self.assertIsNone(new_card["expert_final_hs"]["source_id"])
        self.assertIsNone(new_card["submission_status"])

    def test_item_issue_does_not_leak_to_same_name_sibling_in_same_version(self):
        self.item_b.name = self.item_a.name
        self.item_b.save(update_fields=["name"])
        response = self._expert_return()
        self.assertEqual(response.status_code, 200, response.content)
        own = self._card(self.item_a)["review_issues"]
        sibling = self._card(self.item_b)["review_issues"]
        self.assertEqual(len(own), 2)
        self.assertEqual(len(sibling), 1)
        self.assertIsNone(sibling[0]["item_id"])
        self.assertEqual(sibling[0]["message"], "V1整版缺签章")

    def test_same_version_resubmission_issues_keep_distinct_decision_ids(self):
        first = self._expert_return(issues=[{"message": "第一次缺签章"}]).json()
        second_submission = self._new_snapshot(revision=2)
        second = self._expert_return(second_submission, [{"message": "第二次缺材质证明"}]).json()
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        entries = self._card(self.item_a)["review_issues"]
        self.assertEqual({i["submission_id"] for i in entries}, {str(self.submission.pk), str(second_submission.pk)})
        self.assertEqual({i["decision_id"] for i in entries}, {first["decision_id"], second["decision_id"]})

    def test_unattributed_legacy_task_issue_is_history_only(self):
        issue = Issue.objects.create(task=self.task, kind="legacy", message="来源版本无法证明")
        self.client.force_login(self.operator)
        self.assertEqual(self._card(self.item_a)["review_issues"], [])
        detail = self.client.get(f"/api/v1/tasks/{self.task.pk}").json()
        self.assertEqual(detail["review_issues"], [])
        history = self.client.get(f"/api/v1/tasks/{self.task.pk}/history").json()
        self.assertEqual(history["issues"][0]["issue_id"], str(issue.pk))
        self.assertIsNone(history["issues"][0]["version_id"])

    def test_issue_creation_infers_only_related_version_and_rejects_crossed_references(self):
        issue = Issue.objects.create(task=self.task, item=self.item_a, kind="image", message="读图失败")
        self.assertEqual(issue.version_id, self.version.pk)
        other_task = make_task(self.supplier)
        v2 = make_version(self.task, self.supplier, number=2)
        for overrides in ({"task": other_task}, {"version": v2}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                Issue.objects.create(
                    **{"task": self.task, "item": self.item_a, "kind": "invalid", "message": "x", **overrides},
                )
        decision = Decision.objects.create(
            submission=self.submission, action=Decision.Action.RETURN,
            return_to="supplier", reason="补料", created_by=self.expert,
        )
        inferred = Issue.objects.create(task=self.task, decision=decision, kind="expert_return", message="缺规格")
        self.assertEqual(inferred.version_id, self.version.pk)
        self.assertEqual(inferred.submission_id, self.submission.pk)
        second = self._new_snapshot(revision=2)
        with self.assertRaises(ValidationError):
            Issue.objects.create(task=self.task, submission=second, decision=decision, kind="invalid", message="x")
        with self.assertRaises(ValidationError):
            Issue.objects.create(task=self.task, submission=second, version=v2, kind="invalid", message="x")

    def test_expert_reference_fallback_uses_its_run_submission_not_newer_snapshot(self):
        old_run = Run.objects.create(
            task=self.task, version=self.version, submission=self.submission,
            kind=Run.Kind.EXPERT_CHECK, status=Run.Status.SUCCEEDED, triggered_by=self.expert, is_current=True,
        )
        RunItem.objects.create(run=old_run, item=self.item_a, status=RunItem.Status.SUCCEEDED, result={})
        # 第二次提交换了 HS；旧核查缺 target_hs 时只能回到旧快照，不能跟随最新提交。
        self.submission.status = Submission.Status.RETURNED
        self.submission.save(update_fields=["status"])
        from apps.reviews.models import Confirmation
        from apps.reviews.serializers import item_to_card

        self.item_a.revision = 2
        self.item_a.save(update_fields=["revision"])
        new_confirmation = Confirmation.objects.create(
            item=self.item_a, revision=2, previous_hs=HS_A, confirmed_hs="8536901900", created_by=self.operator,
        )
        new_submission = Submission.objects.create(
            task=self.task, version=self.version, revision=2, created_by=self.operator,
        )
        SubmissionItem.objects.create(
            submission=new_submission, item=self.item_a, confirmation=new_confirmation,
            submitted_hs=new_confirmation.confirmed_hs, item_revision=2,
        )
        card = item_to_card(self.expert, self.item_a)
        self.assertEqual(card.submitted_hs.value, new_confirmation.confirmed_hs)
        self.assertEqual(card.latest_expert_check.submission_id, str(self.submission.pk))
        self.assertEqual(card.latest_expert_check.target_hs, HS_A)
