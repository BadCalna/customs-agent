"""P22：运维脚本的静态与行为检查。

不做容器级演练（那属于交接记录里的实机证据），但要把“脚本本身能跑、不会误删、比对真的会报差异”
锁住——否则一次改错就可能让备份脚本变成删除脚本。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "operations"
SHELL_SCRIPTS = sorted(OPS.glob("*.sh"))


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShellScriptTests(unittest.TestCase):
    def test_scripts_exist(self):
        names = {path.name for path in SHELL_SCRIPTS}
        for expected in ("lib.sh", "backup.sh", "restore.sh", "verify_restore.sh", "drill.sh"):
            self.assertIn(expected, names)

    def test_syntax_is_valid(self):
        for path in SHELL_SCRIPTS:
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(result.returncode, 0, f"{path.name} 语法错误：{result.stderr}")

    def test_scripts_are_executable(self):
        for path in SHELL_SCRIPTS:
            self.assertTrue(path.stat().st_mode & 0o111, f"{path.name} 缺少可执行权限")

    def test_uses_set_euo_pipefail(self):
        for path in SHELL_SCRIPTS:
            self.assertIn("set -euo pipefail", path.read_text(encoding="utf-8"), path.name)

    def test_backup_never_deletes(self):
        """备份脚本只读：不能出现删除库/删除文件的命令。"""
        text = (OPS / "backup.sh").read_text(encoding="utf-8")
        for forbidden in ("dropdb", "rm -rf", "DROP DATABASE", "truncate"):
            self.assertNotIn(forbidden, text, f"backup.sh 不应包含 {forbidden}")

    def test_restore_refuses_to_overwrite(self):
        text = (OPS / "restore.sh").read_text(encoding="utf-8")
        self.assertIn("目标库已存在", text)
        self.assertIn("本脚本不覆盖", text)

    def test_restore_targets_isolated_names(self):
        """恢复默认落到 *_restore_* 库与独立目录，不碰业务库。"""
        text = (OPS / "restore.sh").read_text(encoding="utf-8")
        self.assertIn("_restore_", text)
        self.assertIn("--target-db", text)
        self.assertIn("--target-files", text)

    def test_scripts_avoid_variable_before_multibyte(self):
        """bash 3.2（macOS 自带）会把 $VAR 后面紧跟的中文字节算进变量名，必须用 ${VAR}。"""
        import re

        risky = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\x00-\x7F])")
        for path in SHELL_SCRIPTS:
            for match in risky.finditer(path.read_text(encoding="utf-8")):
                self.fail(f"{path.name} 中 ${match.group(1)} 紧跟非 ASCII 字符，请改用 ${{{match.group(1)}}}")


class CompareCountsTests(unittest.TestCase):
    """compare_counts.py 是恢复验收的判定器，必须“一致=0，不一致=1”。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.script = OPS / "compare_counts.py"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, payload: dict) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def payload(self, count: int = 3, digest: str = "a" * 64) -> dict:
        return {
            "db_vendor": "postgresql",
            "db_name": "customs_agent",
            "tables": {"reviews_task": count, "accounts_user": 5},
            "files": [{"path": "tasks/t/versions/v/original/x.xlsx", "size": 10, "sha256": digest}],
            "file_bytes": 10,
            "file_count": 1,
        }

    def run_compare(self, left: Path, right: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), str(left), str(right)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_identical_inputs_pass(self):
        result = self.run_compare(self.write("a.json", self.payload()), self.write("b.json", self.payload()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("一致", result.stdout)

    def test_row_count_difference_fails(self):
        result = self.run_compare(
            self.write("a.json", self.payload(count=3)), self.write("b.json", self.payload(count=4))
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("reviews_task", result.stdout)

    def test_file_hash_difference_fails(self):
        result = self.run_compare(
            self.write("a.json", self.payload()), self.write("b.json", self.payload(digest="b" * 64))
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("文件内容不同", result.stdout)

    def test_missing_table_fails(self):
        missing = self.payload()
        missing["tables"] = {"accounts_user": 5}
        result = self.run_compare(self.write("a.json", self.payload()), self.write("b.json", missing))
        self.assertEqual(result.returncode, 1)
        self.assertIn("缺少表", result.stdout)

    def test_extra_table_is_only_a_hint(self):
        extra = self.payload()
        extra["tables"] = dict(extra["tables"], reviews_new_table=0)
        result = self.run_compare(self.write("a.json", self.payload()), self.write("b.json", extra))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("多出", result.stdout)


class TableCountsHelperTests(unittest.TestCase):
    """file_inventory 是备份清单的来源：内容寻址、排除暂存区。"""

    def setUp(self):
        self.module = load_module(OPS / "table_counts.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_root_gives_empty_inventory(self):
        self.assertEqual(self.module.file_inventory(self.root / "nope"), [])

    def test_inventory_hashes_and_sizes(self):
        target = self.root / "tasks" / "t1" / "versions" / "v1" / "original"
        target.mkdir(parents=True)
        (target / "a.xlsx").write_bytes(b"hello")

        inventory = self.module.file_inventory(self.root)
        self.assertEqual(len(inventory), 1)
        entry = inventory[0]
        self.assertEqual(entry["path"], "tasks/t1/versions/v1/original/a.xlsx")
        self.assertEqual(entry["size"], 5)
        self.assertEqual(
            entry["sha256"], "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_staging_directory_is_excluded(self):
        (self.root / "incoming").mkdir()
        (self.root / "incoming" / "half.bin").write_bytes(b"partial")
        kept = self.root / "tasks"
        kept.mkdir()
        (kept / "b.xlsx").write_bytes(b"kept")

        paths = [item["path"] for item in self.module.file_inventory(self.root)]
        self.assertEqual(paths, ["tasks/b.xlsx"], "incoming/ 是事务暂存区，不该进备份")


if __name__ == "__main__":
    unittest.main()
