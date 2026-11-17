"""Task runner executes real client/export UI behavior, not source-only assertions."""
import shutil
import subprocess
from django.conf import settings
from django.test import SimpleTestCase


class ExportFrontendTests(SimpleTestCase):
    def test_attachment_and_selection_behavior(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'P20 frontend behavior requires Node')
        result = subprocess.run([node, '--test', 'tests/p20/frontend.test.mjs'], cwd=settings.BASE_DIR,
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
