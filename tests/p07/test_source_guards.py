"""P07 组件源码守卫：纯渲染、无网络/存储、无硬编码结论。

这些断言不是重复运行时测试，而是锁住架构边界：
- 组件只能接收数据与回调，不能自己 ``fetch`` 或写浏览器存储，否则 fixture 页面
  会绕过 ``web/api.js`` 的 CSRF/错误分类，也会破坏“组件不改业务状态”的验收；
- 组件不能硬编码“通过/批准”等人工结论，审批状态只能来自 ``task_stage`` /
  ``submission_status`` 字段；展示文案不能替代字段；
- 组件不能引入 CDN 或框架，保持原生 ES module、无构建。
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

COMPONENTS = Path(settings.BASE_DIR) / "web" / "components"
COMPONENT_FILES = (
    COMPONENTS / "commodity-list.js",
    COMPONENTS / "commodity-card.js",
    COMPONENTS / "hs-comparison.js",
)


def read_all() -> str:
    return "\n".join(path.read_text("utf-8") for path in COMPONENT_FILES)


class P07ComponentSourceGuardTest(SimpleTestCase):
    def test_components_do_not_send_requests_or_touch_storage(self):
        source = read_all().lower()
        # 组件不发请求：请求统一走 web/api.js；这里出现任何一个都意味着越权访问后端。
        for token in ("fetch(", "xmlhttprequest", "axios", "navigator.sendbeacon"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)
        # 组件不持久化状态：刷新后以服务端/页面状态为准，避免第二套事实来源。
        for token in ("localstorage", "sessionstorage", "indexeddb", "document.cookie"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)
        self.assertNotIn("api.js", source)

    def test_components_do_not_hardcode_human_conclusions(self):
        source = read_all()
        # 审批结论只能来自字段；组件不能自己写死“通过/批准/已核实”。
        for token in ("通过", "批准", "已核实"):
            with self.subTest(token=token):
                self.assertNotIn(token, source, f"组件源码出现硬编码结论词：{token}")

    def test_components_do_not_add_external_or_framework_dependencies(self):
        source = read_all()
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)
        self.assertNotIn("cdn.", source.lower())
        for token in ("react", "vue", "svelte", "node_modules"):
            with self.subTest(token=token):
                self.assertNotIn(token, source.lower())

    def test_components_are_exported_pure_render_functions(self):
        expected = {
            "commodity-list.js": "export function renderCommodityList",
            "commodity-card.js": "export function renderCommodityCard",
            "hs-comparison.js": "export function renderHsComparison",
        }
        for filename, signature in expected.items():
            with self.subTest(filename=filename):
                source = (COMPONENTS / filename).read_text("utf-8")
                self.assertIn(signature, source)
                # 组件只在传入容器上挂事件，不接 document/window 级监听。
                self.assertNotIn("document.addEventListener", source)
                self.assertNotIn("window.addEventListener", source)
                self.assertIn("onSelect", source, "交互只允许通过传入回调")

    def test_list_is_keyboard_reachable_and_aria_labeled(self):
        source = (COMPONENTS / "commodity-list.js").read_text("utf-8")
        for token in (
            'role="listbox"',
            'role="option"',
            'aria-selected="',
            "aria-label=",
            "ArrowDown",
            "ArrowUp",
            "Escape",
            "onSelectItem",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_card_is_aria_labeled_and_exposes_images(self):
        source = (COMPONENTS / "commodity-card.js").read_text("utf-8")
        for token in (
            'role="article"',
            "aria-labelledby=",
            'alt="',
            "data-image-id=",
            "aria-label=",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_stage_null_rules_are_explicit_in_source(self):
        source = (COMPONENTS / "hs-comparison.js").read_text("utf-8")
        for token in ("未给出建议", "source_id", '"—"', "供应商原码", "空白", "不回写"):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_mock_and_comparison_null_are_visible(self):
        source = read_all()
        for token in ("MOCK", "result_mode", "不是真实归类结论", "未得出结论"):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_recode_scope_wording_is_explicit(self):
        source = (COMPONENTS / "commodity-card.js").read_text("utf-8")
        for token in (
            "原提交码",
            "专家终值",
            "target_hs",
            "本次核查针对的是原提交码",
            "不属于上述核查目标",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_image_failure_and_absence_are_distinct_in_source(self):
        source = (COMPONENTS / "commodity-card.js").read_text("utf-8")
        for token in ("read_failed", "absent", "不能当作无图", "不是读取失败"):
            with self.subTest(token=token):
                self.assertIn(token, source)
