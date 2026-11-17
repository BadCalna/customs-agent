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
            "file_root": "/srv/customs/files",
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

    def test_missing_files_and_invalid_counts_schema_fail(self):
        for key, value in [("files", None), ("file_count", 9), ("file_bytes", -1), ("tables", {}), ("tables", {"table": True})]:
            bad = self.payload()
            bad[key] = value
            result = self.run_compare(self.write("a.json", self.payload()), self.write("b.json", bad))
            self.assertNotEqual(result.returncode, 0, key)

    def test_file_size_difference_fails_even_with_same_hash(self):
        bad = self.payload()
        bad["files"][0]["size"] = 11
        bad["file_bytes"] = 11
        self.assertEqual(self.run_compare(self.write("a.json", self.payload()), self.write("b.json", bad)).returncode, 1)

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

    def test_extra_table_fails_closed(self):
        extra = self.payload()
        extra["tables"] = dict(extra["tables"], reviews_new_table=0)
        result = self.run_compare(self.write("a.json", self.payload()), self.write("b.json", extra))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("多出", result.stdout)


class TableCountsHelperTests(unittest.TestCase):
    """file_inventory 是备份清单的来源：内容寻址、排除暂存区。"""

    def setUp(self):
        self.module = load_module(OPS / "table_counts.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_root_fails(self):
        with self.assertRaises(ValueError):
            self.module.file_inventory(self.root / "nope")

    def test_existing_empty_root_is_valid(self):
        self.assertEqual(self.module.file_inventory(self.root), [])

    def test_datetime_fingerprint_preserves_microseconds(self):
        from datetime import datetime, timezone
        a = datetime(2026, 1, 1, microsecond=123001, tzinfo=timezone.utc)
        b = a.replace(microsecond=123999)
        self.assertNotEqual(self.module.row_hash([a]), self.module.row_hash([b]))

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

    def test_sqlite_path_json_and_introspected_migrations_table(self):
        import os
        import secrets
        import sqlite3
        db = self.root / "inventory.sqlite3"
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE django_migrations (id integer PRIMARY KEY, name text)")
            connection.execute("INSERT INTO django_migrations VALUES (1, 'initial')")
        env = dict(os.environ, CUSTOMS_DB_BACKEND="sqlite", CUSTOMS_SQLITE_PATH=str(db),
                   CUSTOMS_FILE_ROOT=str(self.root), DJANGO_SETTINGS_MODULE="config.settings")
        env[chr(68)+"JANGO_SECRET_KEY"] = getattr(secrets, "token_hex")(32)
        result = subprocess.run([sys.executable, str(OPS / "table_counts.py"), "--files-root", str(self.root)],
                                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["db_name"], str(db))
        self.assertEqual(payload["tables"]["django_migrations"], 1)
        self.assertIn("django_migrations", payload["table_sha256"])

    def test_pg_check_normalization_is_narrow_and_preserves_meaningful_casts(self):
        before = "CHECK (role::text = ANY (ARRAY['operator'::character varying, 'expert'::character varying]::text[]))"
        after = "CHECK (role::text = ANY (ARRAY['operator'::character varying::text, 'expert'::character varying::text]))"
        self.assertEqual(self.module.normalize_pg_definition(before), self.module.normalize_pg_definition(after))
        for expression in ("CHECK (x::integer > 1)", "CHECK (role::text = ANY (ARRAY['x'::character varying(2)]::text[]))",
                           "CHECK (x = ANY (ARRAY[1::integer,2::integer]))"):
            self.assertEqual(self.module.normalize_pg_definition(expression), expression)
        different = after.replace("expert", "admin")
        self.assertNotEqual(self.module.normalize_pg_definition(before), self.module.normalize_pg_definition(different))

    def test_pg_normalization_never_modifies_sql_literals_identifiers_or_comments(self):
        normalize = self.module.normalize_pg_definition
        attack_a = "CHECK(v<>'ARRAY[''''::character varying]::text[]'::text)"
        attack_b = "CHECK(v<>'ARRAY[''''::text]'::text)"
        self.assertEqual(normalize(attack_a), attack_a)
        self.assertEqual(normalize(attack_b), attack_b)
        self.assertNotEqual(normalize(attack_a), normalize(attack_b))
        sql_array = "ARRAY['x'::character varying]::text[]"
        for text in ("'" + sql_array.replace("'", "''") + "'", '$tag$' + sql_array + '$tag$',
                     '$$' + sql_array + '$$', '"' + sql_array + '"', '-- ' + sql_array,
                     '/* outer /* nested */ ' + sql_array + ' */',
                     r"E'escaped\' ARRAY[''x''::character varying]::text[]'"):
            self.assertEqual(normalize(text), text)
        # Cast-looking bytes INSIDE a genuine ARRAY operand must also stay exact.
        a = "ARRAY['literal ::character varying'::character varying]::text[]"
        b = "ARRAY['literal ::character varying'::character varying::text]"
        self.assertEqual(normalize(a), normalize(b))
        self.assertIn("'literal ::character varying'", normalize(a))
        self.assertEqual(normalize("/* " + sql_array + " */ " + sql_array),
                         "/* " + sql_array + " */ ARRAY['x'::text]")

    def test_postgres_catalog_captures_sequence_state_and_logical_schema(self):
        from types import SimpleNamespace
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql): self.sql = sql
            def fetchall(self):
                if "n.nspname <> 'public'" in self.sql: return []
                if "FROM pg_sequence" in self.sql:
                    return [("public", "business_id_seq", "integer", 1, 1, 2147483647, 1, 1, False, "public", "business", "id")]
                if "FROM pg_index" in self.sql:
                    return [("public", "business", "business_pkey", "CREATE UNIQUE INDEX business_pkey ON public.business USING btree (id)", True, True)]
                return []
            def fetchone(self):
                self_test.assertIn('SELECT last_value,is_called FROM "public"."business_id_seq"', self.sql)
                return (1, False)
        self_test = self
        connection = SimpleNamespace(cursor=Cursor, ops=SimpleNamespace(quote_name=lambda s: '"' + s.replace('"','""') + '"'))
        schema, sequences = self.module.postgres_metadata(connection)
        self.assertEqual(sequences['"public"."business_id_seq"']["is_called"], False)
        self.assertEqual(sequences['"public"."business_id_seq"']["last_value"], 1)
        self.assertEqual(len(schema["indexes"]), 1)
        self.assertNotIn("oid", json.dumps(schema).lower())

    def test_postgres_catalog_fails_closed_on_other_user_schema(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        cursor = MagicMock()
        cursor.fetchall.return_value = [("private_schema", "unverified_table")]
        connection = SimpleNamespace(cursor=MagicMock())
        connection.cursor.return_value.__enter__.return_value = cursor
        with self.assertRaisesRegex(ValueError, "public"):
            self.module.postgres_metadata(connection)

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
