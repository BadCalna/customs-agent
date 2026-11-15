"""P08 asset routes + real Node behavioral checks, wired into the task runner."""
import os
import shutil
import subprocess

from django.conf import settings
from django.test import SimpleTestCase, override_settings


class JavascriptBehaviorTests(SimpleTestCase):
    def test_component_behaviors(self):
        node = os.environ.get("P08_NODE") or shutil.which("node")
        if not node:
            self.fail("Node is required for P08 behavior tests; set P08_NODE to its executable")
        result = subprocess.run(
            [node, "--test", "tests/p08/components.test.mjs"],
            cwd=settings.BASE_DIR, capture_output=True, text=True, encoding="utf-8", timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


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
