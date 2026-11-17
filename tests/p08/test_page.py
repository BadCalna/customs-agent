"""P08 生产页面入口：/tasks/ 与页面资源只在 DEBUG=True 可用，且一律 no-store。

数据权限不在这里校验（数据来自 /api/v1 或契约样例）。这里固定的是与安全相关的
三件事：路由可达、生产模式 404、缓存头为 no-store；另外顺带固定 fixture 模式
读取的 DEMO-SET.json 在 HTTP 层可用。
"""
import json
import posixpath
import re

from django.test import SimpleTestCase, override_settings

P08_PAGE = "/tasks/"
# 页面自身 + 它 import 的模块与样式（createClient / createTaskList / createRunProgress）。
P08_ASSETS = (
    "pages/p08.js",
    "api.js",
    "components/task-list.js",
    "components/run-progress.js",
    "styles/tasks.css",
)
MODULE_SPECIFIER = re.compile(r"""(?:from|import)\s*\(?\s*["']([^"']+)["']""")


def read_body(response):
    return b"".join(response.streaming_content).decode("utf-8")


@override_settings(DEBUG=True)
class TasksPageRouteTests(SimpleTestCase):
    def test_tasks_page_serves_the_p08_production_shell(self):
        response = self.client.get(P08_PAGE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")
        body = read_body(response)
        response.close()
        # 页面必须挂载生产脚本与组件样式，并且不是 tests/p08 下的自测预览页。
        self.assertIn("/demo/assets/pages/p08.js", body)
        self.assertIn("/demo/assets/styles/tasks.css", body)
        # p08.js 的 bootstrap 逐个 getElementById 这些挂载点；缺一个页面就装配不出来。
        for mount_id in ("p08-tasks", "p08-run", "p08-identity", "p08-notice", "p08-summary", "p08-mode"):
            self.assertIn(f'id="{mount_id}"', body)
        self.assertNotIn("preview.js", body)

    def test_page_module_and_component_assets_are_served_no_store(self):
        content_types = {
            "pages/p08.js": "text/javascript; charset=utf-8",
            "api.js": "text/javascript; charset=utf-8",
            "components/task-list.js": "text/javascript; charset=utf-8",
            "components/run-progress.js": "text/javascript; charset=utf-8",
            "styles/tasks.css": "text/css; charset=utf-8",
        }
        for path in P08_ASSETS:
            with self.subTest(path=path):
                response = self.client.get("/demo/assets/" + path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], content_types[path])
                self.assertEqual(response["Cache-Control"], "no-store")
                response.close()

    def test_page_module_graph_is_served(self):
        """从 /tasks/ 出发，页面引用的样式/脚本与 ESM import 图在 HTTP 层都能取到。

        这是“页面可加载”的自动化证据：不靠人工点开浏览器，而是按浏览器同样的
        相对路径规则解析 import，逐个请求静态入口。
        """
        page = self.client.get(P08_PAGE)
        body = read_body(page)
        page.close()
        urls = re.findall(r"""(?:href|src)="/?(demo/[^"']+)""", body)
        self.assertTrue(urls, body[:400])

        visited, pending = set(), list(urls)
        while pending:
            url = pending.pop()
            if url in visited:
                continue
            visited.add(url)
            response = self.client.get("/" + url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response["Cache-Control"], "no-store", url)
            if url.endswith(".js"):
                source = read_body(response)
                for specifier in MODULE_SPECIFIER.findall(source):
                    if specifier.startswith("/"):
                        pending.append(specifier.lstrip("/"))
                    elif specifier.startswith("."):
                        target = posixpath.normpath(
                            posixpath.join(posixpath.dirname(url), specifier)
                        )
                        pending.append(target)
            response.close()

        self.assertLessEqual(
            {
                "demo/assets/pages/p08.js",
                "demo/assets/styles.css",
                "demo/assets/styles/tasks.css",
                "demo/assets/api.js",
                "demo/assets/components/task-list.js",
                "demo/assets/components/run-progress.js",
            },
            visited,
        )

    def test_fixture_mode_dataset_is_available_over_http(self):
        # createClient 的默认 fixtureUrl；fixture 模式据此加载任务与执行样例。
        response = self.client.get("/demo/fixtures/DEMO-SET.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")
        payload = json.loads(read_body(response))
        response.close()
        self.assertTrue(payload["tasks"])
        self.assertTrue(payload["runs"])

    @override_settings(DEBUG=False)
    def test_page_and_assets_are_unavailable_outside_development(self):
        self.assertEqual(self.client.get(P08_PAGE).status_code, 404)
        for path in P08_ASSETS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get("/demo/assets/" + path).status_code, 404)
