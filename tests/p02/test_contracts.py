"""契约发布物的一致性：schema 不漂移、OpenAPI 不漂移、样例可校验。

这些检查不需要数据库，供 `make test-contracts` 独立运行。
"""
import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.accounts.permissions import ROLE_ACTIONS
from apps.api.fixtures import FixtureDoc
from apps.api.management.commands.export_contracts import SCHEMAS
from apps.api.schemas import (
    CONTRACT_VERSION,
    Action,
    ComparisonStatus,
    ImageStatus,
    RunStatus,
    SubmissionStatus,
    TaskStage,
)
from apps.reviews.models import ItemImage, Submission, Task
from apps.runs.models import Run, RunItem

CONTRACTS = Path(settings.BASE_DIR) / "contracts"


class SchemaDriftTest(SimpleTestCase):
    def test_schema_files_match_code(self):
        for group, models in SCHEMAS.items():
            for model in models:
                path = CONTRACTS / "schemas" / group / f"{model.__name__}.json"
                self.assertTrue(path.exists(), f"缺少契约文件 {path}，请执行 make contracts")
                self.assertEqual(
                    json.loads(path.read_text()), model.model_json_schema(),
                    f"{model.__name__} 的契约文件与代码不一致，请执行 make contracts 并提交差异",
                )

    def test_openapi_matches_code(self):
        from config.urls import api_v1

        path = CONTRACTS / "openapi" / "v1.json"
        self.assertTrue(path.exists(), "缺少 contracts/openapi/v1.json")
        # get_openapi_schema 返回 pydantic 对象，先归一成 JSON 数据再比较。
        current = json.loads(json.dumps(api_v1.get_openapi_schema()))
        self.assertEqual(json.loads(path.read_text()), current)

    def test_contract_version_constant(self):
        self.assertEqual(CONTRACT_VERSION, "1.1.0")


class EnumAlignmentTest(SimpleTestCase):
    """契约枚举与数据库枚举必须同源，防止两边各自漂移。"""

    def test_comparison_status_six_values(self):
        self.assertEqual(
            {item.value for item in ComparisonStatus}, set(RunItem.ComparisonStatus.values),
        )
        self.assertEqual(len(ComparisonStatus), 6)

    def test_other_enums_match_models(self):
        self.assertEqual({i.value for i in TaskStage}, set(Task.Stage.values))
        self.assertEqual({i.value for i in SubmissionStatus}, set(Submission.Status.values))
        self.assertEqual({i.value for i in ImageStatus}, set(ItemImage.Status.values))
        self.assertEqual({i.value for i in RunStatus}, set(Run.Status.values))
        # 行状态是执行状态的子集：superseded 只发生在整轮执行上，不会出现在单行。
        self.assertLessEqual(set(RunItem.Status.values), {i.value for i in RunStatus})

    def test_actions_match_permission_matrix(self):
        expected = set().union(*ROLE_ACTIONS.values())
        self.assertEqual({i.value for i in Action}, expected)


class FixtureTest(SimpleTestCase):
    def _load(self, scenario):
        return json.loads((CONTRACTS / "fixtures" / f"{scenario}.json").read_text())

    def test_all_ten_scenarios_present_and_valid(self):
        expected = {f"FIX-{index:02d}" for index in range(1, 11)}
        actual = {path.stem for path in (CONTRACTS / "fixtures").glob("FIX-*.json")}
        self.assertEqual(actual, expected)
        for scenario in sorted(expected):
            doc = FixtureDoc.model_validate(self._load(scenario))
            self.assertEqual(doc.scenario, scenario)
            self.assertEqual(doc.contract_version, CONTRACT_VERSION)

    def test_fix02_missing_facts_keeps_null_with_run_reference(self):
        card = FixtureDoc.model_validate(self._load("FIX-02")).card
        self.assertIsNone(card.precheck_suggested_hs.value)
        self.assertIsNotNone(card.precheck_suggested_hs.source_id)
        self.assertEqual(card.comparison_status, ComparisonStatus.NEEDS_CONFIRMATION)
        # 推荐与比较两阶段会报告同一条缺事实问题，落库时必须去重。
        self.assertEqual([issue.code for issue in card.issues], ["MISSING_USAGE"])

    def test_fix04_blank_and_invalid_are_distinct(self):
        doc = FixtureDoc.model_validate(self._load("FIX-04"))
        self.assertEqual(doc.card.facts.supplier_hs_raw, "")
        self.assertEqual(doc.card.comparison_status, ComparisonStatus.SUPPLIER_MISSING)
        self.assertEqual(doc.related_cards[0].facts.supplier_hs_raw, "ABC")
        self.assertEqual(doc.related_cards[0].comparison_status, ComparisonStatus.SUPPLIER_INVALID)

    def test_fix05_partial_failure_counts(self):
        run = FixtureDoc.model_validate(self._load("FIX-05")).run
        self.assertEqual(run.progress.total, 8)
        self.assertEqual(run.progress.failed, 1)
        self.assertEqual(run.progress.succeeded, 7)
        failed = [row for row in run.items if row.status == RunStatus.FAILED]
        self.assertTrue(failed[0].error_message)

    def test_fix07_empty_page_and_forbidden_are_separate(self):
        doc = FixtureDoc.model_validate(self._load("FIX-07"))
        self.assertEqual(doc.task_page.items, [])
        self.assertEqual(doc.error.error.code, "not_found")

    def test_fix08_conflict_shape(self):
        doc = FixtureDoc.model_validate(self._load("FIX-08"))
        self.assertEqual(doc.error.error.code, "conflict")

    def test_fix10_recode_keeps_original_target(self):
        card = FixtureDoc.model_validate(self._load("FIX-10")).card
        self.assertEqual(card.latest_expert_check.target_hs, card.submitted_hs.value)
        self.assertNotEqual(card.expert_final_hs.value, card.submitted_hs.value)

    def test_fix09_old_version_run_remains_readable(self):
        doc = FixtureDoc.model_validate(self._load("FIX-09"))
        self.assertTrue(doc.run.is_current)
        self.assertNotEqual(doc.run.version_id, doc.card.version_id)
        self.assertNotEqual(doc.run.run_id, doc.card.latest_run_id)
