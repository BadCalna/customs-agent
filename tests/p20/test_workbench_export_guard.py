"""workbench.html 导出守卫：原型页不得再伪造导出成功。

``workbench.html`` 的任务与商品行来自 ``mapDataset()``，没有真实的 ``version_id`` /
``run_id`` / ``submission_id``，无法满足「结果集必须属于请求版本」的导出契约。
这里锁住“删掉假成功”的三条不变量（**源码级守卫**，不冒充浏览器点击验收）：

1. 不在浏览器里生成任何文件（无 ``Blob`` / ``createObjectURL`` / ``a.download``），
   也不再提示「已导出」；
2. ``data-act="export"`` 只走 ``exportUnavailable()`` 说明现状；
3. LIVE 模式禁用控件并给出迁移入口，FIXTURE/DEMO 明示样例模式不可导出真实文件。
"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

HTML_PATH = Path(settings.BASE_DIR) / "web" / "workbench.html"


def inline_script() -> str:
    """只取 workbench 的内联脚本，避免把样式或 HTML 文案当成 JS 断言。"""
    html = HTML_PATH.read_text("utf-8")
    start = html.index("<script>") + len("<script>")
    return html[start:html.index("</script>", start)]


class WorkbenchExportGuardTests(SimpleTestCase):
    def setUp(self):
        self.script = inline_script()

    def test_prototype_no_longer_generates_or_announces_a_file(self):
        for banned in ("createObjectURL", "new Blob(", "a.download", "exportCsv"):
            self.assertNotIn(banned, self.script, f"workbench 不得再生成前端文件：{banned}")
        self.assertNotIn("已导出", self.script, "原型页不得再提示导出成功")

    def test_export_action_only_explains_why_it_is_unavailable(self):
        self.assertIn("if(act==='export'){exportUnavailable();return;}", self.script)
        self.assertIn("function exportUnavailable()", self.script)
        self.assertIn("样例模式（FIXTURE/DEMO）不可导出真实文件", self.script)

    def test_live_mode_disables_and_points_to_the_real_pages(self):
        self.assertIn("function exportControl()", self.script)
        self.assertIn("window.WB_MODE==='live'", self.script)
        self.assertIn("disabled aria-disabled=\"true\"", self.script)
        self.assertIn("导出入口已迁移到上传/任务页（/uploads/、/tasks/）", self.script)
