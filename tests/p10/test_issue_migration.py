"""0009 的保守回填：有证据才归属，不能把历史任务级问题猜到最新版本。"""
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.reviews.models import AuditEvent, Decision, Issue
from apps.runs.models import Run
from tests.p01.factories import make_version
from tests.p10.factories import SubmissionFixture


class IssueBackfillTests(SubmissionFixture):
    def setUp(self):
        super().setUp()
        old_apps = MigrationExecutor(connection).loader.project_state(
            [("reviews", "0008_import_job_superseded")],
        ).apps
        self.LegacyIssue = old_apps.get_model("reviews", "Issue")
        self.backfill = import_module("apps.reviews.migrations.0009_issue_lineage").backfill_issue_lineage

    def _run_migration_data(self):
        self.backfill(apps, SimpleNamespace(connection=connection))

    def test_legacy_item_run_and_explicit_audit_ids_only_backfill_proven_lineage(self):
        item_issue = self.LegacyIssue.objects.create(
            task_id=self.task.pk, item_id=self.item_a.pk, kind="expert_return", message="旧商品缺规格",
        )
        run = Run.objects.create(
            task=self.task, version=self.version, submission=self.submission,
            kind=Run.Kind.EXPERT_CHECK,
        )
        run_issue = self.LegacyIssue.objects.create(
            task_id=self.task.pk, run_id=run.pk, kind="technical", message="旧执行错误",
        )
        version_issue = self.LegacyIssue.objects.create(
            task_id=self.task.pk, kind="operator_return", message="整版缺签章",
        )
        unknown = self.LegacyIssue.objects.create(
            task_id=self.task.pk, kind="expert_return", message="旧任务问题无归属依据",
        )
        decision = Decision.objects.create(
            submission=self.submission, action="return", return_to="supplier",
            reason="补料", created_by=self.expert,
        )
        AuditEvent.objects.create(
            task=self.task, version=self.version, action="task.returned", stage="business_return",
            details={"issue_ids": [str(version_issue.pk)], "return_to": "supplier"},
        )
        # 仅有相近时间/相同任务/一份退回决定，都不足以断言 unknown 属于这个快照。
        AuditEvent.objects.create(
            task=self.task, version=self.version, action="decision.returned", stage="expert_decision",
            details={"decision_id": str(decision.pk), "submission_id": str(self.submission.pk)},
        )
        make_version(self.task, self.supplier, number=2)
        self._run_migration_data()
        for original in (item_issue, run_issue, version_issue):
            row = Issue.objects.get(pk=original.pk)
            self.assertEqual(row.version_id, self.version.pk)
            self.assertIsNone(row.decision_id)
        self.assertEqual(Issue.objects.get(pk=run_issue.pk).submission_id, self.submission.pk)
        self.assertIsNone(Issue.objects.get(pk=item_issue.pk).submission_id)
        self.assertIsNone(Issue.objects.get(pk=unknown.pk).version_id)
        before = list(Issue.objects.order_by("pk").values())
        self._run_migration_data()
        self.assertEqual(before, list(Issue.objects.order_by("pk").values()))

    def test_conflicting_legacy_item_and_audit_versions_are_not_guessed(self):
        v2 = make_version(self.task, self.supplier, number=2)
        original = self.LegacyIssue.objects.create(
            task_id=self.task.pk, item_id=self.item_a.pk, kind="operator_return", message="旧数据归属冲突",
        )
        AuditEvent.objects.create(
            task=self.task, version=v2, action="task.returned", stage="business_return",
            details={"issue_ids": [str(original.pk)]},
        )
        self._run_migration_data()
        row = Issue.objects.get(pk=original.pk)
        self.assertIsNone(row.version_id)
        self.assertIsNone(row.submission_id)
        self.assertIsNone(row.decision_id)
