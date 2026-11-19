"""Architecture, prompt-sync, and forbidden-dependency AST checks for P18."""
from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from adapters.engines.expert_check import SYSTEM_PROMPT

ROOT = Path(settings.BASE_DIR)
ENGINE_PATH = ROOT / "adapters" / "engines" / "expert_check.py"
PROMPT_PATH = ROOT / "adapters" / "engines" / "prompts" / "expert" / "system_prompt.txt"
COMPONENT_PATH = ROOT / "web" / "components" / "expert-checks.js"
CARD_PATH = ROOT / "web" / "components" / "commodity-card.js"
PRECHECK_PATH = ROOT / "adapters" / "engines" / "precheck.py"

FORBIDDEN_IMPORT_PREFIXES = (
    "django",
    "config",
    "apps.reviews",
    "apps.runs",
    "apps.files",
    "apps.tasks",
    "requests",
    "httpx",
    "urllib",
    "http.client",
    "socket",
)


class TestExpertDependencies(SimpleTestCase):
    def test_expert_check_engine_has_no_orm_network_settings_or_get_engine_calls(self):
        tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"), filename=str(ENGINE_PATH))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES),
                        f"禁止在 expert_check.py 导入 {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertFalse(
                    node.module.startswith(FORBIDDEN_IMPORT_PREFIXES),
                    f"禁止在 expert_check.py 导入 {node.module}",
                )
                if node.module == "adapters.engines.protocols" or node.module == ".protocols":
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name,
                            "get_engine",
                            "P18 引擎严禁在核查中重新解析 get_engine()，必须使用冻结版本/注入依赖",
                        )
            elif isinstance(node, ast.Name) and node.id == "get_engine":
                self.fail("expert_check.py 不得引用 get_engine")

    def test_system_prompt_matches_prompt_file(self):
        disk_prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        self.assertEqual(SYSTEM_PROMPT, disk_prompt)
        for required_phrase in ("elements_semantic_conflict", "image_verdict", "退税", "监管", "unknown", "锚定"):
            self.assertIn(required_phrase, disk_prompt)

    def test_component_is_standalone_and_boundary_files_kept_intact(self):
        comp = COMPONENT_PATH.read_text(encoding="utf-8")
        self.assertIn("export function renderExpertChecks(", comp)
        card = CARD_PATH.read_text(encoding="utf-8")
        self.assertIn("export function renderCommodityCard(el, options = {})", card)
        precheck = PRECHECK_PATH.read_text(encoding="utf-8")
        self.assertIn("def expert_check(self, payload: ExpertCheckInput) -> ExpertCheckOutput:", precheck)
