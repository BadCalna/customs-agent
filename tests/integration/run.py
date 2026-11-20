"""Run P21 API acceptance in an isolated, explicitly synthetic environment.

Windows: <python> tests/integration/run.py --backend sqlite
Postgres: configure a loopback customs_p21* / p21_* database, then --backend postgres.
Real Redis/worker and browser acceptance are separate evidence, never implied here.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
PG_KEYS = ("CUSTOMS_PG_NAME", "CUSTOMS_PG_USER", "CUSTOMS_PG_PASSWORD",
           "CUSTOMS_PG_HOST", "CUSTOMS_PG_PORT")
CORE_LABELS = [f"tests/p{number:02d}" for number in range(21)] + [
    "tests/p15_pg", "tests/p16_real", "tests/p23", "tests/integration",
]


def environment(backend: str, directory: Path) -> dict[str, str]:
    """Do not inherit application configuration or read the checkout's .env."""
    inherited = os.environ
    result = {key: value for key, value in inherited.items()
              if not key.startswith(("CUSTOMS_", "DJANGO_"))}
    result.update(
        PYTHON_DOTENV_DISABLED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        DJANGO_SETTINGS_MODULE="config.settings", DJANGO_SECRET_KEY=uuid.uuid4().hex,
        CUSTOMS_DB_BACKEND=backend, CUSTOMS_ENGINE_MODE="mock",
        CUSTOMS_EXECUTOR_MODE="inline", CUSTOMS_NOTIFICATION_MODE="mock",
        CUSTOMS_PAGE_MODE="live", CUSTOMS_HISTORY_REFERENCE_ENABLED="0",
        CUSTOMS_SQLITE_PATH=str(directory / "acceptance.sqlite3"),
        CUSTOMS_FILE_ROOT=str(directory / "files"),
    )
    if backend == "postgres":
        name = inherited.get("CUSTOMS_PG_NAME", "")
        host = inherited.get("CUSTOMS_PG_HOST", "127.0.0.1")
        if not re.fullmatch(r"(?:customs_p21|p21_)[a-zA-Z0-9_]*", name):
            raise ValueError("P21 PostgreSQL requires an explicit customs_p21* or p21_* database")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("P21 PostgreSQL acceptance only accepts a loopback host")
        if not inherited.get("CUSTOMS_PG_USER"):
            raise ValueError("Set CUSTOMS_PG_USER for the isolated P21 cluster")
        result.update({key: inherited[key] for key in PG_KEYS if key in inherited})
    if inherited.get("CUSTOMS_OPENPYXL_PYTHON"):
        result["CUSTOMS_OPENPYXL_PYTHON"] = inherited["CUSTOMS_OPENPYXL_PYTHON"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--verbosity", type=int, choices=(1, 2), default=2)
    parser.add_argument("--core-regression", action="store_true",
                        help="Also run P00-P20, PostgreSQL history, real-reference and P23 modules; P22 is separate")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    scope = "core" if args.core_regression else "integration"
    output = (args.output or ROOT / ".local/p21" / f"{scope}-{args.backend}.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".log")
    labels = CORE_LABELS if args.core_regression else ["tests/integration"]
    command = [sys.executable, str(ROOT / "manage.py"), "test", *labels,
               "--noinput", "--verbosity", str(args.verbosity)]
    report = {
        "suite": "P21", "scope": scope, "backend": args.backend, "engine": "mock", "executor": "inline",
        "platform": platform.platform(), "python": platform.python_version(),
        "sample_kind": "synthetic", "status": "failed", "returncode": None,
        "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "command": ["python", "manage.py", *command[2:]],
        "log": str(log_path),
        "not_verified": ["real model/professional classification quality", "human acceptance",
                         "separate Redis/worker processes", "browser UI", "P22 recovery"],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="customs-p21-") as directory:
            child_env = environment(args.backend, Path(directory))
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=child_env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace")
                lines = []
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    lines.append(line)
                report["returncode"] = process.wait()
                transcript = "".join(lines)
                count = re.search(r"Ran (\d+) tests? in", transcript)
                report["tests_run"] = int(count.group(1)) if count else 0
                skipped = re.search(r"skipped=(\d+)", transcript)
                report["skipped"] = int(skipped.group(1)) if skipped else 0
                for category in ("failures", "errors"):
                    found = re.search(rf"{category}=(\d+)", transcript)
                    report[category] = int(found.group(1)) if found else 0
                if process.returncode == 0 and report["tests_run"] > 0:
                    report["status"] = "passed"
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        report["finished_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"P21 report: {output}")
    return 0 if report["status"] == "passed" else (report["returncode"] or 2)


if __name__ == "__main__":
    raise SystemExit(main())
