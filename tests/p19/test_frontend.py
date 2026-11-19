"""P19运行真实ES模块行为回归；不代替浏览器视觉验收。"""
from pathlib import Path
import shutil
import subprocess
from django.conf import settings
from django.test import SimpleTestCase


class MessageFrontendTests(SimpleTestCase):
    def test_message_components_and_page_navigation(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "P19前端验证需要Node")
        scripts = sorted(str(p.relative_to(settings.BASE_DIR)) for p in
                         (Path(settings.BASE_DIR) / "tests/p19").glob("*.test.mjs"))
        self.assertEqual(len(scripts), 2)
        result = subprocess.run([node, "--test-reporter=tap", "--test", *scripts],
                                cwd=settings.BASE_DIR, capture_output=True, text=True,
                                encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# tests 13", result.stdout)
        self.assertIn("# pass 13", result.stdout)
        self.assertIn("# fail 0", result.stdout)
        self.assertIn("# skipped 0", result.stdout)
