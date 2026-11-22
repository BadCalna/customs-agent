import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings


class ProductionEntryTests(SimpleTestCase):
    def test_product_name_on_every_web_entry(self):
        import re

        for page in (Path(settings.BASE_DIR) / "web").rglob("*.html"):
            with self.subTest(page=page.name):
                title = re.search(r"<title>(.*?)</title>", page.read_text(encoding="utf-8"))
                self.assertIsNotNone(title)
                self.assertIn("关务预审工作台", title.group(1))

    def test_decision_drafts_survive_poll_and_submit_the_edited_values(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = Path(settings.BASE_DIR) / "tests" / "p02" / "test_production_draft.cjs"
        result = subprocess.run(["node", str(script)], cwd=settings.BASE_DIR,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@override_settings(DEBUG=False)
class ProductionRoutesTests(TestCase):
    def test_anonymous_deep_link_opens_the_dedicated_login(self):
        response = self.client.get("/", {"task": "task A", "version": "v1", "next": "//other.invalid"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/login/?task=task+A&version=v1")
        self.assertIn("no-store", response.headers["Cache-Control"])
        login = self.client.get(response.url)
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"/app/assets/login.js", b"".join(login.streaming_content))
        self.assertIn("csrftoken", login.cookies)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_signed_in_login_returns_only_to_the_workbench(self):
        user = get_user_model().objects.create_user(username="reviewer-test", role="operator")
        self.client.force_login(user)
        self.assertRedirects(self.client.get("/login/?next=https://other.invalid"), "/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/login/?task=a&version=b&next=//other.invalid"),
                             "/?task=a&version=b", fetch_redirect_response=False)

    def test_admin_only_session_can_still_open_business_login(self):
        admin = get_user_model().objects.create_user(username="site-admin", is_staff=True)
        self.client.force_login(admin)
        self.assertRedirects(self.client.get("/"), "/login/")

    def test_production_entry_serves_only_explicit_assets(self):
        user = get_user_model().objects.create_user(username="reviewer-test", role="operator")
        self.client.force_login(user)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/app/assets/production.js", b"".join(page.streaming_content))
        self.assertEqual(self.client.get("/app/assets/production.js").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/production.css").status_code, 200)
        self.assertEqual(self.client.get("/app/assets/workbench.html").status_code, 404)
        self.assertEqual(self.client.get("/demo/").status_code, 404)
