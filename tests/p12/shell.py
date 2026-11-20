"""Run the real P12 POSIX scripts with an explicit local Bash interpreter."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def bash_command():
    if os.name == "nt":
        # Windows' bash.exe may mean WSL. Use Bash from the same installation as
        # the already installed Git CLI instead, with no profile/PATH mutation.
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
                candidate = root / relative
                if candidate.is_file():
                    return [str(candidate), "--noprofile", "--norc"]
        raise RuntimeError("P12 shell tests require the Bash bundled with installed Git for Windows")
    bash = shutil.which("bash")
    if not bash:
        raise RuntimeError("P12 shell tests require an installed Bash interpreter")
    return [bash, "--noprofile", "--norc"]


def shell_environment(environment=None):
    child = dict(os.environ if environment is None else environment)
    child.pop("BASH_ENV", None)
    child.pop("ENV", None)
    child["PYTHON"] = sys.executable
    child.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if os.name == "nt":
        # Git Bash accepts native drive paths with forward slashes. Keep host,
        # database and other semantic configuration values completely intact.
        for name in ("PYTHON", "CUSTOMS_PG_BIN", "CUSTOMS_PGDATA", "CUSTOMS_PG_SOCKET",
                     "CUSTOMS_PG_LOG", "CUSTOMS_FILE_ROOT", "CUSTOMS_SQLITE_PATH", "CUSTOMS_SOURCE_FILE_ROOT"):
            if child.get(name):
                child[name] = Path(child[name]).as_posix()
    return child


def run_database_script(script, *arguments, env=None):
    return subprocess.run(
        [*bash_command(), Path(script).as_posix(), *map(str, arguments)],
        env=shell_environment(env), capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
