"""Run with unittest or Django. Browser audit is separate from these deterministic fixtures."""
from pathlib import Path
import shutil
import subprocess
import unittest


class ProductionJavaScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed for the production UI tests')
    def test_production_ui_modules(self):
        directory = Path(__file__).parent
        scripts = [directory / 'test_insights.mjs', directory / 'test_queue.mjs']
        result = subprocess.run(['node', '--test', *map(str, scripts)], capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
