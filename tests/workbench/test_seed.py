"""独立验收种子：初始可操作、版本隔离、真实业务写入后重跑不重置。"""
import json
import os
from io import StringIO
from secrets import token_urlsafe
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from apps.accounts.models import Role, TaskAssignment, User
from apps.accounts.permissions import visible_tasks
from apps.reviews.management.commands.seed_workbench import (
    ACCOUNTS,
    APPROVE_ITEMS,
    APPROVE_TITLE,
    BATCHES,
    DISCLAIMER,
    RETURN_V1_ITEMS,
    RETURN_V2_ITEMS,
    item_key,
    seed_id,
    version_key,
)
from apps.reviews.models import (
    AuditEvent,
    Confirmation,
    Decision,
    DecisionItem,
    FileVersion,
    IdempotencyRecord,
    Issue,
    Item,
    ItemImage,
    ItemMember,
    Submission,
    SubmissionItem,
    Task,
)
from apps.reviews.schemas import ConfirmationIn, DecisionIn, SubmissionIn
from apps.reviews.serializers import item_to_card, item_to_list_entry, task_to_detail
from apps.reviews.services.confirmations import write_confirmation
from apps.reviews.services.decisions import decide
from apps.reviews.services.submissions import create_submission
from apps.runs.models import Run, RunItem

