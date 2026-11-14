"""P02 组件演示壳的交付验证：静态资源、受控路由与 fixture/live 同构。

演示壳不在 API 契约里，但它是任务包明确交付物，且很容易在后续重构中悄悄断链
（改路径、改导出字段、把 web/ 挪走）。这里锁四件事：

1. ``web/`` 五个文件存在，index.html 引用的是 ``/demo/assets/``；
2. ``/demo/`` 三个路由能取到资源，越界路径与非白名单后缀一律 404；
3. 提交进仓的 ``contracts/fixtures/DEMO-SET.json`` 能通过 DemoSet 校验；
4. 同一份演示数据由真实 ORM 组装的 fixture 与 live 端点逐字段相同。
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TestCase

from apps.accounts.models import User
from apps.api.fixtures import DemoSet
from apps.api.management.commands.export_contracts import build_demo_set
from apps.api.schemas import CONTRACT_VERSION
from apps.reviews.management.commands.seed_contracts import MAIN_TITLE, RECODE_TITLE
from apps.reviews.models import Task
from apps.runs.models import Run

WEB_ROOT = Path(settings.BASE_DIR) / "web"
DEMO_SET_FILE = Path(settings.BASE_DIR) / "contracts" / "fixtures" / "DEMO-SET.json"


class DemoAssetsTest(SimpleTestCase):
    """演示壳文件与页面引用，不需要数据库。"""

    def test_shell_files_exist(self):
        for name in ("index.html", "app.js", "api.js", "state.js", "styles.css"):
            self.assertTrue((WEB_ROOT / name).is_file(), f"缺少 web/{name}")

    def test_index_references_demo_assets(self):
        html = (WEB_ROOT / "index.html").read_text("utf-8")
        self.assertIn("/demo/assets/styles.css", html)
        self.assertIn("/demo/assets/app.js", html)
        # 演示壳是原生 ES module，不应引入构建产物或 CDN 依赖
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", html)

    def test_fixture_mode_reads_the_committed_data_set(self):
        api = (WEB_ROOT / "api.js").read_text("utf-8")
        self.assertIn("DEMO-SET.json", api)
        state = (WEB_ROOT / "state.js").read_text("utf-8")
        # 版本切换必须能丢弃晚到的旧响应（D3 / FIX-09）
        self.assertIn("isCurrent", state)


class DemoRouteTest(SimpleTestCase):
    """受控静态路由：能取到文件，越界与非法后缀返回 404。"""

    def setUp(self):
        self.client = Client()

    def test_index_is_served_without_cache(self):
        response = self.client.get("/demo/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn(b"/demo/assets/app.js", b"".join(response.streaming_content))

    def test_assets_are_served_with_content_type(self):
        for path, content_type in (
            ("/demo/assets/app.js", "text/javascript"),
            ("/demo/assets/styles.css", "text/css"),
            ("/demo/assets/api.js", "text/javascript"),
            ("/demo/assets/state.js", "text/javascript"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(content_type, response["Content-Type"], path)

    def test_fixture_route_serves_demo_set(self):
        response = self.client.get("/demo/fixtures/DEMO-SET.json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response["Content-Type"])
        payload = json.loads(b"".join(response.streaming_content))
        self.assertEqual(payload["contract_version"], CONTRACT_VERSION)

    def test_escaping_the_web_root_is_404(self):
        for path in (
            "/demo/assets/../config/settings.py",
            "/demo/assets/../../Makefile",
            "/demo/assets/../fixtures",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_escaping_the_fixture_root_is_404(self):
        for path in (
            "/demo/fixtures/../../../Makefile",
            "/demo/fixtures/../DEMO-SET.json.bak",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_missing_asset_is_404(self):
        self.assertEqual(self.client.get("/demo/assets/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/demo/fixtures/FIX-99.json").status_code, 404)


class DemoSetIsomorphismTest(TestCase):
    """fixture 与 live 同构：同一份数据两条路径必须逐字段一致。"""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        call_command("seed_contracts", verbosity=0)
        cls.main = Task.objects.get(title=MAIN_TITLE)
        cls.recode = Task.objects.get(title=RECODE_TITLE)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(User.objects.get(username="operator_a"))
        self.client.get("/api/v1/me")

    def test_committed_file_round_trips_through_the_schema(self):
        payload = json.loads(DEMO_SET_FILE.read_text("utf-8"))
        data = DemoSet.model_validate(payload)
        self.assertEqual(data.contract_version, CONTRACT_VERSION)
        self.assertGreaterEqual(len(data.tasks), 2)
        self.assertTrue(data.items, "DEMO-SET 必须包含按版本索引的商品行")
        self.assertTrue(data.cards, "DEMO-SET 必须包含商品卡片")
        self.assertTrue(data.runs, "DEMO-SET 必须包含当前执行")

    def test_task_detail_matches_live(self):
        demo = build_demo_set(self.main, self.recode)
        task = next(item for item in demo.tasks if item.task_id == str(self.main.pk))
        live = self.client.get(f"/api/v1/tasks/{self.main.pk}")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(task.model_dump(mode="json"), live.json())

    def test_item_list_and_card_match_live(self):
        version = self.main.versions.get(number=2)
        demo = build_demo_set(self.main, self.recode)
        rows = demo.items[str(version.pk)]
        live_rows = self.client.get(f"/api/v1/versions/{version.pk}/items?limit=100").json()["items"]
        self.assertEqual([row.model_dump(mode="json") for row in rows], live_rows)

        item = version.items.get(source_row=7)
        card = demo.cards[str(item.pk)]
        live_card = self.client.get(f"/api/v1/versions/{version.pk}/items/{item.pk}")
        self.assertEqual(live_card.status_code, 200)
        self.assertEqual(card.model_dump(mode="json"), live_card.json())

    def test_run_detail_matches_live(self):
        version = self.main.versions.get(number=2)
        run = Run.objects.filter(version=version, is_current=True).first()
        self.assertIsNotNone(run, "演示数据应包含 v2 的当前执行")
        demo = build_demo_set(self.main, self.recode)
        live = self.client.get(f"/api/v1/runs/{run.pk}")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(demo.runs[str(run.pk)].model_dump(mode="json"), live.json())
