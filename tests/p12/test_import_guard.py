"""P12 迁移脚本的导入保护：loaddata 对相同主键是覆盖，必须先拒绝非空目标库。"""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE_DIR = Path(settings.BASE_DIR)
SCRIPT = BASE_DIR / "scripts" / "database" / "copy_sqlite_to_pg.sh"


class ImportGuardTest(SimpleTestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")

    def test_refuses_non_empty_target_before_loaddata(self):
        self.assertIn("检查目标库为空", self.source)
        self.assertIn("exit 3", self.source)
        # 拒绝必须发生在 loaddata 之前：否则覆盖已经发生，报错也没用。
        guard = self.source.index("nonempty=")
        load = self.source.index("loaddata --ignorenonexistent")
        self.assertLess(guard, load)

    def test_guard_is_explained_as_overwrite_not_error(self):
        self.assertIn("覆盖", self.source)
        self.assertNotIn("会因主键/唯一约束报错", self.source)

    def test_pg_env_is_mapped_for_django(self):
        # pg_env.sh 用 PG* 变量，Django 读 CUSTOMS_PG_*，缺映射时脚本直接崩。
        for name in ("CUSTOMS_PG_NAME", "CUSTOMS_PG_USER", "CUSTOMS_PG_HOST", "CUSTOMS_PG_PORT"):
            with self.subTest(name=name):
                self.assertIn(f'export {name}="$', self.source)