MODELS = (
    User, TaskAssignment, Task, FileVersion, Item, ItemImage, ItemMember,
    Run, RunItem, Confirmation, Submission, SubmissionItem, Decision, DecisionItem,
    Issue, AuditEvent, IdempotencyRecord,
)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SeedWorkbenchTests(TestCase):
    def setUp(self):
        self.demo_password = token_urlsafe(24)
        self.enterContext(patch.dict(os.environ, WORKBENCH_DEMO_PASSWORD=self.demo_password))

    def _seed(self):
        output = StringIO()
        call_command("seed_workbench", "--json", stdout=output)
        return json.loads(output.getvalue())

    def _user(self, role):
        return User.objects.get(role=role, username=f"workbench_{role}")

    def _snapshot(self, ids=None):
        return {
            model: list(
                (model.objects.filter(pk__in=ids[model]) if ids else model.objects.all())
                .order_by("pk").values()
            )
            for model in MODELS
        }

    def _assert_rerun_preserves_every_row(self):
        before = self._snapshot()
        manifest = self._seed()
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(all(not batch["created"] for batch in manifest["batches"]))
        return manifest

    def test_no_default_password_or_partial_seed(self):
        with patch.dict(os.environ, WORKBENCH_DEMO_PASSWORD=""):
            with self.assertRaisesMessage(CommandError, "WORKBENCH_DEMO_PASSWORD"):
                self._seed()
        self.assertFalse(User.objects.exists())
        self.assertFalse(Task.objects.exists())

    @override_settings(ENGINE_MODE="openai")
    def test_non_mock_engine_is_rejected_before_seeding(self):
        with self.assertRaisesMessage(CommandError, "sqlite / mock / inline"):
            self._seed()
        self.assertFalse(User.objects.exists())

    def test_standalone_seed_has_stable_ids_accounts_and_private_grants(self):
        manifest = self._seed()
        self.assertEqual(manifest["result_mode"], "mock")
        self.assertNotIn(self.demo_password, json.dumps(manifest))
        self.assertIn("不证明专业有效性", manifest["disclaimer"])
        self.assertEqual(Task.objects.count(), 2)
        self.assertEqual(FileVersion.objects.count(), 3)
        self.assertEqual(Item.objects.count(), 7)
        self.assertEqual(User.objects.count(), 3)
        self.assertEqual(TaskAssignment.objects.count(), 4)
        self.assertEqual(str(seed_id("approve/task")), "ccd6ba4c-7180-5865-a208-d41a9754a93c")
        self.assertEqual(str(seed_id("return/task")), "a286802f-6708-50e6-b541-be293e860b05")
        self.assertEqual(str(seed_id("return/v2")), "2cfab3fd-3aa2-5106-826f-43068ce3a36d")
        self.assertEqual(
            str(seed_id("return/v2/item/sensor")), "8a226944-e5d7-5df5-b40a-3c84868a680d",
        )
        expected_tasks = {seed_id(f"{batch}/task") for batch, _, _ in BATCHES}
        for username, display_name, role in ACCOUNTS:
            user = User.objects.get(username=username)
            self.assertEqual(user.pk, seed_id(f"account/{username}"))
            self.assertEqual(user.role, role)
            self.assertEqual(user.display_name, display_name)
            self.assertTrue(user.check_password(self.demo_password))
            self.assertFalse(user.is_staff)
            self.assertFalse(user.is_superuser)
            self.assertEqual(set(visible_tasks(user).values_list("pk", flat=True)), expected_tasks)
        self.assertFalse(TaskAssignment.objects.filter(role=Role.SUPPLIER).exists())

    def test_current_batches_begin_with_no_manual_conclusions(self):
        self._seed()
        operator = self._user(Role.OPERATOR)
        for batch, _, versions in BATCHES:
            task = Task.objects.get(pk=seed_id(f"{batch}/task"))
            number, specs = versions[-1]
            detail = task_to_detail(operator, task)
            self.assertEqual(task.stage, Task.Stage.AWAITING_CONFIRMATION)
            self.assertEqual(detail.current_version_id, str(seed_id(version_key(batch, number))))
            self.assertFalse(Issue.objects.filter(item__version_id=detail.current_version_id).exists())
            for spec in specs:
                item = Item.objects.get(pk=seed_id(item_key(batch, number, spec)))
                card = item_to_card(operator, item)
                row = item_to_list_entry(item)
                self.assertEqual(item.revision, 1)
                self.assertEqual(row.supplier_hs_raw, spec.supplier_hs)
                self.assertEqual(card.precheck_suggested_hs.value, spec.suggested_hs)
                self.assertIsNone(row.business_confirmed_hs)
                self.assertIsNone(row.submitted_hs)
                self.assertIsNone(row.expert_final_hs)
                self.assertIsNone(card.business_confirmed_hs.source_id)
                self.assertIsNone(card.submitted_hs.source_id)
                self.assertIsNone(card.expert_final_hs.source_id)
                self.assertIsNone(card.latest_expert_check)
                self.assertFalse(Issue.objects.filter(item=item).exists())
                self.assertFalse(item.confirmations.exists())
            self.assertFalse(Submission.objects.filter(version_id=detail.current_version_id).exists())

    def test_same_names_in_v1_v2_have_different_ids_codes_and_scoped_history(self):
        self._seed()
        expert = self._user(Role.EXPERT)
        old_submission = Submission.objects.get(version_id=seed_id("return/v1"))
        old_decision = Decision.objects.get(submission=old_submission)
        self.assertEqual(old_submission.status, Submission.Status.RETURNED)
        self.assertEqual(old_decision.action, Decision.Action.RETURN)
        self.assertEqual(old_decision.return_to, Decision.ReturnTo.SUPPLIER)
        self.assertFalse(old_decision.items.exists())
        self.assertEqual(old_submission.items.count(), 2)
        self.assertEqual(Issue.objects.count(), 2)
        for old_spec, new_spec in zip(RETURN_V1_ITEMS, RETURN_V2_ITEMS):
            old = Item.objects.get(pk=seed_id(item_key("return", 1, old_spec)))
            current = Item.objects.get(pk=seed_id(item_key("return", 2, new_spec)))
            self.assertEqual(old.name, current.name)
            self.assertEqual(old.source_row, current.source_row)
            self.assertNotEqual(old.pk, current.pk)
            self.assertNotEqual(old.supplier_hs_raw, current.supplier_hs_raw)
            self.assertNotEqual(old.run_items.get().suggested_hs, current.run_items.get().suggested_hs)
            issue = Issue.objects.get(item=old)
            self.assertEqual(issue.item.version_id, old.version_id)
            event = AuditEvent.objects.get(version=old.version, action="decision.returned")
            self.assertIn(str(issue.pk), event.details["issue_ids"])
            self.assertEqual(event.details["submission_id"], str(old_submission.pk))
            self.assertEqual(event.details["decision_id"], str(old_decision.pk))
            self.assertEqual(issue.kind, "expert_return")
            self.assertTrue(issue.message.strip())
            self.assertFalse(Issue.objects.filter(item=current).exists())
            self.assertEqual(old_submission.items.get(item=old).submitted_hs, old_spec.expected_hs)
            self.assertIsNone(item_to_card(expert, current).business_confirmed_hs.value)
        task = Task.objects.get(pk=seed_id("return/task"))
        self.assertEqual(task.stage, Task.Stage.AWAITING_CONFIRMATION)
        self.assertFalse(Issue.objects.filter(item__version_id=seed_id("return/v2")).exists())

    def test_synthetic_engine_rows_keep_originals_and_claim_no_professional_validity(self):
        self._seed()
        self.assertEqual(Run.objects.count(), 3)
        self.assertEqual(RunItem.objects.count(), 7)
        for batch, _, versions in BATCHES:
            for number, specs in versions:
                run = Run.objects.get(version_id=seed_id(version_key(batch, number)))
                self.assertEqual(run.kind, Run.Kind.PRECHECK)
                self.assertEqual(run.status, Run.Status.SUCCEEDED)
                self.assertTrue(run.is_current)
                for spec in specs:
                    item = Item.objects.get(pk=seed_id(item_key(batch, number, spec)))
                    result = run.items.get(item=item)
                    self.assertEqual(item.supplier_hs_raw, spec.supplier_hs)
                    self.assertEqual(result.suggested_hs, spec.suggested_hs)
                    self.assertNotEqual(result.suggested_hs, item.supplier_hs_raw)
                    self.assertEqual(result.comparison_status, RunItem.ComparisonStatus.INCONSISTENT)
                    self.assertEqual(result.result["result_mode"], "mock")
                    self.assertTrue(result.result["synthetic"])
                    self.assertEqual(result.result["disclaimer"], DISCLAIMER)
                    self.assertIsNone(result.confidence)
                    self.assertEqual(result.result["evidence"][0]["source_type"], "synthetic")
                    self.assertFalse(result.result["evidence"][0]["query"]["external_query_performed"])

    def test_repeat_seed_changes_no_rows_or_timestamps(self):
        self._seed()
        self._assert_rerun_preserves_every_row()

    def _confirm_and_submit(self, batch, number, specs):
        operator = self._user(Role.OPERATOR)
        task = Task.objects.get(pk=seed_id(f"{batch}/task"))
        lines = []
        for spec in specs:
            item = Item.objects.get(pk=seed_id(item_key(batch, number, spec)))
            confirmation, _, _, _ = write_confirmation(
                operator, str(item.version_id), str(item.pk),
                ConfirmationIn(confirmed_hs=spec.expected_hs, expected_revision=item.revision,
                               reason=f"合成验收选择 {spec.choice}；不作为真实归类结论"),
            )
            lines.append({"item_id": str(item.pk), "confirmation_id": str(confirmation.pk)})
        # 先证明人工确认保留，再继续整批提交与异步回调。
        self._assert_rerun_preserves_every_row()
        task.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            response = create_submission(
                operator, str(task.pk), SubmissionIn(
                    version_id=str(seed_id(version_key(batch, number))), revision=task.revision, items=lines,
                ),
            )
        submission = Submission.objects.get(pk=response["submission_id"])
        self.assertEqual(
            {str(line.item_id): line.submitted_hs for line in submission.items.all()},
            {str(seed_id(item_key(batch, number, spec))): spec.expected_hs for spec in specs},
        )
        return submission

    def test_real_confirmation_submit_approve_return_flows_survive_rerun(self):
        self._seed()
        approved = self._confirm_and_submit("approve", 1, APPROVE_ITEMS)
        returned = self._confirm_and_submit("return", 2, RETURN_V2_ITEMS)
        self._assert_rerun_preserves_every_row()
        expert = self._user(Role.EXPERT)
        approved.task.refresh_from_db()
        decide(expert, str(approved.pk), DecisionIn(
            action="approve", reason="合成整批通过验收",
            items=[{"item_id": str(line.item_id), "final_hs": line.submitted_hs} for line in approved.items.all()],
        ))
        returned.task.refresh_from_db()
        decide(expert, str(returned.pk), DecisionIn(
            action="return", return_to="supplier",
            reason="合成 V2 专家退回验收：补充传感组件用途和连接组件材质",
            issues=[{"item_id": str(line.item_id), "message": f"合成 V2：请为 {line.item.name} 补充规格说明"}
                    for line in returned.items.select_related("item")],
        ))
        approved.refresh_from_db()
        returned.refresh_from_db()
        self.assertEqual(approved.status, Submission.Status.APPROVED)
        self.assertEqual(returned.status, Submission.Status.RETURNED)
        self.assertEqual(DecisionItem.objects.filter(decision__submission=approved).count(), len(APPROVE_ITEMS))
        self.assertFalse(DecisionItem.objects.filter(decision__submission=returned).exists())
        new_issues = Issue.objects.filter(item__version=returned.version)
        self.assertEqual(new_issues.count(), len(RETURN_V2_ITEMS))
        self.assertEqual(set(new_issues.values_list("item_id", flat=True)), set(returned.items.values_list("item_id", flat=True)))
        manifest = self._assert_rerun_preserves_every_row()
        self.assertEqual([batch["actual_stage"] for batch in manifest["batches"]], ["approved", "returned"])
        # 执行完成后，供应商原值与合成 AI 建议仍保留，不因人工选择而回填。
        for batch, number, specs in (("approve", 1, APPROVE_ITEMS), ("return", 2, RETURN_V2_ITEMS)):
            for spec in specs:
                item = Item.objects.get(pk=seed_id(item_key(batch, number, spec)))
                self.assertEqual(item.supplier_hs_raw, spec.supplier_hs)
                self.assertEqual(item.run_items.get(run__kind=Run.Kind.PRECHECK).suggested_hs, spec.suggested_hs)

    def test_password_renamed_task_revoked_grant_and_new_version_are_kept(self):
        self._seed()
        operator = self._user(Role.OPERATOR)
        changed_password = token_urlsafe(24)
        operator.set_password(changed_password)
        operator.display_name = "人工改名"
        operator.save(update_fields=["password", "display_name"])
        task = Task.objects.get(pk=seed_id("approve/task"))
        task.title = "改过的验收任务"
        task.stage = Task.Stage.RETURNED
        task.revision = 42
        task.save(update_fields=["title", "stage", "revision", "updated_at"])
        TaskAssignment.objects.filter(task=task, user=operator).delete()
        FileVersion.objects.create(task=task, number=2, original_filename="合成新上传.xlsx", uploaded_by=task.supplier)
        Issue.objects.update(status=Issue.Status.RESOLVED)
        self._assert_rerun_preserves_every_row()
        operator.refresh_from_db()
        self.assertTrue(operator.check_password(changed_password))
        self.assertFalse(TaskAssignment.objects.filter(task=task, user=operator).exists())

    def test_existing_demo_contracts_and_same_title_business_task_are_untouched(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_contracts", stdout=StringIO())
        Task.objects.create(
            title=APPROVE_TITLE, supplier=User.objects.get(username="supplier_a"),
            stage=Task.Stage.APPROVED, revision=99,
        )
        before = self._snapshot()
        ids = {model: [row["id"] for row in rows] for model, rows in before.items()}
        self._seed()
        self.assertEqual(self._snapshot(ids), before)
        for role in (Role.SUPPLIER, Role.OPERATOR, Role.EXPERT):
            self.assertEqual(visible_tasks(self._user(role)).count(), 2)

    def test_account_name_collision_rolls_back_without_taking_over_existing_user(self):
        User.objects.create_user(username="workbench_operator", password=token_urlsafe(24), role=Role.OPERATOR)
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, "拒绝接管"):
            self._seed()
        self.assertEqual(self._snapshot(), before)

    def test_task_uuid_collision_rolls_back_without_changing_other_supplier_data(self):
        supplier = User.objects.create_user(username="unrelated_supplier", role=Role.SUPPLIER)
        Task.objects.create(pk=seed_id("approve/task"), supplier=supplier, title="其它业务", stage=Task.Stage.APPROVED)
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, "其它供应商"):
            self._seed()
        self.assertEqual(self._snapshot(), before)
