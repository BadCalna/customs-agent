#!/usr/bin/env bash
# Shared launcher: project Python preferred; stdout is machine-readable JSON only.
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/../.." && pwd)"
operations_main() {
  local python_bin="${OPERATIONS_PYTHON:-${PYTHON:-}}"
  if [ -z "$python_bin" ]; then
    if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
      python_bin="$REPO_ROOT/.venv/bin/python"
    elif [ -x "$REPO_ROOT/.venv-customs/bin/python" ]; then
      python_bin="$REPO_ROOT/.venv-customs/bin/python"
    else
      python_bin=python3
    fi
  fi
  "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python >=3.12 required; set OPERATIONS_PYTHON to project venv")'
  umask 077
  exec "$python_bin" "$OPS_DIR/operations.py" "$@"
}
