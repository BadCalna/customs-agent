"""P10 只读历史：版本/提交/决定分组、跨版本候选对照、无副作用。"""
from apps.reviews.models import Decision, DecisionItem, ItemMember
from tests.p01.factories import assign, make_item, make_task, make_user, make_version

from .factories import HS_A, HS_B, SubmissionFixture


class HistoryApiTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        self.decision = Decision.objects.create(
            submission=self.submission, action=Decision.Action.APPROVE,
            reason="专家通过", created_by=self.expert,
        )
        DecisionItem.objects.create(
            decision=self.decision, item=self.item_a,
            previous_hs=HS_A, final_hs=HS_A, change_reason="",
        )
        self.url = f"/api/v1/tasks/{self.task.pk}/history"
        self.client.force_login(self.operator)

    def test_history_groups_versions_submissions_decisions(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["task_id"], str(self.task.pk))
        self.assertEqual(len(body["versions"]), 1)
        version = body["versions"][0]
        self.assertTrue(version["is_current"])
        self.assertEqual(len(version["items"]), 2)
        self.assertEqual(len(version["confirmations"]), 2)
        self.assertEqual(len(version["submissions"]), 1)
        submission = version["submissions"][0]
        self.assertEqual(submission["submission_id"], str(self.submission.pk))
        self.assertEqual(submission["items"][0]["submitted_hs"], HS_A)
        self.assertEqual(submission["decisions"][0]["action"], "approve")
        self.assertEqual(submission["decisions"][0]["items"][0]["final_hs"], HS_A)

    def test_history_includes_audit_events(self):
        body = self.client.get(self.url).json()
        self.assertIn("audit_events", body)
        self.assertIn("issues", body)

    def test_cross_version_candidates_by_contract_and_name(self):
        ItemMember.objects.create(
            item=self.item_a, source_sheet="装箱明细", source_row=8, contract_no="C1", item_no="1",
        )
        old_version = make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        new_item = make_item(old_version, row=15, name="V2同名商品")
        ItemMember.objects.create(
            item=new_item, source_sheet="装箱明细", source_row=15, contract_no="C1", item_no="1",
        )
        body = self.client.get(self.url).json()
        candidates = body["cross_version_candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["item_id"], str(new_item.pk))
        self.assertEqual(candidate["candidate_item_id"], str(self.item_a.pk))
        self.assertEqual(candidate["basis"], "contract_no+item_no")
        self.assertIn("候选", candidate["note"])
        flags = {v["version_id"]: v["is_current"] for v in body["versions"]}
        self.assertTrue(flags[str(old_version.pk)])            # V2 是当前版本
        self.assertFalse(flags[str(self.version.pk)])          # V1 只读回查

    def test_name_fallback_candidate(self):
        other = make_version(self.task, self.supplier, number=2, filename="V2.xlsx")
        make_item(other, row=9, name="合成商品A")
        body = self.client.get(self.url).json()
        self.assertEqual(body["cross_version_candidates"][0]["basis"], "name")

    def test_get_is_side_effect_free(self):
        first = self.client.get(self.url)
        self.task.refresh_from_db()
        revision, runs = self.task.revision, self.task.runs.count()
        self.client.get(self.url)
        self.task.refresh_from_db()
        self.assertEqual(self.task.revision, revision)
        self.assertEqual(self.task.runs.count(), runs)
        self.assertEqual(first.status_code, 200)

    def test_anonymous_gets_401(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_unassigned_operator_gets_404(self):
        self.client.force_login(self.other_operator)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_other_supplier_gets_404(self):
        self.client.force_login(self.other_supplier)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_expert_can_read_history(self):
        self.client.force_login(self.expert)
        self.assertEqual(self.client.get(self.url).status_code, 200)
