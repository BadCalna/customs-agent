"""P23 依赖与边界：不引网络/向量库/模型库，不改环境变量，不新增 pip 依赖。

守的规则：导入流水线只允许标准库 + Django/DRF 契约层；不允许联网、不允许 Embedding/
向量库、不允许把 openpyxl/xlsxwriter/pandas 加进依赖（读取走只读 OOXML 解析），
也不允许在代码里改写 ``CUSTOMS_*`` 环境变量（.env 由使用方自己指）。
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
MODULES = sorted(
    (BASE_DIR / "adapters" / "sources" / "reference_import").glob("*.py")
) + [BASE_DIR / "adapters" / "sources" / "tariff_commentary.py"]

FORBIDDEN_TOP_LEVEL = {
    "requests",
    "httpx",
    "urllib",
    "urllib3",
    "http",
    "socket",
    "aiohttp",
    "openai",
    "anthropic",
    "transformers",
    "embedding",
    "faiss",
    "chromadb",
    "qdrant_client",
    "pinecone",
    "numpy",
    "pandas",
    "sklearn",
    "torch",
    "openpyxl",
    "xlsxwriter",
    "xlrd",
}

#: 允许在代码里“读”环境变量，但不允许“写”。
ENV_ASSIGNMENT = re.compile(
    r"""os\.environ\s*\[\s*["']CUSTOMS_[A-Z0-9_]*["']\s*\]\s*="""
)
ENV_SETDEFAULT = re.compile(r"""os\.environ\.setdefault\s*\(\s*["']CUSTOMS_""")
ENV_PUTENV = re.compile(r"""os\.putenv\s*\(""")


def imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


class DependencyBoundaryTest(SimpleTestCase):
    def test_modules_exist(self):
        self.assertGreaterEqual(len(MODULES), 10, "P23 适配器模块应全部纳入检查")
        names = {path.name for path in MODULES}
        for expected in (
            "model.py",
            "sources.py",
            "convert_catalog.py",
            "convert_tax.py",
            "convert_commentary.py",
            "snapshot.py",
            "publish.py",
            "query_check.py",
            "cli.py",
            "tariff_commentary.py",
        ):
            self.assertIn(expected, names)

    def test_no_network_or_vector_library_imports(self):
        for path in MODULES:
            with self.subTest(module=path.name):
                forbidden = imported_top_level(path) & FORBIDDEN_TOP_LEVEL
                self.assertEqual(
                    forbidden,
                    set(),
                    f"{path.name} 引入了不允许的网络/向量/模型/电子表格库：{sorted(forbidden)}",
                )

    def test_no_model_or_network_calls_in_source_text(self):
        # 向量化相关只查 import（AST），文档里出现“不引入 Embedding”这类否定说明是允许的。
        patterns = {
            "requests.": "网络请求",
            "httpx.": "网络请求",
            "urlopen(": "网络请求",
            "openai.": "模型调用",
            "faiss.": "向量库",
        }
        for path in MODULES:
            text = path.read_text(encoding="utf-8").lower()
            for token, label in patterns.items():
                with self.subTest(module=path.name, token=token):
                    self.assertNotIn(token, text, f"{path.name} 出现{label}痕迹：{token}")

    def test_no_customs_environment_assignment(self):
        for path in MODULES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertIsNone(ENV_ASSIGNMENT.search(text),
                                  f"{path.name} 直接改写 CUSTOMS_* 环境变量")
                self.assertIsNone(ENV_SETDEFAULT.search(text),
                                  f"{path.name} 用 setdefault 改写 CUSTOMS_* 环境变量")
                self.assertIsNone(ENV_PUTENV.search(text), f"{path.name} 调用了 os.putenv")

    def test_pyproject_dependencies_are_unchanged(self):
        payload = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = payload["project"]["dependencies"]
        names = {
            re.split(r"[<>=!~\[; ]", item, maxsplit=1)[0].strip().lower()
            for item in dependencies
        }
        self.assertIn("django", names, "项目依赖里必须有 Django")
        for forbidden in ("openpyxl", "xlsxwriter", "pandas", "numpy", "faiss", "chromadb"):
            self.assertNotIn(
                forbidden, names, f"P23 不得给项目新增依赖：{forbidden}（读取走只读 OOXML 解析）"
            )
        self.assertEqual(len(dependencies), 5, "依赖数量应与 main 一致（5 项）")

    def test_tests_do_not_touch_repository_output_directories(self):
        """测试自己也不许把输出写进仓库（例如 .local/reference-import）。"""

        p23_tests = BASE_DIR / "tests" / "p23"
        for path in p23_tests.glob("*.py"):
            if path.name.startswith("test_") and path.name != "test_dependencies.py":
                text = path.read_text(encoding="utf-8")
                with self.subTest(module=path.name):
                    self.assertNotIn('".local', text, f"{path.name} 不应把输出写到 .local/")
                    self.assertNotIn("'.local", text, f"{path.name} 不应把输出写到 .local/")
                    self.assertNotIn("DEFAULT_OUT_ROOT", text,
                                     f"{path.name} 不应使用默认输出目录（必须用临时目录）")
