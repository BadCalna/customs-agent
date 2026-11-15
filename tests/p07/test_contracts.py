"""P07 依赖的契约 fixture 与 schema 漂移防线。

组件只认 D3 第 3 节字段。这里用 P02 已发布的 ``FixtureDoc``/``ItemCard``
Pydantic 模型校验五份固定样例，并把 ItemCard schema 的枚举、必填字段与组件源码
对照：schema 或 fixture 改了而组件没跟上时，测试会给出具体缺项，而不是等到
浏览器里才发现空白卡片。
"""
import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.api.fixtures import FixtureDoc

ROOT = Path(settings.BASE_DIR)
FIXTURE_DIR = ROOT / "contracts" / "fixtures"
ITEM_CARD_SCHEMA = ROOT / "contracts" / "schemas" / "items" / "ItemCard.json"
COMPONENTS = ROOT / "web" / "components"

SCENARIOS = ("FIX-01", "FIX-02", "FIX-03", "FIX-04", "FIX-10")
STAGE_FIELDS = (
    "precheck_suggested_hs",
    "business_confirmed_hs",
    "submitted_hs",
    "expert_suggested_hs",
    "expert_final_hs",
)
CARD_FIELDS_USED_BY_P07 = (
    "task_id",
    "version_id",
    "item_id",
    "revision",
    "source_location",
    "facts",
    "images",
    "image_status",
    "precheck_suggested_hs",
    "business_confirmed_hs",
    "submitted_hs",
    "expert_suggested_hs",
    "expert_final_hs",
    "comparison_status",
    "issues",
    "evidence_status",
    "confidence",
    "latest_run_id",
    "latest_expert_check",
    "result_mode",
    "task_stage",
    "allowed_actions",
)


def load_fixture(scenario: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{scenario}.json").read_text("utf-8"))


def component_source() -> str:
    return "\n".join(
        (COMPONENTS / name).read_text("utf-8")
        for name in ("commodity-list.js", "commodity-card.js", "hs-comparison.js")
    )


class P07FixtureContractTest(SimpleTestCase):
    """五份输入样例必须能被契约模型解析，且字段足够驱动本包组件。"""

    def test_required_scenarios_parse_through_fixture_schema(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                payload = load_fixture(scenario)
                self.assertEqual(payload["scenario"], scenario)
                doc = FixtureDoc.model_validate(payload)
                self.assertIsNotNone(doc.card, f"{scenario} 必须包含 card")
                self.assertTrue(doc.title, f"{scenario} 缺少 title")

    def test_cards_expose_every_p07_field(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                card = load_fixture(scenario)["card"]
                missing = [field for field in CARD_FIELDS_USED_BY_P07 if field not in card]
                self.assertFalse(missing, f"{scenario} 缺字段：{missing}")
                for stage in STAGE_FIELDS:
                    self.assertIn(stage, card)
                    self.assertIsInstance(card[stage], dict)
                    self.assertIn("value", card[stage])
                    self.assertIn("source_id", card[stage])

    def test_fix10_check_targets_the_submitted_code_not_the_new_final(self):
        payload = load_fixture("FIX-10")
        card = FixtureDoc.model_validate(payload).card
        self.assertIsNotNone(card)
        self.assertIsNotNone(card.latest_expert_check, "FIX-10 必须带专家核查引用")
        self.assertTrue(card.submitted_hs.value, "FIX-10 必须带原提交码")
        self.assertTrue(card.expert_final_hs.value, "FIX-10 必须带专家终值")
        self.assertNotEqual(card.submitted_hs.value, card.expert_final_hs.value, "FIX-10 是改码场景")
        # 核查目标是提交码；终值来自另一条决定引用，不能拿它冒充核查目标。
        self.assertEqual(card.latest_expert_check.target_hs, card.submitted_hs.value)
        self.assertNotEqual(card.latest_expert_check.run_id, card.expert_final_hs.source_id)

    def test_fix04_keeps_blank_supplier_code_even_when_suggestion_exists(self):
        card = FixtureDoc.model_validate(load_fixture("FIX-04")).card
        self.assertEqual(card.facts.supplier_hs_raw, "", "FIX-04 原值必须保留空白")
        self.assertTrue(card.precheck_suggested_hs.value, "FIX-04 同时带预审建议，用来验证不回填")

    def test_fix02_distinguishes_executed_but_no_suggestion(self):
        card = FixtureDoc.model_validate(load_fixture("FIX-02")).card
        self.assertIsNone(card.precheck_suggested_hs.value)
        self.assertTrue(card.precheck_suggested_hs.source_id, "空值必须仍带来源引用")

    def test_fix02_absent_and_fix03_read_failed_are_different(self):
        absent = FixtureDoc.model_validate(load_fixture("FIX-02")).card
        failed = FixtureDoc.model_validate(load_fixture("FIX-03")).card
        self.assertEqual(absent.image_status, "absent")
        self.assertEqual(failed.image_status, "read_failed")
        self.assertNotEqual(absent.image_status, failed.image_status)
        self.assertTrue(failed.images, "read_failed 必须保留图片明细和来源单元格")
        self.assertTrue(failed.images[0].source_cell)
        self.assertTrue(failed.images[0].error_message)

    def test_fixtures_carry_mock_mode(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertEqual(load_fixture(scenario)["card"]["result_mode"], "mock")

    def test_comparison_status_is_contract_enum_or_null(self):
        schema = json.loads(ITEM_CARD_SCHEMA.read_text("utf-8"))
        allowed = set(schema["$defs"]["ComparisonStatus"]["enum"])
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                status = load_fixture(scenario)["card"].get("comparison_status")
                self.assertTrue(status is None or status in allowed, status)


class P07SchemaDriftTest(SimpleTestCase):
    """schema 枚举、必填字段与组件源码必须对得上。"""

    def test_all_six_comparison_statuses_are_rendered(self):
        schema = json.loads(ITEM_CARD_SCHEMA.read_text("utf-8"))
        source = (COMPONENTS / "hs-comparison.js").read_text("utf-8")
        for status in schema["$defs"]["ComparisonStatus"]["enum"]:
            with self.subTest(status=status):
                self.assertIn(f"{status}:", source, f"hs-comparison.js 缺比较结论 {status}")
        self.assertIn("未得出结论", source, "comparison_status=null 必须有明确占用文案")

    def test_all_image_statuses_are_rendered(self):
        schema = json.loads(ITEM_CARD_SCHEMA.read_text("utf-8"))
        source = (COMPONENTS / "hs-comparison.js").read_text("utf-8")
        for status in schema["$defs"]["ImageStatus"]["enum"]:
            with self.subTest(status=status):
                self.assertIn(f"{status}:", source, f"hs-comparison.js 缺图片状态 {status}")
        card = (COMPONENTS / "commodity-card.js").read_text("utf-8")
        self.assertIn("不能当作无图", card)
        self.assertIn("不是读取失败", card)

    def test_schema_required_fields_are_used_by_components(self):
        schema = json.loads(ITEM_CARD_SCHEMA.read_text("utf-8"))
        source = component_source()
        for field in schema["required"]:
            with self.subTest(field=field):
                self.assertIn(field, source, f"组件源码未消费 ItemCard.{field}")

    def test_contract_version_is_visible_in_fixture(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertIn("contract_version", load_fixture(scenario))
