"""Isolated Windows development services; no system services or production data.

Python scripts/queue/local_windows.py start|stop|status
Portable archives live under .local/tools. Missing archives are downloaded from
fixed published URLs and checked against the pinned SHA256 before extraction.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
TOOLS = LOCAL / "tools"
# Native initdb misinterprets non-ASCII installation/data paths on this Windows
# locale. Keep portable runtime in this dedicated ASCII user directory.
RUNTIME = Path(os.environ.get("CUSTOMS_P13_RUNTIME", str(Path(os.environ.get("LOCALAPPDATA", str(LOCAL))) / "customs-agent-p13-runtime")))
PG = RUNTIME / "postgres" / "bin"
REDIS = RUNTIME / "redis"
PGDATA, REDISDATA = RUNTIME / "pgdata", RUNTIME / "redisdata"
SOURCES = {
    "postgres17.jar": ("https://repo.maven.apache.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-windows-amd64/17.12.0/embedded-postgres-binaries-windows-amd64-17.12.0.jar",
        "bebadbe868ed53adb1c12033a381e41005915205ea9fedaa2349f5467fa7016b"),
    "redis8.zip": ("https://github.com/redis-windows/redis-windows/releases/download/8.10.3/Redis-8.10.3-Windows-x64-cygwin.zip",
        "8000848c48668c8cd32f21386ce951d0e02c1aefae39443ab9d3de11ff8ebf9e"),
}


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def run(args, **kwargs):
    # pg_ctl children inherit pipe handles on Windows: waiting for pipe EOF can
    # hang even after pg_ctl exits. A real log file avoids that dependency.
    log_path = RUNTIME / "control.log"
    with log_path.open("ab") as log:
        result = subprocess.run([str(a) for a in args], cwd=RUNTIME, creationflags=subprocess.CREATE_NO_WINDOW,
                                stdout=log, stderr=log, timeout=60, **kwargs)
    if result.returncode:
        raise RuntimeError(f"Service command failed ({result.returncode}); see {log_path}")
    return result


def prepare():
    TOOLS.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in SOURCES.items():
        archive = TOOLS / name
        if not archive.exists():
            print(f"Downloading {name}", flush=True)
            with urlopen(url, timeout=60) as source, archive.open("wb") as out:
                while block := source.read(1024*1024):
                    out.write(block)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"Checksum mismatch: {name}; no executable was started")
    if not (TOOLS / "postgres/bin/postgres.exe").exists():
        with ZipFile(TOOLS / "postgres17.jar") as archive:
            with tarfile.open(fileobj=io.BytesIO(archive.read("postgres-windows-x86_64.txz")), mode="r:xz") as data:
                data.extractall(TOOLS / "postgres", filter="data")
    if not (TOOLS / "redis/Redis-8.10.3-Windows-x64-cygwin/redis-server.exe").exists():
        with ZipFile(TOOLS / "redis8.zip") as archive:
            archive.extractall(TOOLS / "redis")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not (PG / "postgres.exe").exists():
        shutil.copytree(TOOLS / "postgres", RUNTIME / "postgres", dirs_exist_ok=True)
    if not (REDIS / "redis-server.exe").exists():
        shutil.copytree(TOOLS / "redis/Redis-8.10.3-Windows-x64-cygwin", REDIS, dirs_exist_ok=True)


def start():
    prepare()
    if not PGDATA.exists():
        run([PG / "initdb.exe", "-D", PGDATA, "-U", "customs_p13_admin", "--auth=trust", "--encoding=UTF8", "--no-locale"])
    if not listening(55413):
        run([PG / "pg_ctl.exe", "-D", PGDATA, "-l", RUNTIME / "postgres.log", "-o", "-h 127.0.0.1 -p 55413", "-w", "start"])
    REDISDATA.mkdir(parents=True, exist_ok=True)
    if not listening(63813):
        process = subprocess.Popen([str(REDIS / "redis-server.exe"), "--bind", "127.0.0.1", "--port", "63813",
            "--protected-mode", "yes", "--dir", ".", "--appendonly", "yes", "--logfile", "redis.log"],
            cwd=REDISDATA, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (LOCAL / "p13-redis.pid").write_text(str(process.pid))
        for _ in range(50):
            if listening(63813):
                break
            if process.poll() is not None:
                raise RuntimeError("Redis did not start; see .local/p13-redisdata/redis.log")
            time.sleep(0.1)
    import psycopg
    from psycopg import sql
    with psycopg.connect(host="127.0.0.1", port=55413, user="customs_p13_admin", dbname="postgres", autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='customs_p13'").fetchone():
            conn.execute("CREATE ROLE customs_p13 LOGIN CREATEDB")
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname='customs_p13'").fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier("customs_p13"), sql.Identifier("customs_p13")))
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text("\n".join([
            "CUSTOMS_DB_BACKEND=postgres", "CUSTOMS_PG_NAME=customs_p13", "CUSTOMS_PG_USER=customs_p13",
            "CUSTOMS_PG_HOST=127.0.0.1", "CUSTOMS_PG_PORT=55413", "CUSTOMS_EXECUTOR_MODE=rq",
            "CUSTOMS_REDIS_URL=redis://127.0.0.1:63813/0", "CUSTOMS_QUEUE_NAME=customs-p13",
            "CUSTOMS_SESSION_COOKIE_NAME=customs_p13_session", "CUSTOMS_QUEUE_TIMEOUT=15",
            "CUSTOMS_QUEUE_LEASE_SECONDS=20", "CUSTOMS_QUEUE_REDELIVERY_SECONDS=3", "",
        ]), encoding="utf-8")
    print("PostgreSQL 127.0.0.1:55413; Redis 127.0.0.1:63813; independent customs_p13 database", flush=True)


def stop():
    if listening(63813):
        from redis import Redis
        Redis(host="127.0.0.1", port=63813).shutdown(save=True)
    if (PGDATA / "postmaster.pid").exists():
        run([PG / "pg_ctl.exe", "-D", PGDATA, "-m", "fast", "-w", "stop"])
    print("Local queue database and broker stopped; all data retained")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This optional helper is Windows-only; use existing PostgreSQL and Redis on Linux/macOS")
    if args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    else:
        print(json.dumps({"postgres": listening(55413), "redis": listening(63813)}))
