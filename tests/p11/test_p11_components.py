"""P11 组件与自测页的交付验证。

这里不做 JS 运行时（仓库没有 Node 测试入口），而是锁住 P11 最容易在后续重构中
断掉的东西：

1. 组件、样式、自测页存在，页面引用的是 ``/demo/assets/`` 受控路由；
2. 三个 P11 文件能被演示路由取到（200 + ``no-store``），越界与未知后缀 404；
3. 写动作只在 ``api.confirm / api.submit / api.decide`` 三处，组件内没有第二套
   传输（fetch / XMLHttpRequest / cookie / localStorage）；
4. 501、409、403、401 分支与“失败保留草稿”在源码中确定存在；
5. FIX-08/09/10 样例可解析，组件引用了样例里的真实字段名；
6. 历史组件的版本守卫、当前/旧版本已通过、候选对照口径不被改掉。

源码断言不是行为测试，不能替代 P09/P10 接入后的真实写动作验证；这一点在交接
文档的“未执行项”中单列。
"""
import json
import re
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


def read(path: Path) -> str:
    return path.read_text("utf-8")


def fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text("utf-8"))


class P11AssetTest(SimpleTestCase):
    """文件与页面引用：没有这些，后面的组件断言无从谈起。"""

    def test_component_style_and_page_exist(self):
        for path in (REVIEW_ACTIONS, VERSION_HISTORY, REVIEW_CSS, P11_PAGE):
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
        # 只在 HTTP 2xx 后清空确认草稿；失败分支没有清空动作
        reset_call = 'state.confirmDrafts.set(itemId, { hs: "", reason: "" });'
        self.assertEqual(self.source.count(reset_call), 2)  # 初始化 + 成功重置各一次
        confirm_start = self.source.index("const response = await WRITE_CALLS.confirm")
        reset_index = self.source.index(reset_call, confirm_start)
        catch_index = self.source.index("} catch (error)", confirm_start)
        self.assertLess(reset_index, catch_index, "确认草稿只能在成功分支重置，不能在 catch 后重置")
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
