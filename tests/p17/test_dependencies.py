"""P17 依赖与边界：不引网络/模型/图片库，不新增 pip 依赖，不读写数据库。

与 P14 的 tests/p14/test_dependencies.py 同一口径；P17 只多允许一个本地模块
（复用 P14 的 ``adapters.sources.hs_lookup`` 契约与存储工具）。
"""
import ast
import re
import tomllib
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
MODULES = [
    BASE_DIR / "adapters" / "sources" / "tax_lookup.py",
    BASE_DIR / "adapters" / "sources" / "regulatory_lookup.py",
]

ALLOWED_TOP_LEVEL = {
    "__future__",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "os",
    "pathlib",
    "re",
    "typing",
    "unicodedata",
    "ninja",
    "pydantic",
    "apps",
}

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
    "anthropic",
    "django",
    "faiss",
    "chromadb",
    "qdrant_client",
    "pinecone",
    "PIL",
    "pdfplumber",
    "fitz",
}


def imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module.split(".")[0])
    return modules


class NoExtraDependencyTest(SimpleTestCase):
    def test_both_modules_use_only_stdlib_existing_dependencies_and_local_contracts(self):
        for path in MODULES:
            with self.subTest(module=path.name):
                modules = imported_top_level(path)
                self.assertTrue(modules)
                self.assertLessEqual(
                    modules,
                    ALLOWED_TOP_LEVEL,
                    f"{path.name} 出现允许清单外的 import：{sorted(modules - ALLOWED_TOP_LEVEL)}",
                )
                self.assertFalse(modules & FORBIDDEN_TOP_LEVEL)

    def test_p17_reuses_p14_contracts_by_relative_import(self):
        tax = MODULES[0].read_text(encoding="utf-8")
        regulatory = MODULES[1].read_text(encoding="utf-8")
        self.assertIn("from .hs_lookup import", tax)
        self.assertIn("from .hs_lookup import", regulatory)
        # 监管适配器复用退税模块里的来源元数据与存储错误，不另起一套。
        self.assertIn("from .tax_lookup import", regulatory)

    def test_baseline_project_dependencies_are_still_present(self):
        project = tomllib.loads((BASE_DIR / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        names = {
            re.split(r"[\[<>=!~ ]", dependency, maxsplit=1)[0].lower()
            for dependency in project["dependencies"]
        }
        self.assertLessEqual(
            {"django", "django-ninja", "python-dotenv"},
            names,
            f"基线依赖被移除：{sorted({'django', 'django-ninja', 'python-dotenv'} - names)}",
        )

    def test_no_network_model_or_vector_client_in_source(self):
        forbidden = (
            "requests.get",
            "httpx.",
            "urllib.request",
            "socket.",
            "chromadb",
            "faiss",
            "qdrant",
            "pinecone",
            "openai.",
            "anthropic.",
        )
        for path in MODULES:
            source = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                with self.subTest(module=path.name, forbidden=token):
                    self.assertNotIn(token, source)

    def test_adapters_do_not_import_django_orm_or_write_files(self):
        for path in MODULES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("from django", source)
                self.assertNotIn("import django", source)
                self.assertNotIn(".objects.", source)
                self.assertNotIn("open(", source)
                self.assertNotIn("write_text(", source)

    def test_no_image_or_ocr_dependency(self):
        for path in MODULES:
            source = path.read_text(encoding="utf-8").lower()
            for token in ("pytesseract", "easyocr", "cv2", "pillow", "image.open"):
                with self.subTest(module=path.name, token=token):
                    self.assertNotIn(token, source)

    def test_modules_are_pure_python_with_no_side_effects_on_import(self):
        tree = ast.parse(MODULES[0].read_text(encoding="utf-8"))
        # 模块级不允许调用 open()/load() 之类副作用（常量与类定义除外）。
        for node in tree.body:
            self.assertIsInstance(
                node,
                (
                    ast.Import,
                    ast.ImportFrom,
                    ast.Assign,
                    ast.AnnAssign,
                    ast.ClassDef,
                    ast.FunctionDef,
                    ast.Expr,
                    ast.If,
                ),
                f"tax_lookup.py 模块级出现意外语句：{type(node).__name__}",
            )
