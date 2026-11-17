"""P07 组件行为测试：把 node --test 跑进仓库测试入口。

2026-11-15 审核指出 P07 的关键文案只用源码关键词断言守护，测不出 R-01 这类
「核查范围提示与实际 target_hs 不符」的边界错误。这里用 Node 直接 import
`web/components/commodity-card.js` 渲染卡片再断言文案，源码守卫仍保留在
`test_source_guards.py` 作为补充。

Node 不可用时明确失败（不 skip）：本包的行为证据只能由它提供。
"""
import os
import shutil
import subprocess

from django.conf import settings
from django.test import SimpleTestCase

NODE_TEST_FILE = "tests/p07/components.test.mjs"
#: 期望的 Node 用例数；新增用例时必须同步改这里，避免「文件被清空也算通过」。
EXPECTED_CASES = 15


class CommodityCardBehaviourTests(SimpleTestCase):
    def test_component_behaviours(self):
        node = os.environ.get("P07_NODE") or shutil.which("node")
        if not node:
            self.fail("跑 P07 行为测试需要 Node；请安装 node 或把 P07_NODE 指向可执行文件")
        result = subprocess.run(
            [node, "--test", NODE_TEST_FILE],
            cwd=settings.BASE_DIR, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        # 只看退出码不够：文件被清空/用例被跳过时 node --test 仍可能 0 退出。
        # 因此按「期望用例数」精确核对，并确认没有失败与跳过。
        self.assertIn(f"# tests {EXPECTED_CASES}", output, output)
        self.assertIn(f"# pass {EXPECTED_CASES}", output, output)
        self.assertRegex(output, r"# fail 0\b")
        self.assertRegex(output, r"# skipped 0\b")