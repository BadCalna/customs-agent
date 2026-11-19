"""Run production-page behavior regressions using Node; not browser coverage."""
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.test import SimpleTestCase


class ProductionFrontendTests(SimpleTestCase):
    @skipUnless(shutil.which("node"), "Node unavailable; production-page behavior tests not run")
    def test_production_page_behaviors(self):
        script = Path(__file__).with_name("frontend.test.mjs")
        result = subprocess.run(
            [shutil.which("node"), "--test-reporter=tap", "--test", str(script)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("# fail 0", result.stdout)
