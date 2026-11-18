"""P11 组件与自测页的交付验证。

两层：

A. ``components.test.mjs``（Node 22 ``node --test``，由 ``JavascriptBehaviorTests`` 用
   subprocess 驱动）直接 import 两个组件，用最小 DOM 桩跑真实交互：跨提交草稿隔离、
   请求在飞时的输入保留、退回问题渲染、构造 data 后挂载、旧 scope 守卫、任务级历史
   接受、修订号拦截、409 刷新失败反馈。行为证据以它为准。
B. 本文件其余用例锁住静态契约：文件/受控路由、写动作只在 ``api`` 三处、501/409/403/401
   分支文案、FIX-08/09/10 字段名、历史守卫口径、回交文档段落。

2026-11-15 审核指出：只断言源码字符串/赋值位置无法发现 scope 绕过与初始数据遗漏，
所以 A 层是本轮新增，B 层只保留「防断链」用途，不再单独充当行为证据。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings

BASE_DIR = Path(settings.BASE_DIR)
WEB_ROOT = BASE_DIR / "web"
FIXTURE_ROOT = BASE_DIR / "contracts" / "fixtures"
P11_DOC = BASE_DIR / "docs" / "开发文档" / "交接" / "P11.md"

REVIEW_ACTIONS = WEB_ROOT / "components" / "review-actions.js"
VERSION_HISTORY = WEB_ROOT / "components" / "version-history.js"
REVIEW_CSS = WEB_ROOT / "styles" / "review-actions.css"
P11_PAGE = WEB_ROOT / "pages" / "p11.html"
P11_CONFLICT = WEB_ROOT / "pages" / "p11-conflict.js"


def read(path: Path) -> str:
    return path.read_text("utf-8")


def fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text("utf-8"))


NODE_TEST_FILE = "tests/p11/components.test.mjs"


class JavascriptBehaviorTests(SimpleTestCase):
    """用 ``node --test`` 直接驱动两个组件；这是 P11 的行为证据层。

    Node 不存在时用 ``self.fail``（而不是 skip）：P11 现在把行为测试当成交付的一部分，
    静默跳过等于把「未验证」写成「已通过」。
    """

    def test_component_behaviors(self):
        node = os.environ.get("P11_NODE") or shutil.which("node")
        if not node:
            self.fail("P11 行为测试需要 Node（node --test）；请安装 Node 或设置 P11_NODE 指向可执行文件")
        result = subprocess.run(
            [node, "--test", NODE_TEST_FILE],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class P11AssetTest(SimpleTestCase):
    """文件与页面引用：没有这些，后面的组件断言无从谈起。"""

    def test_component_style_and_page_exist(self):
        for path in (REVIEW_ACTIONS, VERSION_HISTORY, REVIEW_CSS, P11_PAGE, P11_CONFLICT):
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file(), f"缺少 {path}")
                self.assertGreater(path.stat().st_size, 0, f"{path} 为空")

    def test_page_references_components_and_component_styles(self):
        html = read(P11_PAGE)
        self.assertIn("../components/review-actions.js", html)
        self.assertIn("../components/version-history.js", html)
        self.assertIn("/demo/assets/styles/review-actions.css", html)
        # 沿用 P02 的设计 token，不另起一套视觉变量
        self.assertIn("/demo/assets/styles.css", html)

    def test_page_is_native_module_without_build_or_cdn(self):
        html = read(P11_PAGE)
        self.assertIn('<script type="module">', html)
        self.assertIn("createReviewActions", html)
        self.assertIn("createVersionHistory", html)
        # 白名单：只允许 w3.org 的 SVG 命名空间；禁止外链脚本、样式与 CDN
        stripped = html.replace("http://www.w3.org/2000/svg", "")
        self.assertNotIn("http://", stripped)
        self.assertNotIn("https://", stripped)
        self.assertNotIn("cdn.", stripped.lower())
        self.assertNotIn("<script src=", html)

    def test_page_declares_itself_a_self_test_not_a_formal_page(self):
        html = read(P11_PAGE)
        for marker in ("组件自测页", "不校验权限、不写库", "501", "演示假响应", "不能当作接口可用性证明"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_components_are_plain_es_modules(self):
        actions = read(REVIEW_ACTIONS)
        history = read(VERSION_HISTORY)
        self.assertIn("export function createReviewActions", actions)
        self.assertIn("export function createVersionHistory", history)
        for source in (actions, history):
            self.assertNotIn("require(", source)
            self.assertNotIn("from \"react", source)
            self.assertNotIn("from 'react", source)

    def test_css_covers_both_components_and_uses_design_tokens(self):
        css = read(REVIEW_CSS)
        for marker in (".ra-root", ".vh-root", ".ra-feedback--error", ".vh-rejection", ".ra-item"):
            with self.subTest(marker=marker):
                self.assertIn(marker, css)
        self.assertIn("var(--teal", css)
        self.assertIn("var(--cinnabar", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)

    def test_every_history_class_has_a_style_rule(self):
        """history 组件模板里用到的每个 ``vh-*`` 类都要有 CSS 规则。

        ``vh-note`` 曾经只出现在模板里、CSS 里完全没有规则（退回决定的说明文字
        因此没有任何样式）。这条按「模板类 → CSS 规则」做集合校验，新增类忘记配样式
        会直接变红；``vh-check--warn`` 这类动态拼接的组合类不在 ``class="`` 首位，
        由上面模板字符串里的 ``vh-check`` 规则覆盖。
        """
        classes = set(re.findall(r'class="(vh-[a-z0-9_-]+)', read(VERSION_HISTORY)))
        self.assertGreaterEqual(len(classes), 5, classes)
        css = read(REVIEW_CSS)
        for name in sorted(classes):
            with self.subTest(name=name):
                # 允许 `.vh-x {` 与 `.vh-x,` 两种写法；`(?![\w-])` 防止把 `.vh-check`
                # 误判成命中了 `.vh-check--warn`。
                self.assertRegex(css, rf"\.{re.escape(name)}(?![\w-])", f"{name} 在 review-actions.css 里没有规则")


@override_settings(DEBUG=True)
class P11RouteTest(SimpleTestCase):
    """``/demo/assets/<path>`` 能服务 P11 页面，且不越出 web/。"""

    def setUp(self):
        self.client = Client()

    def test_page_is_served_without_cache(self):
        response = self.client.get("/demo/assets/pages/p11.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "no-store")
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("../components/review-actions.js", body)

    def test_components_and_style_are_served_without_cache(self):
        for path, content_type in (
            ("/demo/assets/components/review-actions.js", "text/javascript"),
            ("/demo/assets/components/version-history.js", "text/javascript"),
            ("/demo/assets/styles/review-actions.css", "text/css"),
            # 409 重新拉取抽成的模块必须能被页面 import，否则页面直接报模块加载失败。
            ("/demo/assets/pages/p11-conflict.js", "text/javascript"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response["Content-Type"])
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_every_page_referenced_asset_is_reachable(self):
        html = read(P11_PAGE)
        references = set(re.findall(r'(?:src|href)="(/demo/assets/[^"]+)"', html))
        self.assertGreaterEqual(len(references), 2)
        for path in references:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_escaping_the_web_root_is_404(self):
        for path in (
            "/demo/assets/../config/settings.py",
            "/demo/assets/../../Makefile",
            "/demo/assets/pages/../../apps/api/demo.py",
            "/demo/assets/pages/p11.html/extra",
            "/demo/assets/pages/p11.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_missing_component_is_404(self):
        self.assertEqual(self.client.get("/demo/assets/components/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/demo/assets/styles/missing.css").status_code, 404)

    @override_settings(DEBUG=False)
    def test_p11_page_is_disabled_outside_dev(self):
        self.assertEqual(self.client.get("/demo/assets/pages/p11.html").status_code, 404)


class ReviewActionsSourceTest(SimpleTestCase):
    """业务员确认/提交与专家通过/退回的源码契约。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = read(REVIEW_ACTIONS)

    def test_all_writes_go_through_the_injected_api(self):
        matches = re.findall(r"api\.([A-Za-z_][A-Za-z0-9_]*)\(", self.source)
        self.assertEqual(sorted(matches), ["confirm", "decide", "submit"])
        self.assertEqual(matches.count("confirm"), 1)
        self.assertEqual(matches.count("submit"), 1)
        self.assertEqual(matches.count("decide"), 1)
        # 没有第二套请求出口
        for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_no_hardcoded_success_copy(self):
        for phrase in ("提交成功", "确认成功", "审批成功", "决定成功", "操作成功", "保存成功", "已成功", "处理成功"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.source)
        # 成功表述只能来自服务端响应
        self.assertIn("服务端已响应", self.source)
        self.assertIn("responseInfo", self.source)
        self.assertIn("不把 HTTP 2xx 写成业务通过", self.source)

    def test_501_and_409_branches_exist(self):
        for marker in (
            "未实现（501）",
            "内容已被他人修改，已刷新",
            "not_implemented",
            "conflict",
            "onConflict",
            "onRefresh",
            "details.owner",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)
        # 501 是错误分支，不是被吞掉的提示
        self.assertIn("status === 501", self.source)
        self.assertIn("info.kind === \"not_implemented\"", self.source)

    def test_401_403_and_csrf_are_distinct(self):
        for marker in (
            "登录已失效（401）",
            "没有该动作的权限（403）",
            "CSRF 校验失败（403）",
            "unauthenticated",
            "forbidden",
            "csrf",
            "csrf_failed",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_failure_keeps_draft_and_recovers_pending(self):
        self.assertIn("草稿已保留", self.source)
        self.assertIn("失败路径不清草稿", self.source)
        # 本轮（R-05）改了「成功后怎么清草稿」：不再无条件重置整行草稿，而是按字段比较
        # 提交时快照。原断言数的是 `state.confirmDrafts.set(itemId, { hs: "", reason: "" });`
        # 两次，这个实现细节已不存在。
        #
        # 注意：这里**不再**断言「清理语句在 catch 之前 / catch 内没有清理」——那种源码
        # 位置断言挡不住「清理被挪进 finally 或在别处多清一次」（失败也会清草稿却全绿）。
        # 真正的守护是 components.test.mjs 的「E3 写请求失败时草稿必须保留」行为用例。
        for marker in (
            "clearConfirmDraftIfUnchanged(itemId, submittedDraft);",
            "clearDecisionDraftsIfUnchanged(submittedDraft, mode);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

        self.assertEqual(self.source.count("state.pending = \"\";"), 3)
        self.assertEqual(self.source.count("finally {"), 3)
        self.assertIn("处理中…", self.source)
        self.assertIn("提交中…", self.source)
        self.assertIn('aria-busy="', self.source)

    def test_submit_preview_blocks_missing_confirmation_without_fallback(self):
        for marker in (
            "整批提交给专家",
            "将提交",
            "缺少业务确认",
            "缺少确认",
            "不会用预审建议、供应商原码、专家建议、旧提交或空值补位",
            "business_confirmed_hs",
            "confirmation_id",
            "revision",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_expert_approve_requires_reason_only_when_recode(self):
        for marker in (
            "改码必须填写原因",
            "final_hs",
            "change_reason",
            "等于提交值",
            "未改的行终值等于提交值；前后值与改码原因由服务端留痕",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_return_requires_target_reason_and_item_issues(self):
        for marker in (
            "退回对象",
            "供应商",
            "业务员",
            "逐商品问题",
            "至少填写一条逐商品问题",
            "return_to",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_permission_hiding_does_not_replace_server_checks(self):
        for marker in (
            "allowed_actions",
            "服务端仍会逐次校验权限",
            "不能替代服务端校验",
            "confirmation.write",
            "submission.create",
            "decision.resolve",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_confirm_form_shows_current_and_previous_values(self):
        for marker in (
            "当前确认",
            "上次修改",
            "专家改码对照（FIX-10）",
            "business_confirmed_hs",
            "submitted_hs",
            "expert_final_hs",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)


class VersionHistorySourceTest(SimpleTestCase):
    """只读历史、版本守卫与候选口径。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = read(VERSION_HISTORY)

    def test_history_is_read_only(self):
        for forbidden in (
            "api.",
            "fetch(",
            "XMLHttpRequest",
            "localStorage",
            "sessionStorage",
            "<input",
            "<textarea",
            "contenteditable",
            "data-ra-act",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)
        self.assertIn("历史只读", self.source)

    def test_version_guard_rejects_mismatched_late_response(self):
        for marker in (
            "activeVersionId",
            "expectedVersion",
            "payloadVersion",
            "版本不匹配，晚到的旧响应已丢弃（FIX-09）",
            "无法确认属于当前版本",
            "已丢弃版本不匹配的历史响应",
            "上一份已接受的历史保持显示",
            "return false",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)
        # state.data 只在守卫通过后赋值，拒绝分支不能覆盖当前视图
        self.assertEqual(self.source.count("state.data = data;"), 1)
        self.assertGreater(
            self.source.index("state.data = data;"),
            self.source.index("版本不匹配，晚到的旧响应已丢弃（FIX-09）"),
        )

    def test_current_and_old_approved_are_distinguished(self):
        for marker in ("当前版本", "旧版本已通过", "当前版本已通过", "不授予当前版本"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_grouping_is_version_submission_decision(self):
        for marker in (
            "版本 / 提交 / 专家决定",
            "submission_id",
            "decision_id",
            "该版本没有提交记录",
            "尚无专家决定",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_cross_version_candidates_do_not_claim_identity(self):
        for marker in (
            "跨版本候选对照",
            "仅作候选提示：不表示同一商品",
            "不建立稳定商品身份",
            "不继承确认、提交或审批结论",
            "候选依据",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_fix10_recode_and_old_expert_check_are_visible(self):
        for marker in (
            "final_hs",
            "change_reason",
            "target_hs",
            "旧核查结论不外推到新终值（FIX-10）",
            "不假设 AI 已核查该终值",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_tables_and_regions_have_accessible_names(self):
        for marker in (
            "<caption class=\"sr-only\">",
            'scope="col"',
            "aria-labelledby",
            'role="alert"',
            'role="note"',
            'role="status"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)


class P11FixtureTest(SimpleTestCase):
    """FIX-08/09/10 可解析，且组件引用这些样例里的真实字段。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fix08 = fixture("FIX-08.json")
        cls.fix09 = fixture("FIX-09.json")
        cls.fix10 = fixture("FIX-10.json")
        cls.demo = fixture("DEMO-SET.json")
        cls.actions = read(REVIEW_ACTIONS)
        cls.history = read(VERSION_HISTORY)
        cls.page = read(P11_PAGE)
        cls.combined = "\n".join((cls.actions, cls.history, cls.page))

    def test_fix08_is_a_parseable_conflict_envelope(self):
        self.assertEqual(self.fix08["scenario"], "FIX-08")
        envelope = self.fix08["error"]
        self.assertEqual(envelope["error"]["code"], "conflict")
        self.assertIn("request_id", envelope)
        self.assertIn("conflict", self.actions)
        self.assertIn("内容已被他人修改，已刷新", self.actions)

    def test_fix09_card_and_run_belong_to_different_versions(self):
        self.assertEqual(self.fix09["scenario"], "FIX-09")
        card_version = self.fix09["card"]["version_id"]
        run_version = self.fix09["run"]["version_id"]
        self.assertNotEqual(card_version, run_version, "FIX-09 需要一份 V1 晚到样例")
        self.assertIn("FIX-09", self.history)
        self.assertIn("version_id", self.history)

    def test_fix10_recode_values_and_old_check_target_are_used(self):
        card = self.fix10["card"]
        submitted = card["submitted_hs"]["value"]
        final = card["expert_final_hs"]["value"]
        target = card["latest_expert_check"]["target_hs"]
        self.assertEqual(submitted, "8544422900")
        self.assertEqual(final, "8536901900")
        self.assertNotEqual(submitted, final)
        self.assertEqual(target, submitted, "旧核查目标应仍是原提交码")
        for marker in ("submitted_hs", "expert_final_hs", "change_reason", "target_hs", "latest_expert_check"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.combined)

    def test_components_reference_the_fixture_field_names(self):
        fields = (
            "task_id",
            "version_id",
            "item_id",
            "revision",
            "source_location",
            "row_number",
            "facts",
            "name",
            "supplier_hs_raw",
            "precheck_suggested_hs",
            "business_confirmed_hs",
            "submitted_hs",
            "expert_final_hs",
            "submission_id",
            "run_id",
            "allowed_actions",
        )
        for field in fields:
            with self.subTest(field=field):
                self.assertIn(field, self.combined)

    def test_demo_set_still_has_the_main_flow_rows(self):
        self.assertIn("tasks", self.demo)
        self.assertIn("items", self.demo)
        self.assertIn("cards", self.demo)
        main = next(task for task in self.demo["tasks"] if "主流程" in task["title"])
        current = next(version for version in main["versions"] if version["is_current"])
        rows = self.demo["items"][current["version_id"]]
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["item_id"] in self.demo["cards"] for row in rows))


class P11PageScriptTest(SimpleTestCase):
    """自测页确实读取四份数据、覆盖 409/401/403 场景，并演示 V1 丢弃。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = read(P11_PAGE)

    def test_page_reads_all_p11_fixtures(self):
        for name in ("FIX-08.json", "FIX-09.json", "FIX-10.json", "DEMO-SET.json"):
            with self.subTest(name=name):
                self.assertIn(name, self.page)

    def test_page_exposes_error_scenarios(self):
        for marker in (
            'value="not_implemented"',
            'value="conflict"',
            'value="forbidden"',
            'value="unauthenticated"',
            'value="network"',
            'value="accepted"',
            "onConflict",
            "conflictRefresh",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_page_demonstrates_late_v1_history_rejection(self):
        for marker in ("lateV1", "panels.history.accept", "versionId", "V1 晚到历史按 version_id 被拒绝"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_demo_api_revision_matches_each_action_payload(self):
        """模拟 2xx 的 revision 必须按 action 取字段（P3）。

        confirm 发 ``expected_revision``、submit 发 ``revision``、decide 没有修订号。
        原先统一用 ``payload.expected_revision``，提交的模拟响应永远算成 revision=1。
        页面是内联 module script、无法 import，所以这里做源码锚点；组件行为证据在
        ``components.test.mjs``。
        """
        self.assertIn(
            'const submittedRevision = action === "confirm" ? payload.expected_revision : '
            'action === "submit" ? payload.revision : undefined;',
            self.page,
        )
        self.assertIn("Number(submittedRevision) + 1", self.page)
        self.assertNotIn("Number(payload.expected_revision || 0) + 1", self.page)

    def test_conflict_refresh_is_an_importable_module(self):
        """P1-6 的 409 接线改成可 import 的模块，行为证据在 Node 测试里。

        这里只锁「页面确实 import 并把重读逻辑交给工厂」这条接线；「重读失败必须抛出」
        由 ``components.test.mjs`` 直接驱动 reload 的 resolve / reject 证明——原先那种
        对页面内联 catch 的正则断言，把 catch 改成 `log(...); if (error && error.fatal) throw`
        也照样全绿，挡不住「普通失败被吞」。
        """
        html = self.page
        self.assertIn('import { createConflictRefresh } from "./p11-conflict.js";', html)
        self.assertIn("createConflictRefresh({ reload: reloadFixtures, log })", html)
        self.assertTrue(P11_CONFLICT.is_file(), "缺少 web/pages/p11-conflict.js")
        module = read(P11_CONFLICT)
        self.assertIn("export function createConflictRefresh(", module)
        self.assertIn("throw error;", module)
        # 页面侧的重读必须是真的重读并重放数据，不能是空实现。
        start = html.index("async function reloadFixtures(")
        block = html[start : html.index("\n    }", start)]
        for marker in ("await loadFixtures()", "currentData = buildData()", "applyDataToPanels()"):
            with self.subTest(marker=marker):
                self.assertIn(marker, block)


class ResidualFixSourceTest(SimpleTestCase):
    """2026-11-15 审核项的实现锚点。

    行为证据在 ``components.test.mjs``；这里只防「修好的地方被静默改回去」，
    不重复断言渲染细节。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.actions = read(REVIEW_ACTIONS)
        cls.history = read(VERSION_HISTORY)

    def test_revision_is_validated_on_raw_values(self):
        for marker in (
            "function revisionOf(",
            "revisionOf(card.revision)",
            "revisionOf(task().revision)",
            "revisionOf(snapshot && snapshot.revision)",
            "Number.isSafeInteger",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.actions)
        # Number() 隐式转换会把 null / 空串变成 0，必须已经去掉
        for forbidden in ("Number(card.revision)", "Number(task().revision)", "Number.isInteger(Number("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.actions)

    def test_drafts_are_scoped_by_task_version_submission(self):
        for marker in (
            "function scopeOf(",
            "function confirmScopeOf(",
            "state.buckets",
            "state.confirmBuckets",
            "clearConfirmDraftIfUnchanged",
            "clearDecisionDraftsIfUnchanged",
            "decisionDraftSnapshot",
            # R-05 清理要绑定提交时的作用域（P2）
            "offScopeFeedback",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.actions)
        for forbidden in ("state.confirmDrafts", "state.decisionDraft"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.actions)

    def test_history_splits_approve_finals_and_return_issues(self):
        for marker in (
            "renderIssueRows",
            "renderFinalRows",
            "退回问题",
            "退回决定只记录逐商品问题",
            "无终值记录",
            "无法与核查目标比对",
            "issue: text(",
            # 逐行终值状态变量不能叫 state：会遮蔽外层组件 state（P3 可维护性）。
            "finalState",
            # 未知 / 缺失 action 也要展示问题文本，不能默认当成“通过”（P3）。
            "unknownAction",
            "不在已知范围（approve / return）内",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.history)
        self.assertNotIn('let state = "missing"', self.history)

    def test_history_reads_constructor_data(self):
        # 构造参数先存 pendingData，accept 通过才落进 state.data（P2：被拒绝的构造数据
        # 不能在 mount 时渲染出来）。
        self.assertIn(
            'pendingData: options.data && typeof options.data === "object" ? options.data : null',
            self.history,
        )
        self.assertIn("if (accept(candidate, { taskId: state.taskId, versionId: state.activeVersionId }))", self.history)
        self.assertIn("state.pendingData = null;", self.history)

    def test_history_guard_only_uses_view_scope(self):
        for marker in (
            "const expectedVersion = state.activeVersionId;",
            "const scopeVersion = text(scope.versionId);",
            "旧 scope 不能覆盖当前视图",
            # 任务级快照必须包含当前视图版本（P1 回归修复）
            "任务级快照不含当前视图版本",
            "versionIds.includes(expectedVersion)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.history)
        # 任务级历史不得再要求「正在查看的版本 == 任务当前版本」
        self.assertNotIn("任务级历史快照的 current_version_id 与当前视图不一致", self.history)


class P11HandoffTest(SimpleTestCase):
    """回交文档必须按模板保留段落，并写清 501 与未执行项。"""

    def test_handoff_follows_template_and_records_limits(self):
        self.assertTrue(P11_DOC.is_file(), "缺少 docs/开发文档/交接/P11.md")
        doc = read(P11_DOC)
        for heading in ("## 派发基线", "## 回交内容", "## Jay 手工测试", "## 主控验收"):
            with self.subTest(heading=heading):
                self.assertIn(heading, doc)
        for marker in ("操作映射", "501", "未执行", "已知限制", "make test TASK=P11"):
            with self.subTest(marker=marker):
                self.assertIn(marker, doc)


class PayloadContractAlignmentTest(SimpleTestCase):
    """P11 生成的请求体必须与 P09/P10 后端字段名一致（Jay 评审）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = read(REVIEW_ACTIONS)

    def test_submit_uses_revision_and_only_confirmation_refs(self):
        self.assertIn("revision,", self.source)
        self.assertIn("confirmation_id: row.confirmationId", self.source)
        self.assertNotIn("submitted_hs: row.submittedHs", self.source)
        self.assertNotIn("item_revision: row.itemRevision", self.source)

    def test_decide_does_not_send_revision_or_previous_hs(self):
        self.assertNotIn("previous_hs: entry.submitted", self.source)

    def test_return_puts_issues_not_items(self):
        self.assertIn("issues: issueRows.map((entry) => ({ item_id: entry.row.item_id, message: entry.issue }))", self.source)
        self.assertNotIn("items: issueRows.map", self.source)
