"""P22 operations coordinator (stdlib only on host).

stdout is exactly one JSON result on success. Progress/child logs go to stderr.
No parsing of human log lines. State and evidence are outside read-only backups.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys

from compare_counts import compare, load, validate
from storage import canonical, related

OPS = Path(__file__).resolve().parent
REPO = OPS.parent.parent


def log(text):
    print(text, file=sys.stderr, flush=True)


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("数据库/角色名称必须为 1..63 位 ASCII SQL 标识符")
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, payload):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    with temp.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


class Runner:
    def __init__(self):
        self.mode = os.environ.get("BACKUP_MODE", "compose")
        if self.mode not in ("compose", "native"):
            raise ValueError("BACKUP_MODE 必须为 compose/native")
        self.python = os.environ.get("PYTHON") or next((str(p) for p in (REPO / ".venv-customs/bin/python", REPO / ".venv/bin/python") if p.is_file()), "python3")
        self.backups = Path(os.environ.get("BACKUP_ROOT", str(REPO / ("deploy/backups" if self.mode == "compose" else ".local/backups")))).resolve()
        self.state = Path(os.environ.get("OPERATIONS_STATE_ROOT", str(self.backups.parent / ".state"))).resolve()
        if related(self.backups, self.state):
            raise ValueError("状态目录必须与备份介质隔离")
        self.dc = ["docker", "compose", "-f", os.environ.get("CUSTOMS_COMPOSE_FILE", str(REPO / "deploy/docker-compose.yml")),
                   "--env-file", os.environ.get("CUSTOMS_ENV_FILE", str(REPO / "deploy/.env"))]
        if self.mode == "compose":
            config = json.loads(self.command(self.dc + ["config", "--format", "json"]))
            app = config["services"]["app"]
            env = app["environment"]
            db_env = config["services"]["db"]["environment"]
            self.source_db = identifier(env.get("CUSTOMS_PG_NAME", db_env["POSTGRES_DB"]))
            self.user = identifier(env.get("CUSTOMS_PG_USER", db_env["POSTGRES_USER"]))
            self.deployment = config.get("name", str(Path(self.dc[3]).resolve()))
            self.root = canonical(env["CUSTOMS_FILE_ROOT"])
            mounts = [canonical(v["target"]) for v in app.get("volumes", [])
                      if v.get("type") in ("volume", "bind") and not v.get("read_only", False)
                      and canonical(v["target"]) in self.root.parents]
            if not mounts:
                raise ValueError("CUSTOMS_FILE_ROOT 必须位于可写持久卷内，且卷内留有隔离恢复空间")
            self.base = max(mounts, key=lambda p: len(p.parts))
            self.restore_parent = canonical(os.environ.get("OPERATIONS_RESTORE_ROOT", str(self.base / "restores")))
            if self.restore_parent == self.base or self.base not in self.restore_parent.parents:
                raise ValueError("恢复父目录必须位于持久卷内")
        else:
            self.source_db = identifier(os.environ.get("CUSTOMS_PG_NAME", "customs_agent"))
            self.user = identifier(os.environ.get("CUSTOMS_PG_USER", "customs_agent"))
            self.root = canonical(os.environ.get("CUSTOMS_FILE_ROOT", str(REPO / ".local/files")))
            if related(self.root, self.backups) or related(self.root, self.state):
                raise ValueError("原件目录不得与备份/状态目录相同或有祖先/后代关系")
            self.base = canonical(os.environ.get("OPERATIONS_RESTORE_ROOT", str(self.root.parent)))
            self.restore_parent = self.base
        self.env = dict(os.environ)
        self.env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        self.env["PGPASSWORD"] = os.environ.get("CUSTOMS_PG_PASSWORD", "")
        self.env.update(CUSTOMS_DB_BACKEND="postgres", CUSTOMS_PG_NAME=self.source_db,
                        CUSTOMS_PG_USER=self.user, CUSTOMS_FILE_ROOT=str(self.root))

    def command(self, args, *, data=None, input_file=None, output=None, env=None):
        log("执行: " + " ".join(str(a) for a in args if not str(a).startswith("CUSTOMS_PG_PASSWORD=")))
        result = subprocess.run([str(a) for a in args], input=data,
                                stdin=input_file if input_file is not None else (subprocess.DEVNULL if data is None else None),
                                stdout=output or subprocess.PIPE, stderr=None, cwd=REPO, env=env, check=True)
        return result.stdout if output is None else None

    def app(self, script, args=(), *, target_db=None, target_files=None, data=None, input_file=None, output=None):
        env = dict(self.env)
        if target_db:
            env["CUSTOMS_PG_NAME"] = target_db
        if target_files:
            env["CUSTOMS_FILE_ROOT"] = str(target_files)
        if self.mode == "compose":
            cmd = self.dc + ["run", "--rm", "--no-deps", "-T"]
            # Explicit overrides, never shell code. Root/db also set for production probes.
            for key in ("CUSTOMS_PG_NAME", "CUSTOMS_FILE_ROOT"):
                cmd += ["-e", f"{key}={env[key]}"]
            cmd += ["app", "python", "scripts/operations/" + script] + list(args)
            return self.command(cmd, data=data, input_file=input_file, output=output)
        return self.command([self.python, OPS / script] + list(args), data=data, input_file=input_file, output=output, env=env)

    def storage(self, action, target=None, *, data=None, input_file=None, output=None, identity=None, preserve_failure=False):
        args = [action, "--root", str(self.root), "--base", str(self.base)]
        if target is not None:
            args += ["--target", str(target)]
        if preserve_failure:
            args += ["--keep-failed-target"]
        if identity is not None:
            args += ["--identity", json.dumps(identity)]
        return self.app("storage.py", args, data=data, input_file=input_file, output=output)

    def pg(self, tool, args=(), *, admin=False, data=None, input_file=None, output=None):
        if self.mode == "compose":
            cmd = self.dc + ["exec", "-T", "db", tool, "-U", self.user]
            return self.command(cmd + list(args), data=data, input_file=input_file, output=output)
        user = identifier(os.environ.get("CUSTOMS_PG_ADMIN_USER", "postgres")) if admin else self.user
        cmd = [tool, "--host=" + os.environ.get("CUSTOMS_PG_HOST", "127.0.0.1"),
               "--port=" + os.environ.get("CUSTOMS_PG_PORT", "5432"), "--username=" + user]
        return self.command(cmd + list(args), data=data, input_file=input_file, output=output, env=self.env)

    def oid(self, name):
        identifier(name)
        raw = self.pg("psql", ["-X", "-v", "ON_ERROR_STOP=1", "-d", "postgres", "-tAc",
                              f"SELECT oid FROM pg_database WHERE datname='{name}'"], admin=True).decode().strip()
        return int(raw) if raw else None

    def targets(self, db, files, backup=None):
        identifier(db)
        if db in (self.source_db, "postgres", "template0", "template1"):
            raise ValueError("不允许生产库/系统库作为恢复或校验目标")
        raw_files = str(files)
        files = canonical(files)
        if related(files, self.root) or files == self.base or self.base not in files.parents:
            raise ValueError("目标必须在隔离边界内，且与原件目录无祖先/后代关系")
        if self.mode == "native":
            for protected in (self.backups, self.state, Path(backup).resolve() if backup else self.backups):
                if related(files, protected):
                    raise ValueError("目标目录与备份/状态证据目录存在危险关系")
        probe = json.loads(self.storage("probe", raw_files))
        if probe["target"] != str(files) or probe["root"] != str(self.root):
            raise ValueError("实际文件路径身份与配置不一致")
        return files, probe

    @contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # All cooperating operations for this exact deployment share a lock,
        # even with different BACKUP_ROOT/state roots.
        key = hashlib.sha256(json.dumps([self.mode, self.deployment if self.mode == "compose" else
                                        [self.env.get("CUSTOMS_PG_HOST", "127.0.0.1"), self.env.get("CUSTOMS_PG_PORT", "5432")]],
                                       sort_keys=True).encode()).hexdigest()
        lockdir = Path(os.environ.get("TMPDIR", "/tmp")) / ("customs-operations-" + str(os.getuid()))
        lockdir.mkdir(mode=0o700, exist_ok=True)
        if lockdir.is_symlink() or lockdir.stat().st_uid != os.getuid() or lockdir.stat().st_mode & 0o077:
            raise ValueError("不安全的锁目录")
        fd = os.open(lockdir / (key + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("已有备份/恢复/校验在运行（并发保护）") from exc
            yield
        finally:
            os.close(fd)

    def counts(self, db, files):
        payload = validate(json.loads(self.app("table_counts.py", ["--files-root", str(files)], target_db=db, target_files=files)))
        if "table_sha256" not in payload:
            raise ValueError("清单缺少逐行数据库内容指纹")
        oid = self.oid(db)
        if payload["db_vendor"] != "postgresql" or payload["db_name"] != db or oid is None or payload.get("database_oid") != oid:
            raise ValueError("实际连接的数据库身份不是请求目标")
        if payload["file_root"] != str(files) or not payload.get("file_identity"):
            raise ValueError("实际文件根身份不是请求目标")
        return payload

    def backup(self, label, quiesce):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", label):
            raise ValueError("label 必须为 1..48 位 ASCII 字母数字、下划线或短横线")
        self.storage("probe")  # missing is an error; a genuinely empty directory is valid
        self.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.backups / (stamp() + "-" + label + "-" + secrets.token_hex(4))
        directory.mkdir(mode=0o700)
        (directory / "INCOMPLETE").write_text("backup in progress\n")
        stopped = []
        success = False
        try:
            if self.mode == "compose" and quiesce:
                running = self.command(self.dc + ["ps", "--status", "running", "--services"]).decode().splitlines()
                # app, web, worker AND any future writer are stopped, preserving exact prior service state.
                stopped = sorted(set(running) - {"db", "redis"})
                atomic_json(directory / "services-before.json", {"running": running, "quiesced": stopped})
                if stopped:
                    self.command(self.dc + ["stop"] + stopped)
            elif self.mode == "native" and quiesce:
                log("警告：native 无法自动停写，请确保所有 web/worker 写入者已停止")
            elif not quiesce:
                log("警告：不停写备份不承诺数据库与文件同一时点")
            with (directory / "db.dump").open("xb") as handle:
                self.pg("pg_dump", ["-Fc", "-d", self.source_db], output=handle)
            with (directory / "files.tar.gz").open("xb") as handle:
                self.storage("archive", output=handle)
            payload = self.counts(self.source_db, self.root)
            atomic_json(directory / "counts.json", payload)
            atomic_json(directory / "meta.json", {"created_at": stamp(), "mode": self.mode, "quiesced": quiesce,
                                                  "db": self.source_db, "files": str(self.root), "label": label})
            manifest = {name: sha256_file(directory / name)
                        for name in ("db.dump", "files.tar.gz", "counts.json")}
            atomic_json(directory / "manifest.json", manifest)
            # Keep traditional checksum manifest for human/offline tools.
            (directory / "manifest.sha256").write_text("".join(f"{v}  {k}\n" for k, v in manifest.items()))
            if not (directory / "db.dump").stat().st_size:
                raise ValueError("数据库转储为空")
            (directory / "INCOMPLETE").unlink()
            (directory / "COMPLETE").write_text("complete\n")
            success = True
        except BaseException as exc:
            (directory / "FAILED").write_text(str(exc) + "\n")
            # No COMPLETE on a failed backup; partial dump retained as evidence.
            log(f"备份失败证据: {directory}")
            raise
        finally:
            if stopped:
                try:
                    self.command(self.dc + ["start"] + stopped)
                except Exception:
                    if success:
                        (directory / "SERVICE_RESUME_FAILED").write_text("需要人工恢复原运行服务\n")
                    raise
        return {"backup_dir": str(directory)}

    def check_backup(self, directory):
        directory = Path(directory).resolve(strict=True)
        if related(directory, self.state):
            raise ValueError("备份与状态目录必须隔离")
        if not (directory / "COMPLETE").is_file() or any((directory / x).exists() for x in ("FAILED", "INCOMPLETE")):
            raise ValueError("备份未完成或曾失败，拒绝恢复")
        manifest = json.loads((directory / "manifest.json").read_text())
        if not isinstance(manifest, dict) or set(manifest) != {"db.dump", "files.tar.gz", "counts.json"}:
            raise ValueError("无效 manifest schema")
        for name, digest in manifest.items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
                raise ValueError("备份清单不一致: " + name)
        expected = load(directory / "counts.json")
        if "table_sha256" not in expected or not expected.get("file_identity"):
            raise ValueError("新版备份必须包含逐行内容指纹与文件根身份，禁止静默降为行数校验")
        return directory

    def restore(self, directory, db=None, files=None, preserve_failure=False):
        directory = self.check_backup(directory)
        suffix = stamp() + "_" + secrets.token_hex(3)
        db = db or self.source_db[:22] + "_restore_" + suffix
        files = files or str(self.restore_parent / ("restore-" + suffix))
        files, probe = self.targets(db, files, directory)
        if probe["exists"]:
            raise ValueError("目标目录已存在（本脚本不覆盖，空目录也不抢占）")
        if self.oid(db) is not None:
            raise ValueError("目标库已存在（本脚本不覆盖）")
        evidence = self.state / ("restore-" + suffix + ".json")
        state = {"status": "starting", "backup_dir": str(directory), "target_db": db, "target_files": str(files)}
        atomic_json(evidence, state)
        owned_oid, file_identity = None, None
        try:
            args = ["-T", "template0", "-E", "UTF8"]
            if self.mode == "native":
                args += ["-O", self.user]
            self.pg("createdb", args + [db], admin=True)
            owned_oid = self.oid(db)
            if owned_oid is None:
                raise ValueError("建库后无法确认数据库身份")
            state.update(database_oid=owned_oid, status="database-created")
            atomic_json(evidence, state)
            with (directory / "db.dump").open("rb") as source:
                self.pg("pg_restore", ["--exit-on-error", "--no-owner", "-d", db], input_file=source)
            with (directory / "files.tar.gz").open("rb") as source:
                file_identity = json.loads(self.storage("extract", files, input_file=source, preserve_failure=preserve_failure))
            state.update(file_identity=file_identity, status="restored")
            atomic_json(evidence, state)
        except BaseException as exc:
            state.update(status="failed", error=str(exc))
            atomic_json(evidence, state)
            if not preserve_failure:
                if file_identity is not None:
                    self.storage("remove", files, identity=file_identity)
                if owned_oid is not None and self.oid(db) == owned_oid:
                    self.pg("dropdb", [db], admin=True)
            log(f"恢复失败，状态证据: {evidence}")
            raise
        return dict(state, state_file=str(evidence))

    def verify(self, directory, db, files, state_file=None):
        directory = self.check_backup(directory)
        files, probe = self.targets(db, files, directory)
        if not probe["exists"]:
            raise ValueError("恢复文件根不存在")
        actual = self.counts(db, files)
        if state_file:
            state = json.loads(Path(state_file).read_text())
            if state["target_db"] != db or state["target_files"] != str(files) or state["database_oid"] != actual["database_oid"] or state["file_identity"] != actual["file_identity"]:
                raise ValueError("恢复目标已被替换，实际身份不匹配")
        evidence = self.state / ("verify-" + stamp() + "-" + secrets.token_hex(4) + ".json")
        atomic_json(evidence, actual)
        problems = compare(load(directory / "counts.json"), actual)
        if problems:
            raise ValueError("校验失败: " + "; ".join(problems) + f"; 实际清单保留在 {evidence}")
        return {"verified": True, "counts_file": str(evidence), "target_db": db, "target_files": str(files)}

    def drill(self, label, quiesce, keep):
        evidence = self.state / ("drill-" + stamp() + "-" + secrets.token_hex(4) + ".json")
        state = {"status": "starting"}
        atomic_json(evidence, state)
        try:
            state.update(self.backup(label, quiesce), status="backed-up")
            atomic_json(evidence, state)
            state.update(self.restore(state["backup_dir"], preserve_failure=True), status="restored")
            atomic_json(evidence, state)
            state.update(self.verify(state["backup_dir"], state["target_db"], state["target_files"], state["state_file"]))
            if not keep:
                files, _ = self.targets(state["target_db"], state["target_files"], state["backup_dir"])
                if self.oid(state["target_db"]) != state["database_oid"]:
                    raise ValueError("拒绝清理：数据库身份已改变")
                self.storage("remove", files, identity=state["file_identity"])
                self.pg("dropdb", [state["target_db"]], admin=True)
            state.update(status="passed", kept=keep)
        except BaseException as exc:
            state.update(status="failed", error=str(exc), kept=True)
            atomic_json(evidence, state)
            log(f"演练失败；隔离目标不清理，终端与状态证据: {evidence}")
            raise
        atomic_json(evidence, state)
        return dict(state, drill_state_file=str(evidence))


def main(argv=None):
    os.umask(0o077)
    def terminate(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, terminate)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("backup", "restore", "verify_restore", "drill"))
    p.add_argument("backup_dir", nargs="?")
    p.add_argument("--label", default=None)
    p.add_argument("--no-quiesce", action="store_true")
    p.add_argument("--target-db")
    p.add_argument("--target-files")
    p.add_argument("--state-file")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args(argv)
    try:
        runner = Runner()
        with runner.lock():
            # Every operation's terminal diagnostics are durable (without secrets).
            transcript = runner.state / (args.operation + "-" + stamp() + "-" + secrets.token_hex(4) + ".log")
            with transcript.open("x") as terminal:
                saved_stderr = os.dup(2)
                os.dup2(terminal.fileno(), 2)
                try:
                    if args.operation == "backup":
                        result = runner.backup(args.label or "manual", not args.no_quiesce)
                    elif args.operation == "drill":
                        result = runner.drill(args.label or "drill", not args.no_quiesce, args.keep)
                    else:
                        if not args.backup_dir:
                            raise ValueError("需要备份目录")
                        if args.operation == "restore":
                            result = runner.restore(args.backup_dir, args.target_db, args.target_files)
                        else:
                            if not args.target_db or not args.target_files:
                                raise ValueError("必须给 --target-db 与 --target-files")
                            result = runner.verify(args.backup_dir, args.target_db, args.target_files, args.state_file)
                except BaseException as exc:
                    log(f"失败: {exc}")
                    raise
                finally:
                    os.dup2(saved_stderr, 2)
                    os.close(saved_stderr)
                    log(f"终端记录: {transcript}")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        log(f"失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
