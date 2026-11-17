"""Node 直接执行仓库里真实前端源码的导出接线测试；不冒充浏览器验收。

Node 缺失时显式失败而不是 skip：本包的前端接线证据只有这两项能提供，
跳过会让「导出按钮接线」变成没有自动化证据的状态（与 p07/p11 的包装层约定一致）。
"""
from pathlib import Path
import shutil
import subprocess

from django.test import SimpleTestCase

NODE = shutil.which("node")
HERE = Path(__file__).resolve().parent


class ExportFrontendTests(SimpleTestCase):
    def run_script(self, name: str) -> subprocess.CompletedProcess:
        if not NODE:
            self.fail("跑 P20 前端接线测试需要 Node；请安装 node 或把 NODE 放进 PATH")
        result = subprocess.run([NODE, str(HERE / name)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)
        return result

    def test_export_client_posts_and_parses_download_headers(self):
        # 覆盖：CSRF 头、POST 路径与体积、filename*/filename 解析、错误体 → ApiError、fixture 501。
        self.run_script("export_client.cjs")

    def test_upload_page_only_exports_with_real_run_reference(self):
        # 覆盖：缺引用/预审未完成不发请求、成功按服务端文件名下载、失败与引用不一致不保存。
        self.run_script("export_page.cjs")
