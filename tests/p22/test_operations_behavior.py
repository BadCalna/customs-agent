"""Executable shell orchestration tests: fake Docker/PG, real archive/file actions.

The stub behaves like stopped containers (exec app fails), persists fake DB OIDs,
and executes actual storage.py. Thus these assert behavior rather than grep strings.
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
OPS = REPO / "scripts/operations"

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, re, subprocess, sys
p = pathlib.Path(os.environ["FAKE_STATE"])
s = json.loads(p.read_text())
a = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == "docker":
    a = a[a.index("--env-file") + 2:]
    s["calls"].append(a)
    def save(): p.write_text(json.dumps(s))
    if a[0] == "config":
        save()
        print(json.dumps({"name": s["project"], "services": {
          "app": {"environment": {"CUSTOMS_FILE_ROOT": s["root"], "CUSTOMS_PG_NAME": "production", "CUSTOMS_PG_USER": "appuser"},
                  "volumes": [{"type": "volume", "target": s["base"]}]},
          "db": {"environment": {"POSTGRES_DB": "production", "POSTGRES_USER": "appuser"}}}}))
        sys.exit(0)
    if a[0] == "ps": print("\n".join(s["running"])); save(); sys.exit(0)
    if a[0] == "stop": s["running"] = [x for x in s["running"] if x not in a[1:]]; save(); sys.exit(0)
    if a[0] == "start": s["running"].extend(a[1:]); save(); sys.exit(0)
    if a[0] == "run":
        env = dict(os.environ)
        i = 4
        while a[i] == "-e":
            k, v = a[i+1].split("=", 1); env[k] = v; i += 2
        assert a[i:i+2] == ["app", "python"], a
        script = a[i+2]; args = a[i+3:]
        save()
        if script.endswith("storage.py"):
            sys.exit(subprocess.call([sys.executable, script] + args, env=env))
        assert script.endswith("table_counts.py"), a
        root = pathlib.Path(env["CUSTOMS_FILE_ROOT"])
        db = env["CUSTOMS_PG_NAME"]
        # Deliberately wrong actual identity catches a lost target override.
        if s.get("wrong_db"): db = "production"
        files = []
        import hashlib
        for f in sorted(root.rglob("*")):
            if f.is_file() and "incoming" not in f.relative_to(root).parts:
                files.append({"path": str(f.relative_to(root)), "size": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
        st = root.stat()
        tables = {"django_migrations": 1, "business": 2}
        if s.get("mismatch") and db != "production": tables["business"] = 9
        print(json.dumps({"db_vendor": "postgresql", "db_name": db, "database_oid": s["dbs"][db],
          "file_root": str(root.resolve()), "file_identity": {"path": str(root.resolve()), "device": st.st_dev, "inode": st.st_ino},
          "tables": tables, "table_sha256": {k: "a"*64 for k in tables}, "files": files,
          "file_count": len(files), "file_bytes": sum(x["size"] for x in files)}))
        sys.exit(0)
    assert a[0] == "exec" and a[2] == "db", "exec of stopped app forbidden"
    a = a[3:]
else:
    s["calls"].append([pathlib.Path(sys.argv[0]).name] + a)
    a = [pathlib.Path(sys.argv[0]).name] + a

tool = a[0]
a = a[1:]
if tool == "psql":
    name = re.search("datname='([^']+)'", a[-1]).group(1)
    print(s["dbs"].get(name, ""))
elif tool == "createdb":
    name = a[-1]
    if name in s["dbs"]: sys.exit(1)
    s["dbs"][name] = max(s["dbs"].values()) + 1
elif tool == "dropdb": del s["dbs"][a[-1]]
elif tool == "pg_dump":
    if s.get("dump_fail"): p.write_text(json.dumps(s)); sys.exit(4)
    sys.stdout.buffer.write(b"FAKE-PG-DUMP")
elif tool == "pg_restore":
    assert "--exit-on-error" in a
    assert sys.stdin.buffer.read() == b"FAKE-PG-DUMP"
    if s.get("restore_fail"): p.write_text(json.dumps(s)); sys.exit(5)
else: raise AssertionError(tool)
p.write_text(json.dumps(s))
'''


class OperationsBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.volume = self.base / "volume"
        self.root = self.volume / "actual-config-root"
        self.root.mkdir(parents=True)
        (self.volume / "restores").mkdir()
        (self.root / "original.txt").write_text("original evidence")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("docker", "pg_dump", "psql", "createdb", "pg_restore", "dropdb"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o700)
        self.state = self.base / "fake.json"
        self.state.write_text(json.dumps({"project": "test-" + secrets.token_hex(8), "base": str(self.volume),
                                         "root": str(self.root), "running": ["db", "app", "worker"],
                                         "calls": [], "dbs": {"production": 100}}))
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        OPERATIONS_PYTHON=sys.executable, PYTHON=sys.executable,
                        BACKUP_MODE="compose", BACKUP_ROOT=str(self.base / "backups"),
                        OPERATIONS_STATE_ROOT=str(self.base / "evidence"), FAKE_STATE=str(self.state),
                        **{chr(68)+"JANGO_SECRET_KEY": secrets.token_hex(32)})

    def fake(self, **changes):
        state = json.loads(self.state.read_text())
        state.update(changes)
        self.state.write_text(json.dumps(state))
        return state

    def run_script(self, name, *args, ok=True):
        result = subprocess.run(["bash", str(OPS / (name + ".sh")), *map(str, args)], env=self.env,
                                cwd=REPO, capture_output=True, text=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr + "\n" + self.logs())
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def logs(self):
        return "\n".join(p.read_text() for p in (self.base / "evidence").glob("*.log"))

    def backup(self):
        return Path(self.run_script("backup")["backup_dir"])

    def test_full_shell_drill_uses_run_after_stop_and_restores_only_original_services(self):
        result = self.run_script("drill")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(Path(result["target_files"]).parent, self.volume / "restores")
        self.assertFalse(Path(result["target_files"]).exists())
        state = json.loads(self.state.read_text())
        self.assertEqual(sorted(state["running"]), ["app", "db", "worker"])
        self.assertEqual(state["dbs"], {"production": 100})
        self.assertIn(["stop", "app", "worker"], state["calls"])
        self.assertIn(["start", "app", "worker"], state["calls"])
        self.assertFalse(any(c[0] == "exec" and "app" in c[:4] for c in state["calls"]))
        self.assertTrue(Path(result["drill_state_file"]).is_file())
        self.assertEqual(Path(result["backup_dir"]).stat().st_mode & 0o777, 0o700)

    def test_backup_failure_marks_partial_and_resumes_worker(self):
        self.fake(dump_fail=True)
        self.run_script("backup", ok=False)
        directory = next((self.base / "backups").iterdir())
        self.assertTrue((directory / "FAILED").exists())
        self.assertFalse((directory / "COMPLETE").exists())
        self.assertEqual(sorted(json.loads(self.state.read_text())["running"]), ["app", "db", "worker"])
        self.run_script("restore", directory, ok=False)

    def test_empty_existing_source_is_valid_but_missing_is_not(self):
        (self.root / "original.txt").unlink()
        directory = self.backup()
        self.assertEqual(json.loads((directory / "counts.json").read_text())["files"], [])
        self.root.rmdir()
        self.run_script("backup", ok=False)

    def test_restore_only_reads_media_and_verify_accepts_different_db_names(self):
        directory = self.backup()
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        result = self.run_script("restore", directory, "--target-db", "isolated", "--target-files", self.volume / "isolated")
        verify = self.run_script("verify_restore", directory, "--target-db", "isolated", "--target-files", result["target_files"],
                                 "--state-file", result["state_file"])
        self.assertTrue(verify["verified"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        self.fake(wrong_db=True)
        self.run_script("verify_restore", directory, "--target-db", "isolated", "--target-files", result["target_files"], ok=False)

    def test_restore_rejects_injection_production_paths_ancestors_and_outside_volume(self):
        directory = self.backup()
        for db, path in [("production", self.volume / "new"), ("x'; DROP DATABASE production;--", self.volume / "new"),
                         ("x"*64, self.volume / "new"), ("safe", self.root), ("safe", self.root / "child"),
                         ("safe", self.volume), ("safe", self.base / "outside"), ("safe", self.volume / "quote'unsafe")]:
            self.run_script("restore", directory, "--target-db", db, "--target-files", path, ok=False)
        self.assertEqual(json.loads(self.state.read_text())["dbs"], {"production": 100})

    def test_symlink_target_and_missing_verify_root_rejected(self):
        directory = self.backup()
        (self.volume / "alias").symlink_to(self.volume / "new")
        self.run_script("restore", directory, "--target-db", "safe", "--target-files", self.volume / "alias", ok=False)
        self.run_script("verify_restore", directory, "--target-db", "safe", "--target-files", self.volume / "missing", ok=False)

    def test_restore_failure_cleans_only_its_new_database(self):
        directory = self.backup()
        self.fake(restore_fail=True)
        self.run_script("restore", directory, "--target-db", "new", "--target-files", self.volume / "new", ok=False)
        self.assertEqual(json.loads(self.state.read_text())["dbs"], {"production": 100})
        self.fake(dbs={"production": 100, "preexisting": 101})
        self.run_script("restore", directory, "--target-db", "preexisting", "--target-files", self.volume / "new", ok=False)
        self.assertIn("preexisting", json.loads(self.state.read_text())["dbs"])

    def test_drill_failure_keeps_isolation_and_durable_terminal(self):
        self.fake(mismatch=True)
        self.run_script("drill", ok=False)
        state = json.loads(next((self.base / "evidence").glob("drill-*.json")).read_text())
        self.assertEqual(state["status"], "failed")
        self.assertTrue(Path(state["target_files"]).is_dir())
        self.assertIn(state["target_db"], json.loads(self.state.read_text())["dbs"])
        self.assertIn("校验失败", self.logs())

    def test_deployment_lock_rejects_competing_shell_without_backup_writes(self):
        import fcntl
        import hashlib
        project = json.loads(self.state.read_text())["project"]
        key = hashlib.sha256(json.dumps(["compose", project], sort_keys=True).encode()).hexdigest()
        lockdir = Path(os.environ.get("TMPDIR", "/tmp")) / ("customs-operations-" + str(os.getuid()))
        lockdir.mkdir(mode=0o700, exist_ok=True)
        with (lockdir / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script("backup", ok=False)
            self.assertIn("并发保护", result.stderr)
        self.assertFalse((self.base / "backups").exists())

    def test_missing_content_fingerprint_rejected_not_downgraded(self):
        directory = self.backup()
        counts = json.loads((directory / "counts.json").read_text())
        counts.pop("table_sha256")
        (directory / "counts.json").write_text(json.dumps(counts))
        import hashlib
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["counts.json"] = hashlib.sha256((directory / "counts.json").read_bytes()).hexdigest()
        (directory / "manifest.json").write_text(json.dumps(manifest))
        result = self.run_script("restore", directory, ok=False)
        self.assertIn("禁止静默", result.stderr)

    def test_native_shell_flow_with_fake_postgres(self):
        # Native Django is replaced with the same table_counts stub; storage remains real.
        wrapper = self.bin / "native-python"
        wrapper.write_text("#!/usr/bin/env python3\nimport os,subprocess,sys\n"
                           "a=sys.argv[1:]\n"
                           "if a and a[0].endswith('table_counts.py'):\n"
                           " s=['docker','compose','-f','unused','--env-file','unused','run','--rm','--no-deps','-T','-e','CUSTOMS_PG_NAME='+os.environ['CUSTOMS_PG_NAME'],'-e','CUSTOMS_FILE_ROOT='+os.environ['CUSTOMS_FILE_ROOT'],'app','python','scripts/operations/table_counts.py']+a[1:]\n"
                           "else: s=[sys.executable]+a\n"
                           "sys.exit(subprocess.call(s))\n")
        wrapper.chmod(0o700)
        self.env.update(BACKUP_MODE="native", PYTHON=str(wrapper), CUSTOMS_PG_NAME="production", CUSTOMS_PG_USER="appuser",
                        CUSTOMS_FILE_ROOT=str(self.root))
        result = self.run_script("drill")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(json.loads(self.state.read_text())["dbs"], {"production": 100})

    def test_bad_label_has_no_backup_directory(self):
        for label in ("../oops", "x"*49, "a/b", "quote'"):
            self.run_script("backup", "--label", label, ok=False)
        self.assertFalse((self.base / "backups").exists())

    def test_archive_traversal_fails_without_writing_outside_target(self):
        directory = self.backup()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            item = tarfile.TarInfo("../escape")
            item.size = 4
            archive.addfile(item, io.BytesIO(b"evil"))
        (directory / "files.tar.gz").write_bytes(stream.getvalue())
        import hashlib
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["files.tar.gz"] = hashlib.sha256(stream.getvalue()).hexdigest()
        (directory / "manifest.json").write_text(json.dumps(manifest))
        self.run_script("restore", directory, "--target-db", "new", "--target-files", self.volume / "new", ok=False)
        self.assertFalse((self.volume / "escape").exists())
        self.assertFalse((self.volume / "new").exists())


if __name__ == "__main__":
    unittest.main()
