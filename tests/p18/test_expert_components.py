"""Django wrapper & source guards for P18 web/components/expert-checks.js."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
COMPONENT_JS = ROOT / "web" / "components" / "expert-checks.js"
NODE_TEST_FILE = ROOT / "tests" / "p18" / "expert_checks.test.mjs"


class TestExpertComponents(SimpleTestCase):
    def test_node_component_behavior_tests_pass(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node 可执行文件未就绪，无法运行前端组件测试")
        proc = subprocess.run(
            [node, "--test", str(NODE_TEST_FILE)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    def test_expert_checks_js_has_no_network_or_forbidden_decision_words(self):
        src = COMPONENT_JS.read_text(encoding="utf-8")
        for token in ("fetch(", "XMLHttpRequest", "WebSocket", "localStorage", "已核实", "通过", "批准", "放行"):
            self.assertNotIn(token, src)
