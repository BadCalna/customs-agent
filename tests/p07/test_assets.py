"""P07 静态资源与受控路由：文件存在、页面引用、200/no-store 与越界 404。

P07 只交付组件与 fixture 自测页，没有 JS 运行时；沿用 P02 的测试范式，
用 Django SimpleTestCase 读源码 + 客户端请求受控路由，锁住交付物不会在后续
重构中静默断链。路由本身由 P02 的 ``apps/api/demo.py`` 提供，本包只验证文件
可被同一个白名单路由取到。
"""
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

WEB_ROOT = Path(settings.BASE_DIR) / "web"
PAGE = WEB_ROOT / "pages" / "p07.html"
COMPONENTS = (
    WEB_ROOT / "components" / "commodity-list.js",
    WEB_ROOT / "components" / "commodity-card.js",
    WEB_ROOT / "components" / "hs-comparison.js",
)
STYLESHEET = WEB_ROOT / "styles" / "commodity.css"


class P07AssetFilesTest(SimpleTestCase):
    """交付文件及其相互引用。"""

    def test_delivery_files_exist(self):
        for path in (PAGE, STYLESHEET, *COMPONENTS):
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), f"缺少交付文件 {path}")
                self.assertGreater(path.stat().st_size, 0, f"{path} 为空")

    def test_page_references_all_three_components(self):
        html = PAGE.read_text("utf-8")
        self.assertIn('type="module"', html)
        for name in ("commodity-list.js", "commodity-card.js", "hs-comparison.js"):
            with self.subTest(name=name):
                self.assertIn(f'from "/demo/assets/components/{name}"', html)

    def test_page_uses_shared_design_tokens_and_local_stylesheet(self):
        html = PAGE.read_text("utf-8")
        self.assertIn('href="/demo/assets/styles.css"', html)
        self.assertIn('href="/demo/assets/styles/commodity.css"', html)

    def test_page_is_fixture_only(self):
        html = PAGE.read_text("utf-8")
        # 自测页只读固定样例；live 联调属于 P06/P09/P10 集成后的事。
        self.assertIn("/demo/fixtures/", html)
        self.assertIn("DEMO-SET.json", html)
        self.assertNotIn("/api/v1", html)

    def test_page_has_no_external_or_build_dependency(self):
        html = PAGE.read_text("utf-8")
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("cdn.", html.lower())
        self.assertNotIn("<script src=", html)

    def test_page_exposes_keyboard_status_region(self):
        html = PAGE.read_text("utf-8")
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("Enter", html)
        self.assertIn("Esc", html)


@override_settings(DEBUG=True)
class P07DemoRouteTest(SimpleTestCase):
    """P02 的受控路由必须能服务 P07 新增文件，且保持 no-store。"""

    def setUp(self):
        self.client = Client()

    def test_page_is_served_without_cache(self):
        response = self.client.get("/demo/assets/pages/p07.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "no-store")
        body = b"".join(response.streaming_content)
        self.assertIn(b"/demo/assets/components/commodity-card.js", body)

    def test_components_and_stylesheet_are_served_without_cache(self):
        cases = (
            ("/demo/assets/components/commodity-list.js", "text/javascript"),
            ("/demo/assets/components/commodity-card.js", "text/javascript"),
            ("/demo/assets/components/hs-comparison.js", "text/javascript"),
            ("/demo/assets/styles/commodity.css", "text/css"),
        )
        for path, content_type in cases:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertIn(content_type, response["Content-Type"], path)
                self.assertEqual(response["Cache-Control"], "no-store", path)

    def test_missing_p07_asset_is_404(self):
        for path in (
            "/demo/assets/pages/p07-missing.html",
            "/demo/assets/components/missing.js",
            "/demo/assets/styles/missing.css",
            "/demo/assets/pages/p07.html.bak",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_escaping_the_web_root_is_404(self):
        # 新增 pages/、components/、styles/ 子目录后，路由的 resolve 后越界校验仍必须生效。
        for path in (
            "/demo/assets/../config/settings.py",
            "/demo/assets/pages/../../Makefile",
            "/demo/assets/components/../../../Makefile",
            "/demo/assets/styles/../../apps/reviews/models.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)

    @override_settings(DEBUG=False)
    def test_p07_page_is_disabled_outside_dev(self):
        for path in (
            "/demo/assets/pages/p07.html",
            "/demo/assets/components/commodity-card.js",
            "/demo/assets/styles/commodity.css",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)
