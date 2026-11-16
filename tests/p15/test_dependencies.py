"""P15 依赖约束：不引入向量库/网络客户端/新 pip 依赖。

用语法树检查 import，避免“只是注释里写了 requests”这类误报；
再校验 ``pyproject.toml`` 的运行依赖没有被本任务加料。
"""
import ast
import re
import tomllib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
MODULE_PATH = BASE_DIR / "adapters" / "sources" / "history_search.py"

#: 标准库 + 仓库已有依赖（Django / django-ninja / pydantic 由 ninja 带入）+ 本项目包。
ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "json",
    "os",
    "pathlib",
    "re",
    "typing",
    "unicodedata",
    "ninja",
    "pydantic",
    "apps",
}

#: 明确不允许出现的能力：向量库、网络客户端、模型 SDK、科学计算栈。
FORBIDDEN_TOP_LEVEL = {
    "requests",
    "httpx",
    "urllib",
    "http",
    "socket",
    "aiohttp",
    "numpy",
    "pandas",
    "sklearn",
    "torch",
    "transformers",
    "openai",
    "faiss",
    "chromadb",
    "qdrant_client",
    "pinecone",
}


def imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


class NoExtraDependencyTest(SimpleTestCase):
    def test_imports_are_stdlib_or_existing_project_dependencies(self):
        modules = imported_top_level(MODULE_PATH)
        self.assertTrue(modules, "AST 未解析到任何 import，检查测试本身")
        self.assertLessEqual(
            modules,
            ALLOWED_TOP_LEVEL,
            f"出现允许清单外的 import：{sorted(modules - ALLOWED_TOP_LEVEL)}",
        )
        self.assertFalse(modules & FORBIDDEN_TOP_LEVEL)

    def test_baseline_project_dependencies_still_present(self):
        """只锁 P15 自己的足迹，不要求整份依赖清单不变。

        依赖清单是主控维护的全局文件，别的包（P12 的 psycopg、P03 的 pillow）
        合法增加依赖时不应让 P15 的测试失败；P15 是否"偷偷加料"由上面的
        import 白名单与本文件末尾的禁用库检查负责。
        """
        project = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        names = {
            re.split(r"[\[<>=!~ ]", dependency, maxsplit=1)[0].lower()
            for dependency in project["dependencies"]
        }
        self.assertLessEqual(
            {"django", "django-ninja", "python-dotenv"}, names,
            f"基线依赖被移除：{sorted({'django', 'django-ninja', 'python-dotenv'} - names)}",
        )

    def test_no_vector_store_or_network_client_in_source(self):
        source = MODULE_PATH.read_text(encoding="utf-8").lower()
        for forbidden in ("chromadb", "faiss", "qdrant", "pinecone", "requests.get", "httpx."):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
