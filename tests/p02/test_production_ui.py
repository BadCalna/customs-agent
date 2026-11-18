import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings


class ProductionEntryTests(SimpleTestCase):
    def test_decision_drafts_survive_poll_and_submit_the_edited_values(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = Path(settings.BASE_DIR) / "tests" / "p02" / "test_production_draft.cjs"
        result = subprocess.run(["node", str(script)], cwd=settings.BASE_DIR,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @override_settings(DEBUG=False)
    def test_production_entry_serves_only_explicit_assets(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/app/assets/production.js", b"".join(page.streaming_content))
        self.assertEqual(self.client.get("/app/assets/production.js").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/production.css").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/workbench.html").status_code, 404)
        self.assertEqual(self.client.get("/demo/").status_code, 404)
