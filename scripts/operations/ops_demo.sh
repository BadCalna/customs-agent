#!/usr/bin/env bash
# 本地运维模块演示（01 系统管理 / 02 Agent 调试台 / 03 基础数据中心）。
#
# 只使用仓库内的合成样例与 mock 预览：不出站、不读真实资料、不写真实供应商凭据。
# 演示密钥与密文目录都在仓库之外（$HOME 下），不进 Git、不进同步盘。
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$root"

state="${CUSTOMS_OPS_DEMO_STATE:-$HOME/.local/state/customs-agent/ops-demo}"
store="${CUSTOMS_OPS_DEBUG_SECRET_STORE:-$HOME/.local/state/customs-agent/ops-debug-secrets}"
mkdir -p "$state" "$store"
chmod 700 "$state" "$store"

keys="$state/ops-debug-keys.env"
python_bin="${PYTHON_BIN:-.venv-customs/bin/python}"
[ -x "$python_bin" ] || python_bin=python3

if [ ! -s "$keys" ]; then
  "$python_bin" - "$keys" <<'PY'
import base64, os, secrets, sys

def key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")

fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(f"OPS_DEBUG_DATA_KEY={key()}\nOPS_DEBUG_MASTER_KEY={key()}\n")
PY
fi

set -a
# shellcheck disable=SC1090
. "$keys"
set +a

export CUSTOMS_REFERENCE_FIXTURE=1
export CUSTOMS_OPS_DEBUG_ENABLED=1
export OPS_DEBUG_SECRET_STORE="$store"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.venv-customs}"

manage=("$UV_PROJECT_ENVIRONMENT/bin/python" manage.py)

"${manage[@]}" migrate --noinput
"${manage[@]}" seed_demo
"${manage[@]}" seed_ops_demo

cat <<'INFO'

本地演示准备完成：
  入口   http://127.0.0.1:8000/ops/admin/  （系统管理：模块、账号、审计）
         http://127.0.0.1:8000/ops/data/   （基础数据中心：合成样例，只读）
         http://127.0.0.1:8000/ops/debug/  （Agent 调试台：mock 预览与运行摘要）
  账号   ops_demo / 与 make seed-demo 相同的本地演示口令
  边界   合成样例 + mock 预览；OPS_DEBUG_LIVE_ENABLED 保持关闭，不发外网请求。

INFO

exec "${manage[@]}" runserver "${BIND:-127.0.0.1:8000}"