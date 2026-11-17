"""P08 asset routes + real Node behavioral checks, wired into the task runner."""
import os
import shutil
import subprocess

from django.conf import settings
from django.test import SimpleTestCase, override_settings

NODE_TEST_FILES = ("tests/p08/components.test.mjs", "tests/p08/page.test.mjs")
#: 两个 Node 文件的用例数（components 27 + page 28）；改测试时必须同步，
#: 否则「文件被清空 / 用例全被跳过」会以 0 退出被当成通过。
EXPECTED_CASES = 55


class JavascriptBehaviorTests(SimpleTestCase):
    def test_component_behaviors(self):
        node = os.environ.get("P08_NODE") or shutil.which("node")
        if not node:
            self.fail("Node is required for P08 behavior tests; set P08_NODE to its executable")
        result = subprocess.run(
            [node, "--test", *NODE_TEST_FILES],
            cwd=settings.BASE_DIR, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        self.assertIn(f"# tests {EXPECTED_CASES}", output, output)
        self.assertIn(f"# pass {EXPECTED_CASES}", output, output)
        self.assertRegex(output, r"# fail 0\b")
        self.assertRegex(output, r"# skipped 0\b")


@override_settings(DEBUG=True)
class AssetTests(SimpleTestCase):
    def test_assets_use_existing_guarded_routes(self):
        for path in ("components/task-list.js", "components/run-progress.js", "styles/tasks.css"):
            with self.subTest(path=path):
                response = self.client.get("/demo/assets/" + path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Cache-Control"], "no-store")
                response.close()

    @override_settings(DEBUG=False)
    def test_assets_disabled_outside_development(self):
        for path in ("components/task-list.js", "components/run-progress.js", "styles/tasks.css"):
            self.assertEqual(self.client.get("/demo/assets/" + path).status_code, 404)
